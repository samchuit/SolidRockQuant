"""因子模块测试：基座、截面处理、RankIC/分层分析、MCP 工具."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from solidrock.agent.tools import ALL_TOOLS
from solidrock.data.store import DataStore
from solidrock.experiments.tracker import ExperimentTracker
from solidrock.factors import (
    Factor,
    FactorData,
    analyze_factor,
    load_factor_class,
    neutralize,
    rank_pct,
    winsorize_mad,
    zscore,
)
from solidrock.factors.analysis import _cross_spearman
from tests.conftest import bars_frame

N_SYMBOLS = 8


@pytest.fixture
def varied_store(tmp_path: Path) -> DataStore:
    """截面收益有差异的确定性合成行情（相位错开的正弦 + 漂移）。"""
    store = DataStore(tmp_path / "fac")
    dates = pd.bdate_range("2024-01-02", periods=60)
    store.save_calendar(pd.DataFrame({"date": dates}))
    frames = []
    t = np.arange(len(dates))
    for i in range(N_SYMBOLS):
        symbol = f"00000{i + 1}.SZ"
        closes = 10 + i + 0.6 * np.sin(2 * np.pi * (t + i * 2.5) / 9) + 0.02 * t
        frames.append(bars_frame(symbol, dates, closes))
    store.save_bars(pd.concat(frames, ignore_index=True), source="synthetic")
    return store


def universe() -> list[str]:
    return [f"00000{i + 1}.SZ" for i in range(N_SYMBOLS)]


class Oracle(Factor):
    """完美因子：因子值 = 下一期收益（测试用，生产因子不可能做到）。"""

    lookback = 1

    def compute(self, data: FactorData) -> pd.DataFrame:
        hfq = data.hfq_close()
        return hfq.shift(-1) / hfq - 1


class TestFactorData:
    def test_from_bars_pivot(self, varied_store: DataStore) -> None:
        bars = varied_store.load_bars(universe())
        data = FactorData.from_bars(bars)
        assert data.close.shape == (60, N_SYMBOLS)
        assert list(data.close.columns) == sorted(universe())
        hfq = data.hfq_close()
        # 因子=1 时 hfq == raw
        assert np.allclose(hfq.to_numpy(), data.close.to_numpy())

    def test_getitem_unknown_field(self, varied_store: DataStore) -> None:
        bars = varied_store.load_bars(universe())
        data = FactorData.from_bars(bars)
        with pytest.raises(Exception, match="不存在"):
            _ = data["nope"]

    def test_factor_param_validation(self) -> None:
        class F(Factor):
            params = {"n": 5}

            def compute(self, data):  # type: ignore[no-untyped-def]
                return data.close

        with pytest.raises(Exception, match="未声明参数"):
            F(bad=1)
        assert F(n=10).params == {"n": 10}


class TestProcessing:
    def test_winsorize_mad_clips_outlier(self) -> None:
        df = pd.DataFrame([[1.0, 2.0, 3.0, 4.0, 100.0]], columns=list("abcde"))
        row = winsorize_mad(df).iloc[0]
        assert row.max() < 100.0  # 极端值被截断
        assert row["d"] == pytest.approx(4.0)  # 正常值不动
        # MAD=0 的退化截面：不做截断
        flat = pd.DataFrame([[5.0, 5.0, 5.0, 5.0, 5.0]], columns=list("abcde"))
        assert (winsorize_mad(flat).iloc[0] == 5.0).all()

    def test_zscore_cross_section(self) -> None:
        df = pd.DataFrame({"s1": [1.0, 3.0], "s2": [2.0, 3.0], "s3": [3.0, 3.0]})
        out = zscore(df)
        assert out.iloc[0].mean() == pytest.approx(0.0, abs=1e-9)
        assert out.iloc[0].std(ddof=0) == pytest.approx(1.0, abs=1e-9)
        # 常数截面 → 全 NaN（std=0）
        assert out.iloc[1].isna().all()

    def test_rank_pct(self) -> None:
        df = pd.DataFrame({"a": [10.0], "b": [30.0], "c": [20.0]})
        out = rank_pct(df)
        assert out.iloc[0]["a"] == pytest.approx(1 / 3)
        assert out.iloc[0]["b"] == pytest.approx(1.0)

    def test_neutralize_residual_orthogonal(self) -> None:
        idx = pd.DatetimeIndex(["2024-01-02"])
        x = pd.DataFrame([[1.0, 2.0, 3.0, 4.0]], index=idx, columns=list("abcd"))
        y = pd.DataFrame([[1.0, 5.0, 2.0, 8.0]], index=idx, columns=list("abcd"))
        res = neutralize(y, x)
        # OLS 残差与暴露正交（数学保证），且均值为 0（含截距）
        corr = np.corrcoef(res.iloc[0].to_numpy(), x.iloc[0].to_numpy())[0, 1]
        assert abs(corr) < 1e-9
        assert abs(res.iloc[0].mean()) < 1e-9
        # 暴露缺失的截面保守跳过（保持 NaN）
        x_nan = pd.DataFrame([[1.0, 2.0, np.nan, 4.0]], index=idx, columns=list("abcd"))
        assert neutralize(y, x_nan).iloc[0].isna().all()

    def test_cross_spearman(self) -> None:
        a = pd.Series([1.0, 2.0, 3.0, 4.0])
        assert _cross_spearman(a, a * 2 + 1) == pytest.approx(1.0)
        assert _cross_spearman(a, -a) == pytest.approx(-1.0)
        assert _cross_spearman(a, pd.Series([np.nan] * 4)) is None


class TestAnalyzeFactor:
    def test_oracle_ic_near_one(self, varied_store: DataStore) -> None:
        result = analyze_factor(
            Oracle(),
            varied_store,
            universe(),
            start="2024-01-15",
            end="2024-03-01",
            log_experiment=False,
        )
        assert result.ic_summary["ic_mean"] > 0.95
        assert result.ic_summary["n_days"] > 20

    def test_layer_monotonic_long_short_positive(self, varied_store: DataStore) -> None:
        result = analyze_factor(
            Oracle(),
            varied_store,
            universe(),
            start="2024-01-15",
            end="2024-03-01",
            quantiles=4,
            log_experiment=False,
        )
        stats = result.layer_stats
        assert stats.loc["L-S", "total_return"] > 0  # 多空价差为正
        top = stats.loc["4", "total_return"]
        bottom = stats.loc["1", "total_return"]
        assert top > bottom  # 最高层跑赢最低层

    def test_artifacts_and_experiment(self, varied_store: DataStore) -> None:
        result = analyze_factor(
            Oracle(),
            varied_store,
            universe(),
            start="2024-01-15",
            end="2024-03-01",
            name="oracle-测试",
        )
        assert result.artifacts_dir is not None
        for f in ("report.md", "result.json", "ic.csv", "layer_navs.csv"):
            assert (result.artifacts_dir / f).exists(), f
        report = (result.artifacts_dir / "report.md").read_text(encoding="utf-8")
        assert "RankIC" in report and "Oracle" in report
        tracker = ExperimentTracker(varied_store.root / "experiments.db")
        run = tracker.get_run(result.run_id)
        assert run["kind"] == "factor"
        assert run["metrics"]["ic_mean"] == result.ic_summary["ic_mean"]

    def test_load_factor_class(self, tmp_path: Path) -> None:
        f = tmp_path / "my_factor.py"
        f.write_text(
            "from solidrock.factors import Factor\n"
            "class MyMom(Factor):\n"
            "    lookback = 20\n"
            "    def compute(self, data):\n"
            "        return data.hfq_close().pct_change(20)\n",
            encoding="utf-8",
        )
        cls = load_factor_class(str(f))
        assert cls.__name__ == "MyMom"

    def test_empty_universe_rejected(self, varied_store: DataStore) -> None:
        with pytest.raises(Exception, match="universe 为空"):
            analyze_factor(Oracle(), varied_store, [], start="2024-01-15", end="2024-03-01")


class TestMcpTool:
    def test_run_factor_analysis_envelope(
        self, varied_store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        factor_file = tmp_path / "oracle.py"
        factor_file.write_text(
            "from solidrock.factors import Factor, FactorData\n"
            "class Oracle(Factor):\n"
            "    lookback = 1\n"
            "    def compute(self, data):\n"
            "        hfq = data.hfq_close()\n"
            "        return hfq.shift(-1) / hfq - 1\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SOLIDROCK_DATA_DIR", str(varied_store.root))
        from solidrock.config import get_settings

        get_settings.cache_clear()
        try:
            env = json_envelope = json_loads = None  # noqa: F841
            import json

            raw = ALL_TOOLS["run_factor_analysis"](
                factor_file=str(factor_file),
                universe=universe(),
                start="2024-01-15",
                end="2024-03-01",
                quantiles=3,
                name="mcp-factor",
            )
            envelope = json.loads(raw)
            assert envelope["status"] == "ok", envelope
            data = envelope["data"]
            assert data["factor"] == "Oracle"
            assert data["ic_summary"]["ic_mean"] > 0.95
            assert len(data["artifacts"]) == 4
            assert all(Path(p).exists() for p in data["artifacts"])
            tracker = ExperimentTracker(varied_store.root / "experiments.db")
            assert tracker.get_run(data["run_id"])["kind"] == "factor"
        finally:
            get_settings.cache_clear()
