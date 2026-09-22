"""UnrealHybridAgent — UE5 专用 Hybrid Agent Runtime.

顶层包。子模块划分：

    core        配置、日志、错误、结果类型
    adapters    外部执行后端（Computer Use MCP / Unreal MCP / UE Python / Remote Control）
    router      执行方式选择（含 cost 估算）
    planner     任务 → 步骤计划
    skills      面向 UE5 语义的能力单元（actor_move / level_save / ...）
    validation  执行后验证
    scheduler   重试、退避、方法冷却
    vision      视觉判断（启发式，零模型）
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
