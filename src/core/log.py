"""结构化日志。

两路输出，职责不同：

1. **人类可读日志**  logs/uha-YYYYMMDD.log
   给人和 AI 看"现在在干什么"。

2. **结构化执行记录**  logs/runs/run-<ts>.jsonl
   每次工具调用一行 JSON，字段固定为：

       task / selected_method / reason / tool / arguments /
       result / duration_ms / verification / fallback / attempt

   有了这个文件才能回答"哪种方法在这台机器上最可靠"——
   这正是 Router 后续要做统计学习的输入（第一阶段只做统计展示，不训练模型）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping

_DATE_FMT = "%Y-%m-%dT%H:%M:%S"

_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        ts = time.strftime(_DATE_FMT, time.localtime(record.created))
        return f"{ts} [{record.levelname:<7}] {record.getMessage()}"


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


class _JsonlSink:
    """线程安全的 JSONL 追加写出器。"""

    def __init__(self, path: Path):
        self.path = path
        _ensure_dir(path.parent)
        self._lock = threading.Lock()

    def write(self, payload: Mapping[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class RunLogger:
    """一次运行（run）的记录器。

        log = RunLogger(log_dir=cfg.path("logs"), run_name="demo1",
                        console=True, level="INFO")
        with log.step("actor_move", method="UNREAL_MCP", reason="known actor") as rec:
            rec.set(tool="actor.set_location", arguments={...})
            ... do work ...
            rec.set(result="success", verification={"ok": True})
    """

    def __init__(self, log_dir: Path, *, run_name: str = "run", console: bool = True, level: str = "INFO"):
        self.log_dir = Path(log_dir)
        _ensure_dir(self.log_dir)
        _ensure_dir(self.log_dir / "runs")

        self.run_name = run_name
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_id = f"{run_name}-{stamp}"
        self.jsonl = _JsonlSink(self.log_dir / "runs" / f"{self.run_id}.jsonl")

        self._logger = logging.getLogger(f"uha.{self.run_id}")
        self._logger.setLevel(_LEVELS.get(level.upper(), logging.INFO))
        self._logger.propagate = False
        self._logger.handlers.clear()

        if console:
            sh = logging.StreamHandler()
            sh.setFormatter(_ConsoleFormatter())
            self._logger.addHandler(sh)

        fh = logging.FileHandler(self.log_dir / f"uha-{time.strftime('%Y%m%d')}.log", encoding="utf-8")
        fh.setFormatter(_ConsoleFormatter())
        self._logger.addHandler(fh)

        self.info("=" * 78)
        self.info(f"run start | id={self.run_id}")
        self.info(f"  log dir : {self.log_dir}")
        self.info(f"  jsonl   : {self.jsonl.path}")
        self.info("=" * 78)

    # -- 基础日志 -------------------------------------------------------------

    def info(self, msg: str, *a: Any) -> None:
        self._logger.info(msg, *a)

    def warn(self, msg: str, *a: Any) -> None:
        self._logger.warning(msg, *a)

    def error(self, msg: str, *a: Any) -> None:
        self._logger.error(msg, *a)

    def debug(self, msg: str, *a: Any) -> None:
        self._logger.debug(msg, *a)

    # -- 结构化步骤 -----------------------------------------------------------

    def step(self, task: str, *, method: str, reason: str, fallback: str | None = None) -> "_StepRecord":
        return _StepRecord(self, task=task, method=method, reason=reason, fallback=fallback)

    def event(self, kind: str, **fields: Any) -> None:
        """非工具调用类事件（路由决策、验证结果、降级等）。"""
        self.jsonl.write({"ts": time.time(), "run": self.run_id, "kind": kind, **fields})

    def summarize(self) -> dict[str, Any]:
        """统计本 run 各方法的成功/失败次数 —— 直接回答"哪种方法最可靠"。"""
        stats: dict[str, dict[str, int]] = {}
        path = self.jsonl.path
        if not path.exists():
            return stats
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "step":
                    continue
                m = rec.get("selected_method", "UNKNOWN")
                slot = stats.setdefault(m, {"ok": 0, "failed": 0, "verified": 0})
                if rec.get("result") == "success":
                    slot["ok"] += 1
                else:
                    slot["failed"] += 1
                if (rec.get("verification") or {}).get("ok"):
                    slot["verified"] += 1
        return stats


class _StepRecord:
    """一次工具调用的上下文管理器，退出时自动落盘一行 JSONL。"""

    def __init__(self, logger: RunLogger, *, task: str, method: str, reason: str, fallback: str | None):
        self._log = logger
        self.payload: dict[str, Any] = {
            "kind": "step",
            "ts": time.time(),
            "run": logger.run_id,
            "task": task,
            "selected_method": method,
            "reason": reason,
            "fallback": fallback,
            "tool": None,
            "arguments": {},
            "result": "unknown",
            "duration_ms": None,
            "verification": None,
            "metadata": {},
        }
        self._t0 = 0.0

    # -- 填充 ----------------------------------------------------------------

    def set(self, **fields: Any) -> "_StepRecord":
        self.payload.update(fields)
        return self

    def meta(self, **fields: Any) -> "_StepRecord":
        self.payload["metadata"].update(fields)
        return self

    # -- 上下文协议 -----------------------------------------------------------

    def __enter__(self) -> "_StepRecord":
        self._t0 = time.perf_counter()
        self._log.info(
            f"-> task={self.payload['task']!r} method={self.payload['selected_method']} "
            f"reason={self.payload['reason']!r}"
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.payload["duration_ms"] = round((time.perf_counter() - self._t0) * 1000, 2)
        if exc_type is not None:
            self.payload["result"] = "error"
            self.payload["error"] = {"type": exc_type.__name__, "message": str(exc)}
            self._log.error(f"   FAILED {exc_type.__name__}: {exc}")
        ok = self.payload.get("result") == "success"
        self._log.info(
            f"   {self.payload['result']:<8} {self.payload['duration_ms']:>8.1f} ms"
            + (f"  verify={self.payload['verification']}" if self.payload.get("verification") else "")
        )
        self._log.jsonl.write(self.payload)
        return False  # 不吞异常，让上层 fallback 逻辑处理

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StepRecord {self.payload['task']!r} {self.payload['selected_method']}>"


__all__ = ["RunLogger"]


def default_log_dir() -> Path:
    return Path(os.getenv("UHA_LOG_DIR", str(Path(__file__).resolve().parents[2] / "logs")))
