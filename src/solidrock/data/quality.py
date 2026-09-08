"""数据体检：对本地仓库做健康检查，输出问题清单（Agent 与人工共用）.

检查项：
- **覆盖**：行数、起止日期、与交易日历对照的缺失交易日；
- **完整性**：关键列（OHLC/成交量）缺失；
- **逻辑**：OHLC 越界（写入时已挡，这里复核）、复权因子缺失占比；
- **异常**：单日涨跌幅超阈值（疑似坏点）、零成交连续段（疑似停牌未标注）。

体检是诊断性的（返回问题清单），不修改数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pandas as pd

from solidrock.agent.errors import ErrorCode, err

if TYPE_CHECKING:
    from solidrock.data.store import DataStore


@dataclass
class SymbolHealth:
    symbol: str
    n_rows: int = 0
    first: str | None = None
    last: str | None = None
    missing_calendar_days: int = 0  # 交易日历有、本地没有的交易日数
    ohlc_violations: int = 0
    nan_close: int = 0
    factor_nan_ratio: float = 0.0  # 复权因子缺失占比（1.0 = 完全没有因子）
    zero_volume_streak_max: int = 0
    big_jumps: list[dict] = field(default_factory=list)  # 单日涨跌幅超阈值的样本

    @property
    def ok(self) -> bool:
        return not (self.missing_calendar_days or self.ohlc_violations or self.nan_close or self.big_jumps)


@dataclass
class HealthReport:
    checked: int
    issues: list[SymbolHealth]
    healthy: int

    @property
    def ok(self) -> bool:
        return self.healthy == self.checked

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "healthy": self.healthy,
            "symbols": [
                {
                    "symbol": h.symbol,
                    "ok": h.ok,
                    "n_rows": h.n_rows,
                    "range": [h.first, h.last],
                    "missing_calendar_days": h.missing_calendar_days,
                    "ohlc_violations": h.ohlc_violations,
                    "nan_close": h.nan_close,
                    "factor_nan_ratio": round(h.factor_nan_ratio, 4),
                    "zero_volume_streak_max": h.zero_volume_streak_max,
                    "big_jumps": h.big_jumps,
                }
                for h in self.issues
            ],
        }


def check_store(
    store: DataStore,
    symbols: list[str] | None = None,
    *,
    freq: str = "1d",
    jump_threshold: float = 0.2,
    max_jump_samples: int = 3,
) -> HealthReport:
    """对本地仓库做体检；``jump_threshold`` 为单日涨跌幅告警阈值（0.2 = ±20%）."""
    targets = symbols if symbols is not None else store.symbols(freq)
    if not targets:
        raise err(
            ErrorCode.NO_DATA,
            "本地没有可检查的数据",
            hint="先执行 srq data update 拉取行情",
        )
    cal = store.load_calendar()
    cal_days = set(pd.to_datetime(cal["date"])) if cal is not None and not cal.empty else None

    issues: list[SymbolHealth] = []
    healthy = 0
    for symbol in targets:
        h = SymbolHealth(symbol=symbol)
        path = store.bars_dir(freq) / f"{symbol}.parquet"
        if not path.exists():
            issues.append(h)
            continue
        df = pd.read_parquet(path).sort_values("date").reset_index(drop=True)
        h.n_rows = len(df)
        if df.empty:
            issues.append(h)
            continue
        h.first = str(df["date"].min().date())
        h.last = str(df["date"].max().date())
        h.nan_close = int(df["close"].isna().sum())

        if cal_days is not None:
            dates = set(pd.to_datetime(df["date"]))
            lo, hi = min(dates), max(dates)
            expected = {d for d in cal_days if lo <= d <= hi}
            h.missing_calendar_days = len(expected - dates)

        ohlc_bad = (
            (df["high"] + 1e-6 < df["low"])
            | (df[["open", "close"]].max(axis=1) > df["high"] + 1e-6)
            | (df[["open", "close"]].min(axis=1) < df["low"] - 1e-6)
        )
        h.ohlc_violations = int(ohlc_bad.sum())

        if "adj_factor" in df.columns:
            h.factor_nan_ratio = float(df["adj_factor"].isna().mean())
        if "volume" in df.columns:
            zero = (df["volume"] <= 0).astype(int)
            h.zero_volume_streak_max = int(zero.groupby((zero != zero.shift()).cumsum()).sum().max() or 0)

        ret = df["close"].pct_change().abs()
        jumps = df.loc[ret > jump_threshold, ["date", "close"]]
        for _, row in jumps.head(max_jump_samples).iterrows():
            h.big_jumps.append({"date": str(row["date"].date()), "close": float(row["close"])})

        if h.ok:
            healthy += 1
        issues.append(h)
    return HealthReport(checked=len(targets), issues=issues, healthy=healthy)


def render_health_markdown(report: HealthReport) -> str:
    lines = [
        "# 数据体检报告",
        "",
        f"- 检查 {report.checked} 个符号，健康 {report.healthy} 个，异常 {report.checked - report.healthy} 个",
        "",
    ]
    bad = [h for h in report.issues if not h.ok]
    if not bad:
        lines.append("全部通过 ✅")
        return "\n".join(lines)
    lines.append("| 符号 | 行数 | 区间 | 缺失交易日 | OHLC异常 | NaN收盘 | 因子缺失 | 零成交连段 | 大幅波动 |")
    lines.append("|------|------|------|------------|----------|---------|----------|------------|----------|")
    for h in bad:
        jumps = ", ".join(j["date"] for j in h.big_jumps) or "-"
        lines.append(
            f"| {h.symbol} | {h.n_rows} | {h.first}~{h.last} | {h.missing_calendar_days} "
            f"| {h.ohlc_violations} | {h.nan_close} | {h.factor_nan_ratio * 100:.0f}% "
            f"| {h.zero_volume_streak_max} | {jumps} |"
        )
    lines.append("")
    lines.append("> 大幅波动可能来自换月（主连）或真实行情，请人工确认；因子缺失会导致前复权不可用。")
    return "\n".join(lines)
