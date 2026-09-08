"""DataStore 测试：落盘、增量合并、复权、快照."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.data.store import DataStore
from tests.conftest import make_bars


class TestSaveLoad:
    def test_roundtrip(self, store: DataStore) -> None:
        df = make_bars()
        counts = store.save_bars(df, source="test")
        assert counts == {"000001.SZ": 5}
        out = store.load_bars("000001.SZ")
        assert len(out) == 5
        assert out["close"].tolist() == df["close"].tolist()

    def test_symbols_and_last_date(self, store: DataStore) -> None:
        store.save_bars(make_bars("000001.SZ", periods=3))
        store.save_bars(make_bars("600519.SH", start="2023-05-04", periods=4))
        assert store.symbols() == ["000001.SZ", "600519.SH"]
        assert store.last_date("600519.SH") == pd.Timestamp("2023-05-09")
        assert store.last_date("300750.SZ") is None

    def test_load_multiple_symbols_filtered(self, store: DataStore) -> None:
        store.save_bars(make_bars("000001.SZ"))
        store.save_bars(make_bars("600519.SH"))
        out = store.load_bars(["000001.SZ"])
        assert set(out["symbol"]) == {"000001.SZ"}

    def test_load_date_range(self, store: DataStore) -> None:
        store.save_bars(make_bars(periods=5))
        out = store.load_bars("000001.SZ", start="2024-01-03", end="2024-01-04")
        assert len(out) == 2

    def test_load_columns_filter(self, store: DataStore) -> None:
        store.save_bars(make_bars())
        out = store.load_bars("000001.SZ", columns=["symbol", "date", "close"])
        assert list(out.columns) == ["symbol", "date", "close"]

    def test_load_missing_column_error(self, store: DataStore) -> None:
        store.save_bars(make_bars())
        with pytest.raises(SolidRockError) as exc_info:
            store.load_bars("000001.SZ", columns=["nope"])
        assert exc_info.value.code is ErrorCode.PARAM_INVALID

    def test_load_empty_store(self, store: DataStore) -> None:
        out = store.load_bars("000001.SZ")
        assert out.empty

    def test_provenance(self, store: DataStore) -> None:
        store.save_bars(make_bars(), source="unittest")
        prov = store.provenance()["000001.SZ"]
        assert prov["source"] == "unittest"
        assert prov["rows"] == 5


class TestUpdate:
    def test_idempotent(self, store: DataStore) -> None:
        df = make_bars()
        store.update_bars(df, source="t1")
        totals = store.update_bars(df, source="t1")
        assert totals["000001.SZ"] == 5
        assert store.load_bars("000001.SZ").shape[0] == 5

    def test_append_new_dates(self, store: DataStore) -> None:
        store.update_bars(make_bars(periods=3))
        new = make_bars(start="2024-01-08", periods=3)
        totals = store.update_bars(new)
        assert totals["000001.SZ"] == 6
        out = store.load_bars("000001.SZ")
        assert out["date"].is_monotonic_increasing

    def test_nan_coalesce_from_old(self, store: DataStore) -> None:
        """增量窗口首行 pre_close 为 NaN 时，应保留旧值而不是覆盖成 NaN。"""
        old = make_bars(periods=3)
        old.loc[old.index[-1], "pre_close"] = 9.99  # 旧数据里有值的昨收
        store.update_bars(old)
        new = old.tail(1).copy()
        new["pre_close"] = np.nan
        store.update_bars(new)
        out = store.load_bars("000001.SZ")
        assert out["pre_close"].iloc[-1] == 9.99

    def test_new_value_wins(self, store: DataStore) -> None:
        store.update_bars(make_bars(periods=3))
        new = make_bars(periods=3, base=11.0)  # OHLC 自洽的另一组数据
        store.update_bars(new)
        out = store.load_bars("000001.SZ")
        assert out["close"].iloc[0] == pytest.approx(new["close"].iloc[0])


class TestAdjust:
    def test_hfq(self, store: DataStore) -> None:
        store.save_bars(make_bars(adj_factor=2.0))
        raw = store.load_bars("000001.SZ", adjust="raw")
        hfq = store.load_bars("000001.SZ", adjust="hfq")
        assert hfq["close"].iloc[0] == pytest.approx(raw["close"].iloc[0] * 2.0)

    def test_qfq_uses_latest_factor(self, store: DataStore) -> None:
        # 因子随时间递增：1.0 -> 1.1 -> ... ；qfq 以最新因子归一
        df = make_bars(periods=5)
        df["adj_factor"] = np.linspace(1.0, 2.0, 5)
        store.save_bars(df)
        qfq = store.load_bars("000001.SZ", adjust="qfq")
        raw = store.load_bars("000001.SZ", adjust="raw")
        # 最新一天因子=2.0 → qfq == raw；最早一天 qfq = raw * 1.0/2.0
        assert qfq["close"].iloc[-1] == pytest.approx(raw["close"].iloc[-1])
        assert qfq["close"].iloc[0] == pytest.approx(raw["close"].iloc[0] * 0.5)

    def test_qfq_base_from_full_history(self, store: DataStore) -> None:
        """查询窗口截掉最新日期时，qfq 归一基准仍取全量历史的最新因子。"""
        df = make_bars(periods=5)
        df["adj_factor"] = np.linspace(1.0, 2.0, 5)
        store.save_bars(df)
        qfq = store.load_bars("000001.SZ", end="2024-01-03", adjust="qfq")
        raw = store.load_bars("000001.SZ", end="2024-01-03", adjust="raw")
        assert qfq["close"].iloc[0] == pytest.approx(raw["close"].iloc[0] * 0.5)

    def test_nan_factor_treated_as_one(self, store: DataStore) -> None:
        df = make_bars(adj_factor=None)
        store.save_bars(df)
        hfq = store.load_bars("000001.SZ", adjust="hfq")
        raw = store.load_bars("000001.SZ", adjust="raw")
        assert hfq["close"].tolist() == raw["close"].tolist()

    def test_invalid_adjust(self, store: DataStore) -> None:
        with pytest.raises(SolidRockError):
            store.load_bars("000001.SZ", adjust="qfq2")  # type: ignore[arg-type]


class TestSnapshot:
    def test_create_and_read(self, store: DataStore) -> None:
        store.save_bars(make_bars())
        snap = store.create_snapshot("snap-test")
        assert snap.snapshot == "snap-test"
        assert snap.load_bars("000001.SZ").shape[0] == 5
        assert store.list_snapshots() == ["snap-test"]

    def test_snapshot_is_read_only(self, store: DataStore) -> None:
        store.save_bars(make_bars())
        snap = store.create_snapshot("snap-test")
        with pytest.raises(SolidRockError) as exc_info:
            snap.save_bars(make_bars("600519.SH"))
        assert exc_info.value.code is ErrorCode.SNAPSHOT_READ_ONLY

    def test_snapshot_isolation(self, store: DataStore) -> None:
        """快照创建后，主库新增数据不影响快照内容。"""
        store.save_bars(make_bars(periods=3))
        snap = store.create_snapshot("snap-test")
        store.update_bars(make_bars(periods=5))
        assert snap.load_bars("000001.SZ").shape[0] == 3
        assert store.load_bars("000001.SZ").shape[0] == 5

    def test_duplicate_tag_rejected(self, store: DataStore) -> None:
        store.create_snapshot("snap-a")
        with pytest.raises(SolidRockError) as exc_info:
            store.create_snapshot("snap-a")
        assert exc_info.value.code is ErrorCode.SNAPSHOT_EXISTS

    def test_invalid_tag_rejected(self, store: DataStore) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            store.create_snapshot("../evil")
        assert exc_info.value.code is ErrorCode.PARAM_INVALID

    def test_open_missing_snapshot(self, store: DataStore) -> None:
        with pytest.raises(SolidRockError) as exc_info:
            DataStore(store.root, snapshot="nope")
        assert exc_info.value.code is ErrorCode.SNAPSHOT_NOT_FOUND

    def test_delete_snapshot(self, store: DataStore) -> None:
        store.create_snapshot("snap-a")
        store.delete_snapshot("snap-a")
        assert store.list_snapshots() == []
        with pytest.raises(SolidRockError) as exc_info:
            store.delete_snapshot("snap-a")
        assert exc_info.value.code is ErrorCode.SNAPSHOT_NOT_FOUND
