"""本地数据仓库：Parquet 落盘 + DuckDB 读取 + 版本快照.

目录结构::

    {root}/
    ├── bars/1d/{symbol}.parquet      # 每符号一个文件，全量历史
    ├── calendar.parquet              # 交易日历
    ├── instruments/{asset_type}.parquet
    ├── meta/provenance.json          # 每个符号的数据来源与更新时间
    └── snapshots/{tag}/...           # 不可变快照（只读副本，用于实验复现）

设计要点：
- **增量合并**：``update_bars`` 按 (symbol, date) 去重，新数据优先；
  ``pre_close``/``adj_factor`` 等列若新值为 NaN 则保留旧值（增量窗口首行的
  昨收/因子缺口由历史数据补齐）；
- **复权在读取时计算**：``load_bars(adjust="hfq"|"qfq")``，落库永远是原始价；
- **快照只读**：带 ``snapshot`` 打开的实例禁止写操作，保证实验可复现；
- **DuckDB 仅做参数化读取**：库内不提供原生 SQL 执行入口；需要任意 SQL 分析时，
  直接用 duckdb CLI 打开 ``{root}/bars/1d/*.parquet`` 即可（零部署）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd

from solidrock.agent.errors import ErrorCode, err
from solidrock.data.schema import (
    ADJUSTED_PRICE_COLUMNS,
    COALESCE_COLUMNS,
    DAILY_BAR_COLUMN_NAMES,
    empty_bars_frame,
    validate_bars,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_SNAPSHOT_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
AdjustKind = Literal["raw", "hfq", "qfq"]


class DataStore:
    """本地数据仓库。同一目录多实例时假定单写者。"""

    def __init__(self, root: str | Path, *, snapshot: str | None = None) -> None:
        self.root = Path(root).expanduser().resolve()
        self.snapshot = snapshot
        if snapshot is not None and not self._base_dir.exists():
            raise err(
                ErrorCode.SNAPSHOT_NOT_FOUND,
                f"快照 {snapshot!r} 不存在于 {self.root}",
                hint=f"先调用 store.create_snapshot({snapshot!r})，或用 DataStore(root).list_snapshots() 查看已有快照",
            )

    # ------------------------------------------------------------------ 路径
    @property
    def _base_dir(self) -> Path:
        """实际读取/写入的根目录：快照模式指向 snapshots/{tag}。"""
        return self.root / "snapshots" / self.snapshot if self.snapshot else self.root

    def bars_dir(self, freq: str = "1d") -> Path:
        return self._base_dir / "bars" / freq

    def _bar_file(self, symbol: str, freq: str = "1d") -> Path:
        return self.bars_dir(freq) / f"{symbol}.parquet"

    @property
    def _provenance_file(self) -> Path:
        return self._base_dir / "meta" / "provenance.json"

    # ------------------------------------------------------------------ 写入
    def _ensure_writable(self) -> None:
        if self.snapshot is not None:
            raise err(
                ErrorCode.SNAPSHOT_READ_ONLY,
                f"快照 {self.snapshot!r} 是只读副本，禁止写入",
                hint="写操作请使用不带 snapshot 参数的 DataStore(root)",
            )

    def save_bars(self, df: pd.DataFrame, *, freq: str = "1d", source: str | None = None) -> dict[str, int]:
        """整表落库（按符号整文件覆盖）。返回 {symbol: 行数}。"""
        self._ensure_writable()
        df = validate_bars(df, freq=freq)
        counts: dict[str, int] = {}
        for symbol, part in df.groupby("symbol", sort=False):
            path = self._bar_file(str(symbol), freq)
            _write_parquet_atomic(part.reset_index(drop=True), path)
            counts[str(symbol)] = len(part)
        self._flush_provenance(counts, freq, source)
        return counts

    def update_bars(self, df: pd.DataFrame, *, freq: str = "1d", source: str | None = None) -> dict[str, int]:
        """增量合并落库：按 (symbol, date) 去重、新值优先；NaN 缺口用旧值补齐.

        幂等：同一批数据重复更新结果不变。返回 {symbol: 合并后总行数}。
        """
        self._ensure_writable()
        df = validate_bars(df, freq=freq)
        totals: dict[str, int] = {}
        for symbol, new_part in df.groupby("symbol", sort=False):
            symbol = str(symbol)
            path = self._bar_file(symbol, freq)
            if not path.exists():
                merged = new_part.reset_index(drop=True)
            else:
                merged = _merge_symbol(pd.read_parquet(path), new_part)
            _write_parquet_atomic(merged, path)
            totals[symbol] = len(merged)
        self._flush_provenance(totals, freq, source)
        return totals

    def _flush_provenance(self, counts: dict[str, int], freq: str, source: str | None) -> None:
        self._provenance_file.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {}
        if self._provenance_file.exists():
            meta = json.loads(self._provenance_file.read_text(encoding="utf-8"))
        now = pd.Timestamp.now().isoformat()
        for symbol, rows in counts.items():
            meta.setdefault(freq, {})[symbol] = {"source": source, "rows": rows, "updated_at": now}
        self._provenance_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def provenance(self, freq: str = "1d") -> dict[str, dict[str, Any]]:
        """各符号的数据来源与最近更新时间。"""
        if not self._provenance_file.exists():
            return {}
        meta = json.loads(self._provenance_file.read_text(encoding="utf-8"))
        return meta.get(freq, {})

    # ------------------------------------------------------------------ 读取
    def symbols(self, freq: str = "1d") -> list[str]:
        """已缓存的符号列表。"""
        d = self.bars_dir(freq)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.parquet"))

    def last_date(self, symbol: str, freq: str = "1d") -> pd.Timestamp | None:
        """某符号本地最新交易日；无数据返回 None。"""
        path = self._bar_file(symbol, freq)
        if not path.exists():
            return None
        df = pd.read_parquet(path, columns=["date"])
        return None if df.empty else pd.Timestamp(df["date"].max())

    def row_count(self, symbol: str | None = None, freq: str = "1d") -> int:
        if symbol is not None:
            path = self._bar_file(symbol, freq)
            return 0 if not path.exists() else len(pd.read_parquet(path, columns=["date"]))
        d = self.bars_dir(freq)
        if not d.exists():
            return 0
        return sum(len(pd.read_parquet(p, columns=["date"])) for p in d.glob("*.parquet"))

    def load_bars(
        self,
        symbols: str | Sequence[str] | None = None,
        *,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
        freq: str = "1d",
        columns: Iterable[str] | None = None,
        adjust: AdjustKind = "raw",
    ) -> pd.DataFrame:
        """读取本地数据（DuckDB 参数化扫描 parquet，支持复权计算）.

        - ``adjust="raw"``：原始价（默认）；
        - ``adjust="hfq"``：后复权，``price * adj_factor``（因子 NaN 按 1 处理）；
        - ``adjust="qfq"``：前复权，``price * adj_factor / max(adj_factor)``
          （max 取该符号**全量历史**的最新因子，而非查询窗口内的）。
        """
        if adjust not in ("raw", "hfq", "qfq"):
            raise err(ErrorCode.PARAM_INVALID, f"adjust 仅支持 raw/hfq/qfq，收到 {adjust!r}")

        files = self._parquet_files(symbols, freq)
        if not files:
            return empty_bars_frame()

        import duckdb

        con = duckdb.connect()
        try:
            df = con.execute(
                "SELECT * FROM read_parquet(?, union_by_name=true)",
                [[str(p) for p in files]],
            ).fetchdf()
        finally:
            con.close()

        df = validate_bars(df, freq=freq)
        if start is not None:
            df = df[df["date"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["date"] <= pd.Timestamp(end)]
        df = df.reset_index(drop=True)

        if adjust != "raw":
            df = _apply_adjustment(df, adjust, self, freq)

        if columns is not None:
            cols = list(dict.fromkeys(columns))
            missing = [c for c in cols if c not in df.columns]
            if missing:
                raise err(
                    ErrorCode.PARAM_INVALID,
                    f"请求的列不存在：{missing}",
                    hint=f"可用列：{list(DAILY_BAR_COLUMN_NAMES)}",
                )
            df = df[cols]
        return df.reset_index(drop=True)

    def _parquet_files(self, symbols: str | Sequence[str] | None, freq: str) -> list[Path]:
        d = self.bars_dir(freq)
        files = sorted(d.glob("*.parquet")) if d.exists() else []
        if symbols is not None:
            wanted = {symbols} if isinstance(symbols, str) else set(symbols)
            files = [p for p in files if p.stem in wanted]
        return files

    def _latest_adj_factor(self, symbols: Sequence[str], freq: str) -> pd.Series:
        """各符号全量历史中最新的复权因子（文件按日期有序，取最后非 NaN 值）。"""
        out: dict[str, float] = {}
        for symbol in symbols:
            path = self._bar_file(str(symbol), freq)
            if not path.exists():
                continue
            f = pd.read_parquet(path, columns=["adj_factor"])["adj_factor"].dropna()
            if not f.empty:
                out[str(symbol)] = float(f.iloc[-1])
        return pd.Series(out, dtype="float64")

    # ------------------------------------------------------------------ 日历
    def save_calendar(self, df: pd.DataFrame) -> int:
        """保存交易日历（单列 ``date``，datetime64）。返回天数。"""
        self._ensure_writable()
        if "date" not in df.columns:
            raise err(
                ErrorCode.DATA_FORMAT_INVALID,
                "交易日历需要单列 date",
                hint="TradingCalendar.update 负责从数据源取得标准格式",
            )
        dates = pd.to_datetime(df["date"]).dt.normalize()
        out = pd.DataFrame({"date": dates}).drop_duplicates().sort_values("date").reset_index(drop=True)
        _write_parquet_atomic(out, self._base_dir / "calendar.parquet")
        return len(out)

    def load_calendar(self) -> pd.DataFrame | None:
        """读取交易日历；未缓存返回 None。"""
        path = self._base_dir / "calendar.parquet"
        if not path.exists():
            return None
        return pd.read_parquet(path)

    # ------------------------------------------------------------- 标的列表
    def save_instruments(self, df: pd.DataFrame, asset_type: str = "stock") -> int:
        """保存标的列表（列 ``symbol, name``，可含附加列）。返回行数。"""
        self._ensure_writable()
        if "symbol" not in df.columns:
            raise err(
                ErrorCode.DATA_FORMAT_INVALID,
                "instruments 需要至少包含 symbol 列",
                hint="标准结构：symbol, name",
            )
        out = df.drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)
        path = self._base_dir / "instruments" / f"{asset_type}.parquet"
        _write_parquet_atomic(out, path)
        return len(out)

    def load_instruments(self, asset_type: str = "stock") -> pd.DataFrame | None:
        """读取标的列表；未缓存返回 None。"""
        path = self._base_dir / "instruments" / f"{asset_type}.parquet"
        if not path.exists():
            return None
        return pd.read_parquet(path)

    def search_instruments(self, query: str, *, asset_type: str = "stock", limit: int = 20) -> pd.DataFrame:
        """按符号或名称模糊搜索已缓存标的（大小写不敏感）。"""
        df = self.load_instruments(asset_type)
        if df is None or df.empty:
            return pd.DataFrame(columns=["symbol", "name"])
        q = query.strip().upper()
        mask = df["symbol"].str.upper().str.contains(q, regex=False, na=False)
        if "name" in df.columns:
            mask |= df["name"].astype(str).str.contains(query.strip(), regex=False, na=False)
        return df[mask].head(limit).reset_index(drop=True)

    # ------------------------------------------------------------------ 快照
    def create_snapshot(self, tag: str) -> DataStore:
        """创建不可变快照（硬链接优先，跨设备回退为拷贝）.

        快照覆盖 bars / calendar / instruments / meta。返回指向快照的只读实例。
        """
        self._ensure_writable()
        if _SNAPSHOT_TAG_RE.fullmatch(tag) is None:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"快照名 {tag!r} 非法",
                hint="快照名只能包含字母、数字、点、下划线、连字符，且以字母或数字开头",
            )
        dst = self.root / "snapshots" / tag
        if dst.exists():
            raise err(
                ErrorCode.SNAPSHOT_EXISTS,
                f"快照 {tag!r} 已存在",
                hint="快照不可变；如需新版本请换名字，或先 delete_snapshot",
            )
        dst.mkdir(parents=True)
        for rel in ("bars", "calendar.parquet", "instruments", "meta"):
            s = self.root / rel
            if s.exists():
                _copy_path(s, dst / rel)
        return DataStore(self.root, snapshot=tag)

    def list_snapshots(self) -> list[str]:
        d = self.root / "snapshots"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def delete_snapshot(self, tag: str) -> None:
        self._ensure_writable()
        d = self.root / "snapshots" / tag
        if not d.exists():
            raise err(
                ErrorCode.SNAPSHOT_NOT_FOUND,
                f"快照 {tag!r} 不存在",
                hint=f"已有快照：{self.list_snapshots() or '（无）'}",
            )
        shutil.rmtree(d)


def _write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """原子写入 parquet：先写临时文件再替换.

    必须用 os.replace 而非原地覆写——快照通过硬链接共享旧 inode，
    原地覆写会污染只读快照；replace 换目录项则让旧 inode（快照侧）保持不变。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _apply_adjustment(df: pd.DataFrame, adjust: AdjustKind, store: DataStore, freq: str) -> pd.DataFrame:
    """对价格列应用复权因子。因子 NaN 按 1.0 处理（保持原价）。"""
    factor = df["adj_factor"].fillna(1.0)
    if adjust == "hfq":
        scale = factor
    else:
        # qfq 归一基准 = 该符号全量历史的最新因子（而非查询窗口内）
        latest = store._latest_adj_factor(df["symbol"].unique().tolist(), freq)
        base = df["symbol"].map(latest).fillna(factor).replace(0, 1.0)
        scale = (factor / base).fillna(1.0)
    out = df.copy()
    for col in ADJUSTED_PRICE_COLUMNS:
        if col in out.columns:
            out[col] = out[col] * scale
    return out


def _merge_symbol(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并同一符号的新旧数据：新值优先；COALESCE 列新值为 NaN 时保留旧值."""
    old_idx = old.set_index(["symbol", "date"])
    new_idx = new.set_index(["symbol", "date"])
    old_idx = old_idx[~old_idx.index.duplicated(keep="last")]
    new_idx = new_idx[~new_idx.index.duplicated(keep="last")]
    for col in COALESCE_COLUMNS:
        if col in new_idx.columns and col in old_idx.columns:
            new_idx[col] = new_idx[col].combine_first(old_idx[col])
    old_rest = old_idx.drop(columns=[c for c in COALESCE_COLUMNS if c in old_idx.columns])
    combined = pd.concat([old_rest, new_idx]).reset_index()
    combined = combined.sort_values(["symbol", "date"], kind="stable")
    return combined.drop_duplicates(subset=["symbol", "date"], keep="last").reset_index(drop=True)


def _copy_path(src: Path, dst: Path) -> None:
    """复制单个路径（文件或目录树）；同卷优先硬链接，失败回退真实拷贝."""
    if src.is_dir():
        _copy_tree(src, dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _copy_tree(src: Path, dst: Path) -> None:
    """递归复制目录；同卷优先硬链接（快照近零拷贝），失败回退真实拷贝."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _copy_tree(item, target)
        else:
            try:
                os.link(item, target)
            except OSError:
                shutil.copy2(item, target)
