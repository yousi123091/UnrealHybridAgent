"""UE 侧后端集合。

四个后端实现同一组 op（见 `adapters.base.OPS`），Router 按可用性/成本挑选：

    UnrealMCPBackend    第三方 Unreal MCP（stdio MCP 协议，本机用 GenOrca/unreal-mcp）
    UEPythonBackend     引擎官方 Remote Execution（编辑器在线）
    UECommandletBackend 离线 UnrealEditor-Cmd -run=pythonscript（无编辑器）
    (reserved)          Remote Control HTTP（WebRemoteControl 插件）

第三方 Unreal MCP **不放入本仓库**，通过配置指向外部路径（默认 E:\\MCP\\unreal-mcp）。
"""

from .commandlet import UECommandletBackend
from .ue_python import UEPythonBackend, find_remote_execution_module, load_remote_execution
from .unreal_mcp import UnrealMCPBackend, build_unreal_mcp_backend

__all__ = [
    "UnrealMCPBackend",
    "build_unreal_mcp_backend",
    "UEPythonBackend",
    "UECommandletBackend",
    "find_remote_execution_module",
    "load_remote_execution",
]
