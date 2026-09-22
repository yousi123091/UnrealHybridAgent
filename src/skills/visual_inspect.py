"""Skill: 视觉巡检。

"巡检"要做两件事，而且必须**分开归因**：

    1. **拍证据**：出一张（或一对）可归档的图 —— 这是视觉通道的不可替代之处。
    2. **下结论**：给出"有没有异常、异常是什么"。

第 2 件事在本项目里**优先用结构化数据算**（见 ``vision.judge``）：
"Actor 的包围盒底边高于地面中位数 20cm 以上"是确定性结论，
比"模型觉得它好像浮着"更适合驱动自动修复。VLM 只作为可选增强，
没配 VLM 时不会伪装成有结论。

三种执行方式的产出差异（如实记录，不做假）：

    UNREAL_MCP   UE 内部视口截图（干净，无桌面窗口干扰）+ 结构化判断
    VISION       桌面截图（能看到编辑器 UI，适合排查"面板状态"）+ 结构化判断（若有通道）
    HYBRID       两者都要，且结构化判断必定存在
"""

from __future__ import annotations

from typing import Any, Mapping

from ..core.errors import NotSupported, PreconditionFailed
from ..router.intent import TaskIntent
from ..validation.result import CheckResult
from ..validation.verifiers import Verifier
from ..vision.grounding import VLMGrounder
from ..vision.judge import (
    DEFAULT_GROUND_PERCENTILE,
    DEFAULT_MAX_FLAGGED_RATIO,
    Observation,
    find_reference,
    judge_against_ground,
    judge_by_center,
    judge_floating,
    rects_overlap,
    summarise,
    usable_observations,
    within_region,
)
from . import gui_helpers
from .base import Skill, SkillContext, SkillTrace


def _actor_row(actor: Any) -> dict[str, Any]:
    """把 ActorRef 摊平成字典，**保留 extra 里的包围盒**。

    包围盒是浮空判断的关键输入（底边离地高度），`as_dict()` 默认不带它，
    丢了就会退化到"中心点近似"。这里显式要回来。
    """
    if hasattr(actor, "as_dict"):
        try:
            return actor.as_dict(include_extra=True)
        except TypeError:  # 兼容不支持该参数的旧实现
            return actor.as_dict()
    return dict(actor)


class VisualInspectSkill(Skill):
    """对当前 Level 做一次视觉巡检，并给出可执行的结论。"""

    name = "visual_inspect"
    description = "截图并判断场景是否有异常（如 Actor 浮空）"
    keywords = ("截图", "看看", "巡检", "悬空", "浮空", "漂浮", "视觉", "画面", "观感", "检查一下")
    preferred_methods = ("HYBRID", "VISION")
    supported_methods = frozenset({"UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLET", "VISION", "HYBRID"})

    def is_idempotent(self, params: Mapping[str, Any]) -> bool:
        return True  # 只读（截图 + 几何判定）

    def task_category(self, params: Mapping[str, Any] | None = None) -> str:
        return "visual_inspection"

    def intent(self, params: Mapping[str, Any], **overrides: Any) -> TaskIntent:
        kw: dict[str, Any] = {
            "skill": self.name,
            "description": str(params.get("description") or "视觉巡检：判断场景是否有异常"),
            # 巡检本身只读；但如果调用方要求"顺带修"，由上层串成 actor_move 步骤
            "needs_vision": True,
            "needs_exact_values": False,
            "read_only": True,
            "target_count": int(params.get("limit") or 1),
        }
        kw.update(overrides)
        return TaskIntent(**kw)

    # -- 执行 ----------------------------------------------------------------

    def perform(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> dict[str, Any]:
        artifacts: dict[str, str] = {}
        evidence: dict[str, Any] = {}

        in_editor = method in ("UNREAL_MCP", "HYBRID")
        desktop = method in ("VISION", "HYBRID")

        # --- UE 内部视口截图 -------------------------------------------------
        if in_editor:
            backend = ctx.structured_or_none("UNREAL_MCP") or ctx.structured_or_none("UE_PYTHON")
            if backend is None:
                if method == "UNREAL_MCP":
                    raise NotSupported("UE 内部截图需要一个可用的结构化后端")
                trace.note("没有结构化后端，跳过 UE 内部截图")
            else:
                try:
                    res = backend.viewport_screenshot(
                        width=int(params.get("width", 1280)), height=int(params.get("height", 720))
                    )
                    images = res.get("images") or []
                    if images:
                        path = ctx.save_screenshot(images[0]["data"], "inspect_viewport.png")
                        artifacts["ue_viewport"] = str(path)
                except Exception as exc:  # noqa: BLE001
                    trace.note(f"UE 内部截图失败：{type(exc).__name__}: {exc}")
                    if method == "UNREAL_MCP":
                        raise

        # --- 桌面截图 --------------------------------------------------------
        if desktop:
            shot, path = gui_helpers.desktop_screenshot(ctx, "inspect_desktop.png")
            artifacts["desktop"] = str(path)
            evidence["desktop_size"] = [shot.width, shot.height]

        # --- 结构化结论（语义优先） ----------------------------------------------
        if params.get("check_floating", True):
            # Phase 4A: always emit semantic-first scene inspection
            try:
                from ..vision.semantic_inspect import inspect_scene_semantic

                backend = ctx.structured_or_none("UNREAL_MCP") or ctx.structured_or_none("UE_PYTHON")
                if backend is not None:
                    rows = [_actor_row(r) for r in backend.get_actors(limit=int(params.get("limit") or 400))]
                    sem_report = inspect_scene_semantic(backend, rows, sample_limit=int(params.get("semantic_sample") or 24))
                    evidence["semantic_inspect"] = sem_report
                    trace.after["semantic_inspect"] = sem_report
            except Exception as exc:  # noqa: BLE001
                trace.note(f"semantic inspect failed: {type(exc).__name__}: {exc}")

            verdict = self._floating_verdict(ctx, params, trace)
            if verdict is not None:
                evidence["floating"] = verdict
                evidence["semantic_support"] = verdict.get("semantic") or trace.after.get("semantic_support")
                # Phase 4A default: geometry-only full-scene flagging is NOT actionable
                # when semantic modes show many STRUCTURE/ATTACHED/HANGING actors.
                sem = verdict.get("semantic") or {}
                modes = sem.get("mode_counts") or {}
                structureish = sum(modes.get(k, 0) for k in ("STRUCTURE", "ATTACHED", "HANGING"))
                if verdict.get("basis") == "explicit_ground" and structureish >= 3:
                    verdict["legacy_geometry_warning"] = (
                        "统一地面几何会高估浮空：STRUCTURE/ATTACHED/HANGING 不应按 ground_gap 处理"
                    )
                    if not verdict.get("actionable"):
                        verdict["warning"] = (
                            (verdict.get("warning") or "") + " " + verdict["legacy_geometry_warning"]
                        ).strip()
                    evidence["floating"] = verdict

        # --- 可选：VLM 通道是否可用（只报告，不假装有结论） -------------------
        grounder = VLMGrounder(ctx.bundle.computer_use)
        evidence["vlm_configured"] = bool(grounder.configured())
        if "semantic_support" not in evidence:
            evidence["semantic_support"] = trace.after.get("semantic_support")

        if not artifacts:
            raise NotSupported(f"{method} 没能产出任何截图证据")

        trace.after["artifacts"] = artifacts
        trace.after["evidence"] = evidence
        return {"ok": True, "transport": method, "artifacts": artifacts, "evidence": evidence}

    # -- 验证 ----------------------------------------------------------------

    def verify(self, ctx: SkillContext, method: str, params: Mapping[str, Any], trace: SkillTrace) -> Any:
        v = Verifier()
        arts = trace.after.get("artifacts") or {}
        ev = trace.after.get("evidence") or {}

        v.add(CheckResult(
            "inspect.evidence_captured",
            passed=bool(arts),
            expected="至少一张截图",
            actual=sorted(arts.keys()),
        ))

        floating = ev.get("floating")
        if floating is None:
            v.add(CheckResult(
                "inspect.verdict", passed=False, skipped=True,
                reason="没有结构化通道，无法给出可复现的场景结论（只有图，没有判断）",
            ))
        else:
            actionable = floating.get("actionable", floating.get("reliable", True))
            ref = floating.get("ground_reference") or {}
            names = ", ".join(c["label"] for c in floating.get("candidates", [])[:3])
            ground_desc = (
                f"地面={ref.get('top_z')}（参照物 {ref.get('label')}）"
                if ref else
                f"地面 Z≈{floating.get('ground_z')}（统计推断，第 {floating.get('ground_percentile')} 分位）"
            )
            detail = (
                f"{ground_desc}；"
                f"疑似浮空 {floating.get('floating_count')}/{floating.get('usable_actors')} 个"
                + (f"（{names}）" if names else "")
            )
            if not actionable:
                detail += "；结论**不可执行**，不会据此自动修改场景"
            v.add(CheckResult(
                "inspect.verdict", passed=bool(actionable),
                skipped=not actionable,
                expected="给出可执行的浮空判定",
                actual={
                    "floating_count": floating.get("floating_count"),
                    "flagged_ratio": floating.get("flagged_ratio"),
                    "basis": floating.get("basis"),
                    "reliable": floating.get("reliable"),
                    "actionable": actionable,
                },
                detail=detail,
                reason=floating.get("warning") if not actionable else "",
            ))

        v.add(CheckResult(
            "inspect.vlm_channel",
            passed=True,
            actual="已配置" if ev.get("vlm_configured") else "未配置",
            detail="VLM 只作为可选增强；未配置不影响确定性判断",
            skipped=not ev.get("vlm_configured"),
            reason="Agent-TARS vlmConfigured=false，语义视觉定位不可用",
        ))

        # 修完之后的复验：这一趟的目的是"确认异常已经没了"，
        # 所以除了"判定可执行"，还要单独检查浮空数归零。
        if params.get("expect_no_floating") and floating is not None:
            n = floating.get("floating_count")
            v.add(CheckResult(
                "inspect.no_floating_remaining",
                passed=(n == 0),
                expected=0,
                actual=n,
                detail=(
                    "巡检范围内已无浮空构件"
                    if n == 0 else
                    "巡检范围内仍有浮空构件："
                    + ", ".join(c["label"] for c in floating.get("candidates", [])[:3])
                ),
            ))

        return v.run()

    # -- 内部 ----------------------------------------------------------------

    @staticmethod
    def _semantic_pass(rows, verdict, ctx, params) -> dict[str, Any] | None:
        if verdict is None:
            return None
        return verdict.get("semantic") or None

    @staticmethod
    def _floating_verdict(
        ctx: SkillContext, params: Mapping[str, Any], trace: SkillTrace
    ) -> dict[str, Any] | None:
        """用结构化数据算"有没有东西浮空"。拿不到数据就返回 None（不猜）。

        两种模式，**优先受控模式**：

        ``ground_ref="<某块地板/广场的 label>"``
            受控：地面高度取自该参照物的**顶面**，可选再用 ``region``
            限定巡检范围。这是推荐用法——多层建筑里唯一站得住的做法。

        不给 ``ground_ref``
            退化为"统计地面"（低分位）。这条路**已知不可靠**
            （实测多层场景误报 48%~95%），所以结论一律挂 ``unguided`` 警告，
            读的人能立刻看出这不是能自动改场景的依据。
        """
        backend = None
        for m in ("UNREAL_MCP", "UE_PYTHON", "UE_COMMANDLE", "UE_COMMANDLET"):
            backend = ctx.structured_or_none(m)
            if backend is not None:
                break
        if backend is None:
            return None

        try:
            rows = [_actor_row(r) for r in backend.get_actors(limit=int(params.get("limit") or 2000))]
        except Exception as exc:  # noqa: BLE001
            trace.note(f"读取 Actor 列表失败：{type(exc).__name__}: {exc}")
            return None

        obs = [Observation.from_payload(r) for r in rows]
        tol = float(params.get("ground_tolerance", 5.0))
        pct = float(params.get("ground_percentile", DEFAULT_GROUND_PERCENTILE))
        max_ratio = float(params.get("max_flagged_ratio", DEFAULT_MAX_FLAGGED_RATIO))

        # --- 语义优先：先分类支撑模式，再决定谁做确定性几何检查 ---
        from ..vision.semantic_support import SemanticSupportResolver, SupportMode, select_for_geometric_check

        resolver = SemanticSupportResolver(overrides=(params.get("semantic") or {}))
        sem_results = resolver.resolve_many(rows)
        geo_ok, hanging_skip, needs_ai = select_for_geometric_check(sem_results)
        hanging_labels = {r.label for r in hanging_skip}
        # GROUND 模式才做「相对显式地面」的 gap 检查；
        # STRUCTURE/ATTACHED 仍可出现在 candidates 里，但 recommended_check 不同。
        ground_mode_labels = {
            r.label for r in sem_results
            if r.support_mode == SupportMode.GROUND and r.confidence >= 0.75
        }
        # 无语义信号时保持旧行为（全部参与），避免回归测试被语义误杀
        use_semantic_filter = bool(params.get("use_semantic", True)) and any(
            r.support_mode != SupportMode.UNKNOWN for r in sem_results
        )

        # --- 受控模式：显式地面参照物 +（可选）限定区域 ---
        ground_ref = str(params.get("ground_ref") or "").strip()
        if ground_ref:
            ref = find_reference(obs, ground_ref)
            if ref is None:
                raise PreconditionFailed(
                    f"找不到地面参照物 {ground_ref!r}（或它没有包围盒）——"
                    "不猜地面高度，宁可中止巡检",
                    details={"ground_ref": ground_ref, "scanned": len(obs)},
                )

            region = params.get("region")
            if region is None and params.get("region_pad") is not None:
                x0, x1, y0, y1 = ref.footprint
                pad = float(params["region_pad"])
                region = [x0 - pad, x1 + pad, y0 - pad, y1 + pad]
            if region is not None:
                region = [float(v) for v in region]

            scope = [o for o in obs if region is None or within_region(o, region)]
            cands = judge_against_ground(
                obs,
                ground_z=ref.top_z,
                ground_tolerance=tol,
                region=region,
                exclude_labels=(ref.label,),
            )
            if use_semantic_filter:
                # Phase 4A: only high-confidence GROUND participates in ground-gap floating.
                # STRUCTURE/ATTACHED/HANGING/UNKNOWN must not be auto-flagged as "floating
                # => place on world ground".
                filtered = []
                for c in cands:
                    label = getattr(c, "label", None)
                    if label is None and isinstance(c, Mapping):
                        label = c.get("label")
                    if label is None:
                        c_obs = getattr(c, "observation", None)
                        label = getattr(c_obs, "label", None)
                    if label in hanging_labels:
                        continue
                    if ground_mode_labels and label not in ground_mode_labels:
                        continue
                    filtered.append(c)
                cands = filtered
                out_sem_note = (
                    f"semantic filter: only GROUND({len(ground_mode_labels)}) in ground-gap; "
                    f"hanging_skipped={len(hanging_labels)}; needs_ai={len(needs_ai)}"
                )
            else:
                out_sem_note = "semantic filter off"
            usable_n = len(usable_observations(scope)) if region is not None else len(usable_observations(obs))

            scoped = region is not None
            coverage_ok = True if not scoped else rects_overlap(ref.footprint, region)

            out = summarise(
                cands,
                scanned=len(obs),
                usable=max(1, usable_n),
                basis="explicit_ground",
                max_flagged_ratio=max_ratio,
                ground_reference=ref.as_dict(),
                enforce_ratio=not scoped,
                coverage_ok=coverage_ok,
            )
            if region is not None:
                out["region"] = [round(float(v), 2) for v in region]
            out["scanned"] = len(obs)
            out["scope"] = "region" if region is not None else "whole_scene_with_explicit_ground"
            out["semantic"] = {
                "resolver": "SemanticSupportResolver",
                "filter": out_sem_note,
                "ground_mode_labels": sorted(ground_mode_labels)[:40],
                "hanging_skipped": sorted(hanging_labels)[:50],
                "needs_ai": [r.label for r in needs_ai][:50],
                "mode_counts": _mode_counts(sem_results),
                "principle": "semantic_first_geometry_second",
            }
            # 附在 trace 上，供 perform 的 evidence.semantic_support 使用
            trace.after["semantic_support"] = out["semantic"]
            return out

        # --- 退化模式：统计地面（不可靠，如实标注） ---
        has_bounds = any(o.bottom_z is not None for o in obs)
        if has_bounds:
            usable = usable_observations(obs)
            out = summarise(
                judge_floating(obs, ground_tolerance=tol, ground_percentile=pct),
                scanned=len(obs), usable=len(usable), basis="statistical_low_percentile",
                ground_percentile=pct, max_flagged_ratio=max_ratio, unguided=True,
            )
        else:
            out = summarise(
                judge_by_center(obs, ground_tolerance=tol),
                scanned=len(obs), usable=len(obs), basis="center_fallback",
                max_flagged_ratio=max_ratio, unguided=True,
            )
            out["warning"] = (out.get("warning", "") +
                              " 后端未提供包围盒，判断精度更低（按中心点近似）。").strip()
        out["scanned"] = len(obs)
        out["semantic"] = {
            "resolver": "SemanticSupportResolver",
            "mode_counts": _mode_counts(sem_results),
            "needs_ai": [r.label for r in needs_ai][:20],
        }
        trace.after["semantic_support"] = out["semantic"]
        return out


def _mode_counts(results: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        key = r.support_mode.value if hasattr(r.support_mode, "value") else str(r.support_mode)
        out[key] = out.get(key, 0) + 1
    return out


__all__ = ["VisualInspectSkill"]
