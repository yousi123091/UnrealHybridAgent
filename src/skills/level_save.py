"""Skill: 保存当前 Level。

这是唯一一个**默认走 GUI** 的动作，理由写在 Router 的 ``rule_save_shortcut`` 里：
Ctrl+S 是 UE 既定交互，1 步完成，而且它顺带覆盖"用户自己手动改过、还没保存"
的部分——纯 API 保存只保存 API 自己动过的那部分脏包语义。

验证方式（按证据强度从强到弱）：

1. **磁盘文件 mtime 推进**（最强）——保存真的落盘了，.umap 的修改时间就会变。
   这是进程外的文件系统事实，不依赖编辑器的自我报告。
2. 保存前后脏包列表 -> 0。注意：实测本机 `get_dirty_map_packages()` **不反映**
   Python 侧改动，"0 → 0"这种空证据会被判为 skipped 而不是通过。
3. "保存 API 返回 success"（最弱，只说明调用没抛异常）。
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.errors import NotSupported
from ..router.intent import TaskIntent
from ..validation.result import CheckResult
from ..validation.verifiers import (
    Verifier,
    check_dirty_empty,
    check_file_written,
    check_save_result,
    check_visual_changed,
)
from ..vision.image import diff, load_rgb
from . import gui_helpers
from .base import Skill, SkillContext, SkillTrace


class LevelSaveSkill(Skill):
    """保存当前打开的 Level。"""

    name = "level_save"
    description = "保存当前 Level（Ctrl+S 或保存 API）"
    keywords = ("保存", "save")
    preferred_methods = ("UNREAL_MCP", "UE_PYTHON", "KEYBOARD")
    supported_methods = frozenset({"UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET", "KEYBOARD", "HYBRID"})

    def is_idempotent(self, params: Mapping[str, Any]) -> bool:
        # 保存两次 = 保存一次（实测 UNREAL_MCP 路径重复调用无害）。
        return True

    def task_category(self, params: Mapping[str, Any] | None = None) -> str:
        return "level_save"

    def intent(self, params: Mapping[str, Any], **overrides: Any) -> TaskIntent:
        kw: dict[str, Any] = {
            "skill": self.name,
            "description": "保存当前 Level",
            "target_count": 1,
            # 结构化保存可验证；不再默认把 Ctrl+S 当首选捷径
            "needs_exact_values": False,
            "known_shortcut": "",
            "read_only": False,
        }
        prefer_api = params.get("prefer_api")
        if prefer_api is None:
            prefer_api = True  # Phase 4A default: structured save first
        if prefer_api:
            kw["context"] = {"prefer_api_save": True}
        kw.update(overrides)
        return TaskIntent(**kw)

    # -- 基线 ----------------------------------------------------------------

    def preflight(self, ctx: SkillContext, params: Mapping[str, Any]) -> dict[str, Any]:
        before: dict[str, Any] = {}
        for method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
            backend = ctx.structured_or_none(method)
            if backend is None:
                continue
            try:
                before = {"method": method, "dirty": backend.level_dirty_state()}
            except Exception:
                continue
            # 关键证据基线：Level 文件的 mtime
            try:
                before["file"] = backend.level_file_stat()
            except Exception:  # noqa: BLE001 - 拿不到就靠别的证据，验证器会报 skipped
                pass
            break
        if params.get("capture_screenshot", True):
            try:
                _, path = gui_helpers.desktop_screenshot(ctx, "save_before.png")
                before["screenshot"] = str(path)
            except Exception:
                pass
        return before

    # -- 执行 ----------------------------------------------------------------

    def perform(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> dict[str, Any]:
        if method == "KEYBOARD":
            res = gui_helpers.ctrl_s_save(ctx)
            trace.result["ctrl_s"] = res
            trace.note("Ctrl+S 已发送")
        elif method in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
            backend = ctx.structured(method)
            res = backend.save_level()
            trace.result["save_level"] = {k: v for k, v in res.items() if k != "images"}
            trace.note(f"{method} 保存 API 返回：{res.get('success', res.get('ok'))}")
        elif method == "HYBRID":
            backend = ctx.structured("HYBRID")
            res = backend.save_level()
            trace.result["save_level"] = {k: v for k, v in res.items() if k != "images"}
            trace.note("HYBRID：先走保存 API")
        elif method == "MOUSE":
            # 菜单 File > Save All 之类的退路很脆；只在真有需要时再补
            raise NotSupported("level_save 的 MOUSE 路径未实现（请用 KEYBOARD 的 Ctrl+S，或结构化保存 API）")
        elif method == "VISION":
            raise NotSupported("保存是写操作，VISION 不适用于 level_save")
        else:
            raise NotSupported(f"level_save 不支持 {method}")

        if params.get("capture_screenshot", True):
            try:
                _, path = gui_helpers.desktop_screenshot(ctx, "save_after.png")
                trace.after["screenshot"] = str(path)
            except Exception:
                pass

        return {"ok": True, "transport": method, "result": _slim(trace.result)}

    # -- 验证 ----------------------------------------------------------------

    def verify(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> Any:
        v = Verifier()

        # 1) 动作本身是否被报成功（弱证据，但要有）
        v.add(check_save_result(trace.result.get("save_level") or trace.result.get("ctrl_s")))

        backend = None
        for m in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET"):
            backend = ctx.structured_or_none(m)
            if backend is not None:
                break

        # 2) **最强证据**：磁盘上的 .umap 是否真的被写过
        before_file = trace.before.get("file")
        if backend is None:
            v.add(CheckResult(
                "save.file_written", passed=False, skipped=True,
                reason="没有结构化通道，取不到 Level 文件快照",
            ))
        else:
            v.read("file_after", backend.level_file_stat, optional=True)
            v.check(lambda vals: check_file_written(before_file, vals.get("file_after")))

        # 3) 辅助证据：脏包列表。本机该查询不反映 Python 侧改动，
        #    空证据会被 check_dirty_empty 判成 skipped（绝不假绿）。
        before_dirty = (trace.before.get("dirty") or {}).get("dirty_count")
        if backend is None:
            v.add(CheckResult(
                "save.dirty_cleared", passed=False, skipped=True,
                reason="没有结构化通道可查脏包",
            ))
        else:
            v.read("dirty", backend.level_dirty_state, optional=True)
            v.check(lambda vals: check_dirty_empty(vals.get("dirty"), before_count=before_dirty))

        # 4) 截图前后对比：只作为**旁证**。整屏画面是否变化对"保存"不具鉴别力
        #    （保存一个 Level 可能完全不改画面），所以 advisory=True：
        #    变化不足时判 skipped 而不是 failed，避免用弱证据否定硬证据。
        b, a = trace.before.get("screenshot"), trace.after.get("screenshot")
        if b and a:
            try:
                d = diff(load_rgb(b), load_rgb(a), downscale_to_width=320, changed_pixel_threshold=12)
                v.add(check_visual_changed(
                    d, min_ratio=float(params.get("min_visual_change", 0.0002)),
                    name="visual.screen_changed", advisory=True,
                ))
            except Exception as exc:  # noqa: BLE001
                v.add(CheckResult("visual.screen_changed", passed=False, skipped=True,
                                  reason=f"图像比较失败：{type(exc).__name__}: {exc}"))
        else:
            v.add(CheckResult(
                "visual.screen_changed", passed=False, skipped=True,
                reason="没有采到保存前后的截图对比",
            ))

        return v.run()


def _slim(res: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (v if not isinstance(v, (list, dict)) or len(str(v)) < 300 else str(v)[:300])
            for k, v in res.items() if k != "images"}


__all__ = ["LevelSaveSkill"]
