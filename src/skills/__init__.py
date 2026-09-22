"""Skills：语义动作层。

每个 skill 声明"这件事有什么特征"、"用某种方式怎么做"、"做完怎么独立验证"，
但不关心"该用哪种方式"——那是 Router 的职责。
"""

from .actor_find import ActorFindSkill
from .actor_inspect import ActorInspectSkill
from .actor_move import ActorMoveSkill
from .base import Skill, SkillContext, SkillResult, SkillTrace
from .level_save import LevelSaveSkill
from .registry import SKILLS, all_skills, describe_all, get_skill, match_by_text
from .visual_inspect import VisualInspectSkill

__all__ = [
    "Skill",
    "SkillContext",
    "SkillResult",
    "SkillTrace",
    "ActorFindSkill",
    "ActorInspectSkill",
    "ActorMoveSkill",
    "LevelSaveSkill",
    "VisualInspectSkill",
    "SKILLS",
    "get_skill",
    "all_skills",
    "describe_all",
    "match_by_text",
]
