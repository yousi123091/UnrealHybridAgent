"""Router 选路质量统计：first-choice / fallback / regret。

方法健康度回答「这个通道干活行不行」；
本模块回答「Router 首选选得准不准」。

    Router 首选 UNREAL_MCP → 失败 → fallback UE_PYTHON 成功
    → UNREAL_MCP 记一次真实失败（MethodHealth，按 task_category）
    → 同时 Router 记一次 regret（首选不是最终成功路径）
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping

_EMPTY_METHOD = {
    "selected": 0,
    "first_success": 0,
    "first_fail_then_fallback_ok": 0,
    "ultimate_ok": 0,
    "ultimate_fail": 0,
    "attempts_when_selected": 0,
}
_EMPTY_OVERALL = {
    "first_choice_success": 0,
    "tasks": 0,
    "fallback_tasks": 0,
    "total_attempts": 0,
    "regrets": 0,
}


class RouterQualityTracker:
    """持久化 Router Recommendation Quality，按 skill_type × method。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {"skills": {}, "updated": None}
        self.load()

    def load(self) -> None:
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        self.data.setdefault("skills", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated"] = time.time()
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _skill(stats: dict[str, Any], skill_type: str) -> dict[str, Any]:
        st = stats.setdefault(skill_type, {"methods": {}, "overall": dict(_EMPTY_OVERALL)})
        st.setdefault("methods", {})
        st.setdefault("overall", dict(_EMPTY_OVERALL))
        for k, v in _EMPTY_OVERALL.items():
            st["overall"].setdefault(k, v)
        return st

    @staticmethod
    def _method(st: dict[str, Any], method: str) -> dict[str, Any]:
        b = st["methods"].setdefault(method, dict(_EMPTY_METHOD))
        for k, v in _EMPTY_METHOD.items():
            b.setdefault(k, v)
        return b

    def record_outcome(
        self,
        *,
        skill_type: str,
        selected_method: str,
        final_method: str | None,
        ok: bool,
        attempts: int,
        order: list[str] | None = None,
    ) -> None:
        skill_type = str(skill_type or "unknown")
        selected_method = str(selected_method or "")
        attempts = max(1, int(attempts or 1))
        with self._lock:
            st = self._skill(self.data["skills"], skill_type)
            overall = st["overall"]
            overall["tasks"] = int(overall["tasks"]) + 1
            overall["total_attempts"] = int(overall["total_attempts"]) + attempts
            if attempts > 1:
                overall["fallback_tasks"] = int(overall["fallback_tasks"]) + 1

            if selected_method:
                b = self._method(st, selected_method)
                b["selected"] = int(b["selected"]) + 1
                b["attempts_when_selected"] = int(b["attempts_when_selected"]) + attempts
                if ok and attempts == 1 and (final_method or selected_method) == selected_method:
                    b["first_success"] = int(b["first_success"]) + 1
                    overall["first_choice_success"] = int(overall["first_choice_success"]) + 1
                elif ok and attempts > 1:
                    b["first_fail_then_fallback_ok"] = int(b["first_fail_then_fallback_ok"]) + 1
                    overall["regrets"] = int(overall["regrets"]) + 1
                if ok:
                    b["ultimate_ok"] = int(b["ultimate_ok"]) + 1
                else:
                    b["ultimate_fail"] = int(b["ultimate_fail"]) + 1

            if final_method:
                fb = self._method(st, final_method)
                # 首选即最终成功时上面已计 ultimate_ok，避免双计
                if not (ok and final_method == selected_method):
                    if ok:
                        fb["ultimate_ok"] = int(fb["ultimate_ok"]) + 1
            self.save()

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for skill_type, st in (self.data.get("skills") or {}).items():
            overall = st.get("overall") or {}
            tasks = int(overall.get("tasks") or 0)
            fcs = int(overall.get("first_choice_success") or 0)
            fb = int(overall.get("fallback_tasks") or 0)
            ta = int(overall.get("total_attempts") or 0)
            regrets = int(overall.get("regrets") or 0)
            out[skill_type] = {
                "tasks": tasks,
                "first_choice_success_rate": round(fcs / tasks, 4) if tasks else None,
                "fallback_rate": round(fb / tasks, 4) if tasks else None,
                "average_attempts": round(ta / tasks, 4) if tasks else None,
                "method_regret": regrets,
                "methods": st.get("methods") or {},
            }
        return out

    def clear(self) -> None:
        self.data = {"skills": {}, "updated": None}
        self.save()


def skill_category(
    skill_name: str,
    *,
    read_only: bool = False,
    ui_only: bool = False,
    needs_vision: bool = False,
    is_batch: bool = False,
) -> str:
    """映射到健康度/质量统计用的任务类别。"""
    name = str(skill_name or "").lower()
    if is_batch or "batch" in name:
        return "batch_mutation"
    if name in ("visual_inspect",) or (needs_vision and read_only):
        return "visual_inspection"
    if ui_only:
        return "gui_interaction"
    if read_only or name in ("actor_find", "actor_inspect"):
        return "actor_read"
    if name in ("level_save",):
        return "level_save"
    if name in ("actor_move",) or "mutat" in name or "move" in name:
        return "actor_mutation"
    return "general"


__all__ = ["RouterQualityTracker", "skill_category"]
