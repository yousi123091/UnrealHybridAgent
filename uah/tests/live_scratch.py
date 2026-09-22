"""Own a disposable desktop target; keep every click/drag inside its blank canvas."""
import tkinter as tk,subprocess,time,sys,os,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
def main():
    root=tk.Tk();root.title('UHA isolated input test — disposable canvas');root.geometry('420x210+140+220');root.attributes('-topmost',True)
    tk.Label(root,text='P0 lifecycle test • disposable target').pack()
    canvas=tk.Canvas(root,bg='#edf2f5');canvas.pack(fill='both',expand=True)
    counts={'press':0,'release':0,'drag':0}
    for sequence,name in [('<ButtonPress-1>','press'),('<ButtonRelease-1>','release'),('<B1-Motion>','drag')]:
        canvas.bind(sequence,lambda event,n=name:counts.__setitem__(n,counts[n]+1))
    root.update();x=canvas.winfo_rootx();y=canvas.winfo_rooty()
    path=ROOT/'logs/runs/p01_live_scratch.log'
    with path.open('wb') as stream:
        child=subprocess.Popen([str(ROOT/'.venv/Scripts/python.exe'),'-u','tools/p01_live_lifecycle_worker.py',str(x),str(y)],cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        end=time.monotonic()+60
        while child.poll() is None and time.monotonic()<end:root.update();time.sleep(.01)
        if child.poll() is None:child.kill();child.wait(5)
        code=child.returncode
    root.destroy()
    print(json.dumps({'child_exit':code,'actual_canvas_events':counts,'log':str(path)}),flush=True)
    print(path.read_text(encoding='utf-8',errors='replace'),flush=True)
    return code or (0 if counts['press'] and counts['release'] else 1)
if __name__=='__main__':
    try:code=main()
    except BaseException:
        import traceback;traceback.print_exc();code=1
    from uah.hosts.desktop.__main__ import _cleanup_and_exit
    _cleanup_and_exit(None,code)
