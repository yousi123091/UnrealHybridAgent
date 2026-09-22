# 第三方依赖记录

原则：能复用成熟小库就不重造；不为复用而引入庞大框架。第三方尽量 **Adapter/Wrapper**，不 fork 魔改。

## 运行时（UHA 自身）

| 项目 | 地址 / 来源 | 版本 | License | 解决什么 | 为何采用 | 可否替换 |
|---|---|---|---|---|---|---|
| **Agent-TARS Computer Use** | `E:\MCP\Agent-TARS`（本地；配置 `mcp_servers.computer_use.autostart`） | 随仓库（Node server） | 见该仓库 | 桌面鼠标键盘 + 截图 + DesktopLock | 已在本机跑通 HTTP MCP；共享桌面锁，避免 stdio 多进程锁分裂 | 可换其它 Computer Use MCP，需保持 lock 语义 |
| **GenOrca unreal-mcp + UnrealMCPython** | `E:\MCP\unreal-mcp` / `_thirdparty/extracted/...` | v2.2.0 | Apache-2.0 | UE 内 Actor/Level/Vision/line_trace 等 MCP 工具 | 与 UE 5.8 实测兼容；UHA 只做协议适配不改第三方源码 | 可换其它 UE MCP，需重做 DOMAIN_MAP |
| **pywin32**（win32gui/api/con） | https://github.com/mhammond/pywin32 | 随 `.venv`（已装） | PSF-2.0 | 找 UE 窗口、ROI 启发式、DPI、显示器 | Windows 原生、体积小、无 GUI 框架绑架 | 可换 ctypes 裸调 user32，成本更高 |
| **httpx** | https://github.com/encode/httpx | 随 `.venv` | BSD-3-Clause | MCP Streamable HTTP 客户端 | 成熟 HTTP 客户端 | 可用标准库 urllib，功能会缩水 |

## 刻意未引入

| 候选 | 为何不用 |
|---|---|
| UI-TARS / 独立 GUI Grounding VLM | 当前 `vlmConfigured=false`；语义确定问题走规则，模糊问题可留给调用方主 Agent 看图。ROADMAP §24 |
| 第二套 Planner/决策框架 | 执行裁决权必须仍在单一 Router |
| `keyboard` / `pynput` 等热键库 | Emergency Stop 用 **ctypes RegisterHotKey**（零依赖、确定性）即可 |
| 重型 UIAutomation 框架 | Session Calibration 用窗口几何 + UE 默认布局比例启发式，已够用且可配置覆盖 |

## 本项目自研（不重复造外部轮子）

| 模块 | 原因 |
|---|---|
| SemanticSupportResolver | 强领域：UE 构件支撑语义，无现成库匹配 |
| Router multi-signal + quality ledger | 与本项目 MethodHealth/Executor 深度耦合 |
| Session locks（读并行/写串行） | 会话级策略，非通用锁库职责 |
| Control Overlay（tkinter） | 极薄状态窗；tkinter 为标准库（本机 venv 可能未装，已做静默降级） |
| ground_place / line_trace 采样 | 与 Actor extent/幂等绝对坐标约定绑定 |
