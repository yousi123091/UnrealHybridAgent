"""最小但完整的 MCP 客户端（JSON-RPC 2.0）。

支持两种传输，覆盖本项目需要接入的所有 MCP Server：

    StreamableHTTPTransport   POST /mcp  +  mcp-session-id 头（MCP 2025-03-26）
                              —— Agent-TARS Computer Use 服务用它
    StdioTransport            子进程 stdin/stdout 换行分隔 JSON
                              —— Unreal MCP（uv run .../main.py）用它

刻意不引入官方 python-sdk：本项目只用到 `initialize / tools/list / tools/call`
三个方法，自研实现约 200 行，避免给运行时增加一个重依赖，
也避免与 Agent-TARS 侧 Node SDK 的版本节奏耦合。

不做的事（明确边界）：
    - 不实现 sampling / roots / elicitation
    - 不实现 resources / prompts（Agent-TARS 也没有）
    - 不做自动重连（由上层 scheduler 负责）
"""

from __future__ import annotations

import json
import queue
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

try:  # pragma: no cover - 环境相关
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

from ..core.errors import BackendUnavailable, ToolCallFailed, TransportError

CLIENT_NAME = "unreal-hybrid-agent"
CLIENT_VERSION = "0.1.0"

# 从新到旧，握手时逐个尝试
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


@dataclass
class ToolSpec:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, obj: Mapping[str, Any]) -> "ToolSpec":
        return cls(
            name=str(obj.get("name", "")),
            description=str(obj.get("description", "") or ""),
            input_schema=dict(obj.get("inputSchema") or {}),
        )


def _jsonrpc(method: str, params: Mapping[str, Any] | None = None, *, req_id: int | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = dict(params)
    if req_id is not None:
        msg["id"] = req_id
    return msg


def unwrap_tool_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """把 MCP 的 `content:[{type:'text',text:'...'}]` 规整成便于上层使用的东西。

    Computer Use 服务返回的约定是 text 里放一个 JSON 字符串，
    这里尝试解析出来；解析不了就原样放在 `text`。

    图片内容会被抽到 `images`（base64），避免把巨大的 base64 混进日志。
    """
    out: dict[str, Any] = {"is_error": bool(result.get("isError")), "raw": result}
    texts: list[str] = []
    images: list[dict[str, str]] = []
    for item in result.get("content") or []:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "text":
            texts.append(str(item.get("text", "")))
        elif kind == "image":
            images.append({"data": str(item.get("data", "")), "mimeType": str(item.get("mimeType", "image/png"))})

    joined = "\n".join(texts)
    parsed: Any = None
    if joined.strip():
        try:
            parsed = json.loads(joined)
        except json.JSONDecodeError:
            parsed = None

    out["text"] = joined
    out["data"] = parsed if parsed is not None else ({"text": joined} if joined else {})
    out["images"] = images
    if result.get("structuredContent") is not None:
        out["data"] = result["structuredContent"]
    return out


class MCPClient:
    """一个 MCP 会话。子类实现传输细节。"""

    def __init__(self, *, name: str = CLIENT_NAME, timeout: float = 60.0):
        self.name = name
        self.timeout = timeout
        self.server_info: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self._id = 0
        self._tools: list[ToolSpec] | None = None

    # -- 需要子类实现 ---------------------------------------------------------

    def _request(self, message: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _notify(self, message: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # -- 通用能力 -------------------------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def initialize(self) -> dict[str, Any]:
        """握手。**幂等**：已握过手就直接返回缓存结果。

        MCP 的 Streamable HTTP 会话是一次性的：对同一会话重复 ``initialize``，
        服务端（如 Agent-TARS Computer Use）会返回
        ``Invalid Request: Server already initialized``。
        上层经常"再探一次可用性"，所以这里必须自己防重入。
        """
        if self.protocol_version is not None:
            return {
                "server_info": self.server_info,
                "protocol_version": self.protocol_version,
                "reused": True,
            }
        last_err: Exception | None = None
        for version in PROTOCOL_VERSIONS:
            try:
                resp = self._request(
                    _jsonrpc(
                        "initialize",
                        {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": {"name": self.name, "version": CLIENT_VERSION},
                        },
                        req_id=self._next_id(),
                    )
                )
            except TransportError as exc:
                last_err = exc
                continue
            if "error" in resp:
                last_err = TransportError(f"initialize failed: {resp['error']}")
                continue
            result = dict(resp.get("result") or {})
            self.server_info = dict(result.get("serverInfo") or {})
            self.protocol_version = result.get("protocolVersion") or version
            self._notify(_jsonrpc("notifications/initialized", {}))
            return {"server_info": self.server_info, "protocol_version": self.protocol_version, "capabilities": result.get("capabilities") or {}}
        raise TransportError(f"MCP initialize failed for all protocol versions: {last_err}")

    def list_tools(self, *, refresh: bool = False) -> list[ToolSpec]:
        if self._tools is not None and not refresh:
            return self._tools
        resp = self._request(_jsonrpc("tools/list", {}, req_id=self._next_id()))
        if "error" in resp:
            raise ToolCallFailed(f"tools/list failed: {resp['error']}")
        tools = [ToolSpec.from_wire(t) for t in (resp.get("result") or {}).get("tools") or []]
        self._tools = tools
        return tools

    def tool_names(self, *, refresh: bool = False) -> list[str]:
        return [t.name for t in self.list_tools(refresh=refresh)]

    def has_tool(self, name: str) -> bool:
        return name in set(self.tool_names())

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        resp = self._request(
            _jsonrpc("tools/call", {"name": name, "arguments": dict(arguments or {})}, req_id=self._next_id())
        )
        if "error" in resp:
            err = resp["error"] or {}
            raise ToolCallFailed(f"{name} failed: {err.get('message') or err}", details={"rpc_error": err})
        return unwrap_tool_result(resp.get("result") or {})

    # -- HTTP 健康探测辅助（仅 StreamableHTTP 有） -----------------------------

    def ping(self) -> bool:
        try:
            self.list_tools(refresh=True)
            return True
        except Exception:
            return False


class StreamableHTTPTransport(MCPClient):
    """MCP Streamable HTTP 传输。

    与常见"一个 POST 一个响应"的实现不同，这里严格按规范处理两件事：

    1. 响应可能是 `application/json`，也可能是 `text/event-stream`（SSE 帧）；
    2. 会话由响应头 `mcp-session-id` 建立，之后的每个请求都要带上它。
    """

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        name: str = CLIENT_NAME,
        timeout: float = 60.0,
        extra_headers: Mapping[str, str] | None = None,
    ):
        super().__init__(name=name, timeout=timeout)
        if httpx is None:  # pragma: no cover
            raise BackendUnavailable("httpx 未安装：pip install httpx")
        self.url = url
        self.token = token
        self._session_id: str | None = None
        self._extra_headers = dict(extra_headers or {})
        from urllib.parse import urlsplit
        local = urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")
        self._client = httpx.Client(timeout=timeout, trust_env=not local)

    # -- 内部 ----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version or PROTOCOL_VERSIONS[0],
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        h.update(self._extra_headers)
        return h

    @staticmethod
    def _parse_sse(text: str) -> list[dict[str, Any]]:
        """从 SSE 文本里抽出所有 JSON 帧。"""
        out: list[dict[str, Any]] = []
        for block in text.replace("\r\n", "\n").split("\n\n"):
            data_lines = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
            if not data_lines:
                continue
            payload = "\n".join(data_lines)
            if not payload or payload == "[DONE]":
                continue
            try:
                out.append(json.loads(payload))
            except json.JSONDecodeError:
                continue
        return out

    def _post(self, message: Mapping[str, Any]) -> dict[str, Any]:
        try:
            resp = self._client.post(self.url, headers=self._headers(), json=dict(message))
        except Exception as exc:  # httpx 的各种连接错误
            raise TransportError(f"POST {self.url} failed: {exc}") from exc

        sid = resp.headers.get("mcp-session-id")
        if sid:
            if self._session_id and self._session_id != sid:
                self._session_id = sid  # 服务端要求新会话
            else:
                self._session_id = sid

        if resp.status_code >= 400:
            body = resp.text[:500]
            raise TransportError(f"HTTP {resp.status_code} from {self.url}: {body}")

        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            frames = self._parse_sse(resp.text)
        else:
            if not resp.text.strip():
                return {}
            try:
                return json.loads(resp.text)
            except json.JSONDecodeError:
                frames = self._parse_sse(resp.text)

        if not frames:
            return {}
        # 取带 id 的那条作为响应
        want = message.get("id")
        for frame in frames:
            if frame.get("id") == want:
                return frame
        return frames[-1]

    def _request(self, message: Mapping[str, Any]) -> dict[str, Any]:
        return self._post(message)

    def _notify(self, message: Mapping[str, Any]) -> None:
        # 通知不需要响应；失败不致命
        try:
            self._post(message)
        except TransportError:
            pass

    def health(self) -> dict[str, Any]:
        """直接打服务端的 /health（若存在）。仅用于诊断。"""
        base = self.url.split("/mcp")[0]
        try:
            r = self._client.get(f"{base}/health", timeout=5.0)
            return dict(r.json())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def close(self) -> None:
        if self._session_id:
            try:
                self._client.delete(self.url, headers=self._headers())
            except Exception:
                pass
        try:
            self._client.close()
        except Exception:
            pass


class StdioTransport(MCPClient):
    """MCP stdio 传输：客户端负责拉起子进程。

    注意：stdio 是**一客户端一进程**。如果被拉起的服务自身持有独占资源
    （比如 Computer Use 的桌面锁），多 Agent 场景必须改用 HTTP——
    这也是本项目对 Computer Use 强制走 HTTP 的原因。
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        name: str = CLIENT_NAME,
        timeout: float = 60.0,
    ):
        super().__init__(name=name, timeout=timeout)
        if not command:
            raise BackendUnavailable("stdio MCP 需要非空 command")
        self.command = [str(c) for c in command]
        self.env = {**os.environ, **(env or {})}
        self.cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._stderr_buf: list[str] = []
        self._stdout_lines: queue.Queue = queue.Queue()

    # -- 生命周期 --------------------------------------------------------------

    def start(self) -> None:
        exe = shutil.which(self.command[0]) if not os.path.isabs(self.command[0]) else self.command[0]
        if not exe:
            raise BackendUnavailable(f"找不到可执行文件: {self.command[0]}")
        try:
            self._proc = subprocess.Popen(  # noqa: S603
                [exe, *self.command[1:]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self.cwd,
                env=self.env,
            )
        except OSError as exc:
            raise BackendUnavailable(f"启动 stdio MCP 失败: {exc}") from exc

        self._stdout_lines = queue.Queue()
        threading.Thread(target=self._drain_stdout, args=(self._proc.stdout, self._stdout_lines), daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(self._proc.stderr,), daemon=True).start()

    @staticmethod
    def _drain_stdout(stream, lines) -> None:
        try:
            for line in stream:
                lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    def _drain_stderr(self, stream) -> None:
        try:
            for line in stream:
                self._stderr_buf.append(line.rstrip())
                if len(self._stderr_buf) > 400:
                    del self._stderr_buf[:200]
        except (OSError, ValueError):
            pass

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_buf[-20:])

    # -- 传输 ------------------------------------------------------------------

    def _send_line(self, message: Mapping[str, Any]) -> None:
        if not self._proc or not self._proc.stdin:
            raise BackendUnavailable("stdio MCP 进程未启动")
        try:
            self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise TransportError(f"写入 stdio 失败（进程可能已退出）: {exc}\n{self.stderr_tail}") from exc

    def _read_response(self, want_id: int) -> dict[str, Any]:
        assert self._proc and self._proc.stdout
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise TransportError(f"stdio MCP 进程已退出(code={self._proc.returncode})\n{self.stderr_tail}")
            try:
                line = self._stdout_lines.get(timeout=max(.001, deadline-time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                raise TransportError('stdio MCP output closed before response')
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # 子进程偶尔会往 stdout 打非 JSON 日志
            if msg.get("id") == want_id or ("id" not in msg and "method" in msg):
                if msg.get("id") == want_id:
                    return msg
        raise TransportError(f"stdio MCP 响应超时({self.timeout}s)\n{self.stderr_tail}")

    def _request(self, message: Mapping[str, Any]) -> dict[str, Any]:
        if self._proc is None:
            self.start()
        with self._lock:
            self._send_line(message)
            req_id = message.get("id")
            if req_id is None:
                return {}
            return self._read_response(int(req_id))

    def _notify(self, message: Mapping[str, Any]) -> None:
        if self._proc is None:
            self.start()
        with self._lock:
            self._send_line(message)

    def close(self) -> None:
        """关掉子进程与它的管道。

        顺序很重要：先把 Popen 引用从 self 上摘掉，再 terminate，最后显式 close
        三个流。否则 Popen 被 GC 时其 TextIOWrapper 会在解释器退出阶段析构，
        在 Windows 上抛 `OSError: [Errno 22] Invalid argument`
        （"Exception ignored in: <_io.TextIOWrapper ...>"）——
        不致命但会污染每一次 CLI 输出。
        """
        proc = self._proc
        if proc is None:
            return
        self._proc = None

        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
        except Exception:
            pass

        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


__all__ = [
    "MCPClient",
    "StreamableHTTPTransport",
    "StdioTransport",
    "ToolSpec",
    "unwrap_tool_result",
    "PROTOCOL_VERSIONS",
]


def make_client_from_config(section: Mapping[str, Any], *, name: str = CLIENT_NAME) -> MCPClient:
    """按 config 里的 MCP 段构造客户端。

    支持 `transport: http | stdio | auto`（auto = 先 stdio 后 http）。
    """
    transport = str(section.get("transport", "http")).lower()
    timeout = float(section.get("request_timeout_s") or 60)

    def _http() -> MCPClient:
        http_cfg = section.get("http") or {}
        base = str(http_cfg.get("base_url") or section.get("base_url") or "").rstrip("/")
        if not base:
            raise BackendUnavailable("未配置 unreal/computer-use 的 http base_url")
        endpoint = str(http_cfg.get("endpoint") or section.get("endpoint") or "/mcp")
        return StreamableHTTPTransport(
            base + endpoint,
            token=str(http_cfg.get("token") or section.get("token") or ""),
            name=name,
            timeout=timeout,
        )

    def _stdio() -> MCPClient:
        stdio_cfg = section.get("stdio") or {}
        command = [stdio_cfg.get("command") or ""] + list(stdio_cfg.get("args") or [])
        command = [c for c in command if c]
        if not command:
            raise BackendUnavailable("未配置 stdio command")
        return StdioTransport(
            command,
            env=stdio_cfg.get("env") or {},
            cwd=stdio_cfg.get("cwd") or None,
            name=name,
            timeout=timeout,
        )

    if transport == "http":
        return _http()
    if transport == "stdio":
        return _stdio()
    if transport == "auto":
        errors: list[str] = []
        for factory, label in ((_stdio, "stdio"), (_http, "http")):
            try:
                client = factory()
                client.list_tools(refresh=True)
                return client
            except Exception as exc:  # 探测失败就换下一个
                errors.append(f"{label}: {exc}")
        raise BackendUnavailable("auto 探测失败 -> " + " | ".join(errors))
    raise BackendUnavailable(f"未知 transport: {transport}")


def probe_backend(section: Mapping[str, Any], *, name: str = CLIENT_NAME) -> dict[str, Any]:
    """只做可用性探测，返回诊断字典（不抛异常）。"""
    result: dict[str, Any] = {"available": False, "tools": [], "error": None}
    try:
        client = make_client_from_config(section, name=name)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    try:
        info = client.initialize()
        tools = client.tool_names()
        result.update({"available": True, "tools": tools, "server_info": info.get("server_info"), "protocol": info.get("protocol_version")})
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            client.close()
        except Exception:
            pass
    return result
