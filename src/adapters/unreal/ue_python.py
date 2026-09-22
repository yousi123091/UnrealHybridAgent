"""UE Python 后端（编辑器在线）—— 引擎官方 Remote Execution 通道。

实现方式的选择理由：

UE 自带 `PythonScriptPlugin` 的 `Content/Python/remote_execution.py` **本来就是
一个"给外部客户端用"的库**（UDP 组播发现 + TCP 命令通道）。所以这里不去重写协议，
而是**直接加载引擎里那个官方文件**（路径来自配置 `ue.remote_exec_python_path`）。

这样做的收益：
    * 协议细节（组播、节点发现、TCP 端口协商、重连）由 Epic 维护，不会随引擎版本漂移；
    * 本项目零协议代码，只负责"发脚本 / 收结构化结果"；
    * 与 Unreal MCP 互不依赖——即使第三方 MCP 插件加载失败，这条通道依然可用。

代价：需要编辑器处于运行状态（这也是它比 commandlet 更快、且能拿到真实编辑态的原因）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from ...core.errors import BackendUnavailable, ToolCallFailed, TransportError
from ..base import UnrealBackend, ActorRef
from . import ue_scripts

_REMOTE_EXEC_FILE = "remote_execution.py"


#: 官方 Remote Execution 模块在引擎安装目录内的固定相对位置。
#: 这是一条 Epic 定义的路径，与具体机器无关，可安全地由 engine_root 推导。
PLUGIN_PYTHON_REL = "Engine/Plugins/Experimental/PythonScriptPlugin/Content/Python"

#: 环境变量兜底。用于 CI / 临时会话，避免在配置文件里写死绝对路径。
ENV_REMOTE_EXEC = "UHA_UE_REMOTE_EXEC_PATH"


def _engine_root_from_editor_exe(editor_exe: str | Path | None) -> Path | None:
    """从编辑器可执行文件路径上溯出引擎根目录。

    `<EngineRoot>/Engine/Binaries/<Platform>/UnrealEditor.exe` -> `<EngineRoot>`。
    仅当路径形状匹配时才推导；形状不符返回 None，绝不猜测。
    """
    if not editor_exe:
        return None
    p = Path(editor_exe)
    # .../Engine/Binaries/Win64/UnrealEditor.exe
    # parents[0]=Binaries/<Platform>, [1]=Binaries, [2]=Engine, [3]=<EngineRoot>
    if len(p.parents) < 4:
        return None
    if p.parents[2].name.lower() != "engine":
        return None
    return p.parents[3]


def find_remote_execution_module(
    engine_root: Path | None,
    explicit: str | None = None,
    editor_exe: str | Path | None = None,
) -> Path:
    """定位引擎自带的 remote_execution.py。

    查找顺序（全部来自本机配置或环境变量，不含任何开发机固定路径）：

        1. ``explicit``                —— config: ``ue.remote_exec_python_path``
        2. ``engine_root``             —— config: ``ue.engine_root``
        3. ``editor_exe`` 上溯的引擎根  —— config: ``ue.editor_exe``
        4. 环境变量 ``UHA_UE_REMOTE_EXEC_PATH``

    一个都找不到就抛 ``BackendUnavailable``——不猜测、不静默降级到别的通道。
    """
    candidates: list[Path] = []

    def _add_file(p: str | Path | None) -> None:
        if not p:
            return
        q = Path(p)
        candidates.append(q if q.suffix == ".py" else q / _REMOTE_EXEC_FILE)

    def _add_engine_root(root: str | Path | None) -> None:
        if not root:
            return
        candidates.append(Path(root) / PLUGIN_PYTHON_REL / _REMOTE_EXEC_FILE)

    # 1) 显式路径：可以是文件路径，也可以是目录
    _add_file(explicit)
    # 2) 引擎根目录
    _add_engine_root(engine_root)
    # 3) 由编辑器可执行文件上溯出的引擎根目录
    _add_engine_root(_engine_root_from_editor_exe(editor_exe))
    # 4) 环境变量兜底（同样接受文件或目录）
    _add_file(os.getenv(ENV_REMOTE_EXEC) or None)

    seen: set[Path] = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        if c.is_file():
            return c

    raise BackendUnavailable(
        "找不到引擎自带的 remote_execution.py。请在本机 config/agent.config.json 中配置以下任一项："
        f"ue.remote_exec_python_path（指向 <UE_ROOT>/{PLUGIN_PYTHON_REL}/{_REMOTE_EXEC_FILE}）、"
        f"ue.engine_root（<UE_ROOT>）、ue.editor_exe，或设置环境变量 {ENV_REMOTE_EXEC}。"
        "UHA 不会猜测本机路径；未配置时该通道按不可用处理。"
    )


def load_remote_execution(path: Path):
    """以独立模块名加载官方 remote_execution.py（不污染 sys.modules 里的同名模块）。"""
    mod_name = "uha_ue_remote_execution"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover
        raise BackendUnavailable(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class UEPythonBackend(UnrealBackend):
    """通过官方 Remote Execution 在运行中的编辑器里执行 UE Python。"""

    name = "UE_PYTHON"
    supported_ops = (
        "get_actors",
        "find_actor",
        "get_actor_transform",
        "set_actor_location",
        "set_actor_rotation",
        "set_actor_scale",
        "save_level",
        "execute_ue_python",
        "level_dirty_state",
        "current_level",
    )

    def __init__(
        self,
        *,
        engine_root: str | Path | None = None,
        remote_exec_python_path: str | None = None,
        editor_exe: str | Path | None = None,
        multicast_group: str = "239.0.0.1",
        multicast_port: int = 6766,
        multicast_ttl: int = 0,
        bind_address: str = "127.0.0.1",
        discovery_timeout_s: float = 3.0,
        command_timeout_s: float = 30.0,
        project_file: str | None = None,
        timeout: float = 60.0,
        dry_run: bool = False,
    ):
        super().__init__(timeout=timeout, dry_run=dry_run)
        self.engine_root = Path(engine_root) if engine_root else None
        self.remote_exec_path = find_remote_execution_module(
            self.engine_root, remote_exec_python_path, editor_exe
        )
        self.project_file = project_file
        self._mod = load_remote_execution(self.remote_exec_path)
        self._discovery_timeout_s = float(discovery_timeout_s)
        self._command_timeout_s = float(command_timeout_s)

        cfg = self._mod.RemoteExecutionConfig()
        cfg.multicast_group_endpoint = (multicast_group, int(multicast_port))
        cfg.multicast_bind_address = bind_address
        cfg.multicast_ttl = int(multicast_ttl)
        self._cfg = cfg

        self._session = None
        self._session_lock = threading.Lock()
        self._node_id: str | None = None
        self._connected = False

        self._avail_cache: tuple[float, bool] | None = None
        self._avail_ttl = 5.0

    # -- 会话 -----------------------------------------------------------------

    def _ensure_session(self) -> None:
        if self._session is not None:
            return
        with self._session_lock:
            if self._session is not None:
                return
            self._session = self._mod.RemoteExecution(self._cfg)
            self._session.start()

    def _discover_node(self) -> str | None:
        """在 discovery_timeout_s 内找一台活着的编辑器。"""
        self._ensure_session()
        deadline = time.monotonic() + self._discovery_timeout_s
        while time.monotonic() < deadline:
            nodes = self._session.remote_nodes or []
            if nodes:
                # 优先匹配配置里指定的工程，避免连错编辑器
                if self.project_file:
                    want = Path(self.project_file).stem.lower()
                    for n in nodes:
                        blob = json.dumps(n, default=str).lower()
                        if want and want in blob:
                            return str(n.get("node_id"))
                return str(nodes[0].get("node_id"))
            time.sleep(0.15)
        return None

    def _connect(self) -> bool:
        if self._connected and self._session and self._session.has_command_connection():
            return True
        node_id = self._discover_node()
        if not node_id:
            self._connected = False
            return False
        self._session.open_command_connection(node_id)
        self._node_id = node_id
        self._connected = bool(self._session.has_command_connection())
        return self._connected

    # -- 可用性 ---------------------------------------------------------------

    def available(self) -> bool:
        now = time.monotonic()
        if self._avail_cache and (now - self._avail_cache[0]) < self._avail_ttl:
            return self._avail_cache[1]
        ok = False
        try:
            ok = self._connect()
        except Exception:
            ok = False
        self._avail_cache = (now, ok)
        return ok

    def diagnostics(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        try:
            self._ensure_session()
            nodes = self._session.remote_nodes or []
        except Exception:
            pass
        return {
            "backend": self.name,
            "available": self.available(),
            "remote_exec_module": str(self.remote_exec_path),
            "discovered_nodes": len(nodes),
            "connected": self._connected,
            "capabilities": sorted(self.capabilities()),
        }

    # -- 执行 -----------------------------------------------------------------

    def run_script(self, script: str) -> dict[str, Any]:
        """发一段 UE Python 脚本过去，拿回结构化结果。"""
        if self.dry_run:
            return {"ok": True, "dry_run": True, "script_chars": len(script)}

        if not self._connect():
            raise BackendUnavailable(
                "没有发现运行中的 Unreal Editor（Remote Execution 组播无响应）。"
                "请先启动编辑器并启用 Python Editor Script Plugin。"
            )

        tmp = Path(tempfile.gettempdir()) / f"uha_{uuid.uuid4().hex}.json"
        payload = f"{ue_scripts.OUT_PATH_VAR} = {str(tmp)!r}\n" + script

        try:
            data = self._session.run_command(
                payload, unattended=True, exec_mode=self._mod.MODE_EXEC_FILE, raise_on_failure=False
            )
        except Exception as exc:
            self._connected = False
            raise TransportError(f"Remote Execution 调用失败: {exc}") from exc

        result: dict[str, Any] | None = None
        if tmp.is_file():
            try:
                result = json.loads(tmp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                result = None
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass

        if result is None:
            result = ue_scripts.parse_result(str(data.get("result") or "") + "\n" + "\n".join(data.get("output") or []))

        if result is None:
            raise ToolCallFailed(
                "UE 脚本没有返回结构化结果",
                details={"success": data.get("success"), "output_tail": (data.get("output") or [])[-6:]},
            )
        result["_transport"] = "ue_remote_exec"
        return result

    def _call(self, op: str, **kwargs: Any) -> dict[str, Any]:
        self.require(op)
        if self.dry_run:
            return {"ok": True, "dry_run": True, "op": op, "args": kwargs}
        script = _build(op, **kwargs)
        result = self.run_script(script)
        if not result.get("ok"):
            code = result.get("code")
            if code == "ACTOR_NOT_FOUND":
                from ...core.errors import ActorNotFound

                raise ActorNotFound(str(result.get("error")), details=result)
            raise ToolCallFailed(str(result.get("error") or f"{op} 失败"), code=code or "UE_SCRIPT_FAILED", details=result)
        return result

    # -- op 实现 --------------------------------------------------------------

    def get_actors(self, *, name_like: str | None = None, class_like: str | None = None, limit: int = 500) -> list[ActorRef]:
        res = self._call("get_actors", name_like=name_like, class_like=class_like, limit=limit)
        return [ActorRef.from_payload(a) for a in res.get("actors") or []]

    def find_actor(self, name: str) -> ActorRef | None:
        res = self._call("find_actor", name=name)
        actor = res.get("actor")
        return ActorRef.from_payload(actor) if actor else None

    def get_actor_transform(self, name: str) -> ActorRef:
        res = self._call("get_actor_transform", name=name)
        return ActorRef.from_payload(res["actor"])

    def set_actor_location(self, name: str, location: Iterable[float]) -> dict[str, Any]:
        return self._call("set_actor_location", name=name, location=self._as_xyz(location))

    def set_actor_rotation(self, name: str, rotation: Iterable[float]) -> dict[str, Any]:
        return self._call("set_actor_rotation", name=name, location=self._as_xyz(rotation))

    def set_actor_scale(self, name: str, scale: Iterable[float]) -> dict[str, Any]:
        return self._call("set_actor_scale", name=name, location=self._as_xyz(scale))

    def save_level(self) -> dict[str, Any]:
        return self._call("save_level")

    def level_dirty_state(self) -> dict[str, Any]:
        return self._call("level_dirty_state")

    def current_level(self) -> str:
        res = self._call("current_level")
        return str(res.get("level") or "")

    def execute_ue_python(self, code: str) -> dict[str, Any]:
        from .script_policy import validate_editor_script
        validate_editor_script(code)
        return self._call("execute_ue_python", code=code)

    # -- 收尾 -----------------------------------------------------------------

    def close(self) -> None:
        if self._session:
            try:
                self._session.stop()
            except Exception:
                pass
            self._session = None
        self._connected = False


def _build(op: str, **kwargs: Any) -> str:
    """把 op 名字分派到 ue_scripts 里的脚本生成器。"""
    if op == "get_actors":
        return ue_scripts.script_get_actors(
            name_like=kwargs.get("name_like"), class_like=kwargs.get("class_like"), limit=int(kwargs.get("limit") or 500)
        )
    if op == "find_actor":
        return ue_scripts.script_find_actor(str(kwargs["name"]))
    if op == "get_actor_transform":
        return ue_scripts.script_get_actor_transform(str(kwargs["name"]))
    if op in {"set_actor_location", "set_actor_rotation", "set_actor_scale"}:
        builder = {
            "set_actor_location": ue_scripts.script_set_actor_location,
            "set_actor_rotation": ue_scripts.script_set_actor_rotation,
            "set_actor_scale": ue_scripts.script_set_actor_scale,
        }[op]
        return ue_scripts.with_args(builder(str(kwargs["name"])), kwargs.get("location"))
    if op == "save_level":
        return ue_scripts.script_save_level()
    if op == "level_dirty_state":
        return ue_scripts.script_level_dirty_state()
    if op == "current_level":
        return ue_scripts.script_current_level()
    if op == "execute_ue_python":
        return ue_scripts.script_execute_python(str(kwargs["code"]))
    raise ValueError(f"未知 op: {op}")


__all__ = ["UEPythonBackend", "find_remote_execution_module", "load_remote_execution"]
