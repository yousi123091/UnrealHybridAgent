"""Detached loopback Hub, owned by its process, independent of Agent/HUD lifetimes."""
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from .transport import hub_url, probe_hub

def ensure_persistent_url(url):
    parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost'):
        raise ValueError('Automatic Hub launch only supports loopback HTTP')
    return ensure_persistent_hub(host=parsed.hostname,port=parsed.port or 8789)

def ensure_persistent_hub(host='127.0.0.1',port=8789):
    url=hub_url(host,port)
    if probe_hub(url):return url
    if host not in ('127.0.0.1','localhost'):raise ValueError('Local Hub required')
    proc=subprocess.Popen([sys.executable,'-m','uah.tools.uah','hub','--host',host,'--port',str(port),'--quiet'],
        cwd=Path(__file__).resolve().parents[2],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess,'DETACHED_PROCESS',0)|getattr(subprocess,'CREATE_NO_WINDOW',0))
    for _ in range(50):
        if probe_hub(url):return url
        if proc.poll() is not None:break
        time.sleep(.1)
    if probe_hub(url):return url  # Another launcher may have won the bind race.
    raise OSError('Independent UAH Hub did not become healthy')
