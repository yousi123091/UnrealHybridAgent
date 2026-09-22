"""第三方 Unreal MCP 后端（GenOrca/unreal-mcp v2.2.0 契约）。

本文件**不实现** Unreal MCP，只做"协议 + 契约"适配：

    本项目的统一 op 词汇表        第三方 MCP 的真实工具名
    ----------------------        ----------------------
    get_actors            <--     actor  {action: get_all_details}
    find_actor            <--     actor  {action: get_transform}  (命中即返回；未命中再全表模糊扫)
    get_actor_transform   <--     actor  {action: get_transform}
    set_actor_location    <--     actor  {action: set_location}
    set_actor_rotation    <--     actor  {action: set_rotation}
    set_actor_scale       <--     actor  {action: set_scale}
    save_level            <--     level  {action: save_current_level}
    current_level         <--     level  {action: get_current_level_path}
    level_dirty_state     <--     util   {action: execute_python}
    execute_ue_python     <--     util   {action: execute_python}
    viewport_screenshot   <--     vision {action: capture_viewport}   (返回 MCP Image)

两种契约（profile）自动识别：

    flat      工具名就是动作名（每个动作一个 MCP tool）
    domains   少数几个"域工具" + action 参数（GenOrca/unreal-mcp 的 21 域风格）

识别失败时**不猜**：直接标记 unavailable，并在 diagnostics 里打印它到底有哪些工具。

实测要点（v2.2.0，UE 5.8，2026-09 核对源码）：
    * MCP 工具名 = 域名字符串（``actor`` / ``level`` / ``util`` / ``vision`` …），
      每个工具签名固定为 ``(action: str, params: dict = {})``；
    * actor 侧参数名是 ``actor_label``（不是 ``actor_name`` / ``name``）；
    * actor 域**没有** find 类动作，查找必须靠 ``get_transform`` 精确命中或全表过滤；
    * ``vision`` 域的 capture 动作返回 MCP ``Image`` 内容块，不是 text —— 截图要读 ``images``；
    * ``util.execute_python`` 走独立 TCP 类型，返回值里 ``result`` 是 UE 侧 stdout 字符串。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from ...core.errors import BackendUnavailable, NotSupported, ToolCallFailed
from ..base import ActorRef, UnrealBackend
from ..mcp_client import MCPClient, make_client_from_config

#: flat 契约下的候选工具名（按优先级）
FLAT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "get_actors": ("get_actors_in_level", "get_all_level_actors", "list_actors", "get_actors"),
    "find_actor": ("find_actors_by_name", "find_actor_by_name", "find_actor"),
    "get_actor_transform": ("get_actor_transform", "get_actor_properties", "get_actor_location"),
    "set_actor_location": ("set_actor_location", "move_actor", "set_actor_transform"),
    "set_actor_rotation": ("set_actor_rotation", "set_actor_transform"),
    "set_actor_scale": ("set_actor_scale", "set_actor_scale3d", "set_actor_transform"),
    "save_level": ("save_current_level", "save_level"),
    "execute_ue_python": ("execute_python", "run_python", "execute_ue_python"),
}

#: domains 契约（GenOrca 风格）下的 统一 op -> (域工具名, 动作名)
#: 与 plugin 端 ``ue_<action>`` 函数签名严格对应。
DOMAIN_MAP: dict[str, tuple[str, str]] = {
    "get_actors": ("actor", "get_all_details"),
    "find_actor": ("actor", "get_transform"),
    "get_actor_transform": ("actor", "get_transform"),
    "set_actor_location": ("actor", "set_location"),
    "set_actor_rotation": ("actor", "set_rotation"),
    "set_actor_scale": ("actor", "set_scale"),
    "save_level": ("level", "save_current_level"),
    "level_dirty_state": ("util", "execute_python"),
    "current_level": ("level", "get_current_level_path"),
    "execute_ue_python": ("util", "execute_python"),
    "level_file_stat": ("util", "execute_python"),
    "viewport_screenshot": ("vision", "capture_viewport"),
}

#: 只支持 domains 契约、但不在统一 op 表里的额外动作（hybrid 演示要用）
#: 统一走 :meth:`UnrealMCPBackend.call_domain`
EXTRA_DOMAIN_ACTIONS: dict[str, tuple[str, str]] = {
    "world_to_screen": ("util", "world_to_screen"),
    "screen_to_world": ("util", "screen_to_world"),
    "get_viewport_camera": ("util", "get_viewport_camera"),
    "set_viewport_camera": ("util", "set_viewport_camera"),
    "get_actor_bounds": ("actor", "get_actor_bounds"),
    "select_actors": ("actor", "select_actors"),
    "get_selected_actors": ("actor", "get_selected_actors"),
    "get_actors_of_class": ("actor", "get_actors_of_class"),
    "save_all_dirty": ("util", "save_all_dirty"),
    "get_output_log": ("util", "get_output_log"),
    "execute_console_command": ("util", "execute_console_command"),
    "add_actor_tag": ("actor", "add_actor_tag"),
    "get_actor_tags": ("actor", "get_actor_tags"),
    "get_actor_folder": ("actor", "get_actor_folder"),
    "print_message": ("util", "print_message"),
    # 视觉域：capture_actors 会从"抬高的 3/4 视角"框住指定 actor——
    # 这是 Demo2（漂浮物检测）最稳的取图方式：构图可控、目标必然在画面内。
    "capture_actors": ("vision", "capture_actors"),
    "capture_from": ("vision", "capture_from"),
    # 精确落回地面：向下射线取支撑面（ROADMAP §2）
    "line_trace": ("actor", "line_trace"),
}

#: 计算"脏包"的 UE 侧脚本。
#: 用 ``MCPythonHelper.submit_result`` 直接回传结果（干净通道，不污染 Output Log）；
#: 万一该 API 不存在，退回 print —— 两条路最终都会落在响应的 ``result`` 字段里。
DIRTY_STATE_CODE = """
import unreal, json as _json

def _payload():
    pkgs = unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()
    names = []
    for p in pkgs:
        try:
            names.append(str(p.get_name()))
        except Exception:
            names.append(str(p))
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    return {
        "success": True,
        "dirty_count": len(names),
        "dirty_packages": names,
        "level_path": str(world.get_path_name()) if world else None,
    }

_s = _json.dumps(_payload())
try:
    unreal.MCPythonHelper.submit_result(_s)
except Exception:
    print(_s)
"""

#: 查询"当前 Level 对应的磁盘文件"的 stat（path / mtime / size）。
#:
#: 为什么需要它：实测 ``get_dirty_map_packages()`` 在本机不反映 Python 侧改动，
#: 拿它验证"保存成功"是假绿。而 .umap 的 mtime 是**文件系统层面的硬事实**，
#: 保存真的落盘了 mtime 就会推进——这才是可靠的保存证据。
LEVEL_FILE_STAT_CODE = """
import unreal, json as _json, os, glob

def _payload():
    world = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        return {"success": False, "message": "没有打开的编辑器世界"}
    pkg = world.get_outermost()
    pkg_name = str(pkg.get_name()) if pkg else ""
    content = str(unreal.Paths.convert_relative_path_to_full(unreal.Paths.project_content_dir()))
    level = str(world.get_name())

    def _stat(p):
        return {
            "success": True,
            "path": p,
            "mtime": os.path.getmtime(p),
            "size": os.path.getsize(p),
            "package": pkg_name,
            "level": level,
        }

    if "/Game/" in pkg_name:
        rel = pkg_name.split("/Game/", 1)[1]
        cand = os.path.join(content, rel + ".umap")
        if os.path.isfile(cand):
            return _stat(cand)

    base = pkg_name.rsplit("/", 1)[-1] if pkg_name else ""
    if base:
        hits = glob.glob(os.path.join(content, "**", base + ".umap"), recursive=True)
        if hits:
            out = _stat(hits[0])
            out["resolved_by"] = "glob"
            return out

    return {
        "success": False,
        "message": "找不到 Level 文件（可能是未保存的 untitled level）",
        "package": pkg_name,
        "content_dir": content,
    }

_s = _json.dumps(_payload())
try:
    unreal.MCPythonHelper.submit_result(_s)
except Exception:
    print(_s)
"""


class UnrealMCPBackend(UnrealBackend):
    """通过 MCP 协议调用第三方 Unreal MCP（GenOrca/unreal-mcp 等）。"""

    name = "UNREAL_MCP"

    def __init__(
        self,
        section: Mapping[str, Any],
        *,
        timeout: float = 60.0,
        dry_run: bool = False,
    ):
        super().__init__(timeout=timeout, dry_run=dry_run)
        self.section = dict(section)
        self.profile: str | None = None
        self._client: MCPClient | None = None
        self._tools: list[str] = []
        self._flat_ops: dict[str, str] = {}
        self._domain_actions: dict[str, set[str]] = {}
        self._last_error: str | None = None

    # -- 连接与契约识别 -------------------------------------------------------

    def _ensure_client(self) -> MCPClient:
        if self._client is not None:
            process = getattr(self._client, "_proc", None)
            if process is not None and process.poll() is not None:
                raise BackendUnavailable("Unreal MCP stdio process exited; reconnect required")
            return self._client
        self._client = make_client_from_config(self.section, name="unreal-hybrid-agent")
        try:
            self._client.initialize()
            self._tools = self._client.tool_names(refresh=True)
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            failed_client, self._client = self._client, None
            if failed_client is not None:
                failed_client.close()
            raise BackendUnavailable(f"Unreal MCP 连接失败: {exc}") from exc
        self._detect_profile()
        return self._client

    def _detect_profile(self) -> None:
        names = set(self._tools)
        probe = list(self.section.get("probe_tools") or [])

        # 1) 配置显式声明的 probe_tools 全部命中 -> flat
        if probe and set(probe).issubset(names):
            self.profile = "flat"
        # 2) 逐个 op 找 flat 候选（至少 3 个命中才认）
        if self.profile is None:
            for op, cands in FLAT_CANDIDATES.items():
                for c in cands:
                    if c in names:
                        self._flat_ops[op] = c
                        break
            if len(self._flat_ops) >= 3:
                self.profile = "flat"
        # 3) domains 风格：工具名就是域名
        if self.profile is None and {"actor", "level"}.issubset(names):
            self.profile = "domains"

    def _actions_of(self, domain: str) -> set[str]:
        """拉取某个域的动作清单（缓存）。

        这是**真实探测**：拿不到清单就说明这个域没法用，
        绝不用"我以为它有"来冒充能力。返回空集合即视为不可用。
        """
        if domain in self._domain_actions:
            return self._domain_actions[domain]
        try:
            client = self._ensure_client()
            res = client.call_tool(domain, {"action": "list_actions", "params": {}})
            data = res.get("data") or {}
            actions = set((data.get("actions") or {}).keys())
        except Exception as exc:  # noqa: BLE001 - 探测失败就是不可用
            self._last_error = f"list_actions({domain}) 失败: {exc}"
            actions = set()
        self._domain_actions[domain] = actions
        return actions

    def capabilities(self) -> set[str]:
        try:
            self._ensure_client()
        except BackendUnavailable:
            return set()
        if self.profile == "flat":
            return set(self._flat_ops)
        if self.profile == "domains":
            caps: set[str] = set()
            for op, (domain, action) in DOMAIN_MAP.items():
                if domain not in set(self._tools):
                    continue
                if action in self._actions_of(domain):
                    caps.add(op)
            return caps
        return set()

    @property
    def supported_ops(self) -> tuple[str, ...]:  # type: ignore[override]
        return tuple(DOMAIN_MAP)

    def _ue_tcp_endpoint(self) -> tuple[str, int]:
        """UE 插件侧 MCPython TCP 端点（GenOrca 默认 127.0.0.1:12029，可配置覆盖）。"""
        raw = self.section.get("ue_tcp") or {}
        host = str(raw.get("host") or "127.0.0.1")
        try:
            port = int(raw.get("port") or 12029)
        except (TypeError, ValueError):
            port = 12029
        return host, port

    def _ue_reachable(self) -> bool:
        """快速探测 UE 插件 TCP 是否可达。

        MCP server 能列出工具 ≠ 编辑器插件在听。实测：tools/list 与
        list_actions 可能由 MCP 进程本地应答，而真正读写 Actor 时才撞上
        ``Connection refused (127.0.0.1:12029)``。available() 必须反映
        **能不能干活**，而不是 **MCP 进程是否启动**。
        """
        import socket

        host, port = self._ue_tcp_endpoint()
        try:
            with socket.create_connection((host, port), timeout=0.35):
                return True
        except OSError:
            return False

    def available(self) -> bool:
        if self.dry_run:
            return True
        try:
            self._ensure_client()
        except BackendUnavailable:
            return False
        if self.profile is None:
            self._last_error = f"无法识别工具契约；服务端工具: {self._tools[:30]}"
            return False
        if self.profile == "domains" and not self._ue_reachable():
            host, port = self._ue_tcp_endpoint()
            self._last_error = (
                f"UE 侧 MCPython TCP {host}:{port} 不可达"
                "（MCP server 在线 ≠ 编辑器插件在线；请先打开 UE 编辑器并启用 UnrealMCPython）"
            )
            return False
        return bool(self.capabilities())

    def diagnostics(self) -> dict[str, Any]:
        # available() 才是权威：它包含 UE TCP 可达性探测。
        # 此前 diagnostics 用 `bool(caps)` 自算 available，会在 MCP server
        # 在线、编辑器插件离线时误报 OK（实测：doctor 可用性 DOWN 与
        # 后端 available=True 自相矛盾）。
        try:
            caps = sorted(self.capabilities())
        except Exception as exc:  # noqa: BLE001
            caps = []
            self._last_error = self._last_error or f"{type(exc).__name__}: {exc}"
        available = self.available()
        info: dict[str, Any] = {
            "backend": self.name,
            "available": available,
            "transport": self.section.get("transport"),
            "stdio_command": (self.section.get("stdio") or {}).get("command"),
            "profile": self.profile,
            "tools_discovered": len(self._tools),
            "tools": self._tools[:40],
            "capabilities": caps,
            "domain_actions": {k: sorted(v) for k, v in self._domain_actions.items()},
            "error": self._last_error,
        }
        if self._client is not None:
            info["server_info"] = getattr(self._client, "server_info", None)
        return info

    # -- 调用 ----------------------------------------------------------------

    def _raw(self, tool: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """底层调用：返回 unwrap 后的结果字典（**不**判断业务成功与否）。"""
        if self.dry_run:
            return {"ok": True, "dry_run": True, "_tool": tool, "_payload": dict(payload)}
        client = self._ensure_client()
        try:
            return client.call_tool(tool, payload)
        except ToolCallFailed:
            raise
        except Exception as exc:
            raise ToolCallFailed(f"Unreal MCP 调用 {tool} 失败: {exc}") from exc

    @staticmethod
    def _judge(res: Mapping[str, Any], *, tool: str, payload: Any) -> dict[str, Any]:
        data = res.get("data")
        if res.get("is_error"):
            raise ToolCallFailed(
                f"Unreal MCP 返回 isError: {data if not isinstance(data, dict) else data.get('message')}",
                details={"tool": tool, "payload": payload, "result": data},
            )
        if isinstance(data, Mapping):
            if data.get("success") is False or data.get("ok") is False:
                raise ToolCallFailed(
                    str(data.get("message") or data.get("error") or "Unreal MCP 返回失败"),
                    details={"tool": tool, "payload": payload, "result": dict(data)},
                )
            out = dict(data)
        elif isinstance(data, list):
            out = {"items": data}
        else:
            out = {"text": res.get("text", "")}
        out.setdefault("ok", True)
        if res.get("images"):
            out["images"] = res["images"]
        return out

    def call_domain(self, tool: str, action: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """直接调 domains 契约下的 (域, 动作)。给 hybrid 演示用的逃生舱。"""
        if self.profile != "domains":
            raise NotSupported(
                f"{self.name} 当前契约({self.profile})不支持域动作调用",
                details={"tool": tool, "action": action},
            )
        payload = {"action": action, "params": dict(params or {})}
        return self._judge(self._raw(tool, payload), tool=tool, payload=payload)

    def call_native(self, op: str, **params: Any) -> dict[str, Any]:
        """按 EXTRA_DOMAIN_ACTIONS 里的别名调用额外动作。"""
        spec = EXTRA_DOMAIN_ACTIONS.get(op)
        if not spec:
            raise NotSupported(f"{self.name} 没有额外动作 {op}")
        return self.call_domain(spec[0], spec[1], params)

    def has_native(self, op: str) -> bool:
        spec = EXTRA_DOMAIN_ACTIONS.get(op)
        if not spec:
            return False
        if self.profile != "domains":
            return False
        return spec[1] in self._actions_of(spec[0])

    def _invoke(self, op: str, *, soft: bool = False, **params: Any) -> dict[str, Any]:
        if self.dry_run:
            return {"ok": True, "dry_run": True, "op": op, "params": params}
        client = self._ensure_client()

        if self.profile == "flat":
            tool = self._flat_ops.get(op)
            if not tool:
                raise NotSupported(f"Unreal MCP 未提供 {op} 的映射", details={"tools": self._tools})
            payload = _flatten_flat_args(op, params)
            try:
                res = self._raw(tool, payload)
            except ToolCallFailed:
                raise
            if soft:
                return res
            out = self._judge(res, tool=tool, payload=payload)
        elif self.profile == "domains":
            tool, action = DOMAIN_MAP.get(op, (None, None))
            if not tool or tool not in set(self._tools):
                raise NotSupported(
                    f"Unreal MCP 域 {tool} 不可用（op={op}）", details={"tools": self._tools}
                )
            payload = {"action": action, "params": _flatten_domain_args(op, params)}
            try:
                res = self._raw(tool, payload)
            except ToolCallFailed:
                raise
            if soft:
                return res
            out = self._judge(res, tool=tool, payload=payload)
        else:
            raise BackendUnavailable("Unreal MCP 契约未识别")

        out["_transport"] = f"unreal_mcp:{self.profile}"
        out["_tool"] = tool
        return out

    # -- op ------------------------------------------------------------------

    def get_actors(
        self, *, name_like: str | None = None, class_like: str | None = None, limit: int = 500
    ) -> list[ActorRef]:
        res = self._invoke("get_actors", name_like=name_like, class_like=class_like, limit=limit)
        rows = _extract_rows(res)
        actors = [ActorRef.from_payload(r) for r in rows]
        if name_like:
            needle = name_like.lower()
            actors = [a for a in actors if needle in (a.label or a.name).lower()]
        if class_like:
            needle = class_like.lower()
            actors = [a for a in actors if needle in (a.class_name or "").lower()]
        return actors[:limit]

    def find_actor(self, name: str) -> ActorRef | None:
        """精确命中优先（一次调用），未命中再退回全表模糊匹配。

        `get_transform` 未命中时服务端返回 ``success=False``——那是"没找到"，
        不是"调用失败"，所以这里必须用 soft 模式，不能让它变成异常。
        """
        if self.profile == "domains":
            res = self._invoke("find_actor", soft=True, name=name)
            data = res.get("data") if isinstance(res, Mapping) else None
            if isinstance(data, Mapping) and data.get("success"):
                probed = dict(data)
                probed.setdefault("label", name)
                return ActorRef.from_payload(probed)
        elif self.profile == "flat":
            try:
                res = self._invoke("find_actor", name=name)
            except (ToolCallFailed, NotSupported):
                res = {}
            rows = _extract_rows(res)
            if rows:
                return ActorRef.from_payload(rows[0])

        for a in self.get_actors(name_like=name, limit=5):
            if (a.label or a.name).lower() == name.lower():
                return a
        candidates = self.get_actors(name_like=name, limit=5)
        return candidates[0] if candidates else None

    def get_actor_transform(self, name: str) -> ActorRef:
        res = self._invoke("get_actor_transform", name=name)
        rows = _extract_rows(res)
        if rows:
            return ActorRef.from_payload(rows[0])
        raise ToolCallFailed(f"Unreal MCP 未返回 {name} 的 transform", details=res)

    def set_actor_location(self, name: str, location: Iterable[float]) -> dict[str, Any]:
        return self._invoke("set_actor_location", name=name, location=self._as_xyz(location))

    def line_trace(
        self,
        ray_start: Iterable[float],
        ray_end: Iterable[float],
        *,
        trace_channel: str = "Visibility",
        actors_to_ignore_labels: Iterable[str] | None = None,
        trace_complex: bool = True,
    ) -> dict[str, Any]:
        """向下/任意方向射线检测。命中时返回 location/impact_point/hit_actor_label。"""
        return self.call_domain(
            "actor",
            "line_trace",
            {
                "ray_start": self._as_xyz(ray_start),
                "ray_end": self._as_xyz(ray_end),
                "trace_channel": trace_channel,
                "actors_to_ignore_labels": list(actors_to_ignore_labels or []),
                "trace_complex": bool(trace_complex),
            },
        )

    def downward_support_hits(
        self,
        *,
        location: Sequence[float],
        extent: Sequence[float] | None,
        actor_label: str,
        ignore_labels: Iterable[str] | None = None,
        drop: float = 5000.0,
        samples: int = 3,
        inset: float = 0.15,
    ) -> list[dict[str, Any]]:
        """在 Actor 的 XY 采样点向下打射线，返回命中列表（供精确落地用）。

        忽略命中自己：相对位移/精确落地时，射线起点贴着包围盒，容易先撞到自己。
        """
        loc = [float(v) for v in location]
        ext = [float(v) for v in (extent or [0.0, 0.0, 0.0])]
        while len(ext) < 3:
            ext.append(0.0)
        ignore = set(ignore_labels or [])
        ignore.add(actor_label)

        dx = max(0.0, ext[0] * (1.0 - inset))
        dy = max(0.0, ext[1] * (1.0 - inset))
        offsets: list[tuple[float, float]] = [(0.0, 0.0)]
        if samples >= 3:
            offsets.extend([(-dx, -dy), (dx, dy), (-dx, dy), (dx, -dy)])
        if samples >= 5:
            offsets.extend([(0.0, -dy), (0.0, dy)])

        top_z = loc[2] + (ext[2] if ext[2] else 0.0)
        start_z = top_z + 5.0
        end_z = loc[2] - abs(drop)
        hits: list[dict[str, Any]] = []
        for ox, oy in offsets:
            start = [loc[0] + ox, loc[1] + oy, start_z]
            end = [loc[0] + ox, loc[1] + oy, end_z]
            try:
                res = self.line_trace(
                    start, end, actors_to_ignore_labels=sorted(ignore), trace_complex=True
                )
            except Exception:  # noqa: BLE001 - 单条射线失败不拖垮整次采样
                continue
            if not isinstance(res, Mapping):
                continue
            if not res.get("hit") and not (res.get("data") or {}).get("hit"):
                payload = res.get("data") if isinstance(res.get("data"), Mapping) else res
                if not (isinstance(payload, Mapping) and payload.get("hit")):
                    continue
            payload = res.get("data") if isinstance(res.get("data"), Mapping) else res
            if not isinstance(payload, Mapping) or not payload.get("hit"):
                continue
            impact = payload.get("impact_point") or payload.get("location")
            if not impact or len(impact) < 3:
                continue
            hits.append(
                {
                    "sample": [loc[0] + ox, loc[1] + oy],
                    "impact_z": float(impact[2]),
                    "hit_actor_label": payload.get("hit_actor_label"),
                    "raw": {k: payload.get(k) for k in ("distance", "normal", "impact_point")},
                }
            )
        return hits

    def set_actor_rotation(self, name: str, rotation: Iterable[float]) -> dict[str, Any]:
        return self._invoke("set_actor_rotation", name=name, rotation=self._as_xyz(rotation))

    def set_actor_scale(self, name: str, scale: Iterable[float]) -> dict[str, Any]:
        return self._invoke("set_actor_scale", name=name, scale=self._as_xyz(scale))

    def save_level(self) -> dict[str, Any]:
        return self._invoke("save_level")

    def current_level(self) -> str:
        res = self._invoke("current_level")
        for key in ("level_path", "path", "name"):
            if res.get(key):
                return str(res[key])
        return ""

    def level_dirty_state(self) -> dict[str, Any]:
        res = self.execute_ue_python(DIRTY_STATE_CODE)
        payload = _coerce_script_payload(res)
        if isinstance(payload, Mapping):
            out = dict(payload)
            out.setdefault("ok", bool(out.get("success", True)))
            return out
        return {"ok": False, "raw": res}

    def execute_ue_python(self, code: str) -> dict[str, Any]:
        from .script_policy import validate_editor_script
        validate_editor_script(code)
        return self._invoke("execute_ue_python", code=code)

    def level_file_stat(self) -> dict[str, Any]:
        """当前 Level 的磁盘文件快照：``{path, mtime, size}``。

        这是"保存是否真的发生"的**外部证据**：与编辑器内部状态无关，
        直接看文件系统。拿不到（untitled level）时返回 ``success=False``。
        """
        res = self.execute_ue_python(LEVEL_FILE_STAT_CODE)
        payload = _coerce_script_payload(res)
        if isinstance(payload, Mapping):
            out = dict(payload)
            out.setdefault("ok", bool(out.get("success", True)))
            return out
        return {"ok": False, "raw": res}

    def viewport_screenshot(
        self, *, width: int = 1280, height: int = 720, fov: float = 90.0
    ) -> dict[str, Any]:
        """UE 内部视口截图。结果在 ``images`` 里（base64 PNG），不是文本。"""
        res = self._invoke("viewport_screenshot", width=int(width), height=int(height), fov=float(fov))
        images = res.get("images") or []
        return {"ok": bool(images), "images": images, "count": len(images), "_tool": res.get("_tool")}

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


# --- 参数/返回形态归一 --------------------------------------------------------


def _flatten_flat_args(op: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if op in {"set_actor_location", "set_actor_rotation", "set_actor_scale"}:
        xyz = params.get("location") or params.get("rotation") or params.get("scale") or (0, 0, 0)
        out: dict[str, Any] = {"actor_name": params.get("name"), "name": params.get("name")}
        out["location"] = list(xyz)
        out["x"], out["y"], out["z"] = list(xyz)[:3]
        return {k: v for k, v in out.items() if v is not None}
    if op == "find_actor":
        return {"name": params.get("name"), "actor_name": params.get("name")}
    if op == "get_actor_transform":
        return {"actor_name": params.get("name"), "name": params.get("name")}
    if op == "execute_ue_python":
        return {"code": params.get("code"), "script": params.get("code")}
    return {k: v for k, v in params.items() if v is not None}


def _flatten_domain_args(op: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """统一 op 的入参 -> GenOrca 域动作的入参。"""
    if op in {"set_actor_location", "set_actor_rotation", "set_actor_scale"}:
        vec_key = {
            "set_actor_location": "location",
            "set_actor_rotation": "rotation",
            "set_actor_scale": "scale",
        }[op]
        xyz = list(params.get(vec_key) or (0, 0, 0))
        return {"actor_label": params.get("name"), vec_key: xyz}
    if op in {"find_actor", "get_actor_transform"}:
        return {"actor_label": params.get("name")}
    if op == "execute_ue_python":
        return {"code": params.get("code")}
    if op == "viewport_screenshot":
        out: dict[str, Any] = {}
        for k in ("width", "height", "fov"):
            if params.get(k) is not None:
                out[k] = params[k]
        return out
    if op == "get_actors":
        # actor.get_all_details 不接受参数，过滤在客户端做
        return {}
    return {k: v for k, v in params.items() if v is not None}


def _coerce_script_payload(res: Mapping[str, Any]) -> Any:
    """从 execute_python 的响应里把 JSON 结果捞出来。

    形状可能是：``{success, message, result:"<json>"}``（TCP 直返）
    或 ``{ok, ok..., result}`` / 已被 unwrap 成 dict 的形态。
    """
    raw = res.get("result")
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    data = res.get("data")
    if isinstance(data, (dict, list)):
        return data
    return None


#: 一行 actor 记录里可能出现的键（用于判断"这个 dict 是不是一行"）
_ROW_KEYS = (
    "name",
    "label",
    "actor_label",
    "actor_name",
    "class",
    "class_name",
    "type",
    "location",
    "rotation",
    "scale",
    "transform",
    "path",
    "actor_path",
)


def _looks_like_row(obj: Any) -> bool:
    """判断一个字典是不是"一行 actor 记录"。

    实测 v2.2.0 两种行形态：
      * ``get_all_details`` 行: ``{label, class, location, rotation, ...}``
      * ``get_transform``   行: ``{success, actor_label, location, rotation, scale}``
    """
    return isinstance(obj, Mapping) and any(k in obj for k in _ROW_KEYS)


def _extract_rows(res: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从各种可能的返回里捞出 actor 行。

    必须覆盖实测的三种形态：
      1. ``actor.get_all_details`` -> ``{success, actors:[{label,class,location,...}, ...]}``
      2. ``actor.get_transform``   -> 顶层**直接就是一行**
         ``{success, actor_label, location, rotation, scale}``
      3. 某些后端把结果再包一层 ``data`` / ``result``

    早期版本只做了 (1)(3)，导致 get_transform 返回 [] 被误判成"查不到"。
    """
    # 1) 顶层列表字段
    for key in ("actors", "items", "result", "data", "value"):
        value = res.get(key)
        if isinstance(value, list):
            return [dict(v) for v in value if isinstance(v, Mapping)]
    # 2) 嵌套容器里的列表
    for key in ("actor", "result", "data", "value"):
        value = res.get(key)
        if isinstance(value, Mapping):
            for inner in ("actors", "items", "rows"):
                if isinstance(value.get(inner), list):
                    return [dict(v) for v in value[inner] if isinstance(v, Mapping)]
    # 3) 顶层本身就是一行
    if _looks_like_row(res):
        return [dict(res)]
    # 4) 嵌套字典本身就是一行
    for key in ("actor", "result", "data", "value"):
        value = res.get(key)
        if _looks_like_row(value):
            return [dict(value)]
    return []


def build_unreal_mcp_backend(
    section: Mapping[str, Any], *, timeout: float | None = None, dry_run: bool = False
) -> UnrealMCPBackend:
    return UnrealMCPBackend(
        section,
        timeout=float(timeout or section.get("request_timeout_s") or 60),
        dry_run=dry_run,
    )


__all__ = [
    "UnrealMCPBackend",
    "build_unreal_mcp_backend",
    "FLAT_CANDIDATES",
    "DOMAIN_MAP",
    "EXTRA_DOMAIN_ACTIONS",
    "DIRTY_STATE_CODE",
    "LEVEL_FILE_STAT_CODE",
]
