"""Legacy entry point — superseded by ``src/safety/banner_win32.py``.

Kept as a thin shim so existing notes/scripts do not break.

Why it is not the implementation any more:
  * it referenced ``wt.HCURSOR``, which does not exist in ``ctypes.wintypes`` — the module
    raised ``AttributeError`` at import time, so this banner **never actually ran**;
  * it read the safety state from the on-disk file instead of the live controller;
  * it had no explicit user ``Resume`` control (P0.1 §10);
  * being ``WS_EX_TRANSPARENT``, its "click anywhere = STOP" handler could never fire.

The real banner now lives in ``src/safety/banner_win32.py`` and is selected
automatically by ``src/safety/banner.py::build_safety_banner``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.safety.banner import build_safety_banner  # noqa: E402
from src.safety.controller import default_state_path, get_safety_controller  # noqa: E402


def main() -> int:
    sc = get_safety_controller()
    banner = build_safety_banner(sc, geometry="1400x90+20+8")
    sc.set_banner_available(bool(banner.available))
    started = banner.start()
    sc.banner_visible(ok=True)
    print(f"backend={getattr(banner, 'backend', 'tkinter')} started={started} "
          f"visible={banner.visible} state_file={default_state_path()}")
    print("Ctrl+Alt+F12 = EMERGENCY STOP | use the on-screen Resume control to resume")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        banner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
