"""任务意图（Router 的输入）。

Router 不看"自然语言原文"，只看这个结构化意图 —— 这样规则才是可测试、
可解释、可回归的。意图由 `planner` 或调用方显式构造，也可以从任务描述里
用关键词做一次廉价推断（`Intent.from_text`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# 需要"审美/空间判断"的词。命中即认为必须有人看画面。
VISION_KEYWORDS = (
    "看起来",
    "是否合理",
    "合理",
    "穿模",
    "穿插",
    "比例",
    "光照",
    "悬空",
    "漂浮",
    "视觉效果",
    "美观",
    "观感",
    "对齐",
    "显得",
    "不自然",
    "突兀",
    "空旷",
    "拥挤",
    "检查一下画面",
    "截图看",
)

# 已知可用快捷键的单步操作
SHORTCUT_KEYWORDS: dict[str, str] = {
    "保存": "ctrl s",
    "save": "ctrl s",
    "撤销": "ctrl z",
    "undo": "ctrl z",
    "重做": "ctrl y",
    "redo": "ctrl y",
    "复制": "ctrl c",
    "粘贴": "ctrl v",
    "全选": "ctrl a",
}

# 表明"纯 UI / 第三方插件 / 视口拖拽"的词 -> 只能靠鼠标
MOUSE_KEYWORDS = ("拖拽", "拖动", "视口里拖", "gizmo", "面板", "菜单", "工具栏", "下拉", "勾选", "拖动到")

# 批量信号
BATCH_PATTERN = re.compile(r"(\d+)\s*(个|根|块|处|节点|actor|actors)", re.IGNORECASE)


@dataclass
class TaskIntent:
    """一次需要选执行方式的任务。"""

    #: 语义动作名，对应 skills 里的 skill 名（actor_move / level_save / visual_inspect / ...）
    skill: str

    #: 人类可读任务描述（会进日志）
    description: str = ""

    # --- 结构化特征 -------------------------------------------------------

    #: 目标对象数量。>=5 优先结构化通道
    target_count: int = 1

    #: 是否需要精确数值（坐标/旋转/缩放）——只有结构化通道能给
    needs_exact_values: bool = True

    #: 是否需要视觉判断（审美、悬空、穿模、光照）
    needs_vision: bool = False

    #: 是否已知 Actor 名字（能直接用结构化查询定位）
    known_actor: str | None = None

    #: 是否已知快捷键，能把操作压到 1~2 步
    known_shortcut: str | None = None

    #: 是否只能通过 GUI（第三方插件面板、视口 gizmo、右键菜单）
    ui_only: bool = False

    #: 是否只读（只查询不修改）
    read_only: bool = False

    #: 上下文补充（步骤序号、前一次用的方法、失败历史等）
    context: dict[str, Any] = field(default_factory=dict)

    # --- 派生 -------------------------------------------------------------

    @property
    def is_batch(self) -> bool:
        return self.target_count >= 5

    def with_context(self, **kw: Any) -> "TaskIntent":
        self.context.update(kw)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "description": self.description,
            "target_count": self.target_count,
            "needs_exact_values": self.needs_exact_values,
            "needs_vision": self.needs_vision,
            "known_actor": self.known_actor,
            "known_shortcut": self.known_shortcut,
            "ui_only": self.ui_only,
            "read_only": self.read_only,
        }

    # --- 从自然语言推断 ---------------------------------------------------

    @classmethod
    def from_text(cls, text: str, *, skill: str = "", actor: str | None = None) -> "TaskIntent":
        """廉价关键词推断。只用来构造初稿，允许调用方覆盖。"""
        low = text.lower()
        needs_vision = any(k in text or k in low for k in VISION_KEYWORDS)

        shortcut = None
        for key, keys_ in SHORTCUT_KEYWORDS.items():
            if key in low:
                shortcut = keys_
                break

        count = 1
        m = BATCH_PATTERN.search(text)
        if m:
            try:
                count = int(m.group(1))
            except ValueError:
                count = 1

        ui_only = any(k in low for k in MOUSE_KEYWORDS)
        read_only = any(k in text for k in ("查看", "列出", "读取", "查询", "检查")) and not any(
            k in text for k in ("修改", "设置", "移动", "保存", "调整")
        )

        if not skill:
            skill = cls._guess_skill(text)

        return cls(
            skill=skill,
            description=text,
            target_count=count,
            needs_vision=needs_vision,
            known_actor=actor,
            known_shortcut=shortcut,
            ui_only=ui_only,
            read_only=read_only,
            needs_exact_values=not read_only,
        )

    @staticmethod
    def _guess_skill(text: str) -> str:
        if any(k in text for k in ("保存", "save")):
            return "level_save"
        if any(k in text for k in VISION_KEYWORDS):
            return "visual_inspect"
        if any(k in text for k in ("查找", "找到", "寻找", "find")):
            return "actor_find"
        if any(k in text for k in ("查看", "检查", "读取", "inspect", "transform")):
            return "actor_inspect"
        if any(k in text for k in ("移动", "移动", "提高", "调整位置", "抬高", "降低", "move")):
            return "actor_move"
        return "actor_inspect"


__all__ = ["TaskIntent", "VISION_KEYWORDS", "SHORTCUT_KEYWORDS", "MOUSE_KEYWORDS"]
