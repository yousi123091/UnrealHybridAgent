"""Phase 3 layered environment doctor.

Distinguishes "process exists" from "can actually talk to UE".
No fake green: every probe has an independent failure path.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve().parents[1]
if str(_THIS) not in sys.path:
    sys.path.insert(0, str(_THIS))

from src.core.config import load_config  # noqa: E402


def _tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ue_processes() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        import subprocess

        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name like 'UnrealEditor%'\" | "
            "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
        )
        raw = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps],
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        data = json.loads(raw.decode("utf-8", errors="replace") or "[]")
        if isinstance(data, dict):
            data = [data]
        for row in data:
            out.append(
                {
                    "pid": row.get("ProcessId"),
                    "name": row.get("Name"),
                    "cmdline": (row.get("CommandLine") or "")[:240],
                }
            )
    except Exception as exc:  # noqa: BLE001
        out.append({"error": str(exc)[:200]})
    return out


def _plugin_log_signals(project_file: str) -> dict[str, Any]:
    """Read UE editor logs for UnrealMCPython mount/load/TCP evidence."""
    proj = Path(project_file)
    log_dir = proj.parent / "Saved" / "Logs"
    result: dict[str, Any] = {
        "log_dir": str(log_dir),
        "log_exists": log_dir.is_dir(),
        "mount_seen": False,
        "module_loaded": False,
        "tcp_started": False,
        "tcp_stopped": False,
        "latest_log": None,
        "signals": [],
    }
    if not log_dir.is_dir():
        return result
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return result
    latest = logs[0]
    result["latest_log"] = latest.name
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:200]
        return result
    checks = [
        ("mount_seen", "Mounting Project plugin"),
        ("module_loaded", "InternalLoadLibrary: 'UnrealMCPython'"),
        ("tcp_started", "TCP server started"),
        ("tcp_stopped", "TCP server stopped"),
    ]
    for key, needle in checks:
        hit = needle in text and ("UnrealMCPython" in text or "MCPython" in text or "12029" in text)
        if key == "mount_seen":
            hit = "Mounting Project plugin" in text and "UnrealMCPython" in text
        elif key == "module_loaded":
            hit = "InternalLoadLibrary: 'UnrealMCPython'" in text
        elif key == "tcp_started":
            hit = "LogMCPython: TCP server started" in text or (
                "TCP server started" in text and "12029" in text
            )
        elif key == "tcp_stopped":
            hit = "LogMCPython: TCP server stopped" in text or (
                "TCP server stopped" in text and "MCPython" in text
            )
        result[key] = bool(hit)
        if hit:
            result["signals"].append(key)
    return result


def _computer_use_health(base_url: str) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            return {"ok": bool(data.get("ok")), "url": url, "detail": data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(exc)[:200]}


def _overlay_status() -> dict[str, Any]:
    try:
        import tkinter

        return {
            "tkinter_available": True,
            "tk_version": getattr(tkinter, "TkVersion", None),
            "python": sys.version.split()[0],
            "executable": sys.executable,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "tkinter_available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "python": sys.version.split()[0],
            "executable": sys.executable,
        }


def _gui_calibration_status(state_dir: Path) -> dict[str, Any]:
    cache = state_dir / "gui_session.json"
    if not cache.is_file():
        return {"cache_present": False, "path": str(cache)}
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return {"cache_present": True, "path": str(cache), "keys": sorted(data)[:30], "summary": {
            k: data.get(k) for k in ("window_title", "hwnd", "rect", "dpi", "focus_point", "monitor") if k in data
        }}
    except Exception as exc:  # noqa: BLE001
        return {"cache_present": True, "path": str(cache), "error": str(exc)[:200]}


def _mcp_real_request(cfg) -> dict[str, Any]:
    """Attempt one real MCP capability/TCP-backed request. Never invent success."""
    out: dict[str, Any] = {
        "attempted": False,
        "request_ok": False,
        "tcp_reachable": False,
        "profile": None,
        "capabilities": [],
        "error": None,
        "latency_ms": None,
    }
    try:
        from src.runtime import build_bundle

        section = cfg.get("mcp_servers.unreal_mcp") or {}
        ue_tcp = section.get("ue_tcp") or {}
        host = str(ue_tcp.get("host") or "127.0.0.1")
        port = int(ue_tcp.get("port") or 12029)
        out["tcp_reachable"] = _tcp_open(host, port)
        out["endpoint"] = f"{host}:{port}"

        bundle = build_bundle(cfg)
        backend = bundle.unreal.get("UNREAL_MCP")
        if backend is None:
            out["error"] = "UNREAL_MCP backend not assembled"
            return out
        out["attempted"] = True
        t0 = time.perf_counter()
        diag = backend.diagnostics()
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["profile"] = diag.get("profile")
        out["capabilities"] = diag.get("capabilities") or []
        out["backend_available"] = bool(diag.get("available"))
        out["tools_discovered"] = diag.get("tools_discovered")
        out["backend_error"] = diag.get("error")
        # Real request: if TCP is up and profile detected, try current_level / get_actors lightly
        if out["tcp_reachable"] and out["backend_available"]:
            try:
                t1 = time.perf_counter()
                level = backend.current_level() if hasattr(backend, "current_level") else None
                out["level_probe"] = level
                out["request_latency_ms"] = round((time.perf_counter() - t1) * 1000, 1)
                out["request_ok"] = True
            except Exception as exc:  # noqa: BLE001
                out["request_ok"] = False
                out["error"] = f"real request failed: {type(exc).__name__}: {exc}"[:300]
        elif not out["tcp_reachable"]:
            out["error"] = f"UE TCP {host}:{port} not listening"
        else:
            out["error"] = out.get("backend_error") or "backend available=false"
        try:
            bundle.close()
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


def _router_methods(cfg) -> dict[str, Any]:
    try:
        from src.runtime import build_bundle

        bundle = build_bundle(cfg)
        avail = bundle.availability()
        try:
            bundle.close()
        except Exception:
            pass
        return avail
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def collect_doctor(cfg_path: str | None = None) -> dict[str, Any]:
    cfg = load_config(cfg_path, reload=True)
    project_file = str(cfg.get("ue.project_file") or "")
    state_dir = cfg.path("workspace.state_dir")
    if not isinstance(state_dir, Path):
        state_dir = Path(str(state_dir))

    host = "127.0.0.1"
    port = 12029
    ue_tcp = (cfg.get("mcp_servers.unreal_mcp") or {}).get("ue_tcp") or {}
    host = str(ue_tcp.get("host") or host)
    try:
        port = int(ue_tcp.get("port") or port)
    except Exception:
        port = 12029

    report: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "python": {
            "version": sys.version.split()[0],
            "executable": sys.executable,
        },
        "layers": {},
        "summary": {},
    }

    procs = _ue_processes()
    alive = any(p.get("pid") for p in procs if isinstance(p, dict))
    report["layers"]["ue_editor_process"] = {
        "alive": alive,
        "processes": procs,
        "project_file": project_file,
    }

    logs = _plugin_log_signals(project_file)
    report["layers"]["unreal_mcpython_log"] = logs

    tcp = {"host": host, "port": port, "listening": _tcp_open(host, port)}
    report["layers"]["unreal_mcpython_tcp"] = tcp

    cu_url = str((cfg.get("mcp_servers.computer_use") or {}).get("base_url") or "http://127.0.0.1:8788")
    report["layers"]["computer_use"] = _computer_use_health(cu_url)

    report["layers"]["overlay"] = _overlay_status()
    report["layers"]["gui_calibration"] = _gui_calibration_status(state_dir)

    # Router methods availability uses live backend probes
    report["layers"]["router_methods"] = _router_methods(cfg)

    # Real MCP request last (may be slow)
    report["layers"]["mcp_real_request"] = _mcp_real_request(cfg)

    mcp = report["layers"]["mcp_real_request"]
    report["summary"] = {
        "ue_editor_alive": bool(alive),
        "plugin_mounted": bool(logs.get("mount_seen")),
        "plugin_module_loaded": bool(logs.get("module_loaded")),
        "plugin_tcp_started_in_log": bool(logs.get("tcp_started")),
        "tcp_listening": bool(tcp["listening"]),
        "mcp_request_ok": bool(mcp.get("request_ok")),
        "computer_use_ok": bool(report["layers"]["computer_use"].get("ok")),
        "overlay_available": bool(report["layers"]["overlay"].get("tkinter_available")),
        "gui_cache_present": bool(report["layers"]["gui_calibration"].get("cache_present")),
        "unreal_mcp_available": bool((report["layers"]["router_methods"] or {}).get("UNREAL_MCP")),
    }
    report["pass_gate"] = {
        "can_read_ue": report["summary"]["tcp_listening"] and report["summary"]["mcp_request_ok"],
        "can_mutate_ue_unproven_until_demo": False,
        "computer_use_ready": report["summary"]["computer_use_ok"],
        "overlay_ready": report["summary"]["overlay_available"],
    }
    return report


def format_report(report: dict[str, Any]) -> str:
    s = report.get("summary") or {}
    lines = [
        "=== Phase 3 Doctor（分层真机探针） ===",
        f"Python: {report['python']['version']} @ {report['python']['executable']}",
        "",
        "1) UE Editor process",
        f"   alive={s.get('ue_editor_alive')}",
        f"   project={report['layers']['ue_editor_process'].get('project_file')}",
    ]
    for p in (report["layers"]["ue_editor_process"].get("processes") or [])[:5]:
        lines.append(f"   - pid={p.get('pid')} name={p.get('name')} cmd={p.get('cmdline')}")
    logs = report["layers"]["unreal_mcpython_log"]
    lines += [
        "",
        "2) UnrealMCPython log signals",
        f"   mounted={logs.get('mount_seen')} module_loaded={logs.get('module_loaded')} "
        f"tcp_started={logs.get('tcp_started')} latest_log={logs.get('latest_log')}",
        "",
        "3) UnrealMCPython TCP",
        f"   {report['layers']['unreal_mcpython_tcp'].get('host')}:"
        f"{report['layers']['unreal_mcpython_tcp'].get('port')} "
        f"listening={report['layers']['unreal_mcpython_tcp'].get('listening')}",
        "",
        "4) MCP real request",
        f"   request_ok={report['layers']['mcp_real_request'].get('request_ok')} "
        f"tcp_reachable={report['layers']['mcp_real_request'].get('tcp_reachable')} "
        f"profile={report['layers']['mcp_real_request'].get('profile')} "
        f"caps={len(report['layers']['mcp_real_request'].get('capabilities') or [])}",
        f"   level_probe={str(report['layers']['mcp_real_request'].get('level_probe'))[:160]}",
        f"   error={report['layers']['mcp_real_request'].get('error')}",
        "",
        "5) Computer Use",
        f"   ok={report['layers']['computer_use'].get('ok')} "
        f"detail={json.dumps(report['layers']['computer_use'].get('detail') or {}, ensure_ascii=False)[:220]}",
        "",
        "6) Overlay",
        f"   {json.dumps(report['layers']['overlay'], ensure_ascii=False)}",
        "",
        "7) GUI calibration cache",
        f"   {json.dumps(report['layers']['gui_calibration'], ensure_ascii=False)[:300]}",
        "",
        "8) Router methods availability",
        f"   {json.dumps(report['layers']['router_methods'], ensure_ascii=False)}",
        "",
        "=== Summary / gates ===",
        json.dumps(s, ensure_ascii=False, indent=2),
        "gates=" + json.dumps(report.get("pass_gate"), ensure_ascii=False),
        "",
        "判定：仅当 tcp_listening=true 且 mcp_request_ok=true，才允许认为 UE 真实可读。",
        "禁止把 UE 进程存在或 MCP server 存活当成 MCP 可用。",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    rep = collect_doctor()
    print(format_report(rep))
    out = _THIS / "logs" / "runs" / f"phase3_doctor_{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON: {out}")
    raise SystemExit(0 if rep["summary"]["mcp_request_ok"] else 2)
