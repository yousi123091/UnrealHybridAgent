#!/usr/bin/env python
"""UAH 性能实测（需求 §十八：idle CPU / running CPU / 内存 / 事件延迟）。

    python uah/tests/perf_probe.py

只用 stdlib：
* CPU 用 ``time.process_time()``（进程内所有线程的 CPU 时间），
  所以测出来的"CPU 占用"是**进程真实消耗 / 墙钟时间**，不是某个线程的猜测。
* 内存用 Windows 的 ``K32GetProcessMemoryInfo``（ctypes），拿 WorkingSetSize。
  没有 psutil 也不影响。

测量场景刻意贴近真实用法：
  1. **idle** —— Hub 在跑、一个 SSE 订阅者连着、**没有任何事件**。
     这是 HUD 最常待的状态，所以它必须接近 0。
  2. **running** —— 每秒 20 条事件（远高于 UHA 的真实频率：一次任务也就几十条事件）。
  3. **latency** —— POST /event 到 SSE 收到快照的**端到端**延迟（含 HTTP 往返）。
"""

from __future__ import annotations

import ctypes
import json
import statistics
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from uah.adapters.generic.bridge import GenericBridge  # noqa: E402
from uah.core.transport import HubClient, HubServer, pick_free_port  # noqa: E402
from uah.ui.components.card import render_card  # noqa: E402


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> float | None:
    """当前进程 WorkingSetSize（MB）。

    K32 版在 kernel32，老版在 psapi；两个都试一下，
    拿不到就返回 None —— **不猜一个数**，report 里如实写"未取到"。
    """
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    for dll_name, fn_name in (("kernel32", "K32GetProcessMemoryInfo"),
                              ("psapi", "GetProcessMemoryInfo")):
        try:
            dll = getattr(ctypes.windll, dll_name)
            fn = getattr(dll, fn_name)
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
            fn.restype = wintypes.BOOL
            if fn(handle, ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / (1024 * 1024)
        except Exception:  # noqa: BLE001
            continue
    return None


def fmt_mb(value: float | None) -> str:
    return f"{value:.1f} MB" if value is not None else "未取到（该 Windows 未导出内存查询 API）"


class CpuMeter:
    """墙钟窗口内的进程 CPU 占用（所有线程合计）。"""

    def __enter__(self) -> "CpuMeter":
        self._cpu0 = time.process_time()
        self._wall0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.cpu = time.process_time() - self._cpu0
        self.wall = time.perf_counter() - self._wall0
        self.percent = (self.cpu / self.wall * 100.0) if self.wall > 0 else 0.0


def main() -> int:
    port = pick_free_port()
    server = HubServer(port=port).start()
    url = server.url()

    received: list[float] = []
    stop = threading.Event()
    latencies: list[float] = []

    def on_snapshot(snap) -> None:
        received.append(time.perf_counter())

    hud_thread = threading.Thread(
        target=lambda: HubClient(url, timeout_s=30.0).stream(
            on_snapshot, stop=stop, reconnect_delay_s=0.2),
        name="perf-subscriber", daemon=True,
    )
    hud_thread.start()
    time.sleep(0.6)

    bridge = GenericBridge(url)
    print("=" * 62)
    print("UAH 性能实测")
    print("=" * 62)

    # --- 1) idle -----------------------------------------------------------
    warmup = 0.8
    baseline_rss = rss_mb()
    time.sleep(warmup)
    with CpuMeter() as idle:
        time.sleep(6.0)
    idle_rss = rss_mb()
    print(f"\n[idle] Hub + 1 个 SSE 订阅者，无任何事件，持续 {idle.wall:.1f}s")
    print(f"  CPU 时间   : {idle.cpu * 1000:.1f} ms")
    print(f"  平均 CPU   : {idle.percent:.2f} %")
    print(f"  内存       : {fmt_mb(idle_rss)}  (基线 {fmt_mb(baseline_rss)})")

    # --- 2) running --------------------------------------------------------
    rate = 20           # 每秒事件数
    duration = 5.0
    sent = 0
    with CpuMeter() as run:
        t_end = time.perf_counter() + duration
        while time.perf_counter() < t_end:
            bridge.emit(agent="Perf", status="running",
                        task=f"step {sent % 7 + 1}", activity=f"synthetic event {sent}",
                        tool="UNREAL_MCP")
            sent += 1
            time.sleep(1.0 / rate)
    run_rss = rss_mb()
    print(f"\n[running] 以 {rate}/s 灌了 {sent} 条事件，持续 {run.wall:.1f}s")
    print(f"  CPU 时间   : {run.cpu * 1000:.1f} ms")
    print(f"  平均 CPU   : {run.percent:.2f} %")
    print(f"  内存       : {fmt_mb(run_rss)}")
    print(f"  事件吞吐   : {sent / run.wall:.1f} 事件/s")

    # --- 3) 端到端延迟 ------------------------------------------------------
    n = 120
    for i in range(n):
        before = len(received)
        t0 = time.perf_counter()
        bridge.emit(agent="Perf", status="running", task="latency", activity=f"L{i}")
        # 等这条事件真的被订阅者收到
        deadline = t0 + 2.0
        while len(received) <= before and time.perf_counter() < deadline:
            time.sleep(0.0005)
        latencies.append((time.perf_counter() - t0) * 1000.0)

    ok = [x for x in latencies if x < 2000]
    print(f"\n[latency] POST /event → SSE 收到快照（{n} 次，含 HTTP 往返）")
    if ok:
        print(f"  中位数     : {statistics.median(ok):.2f} ms")
        print(f"  均值       : {statistics.fmean(ok):.2f} ms")
        print(f"  最大       : {max(ok):.2f} ms")
        print(f"  最小       : {min(ok):.2f} ms")
        p95 = sorted(ok)[int(len(ok) * 0.95) - 1]
        print(f"  P95        : {p95:.2f} ms")

    # --- 4) 渲染开销 --------------------------------------------------------
    snaps = HubClient(url).state()
    if snaps:
        t0 = time.perf_counter()
        for _ in range(2000):
            render_card(snaps[0])
        per = (time.perf_counter() - t0) / 2000 * 1000
        print(f"\n[render] render_card() 单次 {per:.3f} ms（{len(snaps)} 个 Agent）")

    # --- 5) 单 Agent 事件数上限参考 ----------------------------------------
    print(f"\n[规模] Hub 内 Agent 数 = {len(server.store)}；"
          f"store 统计 = {json.dumps(server.store.stats()['by_status'], ensure_ascii=False)}")

    stop.set()
    server.stop()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
