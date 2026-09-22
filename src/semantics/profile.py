"""Project semantic profiles (Phase 4B)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..vision.semantic_support import SemanticSupportResult, SupportMode

_GROUND_KINDS = ("GLOBAL_GROUND", "LOCAL_GROUND", "ELEVATED_GROUND", "UNKNOWN_GROUND")

_EXTRA_MODES = {
    "ELEVATED_GROUND": SupportMode.GROUND,  # still ground-like, but not world-drop
    "LOCAL_GROUND": SupportMode.GROUND,
    "MARKER": SupportMode.UNKNOWN,
    "DECORATIVE_STRUCTURE": SupportMode.ATTACHED,
    "IGNORE_FOR_GEOMETRY": SupportMode.UNKNOWN,
}


@dataclass
class ProfileRule:
    pattern: str
    role: str
    ground_kind: str | None = None
    support_strategy: str | None = None
    inspection_strategy: str | None = None
    confidence: float = 0.8
    reason: str = ""
    _re: re.Pattern[str] | None = field(default=None, repr=False)

    def compiled(self) -> re.Pattern[str]:
        if self._re is None:
            self._re = re.compile(self.pattern, re.I)
        return self._re


@dataclass
class SemanticProfile:
    name: str
    rules: list[ProfileRule] = field(default_factory=list)
    source: str = ""

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: str = "") -> "SemanticProfile":
        rules = []
        for item in data.get("rules") or []:
            try:
                rules.append(ProfileRule(
                    pattern=str(item["pattern"]),
                    role=str(item["role"]),
                    ground_kind=item.get("ground_kind"),
                    support_strategy=item.get("support_strategy"),
                    inspection_strategy=item.get("inspection_strategy"),
                    confidence=float(item.get("confidence", 0.8)),
                    reason=str(item.get("reason") or data.get("name") or "profile"),
                ))
            except Exception:
                continue
        return cls(name=str(data.get("name") or "profile"), rules=rules, source=source)

    @classmethod
    def load(cls, path: str | Path) -> "SemanticProfile":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data, source=str(p))

    def resolve(self, label: str, *, class_name: str = "", folder: str = "", tags: list[str] | None = None) -> dict[str, Any] | None:
        hay = " | ".join(x for x in (label, folder, class_name, " ".join(tags or [])) if x)
        for rule in self.rules:
            if rule.compiled().search(hay):
                mode = _EXTRA_MODES.get(rule.role, None)
                if mode is None:
                    try:
                        mode = SupportMode(rule.role)
                    except Exception:
                        mode = SupportMode.UNKNOWN
                check = rule.support_strategy
                if not check:
                    if rule.ground_kind in ("ELEVATED_GROUND", "LOCAL_GROUND"):
                        check = "ground_gap_local"
                    elif mode == SupportMode.GROUND:
                        check = "ground_gap"
                    elif mode == SupportMode.STRUCTURE:
                        check = "structure_trace"
                    elif mode == SupportMode.HANGING:
                        check = "none"
                    elif rule.role in ("MARKER", "IGNORE_FOR_GEOMETRY"):
                        check = "none"
                    else:
                        check = "vision"
                return {
                    "label": label,
                    "support_mode": mode.value,
                    "profile_role": rule.role,
                    "ground_kind": rule.ground_kind,
                    "confidence": rule.confidence,
                    "reason": f"profile:{self.name}:{rule.pattern}",
                    "recommended_check": check,
                    "inspection_strategy": rule.inspection_strategy,
                    "actionable": rule.confidence >= 0.75 and mode != SupportMode.UNKNOWN and rule.role not in ("MARKER", "IGNORE_FOR_GEOMETRY"),
                    "needs_ai": rule.role in ("MARKER", "UNKNOWN", "IGNORE_FOR_GEOMETRY") or rule.confidence < 0.75,
                    "profile": self.name,
                }
        return None

    def enrich(self, base: SemanticSupportResult) -> dict[str, Any]:
        hit = self.resolve(base.label)
        out = base.as_dict()
        if hit:
            out.update({
                "support_mode": hit["support_mode"],
                "confidence": hit["confidence"],
                "reason": hit["reason"],
                "recommended_check": hit["recommended_check"],
                "actionable": hit["actionable"],
                "needs_ai": hit["needs_ai"],
                "profile_role": hit.get("profile_role"),
                "ground_kind": hit.get("ground_kind"),
                "profile": hit.get("profile"),
            })
        else:
            out.setdefault("ground_kind", None)
            out.setdefault("profile", self.name)
            # default ground kind heuristic
            if out.get("support_mode") == "GROUND":
                out["ground_kind"] = "GLOBAL_GROUND"
        return out


def default_profile_dir() -> Path:
    return Path(__file__).resolve().parent / "profiles"


def load_profile(name_or_path: str | None, *, config: Mapping[str, Any] | None = None) -> SemanticProfile:
    if not name_or_path:
        name_or_path = (config or {}).get("semantics.profile") or "example_palace"
    p = Path(str(name_or_path))
    if p.is_file():
        return SemanticProfile.load(p)
    candidate = default_profile_dir() / f"{name_or_path}.json"
    if candidate.is_file():
        return SemanticProfile.load(candidate)
    fallback = default_profile_dir() / "default.json"
    if fallback.is_file():
        return SemanticProfile.load(fallback)
    return SemanticProfile(name="empty")


__all__ = ["SemanticProfile", "ProfileRule", "load_profile", "default_profile_dir"]
