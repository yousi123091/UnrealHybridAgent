"""适配器层。

    mcp_client.py   通用 MCP 客户端（Streamable HTTP / stdio）
    base.py         UE 后端统一契约（op 词汇表）
    computer_use.py Computer Use 适配器 -> 外部 Computer Use 服务（见 <AGENT_TARS_ROOT>）
    unreal/         UE 侧后端：unreal_mcp / ue_python / ue_commandlet / remote_control
"""

from .base import OPS, ActorRef, UnrealBackend
from .computer_use import ComputerUseAdapter, Screenshot, ScreenSize, build_from_config
from .mcp_client import (
    MCPClient,
    StdioTransport,
    StreamableHTTPTransport,
    ToolSpec,
    make_client_from_config,
    probe_backend,
    unwrap_tool_result,
)

__all__ = [
    "OPS",
    "ActorRef",
    "UnrealBackend",
    "ComputerUseAdapter",
    "Screenshot",
    "ScreenSize",
    "build_from_config",
    "MCPClient",
    "StdioTransport",
    "StreamableHTTPTransport",
    "ToolSpec",
    "make_client_from_config",
    "probe_backend",
    "unwrap_tool_result",
]
