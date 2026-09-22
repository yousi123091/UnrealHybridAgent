"""User-started real physical takeover checks for the production input worker.

No synthetic takeover callback. This tests the worker boundary, not the remote
ComputerUseAdapter lease/transport path; keep that scope in the evidence.
"""
import ctypes,json,sys,time,threading,queue,os
from pathlib import Path
import tkinter as tk
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.safety.controller import get_safety_controller,reset_safety_controller_for_tests
from src.safety.banner_win32 import Win32SafetyBanner
from src.safety.guarded_input import GuardedInputClient
from src.adapters.computer_use import ComputerUseAdapter

adapter_url=os.environ.get('UHA_HUMAN_ADAPTER_URL','')

class AdapterInput:
    """Exercise public adapter operations; never bypass its gates or remote lease."""
    def __init__(self,hwnd):
        self.cu=ComputerUseAdapter(adapter_url,client_id='uha-human-full-chain',timeout=5)
        self.cu.expected_window=hwnd
        self.context=self.cu.control();self.context.__enter__()
        self.lease_before=self.cu.peek_lock()
        if self.lease_before.get('holder')!=self.cu.client_id:
            self.close();raise RuntimeError('真实服务未授予桌面租约')
    def call(self,tool,args):
        if tool=='computer_drag':return self.cu.drag(args['x1'],args['y1'],args['x2'],args['y2'])
        if tool=='computer_key_press':return self.cu.key_press(args['key'])
        if tool=='computer_move_mouse':return self.cu.move_mouse(args['x'],args['y'])
        raise ValueError(tool)
    def __getattr__(self,name):return getattr(self.cu._input_worker,name)
    def close(self):
        self.saved_worker=self.cu._input_worker
        self.release_result=self.cu.release_control()
        self.lease_after=self.cu.peek_lock()
        self.context.__exit__(None,None,None)
        self.cu.close()

run=ROOT/'logs/runs'/('human-acceptance-'+time.strftime('%Y%m%d-%H%M%S'));run.mkdir(parents=True)
root=tk.Tk();root.title('UHA 完整链路真人验收' if adapter_url else 'UHA 真人安全验收 — 空白测试区');root.geometry('780x530+180+180')
root.attributes('-topmost',True)
events=queue.Queue();results=[];active=None;sc_live=None;closing=False;seq=0
status=tk.StringVar(value='请选择一项；点击后有 5 秒倒计时。倒计时结束前放开鼠标和键盘。')
tk.Label(root,text='UHA 真人安全验收',font=('Microsoft YaHei UI',17)).pack(pady=7)
tk.Label(root,textvariable=status,wraplength=745,font=('Microsoft YaHei UI',11)).pack(pady=5)
buttons=tk.Frame(root);buttons.pack()
canvas=tk.Canvas(root,bg='#e6edf3',height=210,highlightthickness=1);canvas.pack(fill='x',padx=18,pady=12)
canvas.create_text(350,85,text='自动拖拽只在这块空白区域内进行',font=('Microsoft YaHei UI',14))
tk.Label(root,text='接管后在这里输入 abcABC，检查是否正常；文字不会写入日志。').pack()
entry=tk.Entry(root,font=('Consolas',17));entry.pack(fill='x',padx=25,pady=5)
feedback=tk.Frame(root);feedback.pack(pady=8)
def persist():
    (run/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
def report_feedback(ok):
    if not results:return
    results[-1]['user_confirmation']='正常：停止且可以正常操作' if ok else '异常：仍被影响'
    results[-1]['status']='PASS' if ok and results[-1]['machine_checks_pass'] else 'FAIL' if not ok or results[-1].get('error') else 'PARTIAL'
    persist();status.set('已记录你的实际反馈。可选择下一项；有异常请回到对话告诉我。')
tk.Button(feedback,text='已停止，鼠标/打字正常',command=lambda:report_feedback(True)).pack(side='left',padx=8)
tk.Button(feedback,text='仍有异常',command=lambda:report_feedback(False)).pack(side='left',padx=8)
for b in feedback.winfo_children():b.configure(state='disabled')
def stop():
    if sc_live:sc_live.emergency_stop(reason='human_test_stop_button',source='user')
    status.set('已请求停止。本项不能据此记为物理接管通过。')
tk.Button(root,text='停止本项测试',command=stop,bg='#fee2e2').pack()
def worker(mode,coords,hwnd,number):
    global sc_live
    sc=client=banner=None;row={'case':mode,'started_at':time.time(),'scope':'public ComputerUseAdapter + real HTTP lease + guarded worker + controller + banner' if adapter_url else 'production guarded worker + controller + banner; remote adapter lease not exercised','physical_callback_simulated':False}
    intervals=[]
    try:
        if ctypes.windll.user32.GetForegroundWindow()!=hwnd:raise RuntimeError('测试窗口不在前台，未开始输入')
        sc=get_safety_controller(state_path=run/f'gate-{number}.json');sc_live=sc
        # Tk lives on the main UI thread. The runtime's Win32 banner owns its own
        # message thread; never create a second Tcl interpreter in this worker.
        banner=Win32SafetyBanner(sc);sc.set_banner_available(banner.available)
        if not banner.start() or not banner.visible:raise RuntimeError('Safety Banner 未显示')
        sc.request_control(task='真人安全验收 '+mode);sc.banner_visible()
        if adapter_url:
            client=AdapterInput(hwnd);row['lease_before']=client.lease_before
        else:
            sc.grant_control();client=GuardedInputClient(sc.state_path,cancelled=lambda:sc.action_cancelled)
        events.put(('status','现在开始！'+('请按一下 A 键接管，然后检查 Shift 是否已释放。' if mode=='held_shift' else '看到自动拖拽时，'+('直接移动鼠标接管。' if mode=='mouse_mid_drag' else '按一下空格键接管。'))))
        deadline=time.monotonic()+20
        while time.monotonic()<deadline and not sc.action_cancelled:
            item={'begin':time.time(),'operation':'held_shift' if mode=='held_shift' else 'drag'};intervals.append(item)
            try:
                if mode=='held_shift':
                    client.call('computer_key_press',{'key':'shift','expectedWindow':hwnd})
                    item['held_verified']=bool(ctypes.windll.user32.GetAsyncKeyState(0x10)&0x8000)
                    while time.monotonic()<deadline and not sc.action_cancelled:time.sleep(.005)
                else:
                    x,y=coords;client.call('computer_drag',{'x1':x,'y1':y,'x2':x+260,'y2':y+35,'expectedWindow':hwnd})
            except Exception as exc:item['error']=type(exc).__name__+': '+str(exc)
            finally:item['end']=time.time()
            if item.get('error'):break
        det=sc.human_override_detector;stats=det.stats();physical_at=stats.get('last_physical_at',0)
        row['worker_folder']=str(client.folder);row['worker_pid']=client.pid;row['guardian_pid']=client.guard_pid
        # Snapshot immediately, before subsequent feedback mouse/keyboard events.
        row['controller']=sc.snapshot().as_dict();row['detector']=stats;row['actions']=intervals
        row['physical_during_action']=any(i['begin']<=physical_at<=i['end'] for i in intervals)
        kind=stats.get('last_physical_kind','')
        row['expected_physical_kind']=kind.startswith('mouse') if mode=='mouse_mid_drag' else kind.startswith('key')
        if sc.action_cancelled:
            try:client.call('computer_move_mouse',{'x':coords[0],'y':coords[1],'expectedWindow':hwnd});row['later_input_refused']=False
            except PermissionError:row['later_input_refused']=True
            refused=[]
            for _ in range(3):
                try:sc.request_control(task='reopen must fail');refused.append(False)
                except RuntimeError:refused.append(True)
            row['three_reopens_refused']=all(refused)
        time.sleep(.15)
        row['left_button_up']=not bool(ctypes.windll.user32.GetAsyncKeyState(1)&0x8000)
        row['shift_up']=not bool(ctypes.windll.user32.GetAsyncKeyState(0x10)&0x8000)
        rev=client.folder/'revoked.json'
        row['worker_revocation']=json.loads(rev.read_text(encoding='utf-8')) if rev.exists() else None
        row['machine_checks_pass']=bool(row['controller']['state']=='HUMAN_OVERRIDE' and stats['physical_events']>0 and row['physical_during_action'] and row['expected_physical_kind'] and row.get('later_input_refused') and row.get('three_reopens_refused') and row['left_button_up'] and row['shift_up'])
        row['status']='PENDING HUMAN VALIDATION' if row['machine_checks_pass'] else 'PARTIAL'
    except Exception as exc:row.update(status='FAIL',machine_checks_pass=False,error=type(exc).__name__+': '+str(exc))
    finally:
        if sc:sc.release_control(reason='human_test_end')
        if client:
            try:
                process=client.process
                client.close();row['worker_exit']=process.poll()
                if adapter_url:
                    row['release_result']=client.release_result;row['lease_after']=client.lease_after
                    row['remote_lease_released']=not client.lease_after.get('holder')
                    row['terminal_after_release']=sc.state.value
                    if not row['remote_lease_released'] or row['terminal_after_release']!='HUMAN_OVERRIDE':row.update(machine_checks_pass=False,status='FAIL')
                if row['worker_exit']!=0:row.update(machine_checks_pass=False,status='FAIL')
            except Exception as exc:row.update(machine_checks_pass=False,status='FAIL',cleanup_error=str(exc))
        if banner:banner.stop()
        reset_safety_controller_for_tests();sc_live=None
        events.put(('result',row))
def begin(mode,count=5):
    global active,seq
    if active and active.is_alive():return
    for b in buttons.winfo_children():b.configure(state='disabled')
    for b in feedback.winfo_children():b.configure(state='disabled')
    entry.delete(0,'end')
    def countdown(n):
        global active,seq
        if closing:return
        status.set(f'{n} 秒后开始。请放开鼠标和键盘；看到自动动作后再接管。')
        if n:root.after(1000,lambda:countdown(n-1));return
        root.update_idletasks();hwnd=ctypes.windll.user32.GetAncestor(root.winfo_id(),2)
        seq+=1
        active=threading.Thread(target=worker,args=(mode,(canvas.winfo_rootx()+80,canvas.winfo_rooty()+90),hwnd,seq),daemon=True);active.start()
    countdown(count)
for label,mode in [('1 鼠标接管拖拽','mouse_mid_drag'),('2 按键接管拖拽','key_mid_drag'),('3 按住 Shift 时接管','held_shift')]:
    tk.Button(buttons,text=label,command=lambda m=mode:begin(m)).pack(side='left',padx=6)
def pump():
    try:
        while True:
            kind,value=events.get_nowait()
            if kind=='status':status.set(value)
            else:
                results.append(value);persist()
                status.set(('测试未成功执行：'+value['error']+'。此项不算通过，请回到对话。') if value.get('error') else '本项已停止。请移动鼠标，并在输入框检查打字；然后点击反馈。'+(' 机器检查通过，等待你的体验确认。' if value['machine_checks_pass'] else ' 本次未取得完整接管证据，请反馈后回到对话查看。'))
                for b in buttons.winfo_children():b.configure(state='normal')
                for b in feedback.winfo_children():b.configure(state='normal')
    except queue.Empty:pass
    if closing and not (active and active.is_alive()):
        root.destroy();from uah.hosts.desktop.__main__ import _cleanup_and_exit
        _cleanup_and_exit(None,0)
    root.after(50,pump)
def close():
    global closing
    closing=True;stop()
ctypes.windll.user32.GetForegroundWindow.restype=ctypes.c_void_p
ctypes.windll.user32.GetAncestor.argtypes=[ctypes.c_void_p,ctypes.c_uint]
ctypes.windll.user32.GetAncestor.restype=ctypes.c_void_p
root.protocol('WM_DELETE_WINDOW',close)
(run/'ready.json').write_text(json.dumps({'run':str(run),'state':'waiting_for_user_start','cases':['mouse_mid_drag','key_mid_drag','held_shift']}),encoding='utf-8')
print(str(run),flush=True);root.after(50,pump);root.mainloop()
