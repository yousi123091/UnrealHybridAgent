"""Compact and expanded presentations of the shared UAH snapshot/render model."""
from __future__ import annotations
import json
import os
import threading
import time
from pathlib import Path
from .hud import DashboardApp, TEXT_FG, DIM_FG, BUTTON_BG
from ...ui.components.card import render_card


class HudApp(DashboardApp):
    MODES = ('compact', 'expanded', 'dashboard')
    SIZES = {'compact': (320, 48), 'expanded': (320, 216), 'dashboard': (320, 640)}

    def __init__(self, *args, mode=None, settings_path=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.settings_path = Path(settings_path) if settings_path else Path(__file__).resolve().parents[3]/'.state/uah/window.json'
        try:
            self.settings = json.loads(self.settings_path.read_text(encoding='utf-8'))
            if not isinstance(self.settings, dict): self.settings = {}
        except (OSError, ValueError):
            self.settings = {}
        self.mode = mode or self.settings.get('mode', 'compact')
        if self.mode not in self.MODES: self.mode = 'compact'
        self.selected = self.settings.get('selected')
        self._approval_pending = []
        self._approval_busy = False
        self._approval_next = 0.0

    def build(self):
        root = super().build()
        self._dashboard_widgets = list(root.winfo_children())
        self._compact = self.tk.Frame(root, bg='#161b22', highlightthickness=1, highlightbackground='#30363d')
        row = self.tk.Frame(self._compact, bg='#161b22', height=46)
        row.pack(fill='x'); row.pack_propagate(False)
        self._dot = self.tk.Label(row, text='○', bg='#161b22', fg=DIM_FG, font=('Segoe UI',11))
        self._dot.pack(side='left',padx=(9,4))
        self._name = self.tk.Label(row,text='UAH',bg='#161b22',fg=TEXT_FG,font=('Segoe UI',9,'bold'))
        self._name.pack(side='left')
        self._expand = self.tk.Button(row,text='⌄',command=self.toggle_expanded,bg='#161b22',fg=DIM_FG,bd=0,takefocus=0)
        self._expand.pack(side='right',padx=5)
        self._multi = self.tk.Button(row,text='',command=self.next_agent,bg='#161b22',fg=DIM_FG,bd=0,takefocus=0,font=('Segoe UI',8))
        self._multi.pack(side='right')
        self._progress_label = self.tk.Label(row,text='',bg='#161b22',fg=DIM_FG,font=('Segoe UI',8))
        self._progress_label.pack(side='right',padx=3)
        self._action = self.tk.Label(row,text='等待 Agent',bg='#161b22',fg=TEXT_FG,font=('Segoe UI',9),anchor='w')
        self._action.pack(side='left',padx=6,fill='x',expand=True)
        for widget in (row,self._dot,self._name,self._action):
            widget.bind('<Button-1>',self._drag_start)
            widget.bind('<B1-Motion>',self._drag_move)
            widget.bind('<ButtonRelease-1>',lambda e:self._save_settings())
            widget.bind('<Double-Button-1>',lambda e:self.toggle_expanded())
        self._details = self.tk.Frame(self._compact,bg='#161b22')
        self._detail_text = self.tk.Label(self._details,text='',justify='left',anchor='nw',bg='#161b22',fg=TEXT_FG,font=('Segoe UI',9))
        self._detail_text.pack(fill='x',padx=10)
        self._approval_row = self.tk.Frame(self._details,bg='#161b22')
        for label,decision in [('Reject',False),('Approve',True)]:
            self.tk.Button(self._approval_row,text=label,command=lambda d=decision:self._decide(d),takefocus=0).pack(side='left',padx=5)
        controls=self.tk.Frame(self._details,bg='#161b22'); controls.pack(side='bottom',fill='x',padx=8,pady=3)
        for label,fn in [('静音',self.toggle_mute),('Dashboard',lambda:self.set_mode('dashboard')),('关闭',self.close)]:
            self.tk.Button(controls,text=label,command=fn,bg=BUTTON_BG,fg=TEXT_FG,bd=0,takefocus=0,font=('Segoe UI',8)).pack(side='left',padx=3)
        self.menu=self.tk.Menu(root,tearoff=False)
        for mode in self.MODES: self.menu.add_command(label=mode.title(),command=lambda m=mode:self.set_mode(m))
        self.menu.add_command(label='静音 / 取消静音',command=self.toggle_mute)
        self.menu.add_command(label='关闭',command=self.close)
        root.bind('<Button-3>',lambda e:self.menu.tk_popup(e.x_root,e.y_root))
        self.set_mode(self.mode,save=False)
        root.update_idletasks()
        self._no_activate()
        root.deiconify()
        root.update()
        if os.name == "nt": root.attributes("-disabled", False)
        root.update_idletasks()
        self._no_activate()
        return root

    def _no_activate(self):
        if os.name != 'nt': return
        import ctypes
        from ctypes import wintypes as w
        u=ctypes.WinDLL('user32',use_last_error=True)
        u.GetParent.argtypes=[w.HWND];u.GetParent.restype=w.HWND
        u.GetWindowLongW.argtypes=[w.HWND,ctypes.c_int];u.GetWindowLongW.restype=ctypes.c_long
        u.SetWindowLongW.argtypes=[w.HWND,ctypes.c_int,ctypes.c_long]
        hwnd=u.GetParent(self._root.winfo_id()) or self._root.winfo_id()
        u.SetWindowLongW(hwnd,-20,u.GetWindowLongW(hwnd,-20)|0x08000000|0x80)
        self.native_hwnd=hwnd

    def set_mode(self,mode,save=True):
        if mode not in self.MODES: raise ValueError(mode)
        self.mode=mode; root=self._root
        for widget in self._dashboard_widgets: widget.pack_forget()
        self._compact.pack_forget();self._details.pack_forget()
        if mode=='dashboard':
            for i,widget in enumerate(self._dashboard_widgets):
                widget.pack(fill='both' if i==1 else 'x',expand=i==1)
        else:
            self._compact.pack(fill='both',expand=True)
            if mode=='expanded':self._details.pack(fill='both',expand=True)
        root.overrideredirect(mode!='dashboard')
        width,height=self.SIZES[mode]
        root.minsize(width,height)
        x=self.settings.get('x',root.winfo_screenwidth()-width-16) if not save else root.winfo_x()
        y=self.settings.get('y',110) if not save else root.winfo_y()
        x=max(0,min(int(x),root.winfo_screenwidth()-width)); y=max(110,min(int(y),root.winfo_screenheight()-height))
        root.geometry(f'{width}x{height}+{x}+{y}')
        self._expand.configure(text='⌃' if mode=='expanded' else '⌄')
        self._render_compact()
        if save:self._save_settings();self._no_activate()

    def toggle_expanded(self):self.set_mode('compact' if self.mode=='expanded' else 'expanded')

    def _drag_move(self,event):
        dx,dy=getattr(self,'_drag',(0,0));r=self._root
        x=max(0,min(event.x_root-dx,r.winfo_screenwidth()-r.winfo_width()))
        y=max(110,min(event.y_root-dy,r.winfo_screenheight()-r.winfo_height()))
        r.geometry(f'+{x}+{y}')

    def _save_settings(self):
        if self._root is None:return
        try:
            self.settings.update(mode=self.mode,x=self._root.winfo_x(),y=self._root.winfo_y(),selected=self.selected)
            self.settings_path.parent.mkdir(parents=True,exist_ok=True)
            temp=self.settings_path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.settings),encoding='utf-8');temp.replace(self.settings_path)
        except (OSError,self.tk.TclError):pass

    def next_agent(self):
        ids=list(self._order)
        if ids:self.selected=ids[(ids.index(self.selected)+1)%len(ids)] if self.selected in ids else ids[0]
        self._render_compact();self._save_settings()

    def _sync_cards(self):
        super()._sync_cards()
        self._render_compact()

    def _render_compact(self):
        if not hasattr(self,'_compact'):return
        if self.selected not in self._snapshots:self.selected=self._order[0] if self._order else None
        snap=self._snapshots.get(self.selected)
        if snap is None:
            self._name.configure(text='UAH');self._action.configure(text='等待 Agent');self._detail_text.configure(text=self._stream_status)
            self._progress_label.configure(text='');self._multi.configure(text='');self._approval_row.pack_forget();return
        view=render_card(snap); rows={r.label:r.value for r in view.rows}
        action=snap.activity.summary or snap.task.stage or snap.task.name or view.badge
        if snap.status.value in ('WAITING_APPROVAL','WAITING_INPUT','ERROR','DONE','BLOCKED'):action=f'{view.badge} · {action}'
        if view.stale or view.stopped:action=f'{view.footer} · {action}'
        disconnected=self._stream_status!='connected'
        if disconnected:action='离线 · '+action
        self._dot.configure(text=view.badge.split()[0],fg=DIM_FG if disconnected else view.badge_color)
        self._name.configure(text=view.title[:12]);self._action.configure(text=action)
        self._progress_label.configure(text=f'{snap.task.step}/{snap.task.total_steps}' if snap.task.step is not None and snap.task.total_steps else '')
        self._multi.configure(text=f'+{len(self._snapshots)-1}' if len(self._snapshots)>1 else '')
        lines=[f'{label}  {value[:35]}' for label,value in rows.items() if label in ('Task','Stage','Step','Activity','Tool')]
        self._detail_text.configure(text='\n'.join(lines+[view.footer]),wraplength=295)
        pending=[p for p in self._approval_pending if p.get('agent_id')==self.selected]
        self._current_approval=pending[0] if pending else None
        if pending:self._approval_row.pack(fill='x',padx=5)
        else:self._approval_row.pack_forget()

    def _tick(self):
        super()._tick()
        if self._stop.is_set():return
        self._render_compact()
        if not self._flash_until:self._compact.configure(highlightbackground='#30363d')
        if not self._approval_busy and time.monotonic()>self._approval_next:
            self._approval_busy=True;self._approval_next=time.monotonic()+1
            def fetch():
                try:
                    from ...core.approval import approval_call
                    result=approval_call(self.url,'pending',{})
                    self._approval_pending=result.get('pending',[])
                except Exception:self._approval_pending=[]
                finally:self._approval_busy=False
            threading.Thread(target=fetch,daemon=True).start()

    def _decide(self,allow):
        request=getattr(self,'_current_approval',None)
        if not request:return
        self._current_approval=None;self._approval_row.pack_forget()
        def send():
            from ...core.approval import approval_call
            try:approval_call(self.url,'decide',{'req_id':request['req_id'],'allow':allow})
            except Exception:pass  # Agent denies on timeout/disconnection.
        threading.Thread(target=send,daemon=True).start()

    def _on_notify(self,note):
        # Never lift/deiconify over the safety banner or steal focus.
        self._flash_until=time.time()+6
        self.selected=note.agent_id
        if hasattr(self,'_compact'):
            from ...ui.components.card import STATUS_COLORS
            self._compact.configure(highlightbackground=STATUS_COLORS.get(note.status,'#f0883e'))

    def close(self):
        self._save_settings()
        super().close()
