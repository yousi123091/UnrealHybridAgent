"""Skill 注册表。

显式注册而不是自动扫描：技能清单会出现在 README、日志和 `uha list` 里，
显式的一行比"目录里有个文件就自动生效"更好审计——尤其是这个项目
会去改用户的 UE 工程。
"""

from __future__ import annotations

from typing import Iterable

from .actor_find import ActorFindSkill
from .actor_inspect import ActorInspectSkill
from .actor_move import ActorMoveSkill
from .base import Skill, SkillContext, SkillResult, SkillTrace
from .batch_mutation import BatchMutationSkill
from .level_save import LevelSaveSkill
from .visual_inspect import VisualInspectSkill

_SKILL_CLASSES = (
    ActorFindSkill,
    ActorInspectSkill,
    ActorMoveSkill,
    LevelSaveSkill,
    VisualInspectSkill,
    BatchMutationSkill,
)

SKILLS: dict[str, Skill] = {cls().name: cls() for cls in _SKILL_CLASSES}


def get_skill(name: str) -> Skill:
    try:
        return SKILLS[name]
    except KeyError as exc:
        raise KeyError(
            f"未知 skill {name!r}；可用：{', '.join(sorted(SKILLS))}"
        ) from exc


def all_skills() -> list[Skill]:
    return list(SKILLS.values())


def describe_all() -> list[dict[str, object]]:
    return [s.describe() for s in all_skills()]


def match_by_text(text: str) -> list[Skill]:
    """按关键词给文本打分，返回命中的 skill（分数高在前）。

    只用于 `planner` 的初稿推断；真正的执行计划允许调用方显式指定。
    """
    low = text.lower()
    scored: list[tuple[int, Skill]] = []
    for skill in all_skills():
        hits = sum(1 for kw in skill.keywords if kw and kw.lower() in low)
        if hits:
            scored.append((hits, skill))
    scored.sort(key=lambda t: -t[0])
    return [s for _, s in scored]


__all__ = [
    "SKILLS",
    "get_skill",
    "all_skills",
    "describe_all",
    "match_by_text",
    "Skill",
    "SkillContext",
    "SkillResult",
    "SkillTrace",
]
