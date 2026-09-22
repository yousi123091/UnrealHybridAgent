"""Create and inspect actual Tk windows; no synthetic event is called human validation."""
import sys,tempfile,time,ctypes,json,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from uah.hosts.desktop.hud import HudApp
from uah.core.transport import HubServer
from uah.adapters.generic.bridge import GenericBridge
from uah.ui.notify import Notifier
from types import SimpleNamespace

def main():
    import faulthandler
    faulthandler.dump_traceback_later(20, repeat=True)
    print("GUI starting",flush=True)
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'window.json';server=HubServer(port=0).start()
        app=HudApp(server.url(),settings_path=path,notifier=Notifier(sinks=[]),poll_ms=30,clock_ms=80)
        foreground=ctypes.windll.user32.GetForegroundWindow()
        root=app.build();print("built",flush=True);app.start_stream()
        def pump(seconds=.2):
            end=time.monotonic()+seconds
            while time.monotonic()<end:root.update();time.sleep(.01)
        try:
            pump();assert (root.winfo_width(),root.winfo_height())==(320,48)
            assert root.overrideredirect() and root.attributes('-topmost')
            assert ctypes.windll.user32.GetForegroundWindow()==foreground,'HUD stole foreground'
            assert ctypes.windll.user32.GetWindowLongW(app.native_hwnd,-20)&0x08000000
            bridge=GenericBridge(server.url())
            bridge.emit(agent='UHA',status='running',task={'name':'Scene','stage':'Environment Perception','step':3,'total_steps':7},activity='Environment Perception')
            bridge.emit(agent='Other',status='running',task='Other task')
            pump(.4);assert len(app._snapshots)==2
            initial=app.selected;app.next_agent();assert app.selected!=initial
            assert app._multi.cget('text')=='+1'
            app.set_mode('expanded');pump();assert root.winfo_height()==216
            app.set_mode('dashboard');pump();assert len(app._cards)==2 and all(c.frame.winfo_ismapped() for c in app._cards.values())
            app.set_mode('compact');pump();assert root.winfo_height()==48
            app._drag_start(SimpleNamespace(x_root=root.winfo_x()+2,y_root=root.winfo_y()+2))
            app._drag_move(SimpleNamespace(x_root=302,y_root=202));pump()
            assert (root.winfo_x(),root.winfo_y())==(300,200)
            bridge.emit(agent='UHA',status='waiting_approval',activity='Approve changes')
            pump(.3);assert app._flash_until>time.time();assert root.winfo_height()==48
            assert ctypes.windll.user32.GetForegroundWindow()==foreground,'Notification stole foreground'
            old=app.notifier.muted;app.toggle_mute();assert app.notifier.muted!=old
            from uah.core.approval import approval_call
            approval_call(server.url(),'create',{'req_id':'gui-reject','agent_id':'UHA','summary':'GUI test only','timeout_s':5})
            app.set_mode('expanded');pump(1.3)
            assert app._approval_row.winfo_ismapped(), 'Approval buttons not visible'
            buttons=app._approval_row.winfo_children();buttons[0].invoke();pump(.3)
            assert approval_call(server.url(),'poll',{'req_id':'gui-reject'})['decision'] is False
            approval_call(server.url(),'create',{'req_id':'gui-approve','agent_id':'UHA','summary':'GUI test only','timeout_s':5})
            pump(1.3);buttons[1].invoke();pump(.3)
            assert approval_call(server.url(),'poll',{'req_id':'gui-approve'})['decision'] is True
            app.set_mode('compact')
            port=server.port;server.stop();pump(.3)
            server=HubServer(port=port).start();bridge.emit(agent='Recovered',status='done')
            pump(3);assert set(app._snapshots)=={'Recovered'},'SSE restart retained stale agents or did not reconnect'
            assert len(app._after_ids)<10,'timer handles leaked'
            print('active socket before close:',repr(app.client._active_sock),flush=True)
            app.set_mode('expanded');app.close()
            if app._stream_thread.is_alive(): faulthandler.dump_traceback()
            assert not app._stream_thread.is_alive(), 'SSE thread leaked'
            saved=json.loads(path.read_text());assert saved['mode']=='expanded' and saved['x']==300
            second=HudApp(server.url(),settings_path=path,notifier=Notifier(sinks=[]))
            root2=second.build();root2.update()
            assert second.mode=='expanded' and root2.winfo_x()==300 and root2.winfo_y()==200
            second.close()
            print('PASS actual GUI: 320x48, frameless, topmost, no activation, drag, 3 modes, agent switch, persistence, nonmodal notification, Hub restart, bounded timers, window destruction',flush=True)
        finally:
            app.close();server.stop()
    return 0

if __name__=='__main__':
    try:code=main()
    except BaseException:
        import traceback;traceback.print_exc();code=1
    from uah.hosts.desktop.__main__ import _cleanup_and_exit
    _cleanup_and_exit(None, code)
