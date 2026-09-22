"""Windows input execution isolated from the Agent, with a surviving release guard.

Private inherited pipes carry commands; no network listener or third-party patch.
All motion uses one SendInput step, never a non-cancellable SetCursorPos tween.
This provides bounded cooperative cancellation, NOT zero-latency OS guarantees.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

from .human_override import HumanOverrideDetector
from .controller import INJECTION_ACTIVE_STATES
from ..desktop.hotkey import INPUT, MOUSEINPUT, KEYBDINPUT, _send_input, resolve_keys

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = ctypes.c_void_p
kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
kernel32.CreateMutexW.restype = ctypes.c_void_p
kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value), encoding='utf-8')
    os.replace(tmp, path)


def process_handle(pid: int):
    handle = kernel32.OpenProcess(0x100000, False, pid)
    if not handle:
        raise RuntimeError('Cannot monitor input owner process')
    return handle


def alive(handle) -> bool:
    return kernel32.WaitForSingleObject(handle, 0) == 0x102


def key_event(vk: int, up: bool = False, scan: int = 0) -> INPUT:
    event = INPUT(type=1)
    event.union.ki = KEYBDINPUT(vk, scan, (2 if up else 0) | (4 if scan else 0), 0, None)
    return event


def mouse_event(flags: int, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    event = INPUT(type=0)
    event.union.mi = MOUSEINPUT(x, y, data, flags, 0, None)
    return event


def release_ledger(folder: Path) -> dict:
    path = folder/'ledger.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    remaining = []
    for item in data.get('held', []):
        try:
            if item['kind'] == 'key':
                _send_input(key_event(item['vk'], True))
            elif item['kind'] == 'unicode':
                _send_input(key_event(0, True, item['scan']))
            else:
                _send_input(mouse_event(item['up']))
        except Exception:
            remaining.append(item)
    data['held'] = remaining
    atomic_json(path, data)
    return {'ok': not remaining, 'remaining': remaining}


class GuardedInputClient:
    def __init__(self, gate: Path, *, timeout: float = 8.0, cancelled=None):
        if os.name != 'nt':
            raise RuntimeError('Guarded input requires Windows')
        self.folder = Path(tempfile.mkdtemp(prefix='uha-input-'))
        self.timeout = timeout
        self._lock = threading.Lock()
        self._replies: queue.Queue = queue.Queue()
        self._log = (self.folder/'worker.log').open('w', encoding='utf-8')
        self.process = subprocess.Popen(
            [sys.executable, '-u', '-m', __name__, 'worker', str(self.folder), str(gate), str(os.getpid())],
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._closing = threading.Event()
        self._cancel_monitor = None
        if cancelled is not None:
            def monitor_cancel():
                while not self._closing.wait(.005):
                    try:
                        if cancelled():
                            (self.folder/'revoke').touch()
                            return
                    except Exception:
                        (self.folder/'revoke').touch()
                        return
            self._cancel_monitor = threading.Thread(target=monitor_cancel, daemon=True)
            self._cancel_monitor.start()
        try:
            ready = self._reply()
        except BaseException:
            self.close()
            raise
        if not ready.get('ready'):
            self.close()
            raise RuntimeError(f'Guarded input failed to start: {ready}')
        self.pid = ready['pid']
        self.guard_pid = ready['guard_pid']

    def _read(self):
        try:
            for line in self.process.stdout:
                self._replies.put(json.loads(line))
        except Exception as exc:
            self._replies.put({'ok': False, 'error': str(exc)})
        finally:
            self._replies.put({'ok': False, 'error': 'input worker disconnected'})
            # If both worker and guardian die, the live owner is the last release
            # authority. Wait for worker exit before touching its write-ahead ledger.
            try:
                self.process.wait(timeout=3)
                guard_pid = getattr(self, 'guard_pid', None)
                if guard_pid:
                    handle = process_handle(guard_pid)
                    guard_alive = alive(handle)
                    kernel32.CloseHandle(handle)
                else:guard_alive = False
            except RuntimeError:
                guard_alive = False
            except subprocess.TimeoutExpired:
                return
            if not guard_alive:
                try:
                    cleanup = release_ledger(self.folder)
                    atomic_json(self.folder/'owner-cleanup.json', cleanup)
                except Exception as exc:
                    self._replies.put({'ok':False,'error':'Owner cleanup failed: '+str(exc)})

    def _reply(self):
        try:
            return self._replies.get(timeout=self.timeout)
        except queue.Empty:
            (self.folder/'revoke').touch()
            raise TimeoutError('Input worker timeout; independent guard asked to revoke')

    def call(self, tool: str, args: dict):
        with self._lock:
            if self.process.poll() is not None:
                raise PermissionError('Input worker is dead; new input refused')
            self.process.stdin.write(json.dumps({'tool': tool, 'args': args})+'\n')
            self.process.stdin.flush()
            result = self._reply()
            if not result.get('ok'):
                raise PermissionError(result.get('error', 'input refused'))
            return {'data': result}

    def close(self):
        self._closing.set()
        if self._cancel_monitor:self._cancel_monitor.join(timeout=1)
        (self.folder/'revoke').touch()
        try:
            if self.process.stdin:
                self.process.stdin.close()
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=3)
        self._reader.join(timeout=1)
        self._log.close()


class InputWorker:
    def __init__(self, folder: Path, gate: Path, parent: int):
        self.folder, self.gate = folder, gate
        self.desktop_mutex = kernel32.CreateMutexW(None, False, 'Local\\UHA-GuardedInput-v1')
        if not self.desktop_mutex or kernel32.WaitForSingleObject(self.desktop_mutex, 0) not in (0, 0x80):
            raise PermissionError('Another guarded input worker holds this desktop')
        self.parent = process_handle(parent)
        self.revoked = threading.Event()
        self.reason = ''
        self.held: list[dict] = []
        self.sent = 0
        self.expected_window = None
        self.mutex = threading.RLock()
        self.detector = HumanOverrideDetector(on_physical_input=self.physical, move_throttle_s=0)
        self.detector.start()
        if not self.detector.hook_alive:
            raise RuntimeError('Both native input hooks are required')
        atomic_json(folder/'ledger.json', {'held': []})
        self.guard = subprocess.Popen(
            [sys.executable, '-u', '-m', 'src.safety.guarded_input', 'guard', str(folder), str(os.getpid())],
            cwd=Path(__file__).resolve().parents[2], creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(200):
            if (folder/'guard-ready').exists():break
            if self.guard.poll() is not None:raise RuntimeError('Release guard failed')
            time.sleep(.01)
        else:raise RuntimeError('Release guard timeout')
        self.monitor = threading.Thread(target=self.watch, daemon=True)
        self.monitor.start()

    def physical(self, kind):
        # Hook thread must never do file I/O or wait on RPC.
        self.reason = 'physical:'+kind
        self.revoked.set()

    def valid(self):
        if self.revoked.is_set() or not alive(self.parent) or (self.folder/'revoke').exists():return False
        if not self.detector.hook_alive or self.guard.poll() is not None:return False
        try:
            data = json.loads(self.gate.read_text(encoding='utf-8'))
            return (data.get('allow_input') is True and data.get('agent_injection_permission') is True
                    and data.get('banner_visible') is True and data.get('heartbeat_ok') is True
                    and data.get('state') in INJECTION_ACTIVE_STATES
                    and isinstance(data.get('lease_expires_at'), (int,float))
                    and data['lease_expires_at'] > time.time())
        except (OSError, ValueError, TypeError):return False

    def watch(self):
        while not self.revoked.is_set():
            if not self.valid():
                self.reason = self.reason or 'gate/owner/guard unavailable'
                self.revoked.set()
                break
            time.sleep(.005)
        with self.mutex:
            if not self.detector.hook_alive:
                # Remove stalled observers before cleanup input traverses their
                # hook chain. Do not wait for the stalled pump before key-up.
                self.detector.stop(wait=False)
            cleanup = release_ledger(self.folder)
            self.held = cleanup['remaining']
            atomic_json(self.folder/'revoked.json', {'reason': self.reason, 'sent': self.sent, 'cleanup': cleanup})

    def send(self, event, *, hold=None, release=None):
        with self.mutex:
            if not self.valid():raise PermissionError(self.reason or 'guarded input gate closed')
            if self.expected_window and user32.GetForegroundWindow() != self.expected_window:
                raise PermissionError('Target foreground window changed')
            if hold is not None:
                if hold in self.held:raise PermissionError('Duplicate held input')
                self.held.append(hold)
                atomic_json(self.folder/'ledger.json', {'held': self.held})
            # Write-ahead ledger survives a crash immediately before/after SendInput.
            if not self.valid():raise PermissionError('Input revoked before SendInput')
            _send_input(event)
            self.sent += 1
            if release is not None and release in self.held:
                self.held.remove(release)
                atomic_json(self.folder/'ledger.json', {'held': self.held})

    def move(self, x, y):
        # Public CU coordinates are logical primary-screen pixels (same as TARS).
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        if not (0 <= x < width and 0 <= y < height):raise ValueError('Input coordinates outside primary screen')
        self.send(mouse_event(0x8001, round(x*65535/max(width-1,1)), round(y*65535/max(height-1,1))))

    def keys(self, text):
        keys, unknown = resolve_keys(text)
        if unknown or not keys:raise ValueError('Unsupported or empty key names')
        return keys

    def press(self, vk):
        if user32.GetAsyncKeyState(vk)&0x8000:raise PermissionError('Key already down; do not claim user input')
        self.send(key_event(vk), hold={'kind':'key','vk':vk})

    def release(self, vk):
        item={'kind':'key','vk':vk}
        if item not in self.held:raise PermissionError('Key not held by this input worker')
        self.send(key_event(vk,True), release=item)

    def execute(self, tool, args):
        self.expected_window = args.get('expectedWindow')
        if tool == 'computer_move_mouse':self.move(args['x'],args['y'])
        elif tool in ('computer_click','computer_double_click','computer_right_click','computer_drag'):
            right=tool=='computer_right_click';down,up=(8,16) if right else (2,4)
            if user32.GetAsyncKeyState(2 if right else 1)&0x8000:raise PermissionError('Mouse button already down')
            item={'kind':'button','up':up}
            self.move(args.get('x',args.get('x1')),args.get('y',args.get('y1')))
            for _ in range(2 if tool=='computer_double_click' else 1):
                self.send(mouse_event(down),hold=item)
                if tool=='computer_drag':
                    for i in range(1,65):
                        self.move(round(args['x1']+(args['x2']-args['x1'])*i/64),round(args['y1']+(args['y2']-args['y1'])*i/64))
                        time.sleep(.008)
                self.send(mouse_event(up),release=item)
        elif tool == 'computer_key_press':
            for vk in self.keys(args['key']):self.press(vk)
        elif tool == 'computer_key_release':
            for vk in reversed(self.keys(args['key'])):self.release(vk)
        elif tool == 'computer_hotkey':
            keys=self.keys(args['keys'])
            for vk in keys:self.press(vk)
            for vk in reversed(keys):self.release(vk)
        elif tool == 'computer_type':
            raw=str(args['text']).encode('utf-16-le')
            if len(raw)>20000:raise ValueError('Text input exceeds bounded action size')
            for i in range(0,len(raw),2):
                scan=int.from_bytes(raw[i:i+2],'little');item={'kind':'unicode','scan':scan}
                self.send(key_event(0,scan=scan),hold=item)
                self.send(key_event(0,True,scan),release=item)
        elif tool == 'computer_scroll':
            self.move(args['x'],args['y'])
            direction=args.get('direction','down')
            if direction not in ('up','down','left','right'):raise ValueError('Invalid scroll direction')
            for _ in range(max(1,min(10,int(args.get('times',1))))):
                self.send(mouse_event(0x1000 if direction in ('left','right') else 0x800,
                                      data=(120 if direction in ('up','right') else -120)&0xffffffff))
        else:raise ValueError('Unknown guarded input operation')
        return {'ok':True,'backend':'uha_guarded_sendinput','sent':self.sent,'held':list(self.held)}

    def close(self):
        self.revoked.set();self.monitor.join(timeout=2)
        self.detector.stop()
        atomic_json(self.folder/'hook-cleanup.json', self.detector.stats())
        kernel32.CloseHandle(self.parent)
        self.guard.wait(timeout=4)
        kernel32.ReleaseMutex(self.desktop_mutex)
        kernel32.CloseHandle(self.desktop_mutex)


def guard_main(folder: Path, worker_pid: int):
    handle=process_handle(worker_pid)
    (folder/'guard-ready').touch()
    while alive(handle):
        if (folder/'revoked.json').exists():break
        time.sleep(.01)
    # Input executor is gone or acknowledged quiescence; no race with future downs.
    cleanup=release_ledger(folder)
    atomic_json(folder/'guard-cleanup.json',cleanup)
    kernel32.CloseHandle(handle)


def main():
    mode,folder=sys.argv[1],Path(sys.argv[2])
    if mode=='guard':guard_main(folder,int(sys.argv[3]));return
    worker=None
    try:
        worker=InputWorker(folder,Path(sys.argv[3]),int(sys.argv[4]))
        print(json.dumps({'ready':True,'pid':os.getpid(),'guard_pid':worker.guard.pid}),flush=True)
        for line in sys.stdin:
            try:
                request=json.loads(line);reply=worker.execute(request['tool'],request.get('args',{}))
            except Exception as exc:
                worker.reason=str(exc);worker.revoked.set();reply={'ok':False,'error':str(exc)}
            print(json.dumps(reply),flush=True)
    finally:
        if worker:worker.close()


if __name__=='__main__':main()
