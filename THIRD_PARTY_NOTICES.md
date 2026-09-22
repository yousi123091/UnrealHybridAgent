# Third-Party Notices

本文件说明 UHA 与外部组件的关系。**本仓库只包含 UHA 自身代码。**

## 本仓库自身代码

UHA（UnrealHybridAgent）自身源码以 **MIT License** 发布，见 [`LICENSE`](LICENSE)。

## 与外部组件的关系

UHA **不包含、不修改、不重新分发**下列任何项目的源码。它通过进程间协议
（TCP / HTTP / MCP）与这些**独立安装、独立运行**的服务通信。

因此许可义务集中在"如何部署依赖服务"，而不是"分发第三方代码"。

下表"许可证"一列仅为撰写时点的记录。**若有疑问，以对应项目当前公开许可证为准。**

---

### 1. GenOrca / unreal-mcp

| 项目 | 内容 |
|---|---|
| 用途 | Unreal MCP 通道的服务端实现，运行在 UE 编辑器进程内 |
| 官方来源 | <https://github.com/GenOrca/unreal-mcp> |
| UHA 与它的关系 | 仅客户端适配层（[`src/adapters/unreal/unreal_mcp.py`](src/adapters/unreal/unreal_mcp.py)），按对方公开的工具契约调用 |
| 是否 bundled | 否 |
| 如何指向本机安装 | `config/agent.config.json` → `mcp_servers.unreal_mcp`（`stdio.command` 或 `http.base_url`） |
| 是否可替换 | 是。任何实现同一工具契约的 MCP 服务都可替代 |
| 许可证 | **以该项目当前公开许可证为准**（历史上为 Apache-2.0） |

### 2. Agent-TARS（Computer Use / Streamable HTTP MCP）

| 项目 | 内容 |
|---|---|
| 用途 | 桌面级 GUI 通道：截图、鼠标、键盘、桌面租约 |
| 官方来源 | 以该项目的公开仓库为准 |
| UHA 与它的关系 | 仅客户端适配层（[`src/adapters/computer_use.py`](src/adapters/computer_use.py)），通过 HTTP 调用本机服务 |
| 是否 bundled | 否 |
| 如何指向本机安装 | `config/agent.config.json` → `mcp_servers.computer_use.base_url`；可选 `autostart.cwd` |
| 是否可替换 | 是。任何提供相同 MCP 工具契约的 Computer Use 服务都可替代 |
| 许可证 | **以该项目当前公开许可证为准** |

### 3. Unreal Engine / PythonScriptPlugin

| 项目 | 内容 |
|---|---|
| 用途 | UHA 自动化操作的目标引擎；`PythonScriptPlugin` 提供 `remote_execution.py` 通道 |
| 官方来源 | Epic Games |
| UHA 与它的关系 | UHA 在运行时**从本机 UE 安装目录加载**官方 `remote_execution.py`（路径由本机配置给出）。UHA 不复制、不修改、不分发该文件 |
| 是否 bundled | 否 |
| 如何指向本机安装 | `config/agent.config.json` → `ue.engine_root` / `ue.editor_exe` / `ue.remote_exec_python_path` |
| 许可证 | Unreal Engine 受 Epic 的 EULA 约束，**不在本仓库分发范围内** |

### 4. Python 运行时与第三方 Python 包

本仓库在 [`requirements.txt`](requirements.txt) 中声明直接依赖。这些包按其各自
许可证分发，通过 pip 从 PyPI 获取，**不随本仓库源码分发**。

### 5. Node.js 运行时（仅用于外部 Computer Use 服务）

UHA 自身不需要 Node.js。若你自行部署 Agent-TARS 等 Computer Use 服务，
按其官方要求准备 Node.js 运行时。

---

## 使用建议

- 本文件**不构成法律意见**。若你计划分发包含上述组件的二进制产物，
  请自行核对每一项的最新许可条款。
- 若将来任何第三方源码被复制进本仓库，必须同时保留其 `LICENSE` 与 `NOTICE`
  文件，并在此更新本清单。
- 第三方项目**不属于 UHA**。本文件不宣称对它们拥有任何权利。
