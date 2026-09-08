"""标准 schema 校验测试."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from tests.conftest import make_bars

from solidrock.agent.errors import ErrorCode, SolidRockError
from solidrock.data.schema import DAILY_BAR_COLUMN_NAMES, empty_bars_frame, validate_bars


class TestValidate:
    def test_valid_frame_passes(self) -> None:
        df = validate_bars(make_bars())
        assert list(df.columns) == list(DAILY_BAR_COLUMN_NAMES)
        assert df["date"].dtype == "datetime64[ns]"
        assert df["close"].dtype == "float64"

    def test_extra_columns_dropped(self) -> None:
        df = make_bars()
        df["surprise"] = 1
        out = validate_bars(df)
        assert "surprise" not in out.columns

    def test_empty_frame(self) -> None:
        out = validate_bars(pd.DataFrame({"symbol": [], "date": []}))
        assert out.empty
        assert "close" in out.columns

    def test_missing_required_column(self) -> None:
        df = make_bars().drop(columns=["close"])
        with pytest.raises(SolidRockError) as exc_info:
            validate_bars(df)
        assert exc_info.value.code is ErrorCode.DATA_FORMAT_INVALID
        assert "close" in exc_info.value.message

    def test_string_dates_coerced(self) -> None:
        df = make_bars()
        df["date"] = df["date"].astype(str)
        out = validate_bars(df)
        assert out["date"].dtype == "datetime64[ns]"

    def test_ohlc_violation_rejected(self) -> None:
        df = make_bars()
        df.loc[df.index[0], "high"] = df.loc[df.index[0], "low"] - 1.0
        with pytest.raises(SolidRockError) as exc_info:
            validate_bars(df)
        assert exc_info.value.code is ErrorCode.DATA_FORMAT_INVALID

    def test_non_numeric_price_rejected(self) -> None:
        df = make_bars()
        df["close"] = df["close"].astype(object)
        df.loc[df.index[0], "close"] = "--"
        with pytest.raises(SolidRockError) as exc_info:
            validate_bars(df)
        assert exc_info.value.code is ErrorCode.DATA_FORMAT_INVALID

    def test_sorted_by_symbol_date(self) -> None:
        df = pd.concat([make_bars("600519.SH"), make_bars("000001.SZ")], ignore_index=True)
        out = validate_bars(df)
        assert out["symbol"].tolist() == sorted(out["symbol"].tolist())
        part = out[out["symbol"] == "000001.SZ"]
        assert part["date"].is_monotonic_increasing

    def test_suspended_coerced_to_bool(self) -> None:
        df = make_bars()
        df["suspended"] = None
        out = validate_bars(df)
        assert out["suspended"].dtype == "bool"
        assert not out["suspended"].any()

    def test_nan_factor_allowed(self) -> None:
        df = make_bars(adj_factor=None)
        out = validate_bars(df)
        assert out["adj_factor"].isna().all()

    def test_empty_bars_frame_schema(self) -> None:
        df = empty_bars_frame()
        assert list(df.columns) == list(DAILY_BAR_COLUMN_NAMES)
        assert df.empty
        assert np.isnan(df["close"].sum()) or df["close"].empty
