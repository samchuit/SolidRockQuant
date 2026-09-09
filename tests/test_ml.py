"""ML 量化管道测试（合成数据，验证点时纪律/walk-forward/信号桥接）."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from solidrock.data.store import DataStore
from solidrock.factors.base import Factor, FactorData
from solidrock.ml.dataset import build_dataset, stack_panels, walk_forward_splits
from solidrock.ml.models import MLModel
from solidrock.ml.pipeline import walk_forward_ml

N_STOCKS = 8
N_DAYS = 120


def _make_panels(*, predictive: bool = True) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, pd.DatetimeIndex]:
    """合成因子面板：predictive=True 时因子与前瞻收益完全相关."""
    dates = pd.bdate_range("2024-01-02", periods=N_DAYS)
    symbols = [f"{i:06d}.SZ" for i in range(1, N_STOCKS + 1)]
    close = pd.DataFrame(
        np.random.RandomState(42).uniform(10, 50, (N_DAYS, N_STOCKS)),
        index=dates,
        columns=symbols,
    )
    factor_values = {}
    if predictive:
        # 因子值 = 未来 horizon 日收益（完美因子，验证管道正确性）
        fwd = close.shift(-5) / close - 1
        factor_values["alpha"] = fwd
    else:
        factor_values["alpha"] = pd.DataFrame(
            np.random.RandomState(7).uniform(-1, 1, (N_DAYS, N_STOCKS)),
            index=dates,
            columns=symbols,
        )
    # 第二个因子（噪声）
    factor_values["noise"] = pd.DataFrame(
        np.random.RandomState(99).uniform(-1, 1, (N_DAYS, N_STOCKS)),
        index=dates,
        columns=symbols,
    )
    return factor_values, close, dates


class TestStackPanels:
    def test_stack_shape(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=3)
        panels = {
            "f1": pd.DataFrame({"A": [1, 2, 3], "B": [4, 5, 6]}, index=dates),
            "f2": pd.DataFrame({"A": [7, 8, 9], "B": [10, 11, 12]}, index=dates),
        }
        stacked = stack_panels(panels)
        assert list(stacked.columns) == ["f1", "f2"]
        assert len(stacked) == 6  # 3 dates × 2 symbols
        assert stacked.index.names == ["date", "symbol"]


class TestBuildDataset:
    def test_no_lookahead(self) -> None:
        """特征行 (t, s) 的因子值 = t 日截面，不含 t 之后的因子值。"""
        dates = pd.bdate_range("2024-01-02", periods=10)
        panels = {
            "f": pd.DataFrame(np.arange(30).reshape(10, 3).astype(float), index=dates, columns=list("ABC")),
        }
        close = pd.DataFrame(np.arange(30).reshape(10, 3).astype(float) + 100, index=dates, columns=list("ABC"))
        X, y = build_dataset(panels, close, horizon=1)
        # 行 (dates[0], "A") 的特征 = panels["f"].loc[dates[0], "A"] = 0
        assert X.loc[(dates[0], "A"), "f"] == pytest.approx(0.0)
        # 标签 = close[t+1]/close[t]-1（horizon=1）
        expected_y = close.loc[dates[1], "A"] / close.loc[dates[0], "A"] - 1
        assert y.loc[(dates[0], "A")] == pytest.approx(expected_y)

    def test_drops_nan(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=5)
        panels = {"f": pd.DataFrame(np.random.rand(5, 3), index=dates, columns=list("ABC"))}
        close = pd.DataFrame(np.random.rand(5, 3) + 100, index=dates, columns=list("ABC"))
        close.iloc[-1] = np.nan  # 末日无前瞻收益
        X, _ = build_dataset(panels, close, horizon=1)
        assert X.index.get_level_values(0).max() < dates[-1]


class TestWalkForwardSplits:
    def test_temporal_order(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=100)
        splits = walk_forward_splits(dates, train_window=60, test_window=10, step=10)
        assert len(splits) >= 2
        for train, test in splits:
            assert train.max() < test.min()  # 训练在前、测试在后
            assert len(train) == 60
            assert len(test) == 10
        # 相邻窗口滚动推进
        assert splits[0][1].max() < splits[-1][1].max()

    def test_invalid_params(self) -> None:
        dates = pd.bdate_range("2024-01-02", periods=10)
        from solidrock.agent.errors import SolidRockError

        with pytest.raises(SolidRockError):
            walk_forward_splits(dates, train_window=0, test_window=5, step=5)


class TestMLModel:
    def test_fit_predict_sklearn(self) -> None:
        try:
            import sklearn  # noqa: F401
        except ImportError:
            pytest.skip("sklearn not installed")
        X = pd.DataFrame(np.random.rand(100, 3), columns=list("abc"))
        y = X["a"] * 2 + X["b"] - 1 + np.random.rand(100) * 0.01
        model = MLModel.default()
        model.fit(X, y)
        preds = model.predict(X)
        assert len(preds) == 100
        fi = model.feature_importance()
        assert sum(fi.values()) == pytest.approx(1.0) or all(v == 0.0 for v in fi.values())

    def test_untrained_predict_raises(self) -> None:
        from solidrock.agent.errors import SolidRockError

        model = MLModel(estimator=None)
        with pytest.raises(SolidRockError):
            model.predict(pd.DataFrame({"a": [1.0]}))


class TestWalkForwardPipeline:
    def test_predictive_factor_high_ic(self, tmp_path: Path) -> None:
        """完美因子：预测 IC 应接近 1，验证管道端到端正确。"""
        from solidrock.data.futures import _product  # noqa: F401
        from tests.conftest import bars_frame

        factor_values, close, dates = _make_panels(predictive=True)
        symbols = [f"{i:06d}.SZ" for i in range(1, N_STOCKS + 1)]
        store = DataStore(tmp_path / "ml")
        store.save_calendar(pd.DataFrame({"date": pd.bdate_range(dates[0], dates[-1])}))
        frames = []
        for sym in symbols:
            closes = close[sym].tolist()
            frames.append(
                bars_frame(
                    sym,
                    dates,
                    closes,
                    opens=[c - 0.05 for c in closes],
                    highs=[c + 0.05 for c in closes],
                    lows=[c - 0.05 for c in closes],
                    pre_closes=[closes[0], *closes[:-1]],
                )
            )
        store.save_bars(pd.concat(frames, ignore_index=True), source="synthetic")

        from solidrock.factors.base import Factor

        class OracleFactor(Factor):
            lookback = 1

            def compute(self, data: FactorData):
                return factor_values["alpha"]

        result = walk_forward_ml(
            {"alpha": OracleFactor()},
            store,
            symbols,
            start=str(dates[40].date()),
            end=str(dates[-6].date()),
            horizon=5,
            train_window=20,
            test_window=10,
            step=10,
            model_params={"n_estimators": 50},
            log_experiment=False,
        )
        assert result.ic_summary["ic_mean"] > 0.3
        assert result.ic_summary["n_days"] > 10
        assert len(result.window_stats) >= 1

    def test_noise_factor_low_ic(self, tmp_path: Path) -> None:
        """噪声因子：预测 IC 应接近 0。"""
        from tests.conftest import bars_frame

        factor_values, close, dates = _make_panels(predictive=False)
        symbols = [f"{i:06d}.SZ" for i in range(1, N_STOCKS + 1)]
        store = DataStore(tmp_path / "ml2")
        store.save_calendar(pd.DataFrame({"date": pd.bdate_range(dates[0], dates[-1])}))
        frames = []
        for sym in symbols:
            closes = close[sym].tolist()
            frames.append(
                bars_frame(
                    sym,
                    dates,
                    closes,
                    opens=[c - 0.05 for c in closes],
                    highs=[c + 0.05 for c in closes],
                    lows=[c - 0.05 for c in closes],
                    pre_closes=[closes[0], *closes[:-1]],
                )
            )
        store.save_bars(pd.concat(frames, ignore_index=True), source="synthetic")

        from solidrock.factors.base import Factor

        class NoiseFactor(Factor):
            lookback = 1

            def compute(self, data: FactorData):
                return factor_values["alpha"]

        result = walk_forward_ml(
            {"noise": NoiseFactor()},
            store,
            symbols,
            start=str(dates[40].date()),
            end=str(dates[-6].date()),
            horizon=5,
            train_window=20,
            test_window=10,
            step=10,
            log_experiment=False,
        )
        assert abs(result.ic_summary["ic_mean"]) < 0.5

    def test_pipeline_requires_data(self, tmp_path: Path) -> None:
        from solidrock.agent.errors import ErrorCode, SolidRockError

        store = DataStore(tmp_path / "empty")

        class F(Factor):
            def compute(self, data):  # type: ignore[no-untyped-def]
                return pd.DataFrame()

        with pytest.raises(SolidRockError) as exc_info:
            walk_forward_ml(
                F(),
                store,
                ["600519.SH"],
                start="2024-01-02",
                end="2024-06-01",
                log_experiment=False,
            )
        assert exc_info.value.code is ErrorCode.NO_DATA


from pathlib import Path  # noqa: E402
