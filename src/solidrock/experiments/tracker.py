"""实验追踪：每次回测/因子分析自动留痕（SQLite 单文件，零部署）.

这是 Agent 自主迭代的关键基建——没有它，Agent 无法回答"这次改动是否更好"::

    tracker = ExperimentTracker(data_dir / "experiments.db")
    run_id = tracker.log_run(kind="backtest", name="DualMA", config={...},
                             metrics={"sharpe": 1.2, ...}, artifacts_dir=...)
    tracker.compare([run_id_1, run_id_2])   # 并排对比指标

SQL 全部为静态语句 + 占位符参数，无任何字符串拼接。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from solidrock.agent.errors import ErrorCode, err

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    artifacts_dir TEXT,
    data_snapshot TEXT,
    seed INTEGER,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_experiments_kind ON experiments(kind);
CREATE INDEX IF NOT EXISTS idx_experiments_created ON experiments(created_at);
"""

_COLS = "id, created_at, kind, name, metrics_json, data_snapshot, artifacts_dir"


class ExperimentTracker:
    """SQLite 实验追踪器。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ 写入
    def log_run(
        self,
        *,
        kind: str,
        name: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
        artifacts_dir: str | None = None,
        data_snapshot: str | None = None,
        seed: int | None = None,
        notes: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """记录一次实验，返回 run_id（调用方可显式传入以对齐产物目录）。"""
        run_id = run_id or f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO experiments"
                " (id, created_at, kind, name, config_json, metrics_json,"
                "  artifacts_dir, data_snapshot, seed, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    datetime.now(timezone.utc).isoformat(),
                    kind,
                    name,
                    json.dumps(config, ensure_ascii=False, default=str),
                    json.dumps(metrics, ensure_ascii=False, default=str),
                    artifacts_dir,
                    data_snapshot,
                    seed,
                    notes,
                ),
            )
        return run_id

    # ------------------------------------------------------------------ 查询
    def list_runs(self, *, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """最近的实验列表（时间倒序）。"""
        with self._connect() as conn:
            if kind is not None:
                rows = conn.execute(
                    f"SELECT {_COLS} FROM experiments WHERE kind = ? ORDER BY created_at DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_COLS} FROM experiments ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for row in rows:
            record = dict(row)
            record["metrics"] = json.loads(record.pop("metrics_json"))
            out.append(record)
        return out

    def get_run(self, run_id: str) -> dict[str, Any]:
        """单条实验详情（含完整配置）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, created_at, kind, name, config_json, metrics_json,"
                " artifacts_dir, data_snapshot, seed, notes FROM experiments WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise err(
                ErrorCode.NO_DATA,
                f"实验 {run_id!r} 不存在",
                hint="用 list_runs() 查看已有实验",
            )
        record = dict(row)
        record["config"] = json.loads(record.pop("config_json"))
        record["metrics"] = json.loads(record.pop("metrics_json"))
        return record

    def compare(self, run_ids: list[str]) -> pd.DataFrame:
        """并排对比多个实验的核心指标（index=指标，columns=run_id）。"""
        if len(run_ids) < 2:
            raise err(
                ErrorCode.PARAM_INVALID,
                "compare 至少需要 2 个实验",
                hint="用 list_runs() 找到要对比的 run_id",
            )
        data: dict[str, dict[str, Any]] = {}
        for run_id in run_ids:
            run = self.get_run(run_id)
            data[run_id] = {"strategy": run["name"], **run["metrics"]}
        return pd.DataFrame(data)
