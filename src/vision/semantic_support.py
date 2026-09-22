"""SemanticSupportResolver —— 先理解「该怎么被支撑」，再决定是否做悬空检查。

背景（docs/FINDINGS.md#f1）：纯几何在多层建筑里误报率 48%~95%。
屋顶、重檐、台阶、装饰件在 Z 分布上与「浮空」无法用统一地面切开。

本模块**不**用「装饰件就可以悬空」这种偷懒规则：
  * 吊灯（HANGING）才是语义上允许离地的；
  * 屋脊装饰应贴在屋脊上（STRUCTURE / ATTACHED）；
  * 叫 Decoration 但贴墙/贴屋顶的，仍要做支撑检查。

输出 support_mode + confidence + reason + evidence，供上层决定：
  * 高置信 GROUND     → 显式地面几何检查
  * 高置信 STRUCTURE  → 向下 line_trace / 父级支撑检查（不是「必须落地」）
  * 高置信 ATTACHED   → 检查 attachment / 邻接结构
  * 高置信 HANGING    → 跳过悬空告警（但仍可检查是否异常下垂）
  * UNKNOWN / 低置信  → 进入 Vision / AI 模糊路径
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class SupportMode(str, Enum):
    GROUND = "GROUND"
    STRUCTURE = "STRUCTURE"
    ATTACHED = "ATTACHED"
    HANGING = "HANGING"
    UNKNOWN = "UNKNOWN"


#: 高置信阈值：达到才走确定性检查；否则交给 Vision/AI
HIGH_CONFIDENCE = 0.75
LOW_CONFIDENCE = 0.45


@dataclass
class SemanticSupportResult:
    label: str
    support_mode: SupportMode
    confidence: float
    reason: str
    evidence: list[str] = field(default_factory=list)
    # 建议的确定性检查类型
    recommended_check: str = "vision"  # ground_gap | structure_trace | attach_check | none | vision

    @property
    def actionable(self) -> bool:
        return self.confidence >= HIGH_CONFIDENCE and self.support_mode != SupportMode.UNKNOWN

    @property
    def needs_ai(self) -> bool:
        return (
            self.support_mode == SupportMode.UNKNOWN
            or self.confidence < HIGH_CONFIDENCE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "support_mode": self.support_mode.value,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
            "evidence": list(self.evidence),
            "recommended_check": self.recommended_check,
            "actionable": self.actionable,
            "needs_ai": self.needs_ai,
        }


# --- 关键词表 -----------------------------------------------------------------
# 顺序很重要：更具体的模式优先匹配。词是「构件在结构中的角色」，不是用途标签。

_RE_HANGING = re.compile(
    r"(hanging|pendant|lantern|chandelier|吊灯|悬挂|吊饰|吊挂|吊件|灯笼)",
    re.I,
)
_RE_ATTACHED = re.compile(
    r"(wall_?(orn|decor|panel)|ornament|decor|trim|molding|coping|finial|"
    r"crown_?(orn|decor)|eave_?(orn|decor)|柱头|斗拱|墙饰|贴面|线脚|饰件|"
    r"roof_?(crown|orn|decor|finial)|吻兽|脊饰)",
    re.I,
)
_RE_STRUCTURE = re.compile(
    r"(roof|eave|beam|rafter|column|pillar|pier|wall|stair|ramp|plinth|"
    r"foundation|slab|deck|bridge|corridor|platform|dais|台基|台面|"
    r"屋顶|屋面|檐|梁|柱|墙|楼梯|台阶|连廊|廊|基座|地坪梁)",
    re.I,
)
_RE_GROUND = re.compile(
    r"(plaza|floor|ground|terrain|road|path|pavement|yard|court|"
    r"广场|地面|地坪|地板|铺装|道路|地形|广场板)",
    re.I,
)

#: Actor Class 倾向（弱信号，只加置信度，不单独定模式）
_CLASS_HINTS: list[tuple[re.Pattern[str], SupportMode]] = [
    (re.compile(r"Light|Lantern|PointLight|SpotLight", re.I), SupportMode.HANGING),
    (re.compile(r"StaticMesh|SkeletalMesh|StaticMeshActor", re.I), SupportMode.UNKNOWN),
    (re.compile(r"Decal|Trigger|Volume|PlayerStart|NavMesh|ExponentialHeight", re.I), SupportMode.UNKNOWN),
]


class SemanticSupportResolver:
    """基于 label / folder / tags / class / attachment 的规则解析器。

    刻意**不**引入外部 LLM：确定性问题用规则；只有 UNKNOWN 才上 AI。
    规则表可被 config 覆盖（profiles / project），避免写死某一套工程命名。
    """

    def __init__(self, *, overrides: Mapping[str, Any] | None = None):
        ov = dict(overrides or {})
        self.min_confidence = float(ov.get("min_confidence", LOW_CONFIDENCE))
        # 允许项目覆盖：label 正则 -> mode
        self._extra_rules: list[tuple[re.Pattern[str], SupportMode, float]] = []
        for item in ov.get("rules") or []:
            try:
                pat = re.compile(str(item["pattern"]), re.I)
                mode = SupportMode(str(item["mode"]))
                conf = float(item.get("confidence", 0.8))
            except Exception:
                continue
            self._extra_rules.append((pat, mode, conf))

    def resolve(self, actor: Mapping[str, Any] | Any) -> SemanticSupportResult:
        payload = _normalize(actor)
        label = str(payload.get("label") or payload.get("name") or "")
        class_name = str(payload.get("class_name") or payload.get("class") or "")
        folder = str(payload.get("folder") or payload.get("actor_folder") or "")
        mesh = str(payload.get("mesh") or payload.get("mesh_name") or payload.get("asset") or "")
        tags = _as_str_list(payload.get("tags"))
        parent = str(payload.get("parent") or payload.get("attach_parent") or "")
        attached = bool(payload.get("attached") or parent)

        evidence: list[str] = []
        haystack_parts = [label, folder, mesh, " ".join(tags), class_name, parent]
        haystack = " | ".join(p for p in haystack_parts if p)

        # 0) 项目自定义规则优先
        for pat, mode, conf in self._extra_rules:
            if pat.search(haystack):
                evidence.append(f"custom_rule:{pat.pattern}->{mode.value}")
                return SemanticSupportResult(
                    label=label,
                    support_mode=mode,
                    confidence=conf,
                    reason=f"项目自定义规则命中 {pat.pattern}",
                    evidence=evidence,
                    recommended_check=_check_for(mode),
                )

        hits: list[tuple[SupportMode, float, str]] = []

        # 1) 吊挂：语义最明确——允许离开地面/结构面
        if _RE_HANGING.search(haystack):
            hits.append((SupportMode.HANGING, 0.9, "名称/标签含悬挂语义（灯笼/吊灯等）"))
            evidence.append("keyword:HANGING")

        # 2) 结构件：应被结构支撑，**不是**「必须落地」
        if _RE_STRUCTURE.search(haystack):
            # 父级也是结构 → 更像 ATTACHED/STRUCTURE 组合
            conf = 0.8
            reason = "名称/路径含结构构件语义（屋顶/梁柱/墙/台基等）"
            if _RE_ATTACHED.search(label) and _RE_STRUCTURE.search(label):
                # Roof_Crown：结构上的装饰
                hits.append((SupportMode.ATTACHED, 0.82, "结构上的装饰/脊饰（应贴附结构面）"))
                evidence.append("keyword:ATTACHED_on_STRUCTURE")
            else:
                hits.append((SupportMode.STRUCTURE, conf, reason))
                evidence.append("keyword:STRUCTURE")

        # 3) 地面类
        if _RE_GROUND.search(haystack):
            hits.append((SupportMode.GROUND, 0.9, "名称/路径含地面/广场语义"))
            evidence.append("keyword:GROUND")

        # 4) 附着装饰：**不能**仅因叫 decoration 就放行
        if _RE_ATTACHED.search(haystack) and not any(h[0] == SupportMode.HANGING for h in hits):
            # 若同时像结构（Roof_Crown）→ ATTACHED；纯 Decoration 且无结构上下文 → 低置信
            if any(h[0] == SupportMode.STRUCTURE for h in hits) or parent or attached:
                hits.append((SupportMode.ATTACHED, 0.78, "装饰/饰件且存在结构上下文或 attachment"))
                evidence.append("keyword:ATTACHED")
            else:
                # 纯「Decoration」标签：用途≠支撑关系
                hits.append((SupportMode.UNKNOWN, 0.4, "仅识别到装饰语义，无法推断支撑关系（用途≠支撑）"))
                evidence.append("keyword:DECOR_without_support_context")

        # 5) Class 弱信号
        for pat, mode in _CLASS_HINTS:
            if class_name and pat.search(class_name):
                evidence.append(f"class:{class_name}->{mode.value}")
                if mode == SupportMode.HANGING and not any(h[0] == SupportMode.HANGING for h in hits):
                    hits.append((SupportMode.HANGING, 0.7, f"ActorClass {class_name} 倾向吊挂灯具"))
                break

        # 6) Attachment 强信号：已挂在别的 Actor 上
        if attached and not any(h[0] == SupportMode.HANGING for h in hits):
            if parent:
                evidence.append(f"attach_parent:{parent}")
            # 有父级但无更强语义 → ATTACHED 中等置信
            if not hits:
                hits.append((SupportMode.ATTACHED, 0.65, "存在 attachment 父级"))
            elif all(h[0] == SupportMode.UNKNOWN for h in hits):
                hits.append((SupportMode.ATTACHED, 0.65, "存在 attachment 父级（覆盖模糊装饰判定）"))

        if not hits:
            return SemanticSupportResult(
                label=label,
                support_mode=SupportMode.UNKNOWN,
                confidence=0.2,
                reason="无法从 label/folder/class/tags 推断支撑模式",
                evidence=evidence or ["no_signal"],
                recommended_check="vision",
            )

        # 取置信度最高；同分优先更「可检查」的模式（GROUND/STRUCTURE/ATTACHED/HANGING）
        mode_rank = {
            SupportMode.HANGING: 3,
            SupportMode.GROUND: 2,
            SupportMode.STRUCTURE: 2,
            SupportMode.ATTACHED: 2,
            SupportMode.UNKNOWN: 0,
        }
        hits.sort(key=lambda h: (h[1], mode_rank.get(h[0], 1)), reverse=True)
        best_mode, best_conf, best_reason = hits[0]

        # 矛盾信号：例如同时像 GROUND 与 HANGING → 降置信，交给 AI
        modes = {h[0] for h in hits}
        if len(modes) > 1 and SupportMode.HANGING in modes and SupportMode.GROUND in modes:
            best_mode = SupportMode.UNKNOWN
            best_conf = min(best_conf, 0.4)
            best_reason = "语义矛盾（同时像地面与吊挂），交 Vision/AI"
            evidence.append("conflict:GROUND_vs_HANGING")

        # 仅 UNKNOWN 弱装饰
        if best_mode == SupportMode.UNKNOWN:
            best_conf = min(best_conf, 0.45)

        return SemanticSupportResult(
            label=label,
            support_mode=best_mode,
            confidence=float(best_conf),
            reason=best_reason,
            evidence=evidence,
            recommended_check=_check_for(best_mode),
        )

    def resolve_many(
        self, actors: Iterable[Mapping[str, Any] | Any]
    ) -> list[SemanticSupportResult]:
        return [self.resolve(a) for a in actors]


def _check_for(mode: SupportMode) -> str:
    return {
        SupportMode.GROUND: "ground_gap",
        SupportMode.STRUCTURE: "structure_trace",
        SupportMode.ATTACHED: "attach_check",
        SupportMode.HANGING: "none",
        SupportMode.UNKNOWN: "vision",
    }[mode]


def _normalize(actor: Mapping[str, Any] | Any) -> dict[str, Any]:
    if hasattr(actor, "as_dict"):
        try:
            return dict(actor.as_dict(include_extra=True))
        except TypeError:
            return dict(actor.as_dict())
    return dict(actor or {})


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def select_for_geometric_check(
    results: Sequence[SemanticSupportResult],
) -> tuple[list[SemanticSupportResult], list[SemanticSupportResult], list[SemanticSupportResult]]:
    """把解析结果分成三组：确定性几何检查 / 跳过 / 交 AI。

    返回 (geometry_candidates, skipped_hanging, needs_ai)。
    """
    geo: list[SemanticSupportResult] = []
    skip: list[SemanticSupportResult] = []
    ai: list[SemanticSupportResult] = []
    for r in results:
        if r.needs_ai:
            ai.append(r)
        elif r.support_mode == SupportMode.HANGING:
            skip.append(r)
        elif r.support_mode in (SupportMode.GROUND, SupportMode.STRUCTURE, SupportMode.ATTACHED):
            if r.recommended_check != "none":
                geo.append(r)
            else:
                skip.append(r)
        else:
            ai.append(r)
    return geo, skip, ai


__all__ = [
    "SupportMode",
    "SemanticSupportResult",
    "SemanticSupportResolver",
    "select_for_geometric_check",
    "HIGH_CONFIDENCE",
    "LOW_CONFIDENCE",
]
