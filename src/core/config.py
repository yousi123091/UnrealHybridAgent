"""配置加载。

优先级（后者覆盖前者）：
    1. 内置 DEFAULTS
    2. config/agent.config.json（不存在则读 agent.config.example.json）
    3. 环境变量（UHA_ 前缀）

配置里**不允许出现用户路径硬编码**——示例配置里的路径都视为"可配置项"，
真实路径只应写在本地的 agent.config.json（已 gitignore）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

# 项目根目录 = 本文件上溯三级（src/core/config.py -> src/core -> src -> 根）
ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "agent.config.json"
EXAMPLE_CONFIG_PATH = CONFIG_DIR / "agent.config.example.json"

DEFAULTS: dict[str, Any] = {
    "workspace": {
        "root": ".",
        "log_dir": "logs",
        "state_dir": ".state",
        "artifact_dir": "artifacts",
    },
    "ue": {
        "engine_root": "",
        "editor_exe": "",
        "editor_cmd_exe": "",
        "project_file": "",
        "remote_exec_python_path": "",
        "remote_exec": {
            "enabled": True,
            "multicast_group": "239.0.0.1",
            "multicast_port": 6766,
            "multicast_ttl": 0,
            "bind_address": "127.0.0.1",
            "discovery_timeout_s": 3.0,
            "command_timeout_s": 30.0,
        },
        "commandlet": {"enabled": True, "timeout_s": 900, "extra_args": ["-unattended", "-nopause"]},
        "remote_control": {
            "enabled": True,
            "base_url": "http://127.0.0.1:30010",
            "timeout_s": 30.0,
        },
        "search_paths": {"project_roots": []},
    },
    "mcp_servers": {
        "root": "",
        "computer_use": {
            "enabled": True,
            "kind": "computer_use",
            "transport": "http",
            "base_url": "http://127.0.0.1:8788",
            "endpoint": "/mcp",
            "call_endpoint": "/call",
            "health_endpoint": "/health",
            "token": "",
            "autostart": {"enabled": False, "cwd": "", "command": [], "health_wait_s": 20},
            "request_timeout_s": 120,
        },
        "unreal_mcp": {
            "enabled": True,
            "kind": "unreal_mcp",
            "transport": "auto",
            "stdio": {"command": "", "args": [], "env": {}},
            "http": {"base_url": "", "endpoint": "/mcp", "token": ""},
            "tool_contract": "unreal_mcp",
            "probe_tools": [],
            "request_timeout_s": 60,
        },
    },
    "router": {
        # 需求 §8 的硬预算：单方法最多 2 次，整任务最多 5 次降级
        "max_attempts_per_method": 2,
        "max_total_fallbacks": 5,
        "max_fallback_attempts": 4,
        "max_failures_per_method": 2,
        "method_cooldown_s": 60,
        "exploration_rate": 0.0,
        "stats_file": ".state/routing_stats.json",
        "quality_file": ".state/router_quality.json",
        "weights": {
            "success_rate": 0.42,
            "latency": 0.18,
            "precision": 0.16,
            "cost": 0.14,
            "risk": 0.10,
        },
        "preferences": {
            "prefer_structured_over_gui": True,
            "prefer_mcp_over_python": False,
        },
    },
    "desktop": {
        "session_lock_ttl_s": 180,
        "acquire_remote_lock": True,
        "queue_timeout_s": 300,
        "dry_run": False,
        "client_id": "unreal-hybrid-agent",
        "editor_focus_point": None,
        "emergency_hotkey": "ctrl+alt+shift+f12",
        "overlay": {"enabled": False, "geometry": "320x180+20+20"},
        "gui_cache_file": ".state/gui_session.json",
    },
    "gui": {"layout_overrides": {}},
    "vision": {
        "backend": "heuristic",
        "diff": {"downscale_to_width": 320, "changed_pixel_threshold": 12, "changed_ratio_threshold": 0.012},
        "roi": {"world_outliner": None, "details_panel": None, "viewport": None, "toolbar": None},
        "details_location_z_rel": {"x": 0.62, "y": 0.075},
    },
    "safety": {
        "require_preview_above": 10,
        "never_delete_assets": True,
        "never_modify_core_config": True,
        "auto_save_before_mutation": True,
        "max_actors_per_batch": 500,
    },
    "logging": {"level": "INFO", "console": True, "file": True, "max_bytes": 5242880, "backup_count": 5},
}


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并；override 里以 `$comment` 开头的键会被丢弃。"""
    out: dict[str, Any] = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in base.items()}
    for key, value in override.items():
        if key.startswith("$"):
            continue
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _strip_comments(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("$")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def _env_overrides() -> dict[str, Any]:
    """支持少量关键环境变量覆盖，避免把密钥写进配置文件。

    只暴露真正需要在 CI / 临时会话里改的项。
    """
    out: dict[str, Any] = {}
    cu = {}
    if os.getenv("UHA_COMPUTER_USE_URL"):
        url = os.environ["UHA_COMPUTER_USE_URL"].rstrip("/")
        cu["base_url"] = url
    if os.getenv("UHA_COMPUTER_USE_TOKEN"):
        cu["token"] = os.environ["UHA_COMPUTER_USE_TOKEN"]
    if cu:
        out["mcp_servers"] = {"computer_use": cu}

    um = {}
    if os.getenv("UHA_UNREAL_MCP_COMMAND"):
        um["stdio"] = {"command": os.environ["UHA_UNREAL_MCP_COMMAND"]}
    if os.getenv("UHA_UNREAL_MCP_ARGS"):
        um.setdefault("stdio", {})["args"] = os.environ["UHA_UNREAL_MCP_ARGS"].split()
        um.setdefault("stdio", {})["command"] = um["stdio"].get("command", "")
    if os.getenv("UHA_UNREAL_MCP_URL"):
        um["http"] = {"base_url": os.environ["UHA_UNREAL_MCP_URL"].rstrip("/")}
    if um:
        out.setdefault("mcp_servers", {})["unreal_mcp"] = um

    ue = {}
    if os.getenv("UHA_UE_PROJECT"):
        ue["project_file"] = os.environ["UHA_UE_PROJECT"]
    if os.getenv("UHA_UE_ENGINE_ROOT"):
        ue["engine_root"] = os.environ["UHA_UE_ENGINE_ROOT"]
    if ue:
        out["ue"] = ue

    if os.getenv("UHA_DRY_RUN"):
        out["desktop"] = {"dry_run": os.environ["UHA_DRY_RUN"].lower() in {"1", "true", "yes", "on"}}

    return out


class Config:
    """点号访问的配置对象。

        cfg.get("mcp_servers.computer_use.base_url")
        cfg.path("logs")        # 绝对路径
    """

    def __init__(self, data: Mapping[str, Any], source: Path | None = None):
        self._data = _strip_comments(dict(data))
        self.source = source

    # -- 构造 ----------------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        candidate = Path(path) if path else (DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.exists() else None)
        source: Path | None = None
        file_data: dict[str, Any] = {}

        if candidate and Path(candidate).exists():
            file_data = json.loads(Path(candidate).read_text(encoding="utf-8"))
            source = Path(candidate)
        elif EXAMPLE_CONFIG_PATH.exists():
            file_data = json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
            source = EXAMPLE_CONFIG_PATH

        merged = _deep_merge(DEFAULTS, file_data)
        merged = _deep_merge(merged, _env_overrides())
        return cls(merged, source)

    # -- 读取 ----------------------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted: str) -> dict[str, Any]:
        value = self.get(dotted)
        return dict(value) if isinstance(value, Mapping) else {}

    # -- 路径 ----------------------------------------------------------------

    def root(self) -> Path:
        return ROOT

    def path(self, dotted: str, *, ensure_parent: bool = False) -> Path:
        """把配置里的相对路径解析成绝对路径（相对项目根）。"""
        raw = self.get(dotted)
        if raw in (None, ""):
            raw = dotted.split(".")[-1]
        p = Path(str(raw))
        if not p.is_absolute():
            p = ROOT / p
        if ensure_parent:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def is_dry_run(self) -> bool:
        return bool(self.get("desktop.dry_run", False))


_CACHED: Config | None = None


def load_config(path: str | os.PathLike[str] | None = None, *, reload: bool = False) -> Config:
    global _CACHED
    if reload or _CACHED is None:
        _CACHED = Config.load(path)
    return _CACHED


__all__ = ["Config", "load_config", "ROOT", "CONFIG_DIR", "DEFAULTS"]
