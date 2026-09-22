"""Owned blank window for real guarded-input movement and drag cancellation."""
import tkinter as tk
import subprocess,sys,time,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
root=tk.Tk();root.title('UHA guarded input — disposable test canvas');root.geometry('440x240+160+260');root.attributes('-topmost',True)
tk.Label(root,text='Guarded input automatic test — no user action needed').pack()
canvas=tk.Canvas(root,bg='#edf2f5');canvas.pack(fill='both',expand=True)
counts={'down':0,'up':0,'drag':0}
for seq,key in [('<ButtonPress-1>','down'),('<ButtonRelease-1>','up'),('<B1-Motion>','drag')]:
    canvas.bind(seq,lambda e,k=key:counts.__setitem__(k,counts[k]+1))
root.update()
log=ROOT/'logs/runs/p4bc_guarded_canvas.log'
with log.open('wb') as stream:
    child=subprocess.Popen([str(ROOT/'.venv/Scripts/python.exe'),'-u',str(ROOT/'tools/p4bc_guarded_probe.py'),str(canvas.winfo_rootx()+35),str(canvas.winfo_rooty()+35)],cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    while child.poll() is None:root.update();time.sleep(.005)
    root.update()
root.destroy()
print(json.dumps({'exit':child.returncode,'canvas_events':counts,'log':str(log)}),flush=True)
print(log.read_text(encoding='utf-8'),flush=True)
from uah.hosts.desktop.__main__ import _cleanup_and_exit
_cleanup_and_exit(None,child.returncode or int(not counts['down'] or not counts['up']))
