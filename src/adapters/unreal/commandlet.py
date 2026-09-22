"""离线后端：UnrealEditor-Cmd.exe -run=pythonscript。

定位：**没有编辑器可开时的兜底通道**（CI、批处理、无人值守）。

三个必须说清的边界：

1. 它**不能**操作已经打开的编辑器会话 —— UE 的包锁会拒绝，强行跑还可能让
   编辑器保存失败。所以本后端的 `available()` 会在检测到 UnrealEditor
   进程时主动返回 False，避免 Router 选到它。
2. commandlet 上下文里 Editor 子系统常常不存在（`EditorActorSubsystem`
   可能拿不到）。因此脚本里对 `unreal.EditorActorSubsystem()` 的调用都做了
   空值保护；真拿不到时返回结构化错误，而不是崩掉进程。
3. 它**很慢**（要起一个完整引擎进程、加载工程）。所以它的 cost 权重最高，
   Router 只在其他通道都不可用时才会选它。
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from ...core.errors import BackendUnavailable, ToolCallFailed
from ..base import ActorRef, UnrealBackend

DEFAULT_EXTRA_ARGS = ["-unattended", "-nopause", "-nosplash", "-NoSound", "-NullRHI"]


def _editor_running() -> bool:
    """检测 UnrealEditor 是否在跑（用 tasklist，避免依赖 psutil）。"""
    try:
        out = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", "IMAGENAME eq UnrealEditor.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "UnrealEditor.exe" in (out.stdout or "")
    except Exception:
        return False


class UECommandletBackend(UnrealBackend):
    """离线 UE Python 执行。"""

    name = "UE_COMMANDLET"
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
        editor_cmd_exe: str = "",
        project_file: str = "",
        timeout_s: float = 900.0,
        extra_args: Iterable[str] | None = None,
        timeout: float = 900.0,
        dry_run: bool = False,
    ):
        super().__init__(timeout=timeout, dry_run=dry_run)
        self.editor_cmd_exe = str(editor_cmd_exe)
        self.project_file = str(project_file)
        self.timeout_s = float(timeout_s)
        self.extra_args = list(extra_args if extra_args is not None else DEFAULT_EXTRA_ARGS)
        self._last_run: dict[str, Any] = {}

    # -- 可用性 ---------------------------------------------------------------

    def available(self) -> bool:
        if self.dry_run:
            return True
        if not self.editor_cmd_exe or not Path(self.editor_cmd_exe).is_file():
            return False
        if not self.project_file or not Path(self.project_file).is_file():
            return False
        # 关键保护：编辑器开着时绝不用它
        if _editor_running():
            return False
        return True

    def diagnostics(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "available": self.available(),
            "editor_cmd_exe": self.editor_cmd_exe,
            "project_file": self.project_file,
            "reason_unavailable": (
                "编辑器正在运行（commandlet 不能碰已打开的会话）" if _editor_running() else None
            ),
            "last_run": self._last_run,
            "capabilities": sorted(self.capabilities()),
        }

    # -- 执行 -----------------------------------------------------------------

    def run_script(self, script: str) -> dict[str, Any]:
        if not self.available():
            raise BackendUnavailable(
                "UE commandlet 后端不可用（缺 UnrealEditor-Cmd.exe / 工程文件，或编辑器正在运行）"
            )

        from . import ue_scripts

        tmp_json = Path(tempfile.gettempdir()) / f"uha_cmdlet_{uuid.uuid4().hex}.json"
        tmp_script = Path(tempfile.gettempdir()) / f"uha_cmdlet_{uuid.uuid4().hex}.py"
        payload = f"{ue_scripts.OUT_PATH_VAR} = {str(tmp_json)!r}\n" + script
        tmp_script.write_text(payload, encoding="utf-8")

        cmd = [
            self.editor_cmd_exe,
            self.project_file,
            "-run=pythonscript",
            f"-script={tmp_script}",
            *self.extra_args,
        ]
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolCallFailed(f"commandlet 超时({self.timeout_s}s)") from exc
        finally:
            try:
                tmp_script.unlink()
            except OSError:
                pass

        elapsed = round(time.perf_counter() - t0, 2)
        self._last_run = {"elapsed_s": elapsed, "returncode": proc.returncode}

        result: dict[str, Any] | None = None
        if tmp_json.is_file():
            try:
                result = json.loads(tmp_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                result = None
            finally:
                try:
                    tmp_json.unlink()
                except OSError:
                    pass
        if result is None:
            result = ue_scripts.parse_result((proc.stdout or "") + "\n" + (proc.stderr or ""))
        if result is None:
            raise ToolCallFailed(
                "commandlet 没有产出结构化结果（commandlet 上下文中 Editor 子系统可能不可用）",
                details={"returncode": proc.returncode, "tail": (proc.stdout or "")[-800:]},
            )
        result["_transport"] = "ue_commandlet"
        result["_elapsed_s"] = elapsed
        return result

    def _call(self, op: str, **kwargs: Any) -> dict[str, Any]:
        self.require(op)
        if self.dry_run:
            return {"ok": True, "dry_run": True, "op": op, "args": kwargs}
        from .ue_python import _build

        result = self.run_script(_build(op, **kwargs))
        if not result.get("ok"):
            code = result.get("code")
            if code == "ACTOR_NOT_FOUND":
                from ...core.errors import ActorNotFound

                raise ActorNotFound(str(result.get("error")), details=result)
            raise ToolCallFailed(str(result.get("error") or f"{op} 失败"), code=code or "UE_SCRIPT_FAILED", details=result)
        return result

    # -- op ------------------------------------------------------------------

    def get_actors(self, *, name_like: str | None = None, class_like: str | None = None, limit: int = 500) -> list[ActorRef]:
        res = self._call("get_actors", name_like=name_like, class_like=class_like, limit=limit)
        return [ActorRef.from_payload(a) for a in res.get("actors") or []]

    def find_actor(self, name: str) -> ActorRef | None:
        res = self._call("find_actor", name=name)
        return ActorRef.from_payload(res["actor"]) if res.get("actor") else None

    def get_actor_transform(self, name: str) -> ActorRef:
        return ActorRef.from_payload(self._call("get_actor_transform", name=name)["actor"])

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
        return str(self._call("current_level").get("level") or "")

    def execute_ue_python(self, code: str) -> dict[str, Any]:
        return self._call("execute_ue_python", code=code)


__all__ = ["UECommandletBackend", "_editor_running"]
