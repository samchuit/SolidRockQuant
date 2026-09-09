"""策略沙箱：以子进程隔离执行用户（或 Agent 生成）的策略回测.

为什么需要：策略代码可能死循环、直接 ``sys.exit()``、抛出未捕获异常——
在主进程内运行会拖垮调用方（尤其是常驻的 MCP Server）。沙箱用独立子进程
执行，配合超时击杀与错误回传，坏策略伤不到宿主。

隔离范围（v0.3）：
- **进程隔离**：独立解释器进程，崩溃/退出不影响宿主；
- **超时击杀**：超过 ``timeout`` 秒强制终止；
- 内存/CPU 限制因 Windows 支持不完整而暂缓（POSIX 可用 resource，后续版本提供）。

结果经 JSON 文件回传（与 MCP 信封同构），适合 Agent 直接消费。
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from solidrock.agent.errors import ErrorCode


def run_sandboxed_backtest(
    strategy_file: str,
    *,
    start: str,
    end: str,
    data_dir: str | Path,
    params: dict[str, Any] | None = None,
    benchmark: str | None = "000300.SH",
    execution: str = "next_open",
    initial_cash: float = 1_000_000.0,
    name: str | None = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """在沙箱子进程中运行回测，返回结果信封（含 metrics/artifacts）.

    ``timeout``：沙箱最长运行秒数，超时强杀并返回 ``TIMEOUT`` 错误。
    """
    run_dir = Path(tempfile.gettempdir()) / "solidrock-sandbox" / uuid.uuid4().hex[:12]
    run_dir.mkdir(parents=True, exist_ok=True)
    payload_path = run_dir / "payload.json"
    result_path = run_dir / "result.json"
    payload = {
        "strategy_file": str(Path(strategy_file).resolve()),
        "data_dir": str(Path(data_dir).resolve()),
        "config": {
            "start": start,
            "end": end,
            "benchmark": benchmark,
            "execution": execution,
            "initial_cash": initial_cash,
            "name": name,
            "log_experiment": True,
        },
        "params": params or {},
        "result_path": str(result_path),
    }
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "solidrock.agent.sandbox", str(payload_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "data": None,
            "error": {
                "code": ErrorCode.TIMEOUT.value,
                "message": f"沙箱执行超过 {timeout}s 被强制终止",
                "hint": "检查策略是否死循环（on_signal 中不能有无限循环）；或缩小回测区间/降低频率后重试",
            },
        }

    if result_path.exists():
        envelope = json.loads(result_path.read_text(encoding="utf-8"))
        if proc.returncode != 0:
            envelope.setdefault("error", {})["worker_exit"] = proc.returncode
            envelope["error"]["message"] += f"（worker 退出码 {proc.returncode}）"
        return envelope

    # 没有结果文件 = worker 提前崩溃
    stderr_tail = (proc.stderr or "")[-800:]
    code = ErrorCode.STRATEGY_INVALID if proc.returncode == 1 else ErrorCode.INTERNAL_ERROR
    return {
        "status": "error",
        "data": None,
        "error": {
            "code": code.value,
            "message": "沙箱 worker 崩溃，未产出结果" if proc.returncode != 1 else "策略加载/执行失败",
            "hint": "stderr 末尾附于 details；常见原因：策略文件语法错误、内存不足",
            "details": {"returncode": proc.returncode, "stderr_tail": stderr_tail},
        },
    }


def sandbox_worker_main(payload_path: str) -> int:
    """沙箱 worker 入口（子进程）：执行回测并把信封写入 result_path."""
    payload = json.loads(Path(payload_path).read_text(encoding="utf-8"))
    result_path = Path(payload["result_path"])
    envelope: dict[str, Any]
    try:
        from solidrock.backtest import BacktestConfig, BacktestEngine
        from solidrock.data.store import DataStore
        from solidrock.strategy.loader import load_strategy_class

        cfg_kwargs = dict(payload["config"])
        store = DataStore(payload["data_dir"])
        strategy_cls = load_strategy_class(payload["strategy_file"])
        config = BacktestConfig(**cfg_kwargs)
        result = BacktestEngine(strategy_cls(**payload.get("params", {})), config, store).run()
        envelope = {
            "status": "ok",
            "data": {
                "run_id": result.run_id,
                "strategy": result.strategy_name,
                "metrics": result.metrics,
                "rejection_counts": (
                    result.rejections["code"].value_counts().to_dict() if not result.rejections.empty else {}
                ),
                "data_snapshot": result.data_snapshot,
                "final_positions": result.final_positions,
            },
            "artifacts": (
                [str(result.artifacts_dir / f) for f in ("report.md", "result.json", "trades.csv", "nav.csv")]
                if result.artifacts_dir is not None
                else []
            ),
            "error": None,
        }
        # result.json 已由引擎自动写入 artifacts；信封另存给父进程
        result_path.write_text(json.dumps(envelope, ensure_ascii=False, default=str), encoding="utf-8")
        return 0
    except Exception as exc:
        from solidrock.agent.errors import SolidRockError

        if isinstance(exc, SolidRockError):
            error = exc.to_dict()
        else:
            error = {
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": f"{type(exc).__name__}: {exc}",
            }
        envelope = {"status": "error", "data": None, "artifacts": [], "error": error}
        with contextlib.suppress(OSError):
            result_path.write_text(json.dumps(envelope, ensure_ascii=False, default=str), encoding="utf-8")
        return 1


if __name__ == "__main__":
    sys.exit(sandbox_worker_main(sys.argv[1]))
