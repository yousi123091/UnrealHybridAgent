"""验证结果的数据结构。

一个关键约定：**区分"未通过"和"没做"**。

``passed=False`` —— 真的验证了，结论是不通过 → 应该触发回退/报错。
``skipped=True`` —— 证据不足（比如后端不提供包围盒），没验 → 必须如实上报，
                     不能算通过，但也不该当作失败去触发一轮无意义的回退。

很多自动化脚本把这两者混为一谈，结果要么"假绿"（跳过也算通过），
要么"假红"（信息不足狂重试）。这里从类型上就分开。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


def _flatten(items: Any) -> list["CheckResult"]:
    """把"单个检查 / 检查列表 / 忘了展开的嵌套列表"统一拉平。

    这条容错是有意加的：``check_transform_equals`` 返回的是一组检查，
    调用方很容易写成 ``[check_transform_equals(...)]`` —— 外层多套了一层列表。
    如果不拉平，报告里会混进一个 list 对象，然后在 ``report.passed`` 里
    炸出 ``'list' object has no attribute 'skipped'``，
    把一个本来正确的验证结论变成一次崩溃。

    对真正不认识的东西**直接报错**，不静默忽略——宁可吵，不要假绿。
    """
    if isinstance(items, CheckResult):
        return [items]
    out: list[CheckResult] = []
    for item in items:
        if isinstance(item, CheckResult):
            out.append(item)
        elif isinstance(item, (list, tuple)):
            out.extend(_flatten(item))
        else:
            raise TypeError(f"验证报告只接受 CheckResult，收到 {type(item).__name__}: {item!r}")
    return out


@dataclass
class CheckResult:
    """单项检查结果。"""

    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""
    skipped: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
        }
        if self.skipped:
            out["skipped"] = True
        if self.detail:
            out["detail"] = self.detail
        if self.reason:
            out["reason"] = self.reason
        return out

    def line(self) -> str:
        if self.skipped:
            return f"[跳过] {self.name} —— {self.reason or '缺少证据'}"
        mark = "通过" if self.passed else "不通过"
        body = f"{self.name} —— {mark}"
        if self.expected is not None or self.actual is not None:
            body += f"（期望 {self.expected!r} / 实际 {self.actual!r}）"
        return f"[{mark}] {body}" if not self.detail else f"[{mark}] {body} · {self.detail}"


@dataclass
class VerifyReport:
    """一组检查的汇总。"""

    checks: list[CheckResult] = field(default_factory=list)

    # -- 组装 ----------------------------------------------------------------

    def add(self, check: CheckResult | Iterable[CheckResult]) -> "VerifyReport":
        self.checks.extend(_flatten(check))
        return self

    def extend(self, checks: Iterable[CheckResult]) -> "VerifyReport":
        self.checks.extend(_flatten(checks))
        return self

    # -- 结论 ----------------------------------------------------------------

    @property
    def executed(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.skipped]

    @property
    def skipped_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.skipped]

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.executed if not c.passed]

    @property
    def passed(self) -> bool:
        """全部**已执行**的检查都通过（跳过项不计入，但会在报告里列出）。"""
        return not self.failures and bool(self.executed)

    @property
    def inconclusive(self) -> bool:
        """一个能执行的检查都没有 —— 结论是"没验成"，不是"通过"。"""
        return not self.executed

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "inconclusive": self.inconclusive,
            "executed": len(self.executed),
            "skipped": len(self.skipped_checks),
            "failures": [c.as_dict() for c in self.failures],
            "checks": [c.as_dict() for c in self.checks],
        }

    def summary(self) -> str:
        total = len(self.checks)
        ok = len([c for c in self.executed if c.passed])
        skip = len(self.skipped_checks)
        if self.inconclusive:
            head = f"验证未得出结论（{total} 项检查全部缺证据）"
        else:
            head = f"验证{'通过' if self.passed else '不通过'}（{ok}/{len(self.executed)} 项通过"
            head += f"，{skip} 项跳过）" if skip else "）"
        return head


__all__ = ["CheckResult", "VerifyReport"]
