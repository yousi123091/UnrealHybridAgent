# 第三方依赖

**最重要的一条：下列第三方服务不随 UHA 源码分发。**
UHA 仓库只包含 UHA 自身代码；外部服务需要你自行安装、自行运行，然后通过本机
配置告诉 UHA 在哪里找到它们。

UHA 通过进程间协议（TCP / HTTP / MCP）与这些服务通信，**不包含、不修改、
不 vendoring 任何第三方源码**。

---

## 路径占位符约定

本文档与仓库其它文档中使用的占位符：

| 占位符 | 含义 |
|---|---|
| `<UHA_ROOT>` | 你本机 UHA 仓库的根目录 |
| `<AGENT_TARS_ROOT>` | 你本机 Agent-TARS（Computer Use 服务）安装目录 |
| `<UNREAL_MCP_ROOT>` | 你本机 GenOrca / unreal-mcp 安装目录 |
| `<UE_ROOT>` | 你本机 Unreal Engine 安装根目录 |
| `<PYTHON_EXE>` | 你本机使用的 Python 解释器路径 |

**不要照抄历史报告里的具体磁盘路径**——那些是原验证机的路径，已匿名化为占位符。

---

## 一、外部服务（需要单独安装，不随源码提供）

### 1. GenOrca / unreal-mcp

| 项 | 内容 |
|---|---|
| 项目名称 | unreal-mcp（含 UnrealMCPython 插件侧） |
| 官方来源 | <https://github.com/GenOrca/unreal-mcp> |
| UHA 需要它的什么 | UE 内的 Actor / Level / Vision / line_trace 等 MCP 工具 |
| UHA 与它的关系 | 纯客户端适配：`src/adapters/unreal/unreal_mcp.py` 按对方公开工具契约调用 |
| 是否 bundled | **否** |
| 如何指向本机安装 | `config/agent.config.json` → `mcp_servers.unreal_mcp.stdio.command`（或 `.http.base_url`） |
| 是否可替换 | **是**。任何实现同一工具契约的 UE MCP 服务都可替换（需重做 `DOMAIN_MAP`） |
| 许可证 | **以该项目当前公开许可证为准** |

### 2. Agent-TARS（Computer Use / Streamable HTTP MCP）

| 项 | 内容 |
|---|---|
| 项目名称 | Agent-TARS |
| 官方来源 | 以该项目的公开仓库为准 |
| UHA 需要它的什么 | 桌面鼠标 / 键盘 / 截图，以及桌面租约（desktop lock） |
| UHA 与它的关系 | 纯客户端适配：`src/adapters/computer_use.py` 通过 Streamable HTTP 调用；走 HTTP 是为了共享同一把桌面锁，避免 stdio 多进程各算各的锁 |
| 是否 bundled | **否** |
| 如何指向本机安装 | `config/agent.config.json` → `mcp_servers.computer_use.base_url`；可选 `.autostart.cwd` |
| 是否可替换 | **是**。任何提供相同 MCP 工具契约并保留 lock 语义的 Computer Use 服务都可替换 |
| 许可证 | **以该项目当前公开许可证为准** |

UHA 自身的静态审计工具（`tools/p01_input_forensics.py`）在需要审计这个第三方
代码树时，从 `--agent-tars-root`、环境变量 `UHA_AGENT_TARS_ROOT` 或本机 config
读取根目录；**读不到就把该项标记为 `skipped`，绝不当成通过。**

### 3. Unreal Engine（目标引擎，不是依赖库）

| 项 | 内容 |
|---|---|
| 项目名称 | Unreal Engine 5（含 `PythonScriptPlugin`） |
| 官方来源 | Epic Games |
| UHA 需要它的什么 | 作为被自动化的目标编辑器；`PythonScriptPlugin` 提供官方 `remote_execution.py` 通道 |
| UHA 与它的关系 | UHA 在运行时**从本机 UE 安装目录加载**该官方文件，路径完全由本机配置决定 |
| 是否 bundled | **否**（UE 受 Epic EULA 约束，不在本仓库分发范围内） |
| 如何指向本机安装 | `config/agent.config.json` → `ue.engine_root` / `ue.editor_exe` / `ue.remote_exec_python_path`；也可用环境变量 `UHA_UE_ENGINE_ROOT`、`UHA_UE_REMOTE_EXEC_PATH` |
| 是否可替换 | 不适用 |

> UHA **不会**猜测引擎安装位置。找不到就明确报错并要求配置——fail-closed。

### 4. Node.js 运行时

UHA 自身**不需要** Node.js。仅当你自行部署 Agent-TARS 这类 Computer Use 服务时，
按其官方要求准备 Node.js。

---

## 二、Python 直接依赖（pip 安装）

声明在 [`requirements.txt`](../requirements.txt)：

| 包 | 官方来源 | 用途 | 可否替换 |
|---|---|---|---|
| `httpx` | <https://github.com/encode/httpx> | MCP Streamable HTTP 客户端 | 可换标准库 `urllib`，功能会缩水 |
| `Pillow` | <https://python-pillow.org/> | 截图与像素差分 | 可换其它图像库 |
| `pywin32`（仅 Windows） | <https://github.com/mhammond/pywin32> | 查找 UE 窗口、DPI、显示器、窗口几何 | 可换 ctypes 裸调 `user32`，成本更高 |

`tkinter` 随 Windows 官方 Python 安装包提供，不是 pip 包；部分虚拟环境可能没有，
UHA 对此已做静默降级。

---

## 三、刻意未引入

| 候选 | 为何不用 |
|---|---|
| UI-TARS / 独立 GUI Grounding VLM | 语义确定的问题走规则；模糊判断留给调用方主 Agent。见 ROADMAP |
| 第二套 Planner / 决策框架 | 执行裁决权必须仍在单一 Router |
| `keyboard` / `pynput` 等热键库 | Emergency Stop 用 ctypes `RegisterHotKey`（零依赖、行为确定）即可 |
| 重型 UIAutomation 框架 | 窗口几何 + UE 默认布局比例启发式已够用，且可配置覆盖 |

---

## 四、UHA 自研部分（不重复造外部轮子）

| 模块 | 原因 |
|---|---|
| `SemanticSupportResolver` | 强领域：UE 构件支撑语义，无现成库匹配 |
| Router multi-signal + quality ledger | 与本项目 MethodHealth / Executor 深度耦合 |
| Session locks（读并行 / 写串行） | 会话级策略，非通用锁库职责 |
| `ground_place` / line_trace 采样 | 与 Actor extent / 幂等绝对坐标约定绑定 |

---

## 相关文档

- [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) —— 许可与归属声明
- [`STATUS.md`](STATUS.md) —— 当前 Release 的权威状态
