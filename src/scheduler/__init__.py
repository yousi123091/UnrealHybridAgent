"""调度层：一次任务的执行闭环（选路 → 执行 → 验证 → 降级 → 记录）。"""

from .executor import ExecutionRecord, Executor, PlanResult, StepOutcome

__all__ = ["Executor", "ExecutionRecord", "PlanResult", "StepOutcome"]
