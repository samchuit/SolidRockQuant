"""examples 冒烟测试：示例策略必须始终可运行（CI 中作为交付质量门）."""

from __future__ import annotations

from pathlib import Path

import pytest

from solidrock.backtest import BacktestConfig, BacktestEngine
from solidrock.backtest.costs import AShareCostModel
from solidrock.strategy.loader import load_strategy_class
from tests.conftest import make_market_store

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

SYM = "510300.SH"


def zero_cost() -> AShareCostModel:
    return AShareCostModel(
        commission_rate=0.0,
        commission_min=0.0,
        stamp_duty_rate=0.0,
        transfer_fee_rate=0.0,
        slippage_bps=0.0,
    )


@pytest.mark.parametrize(
    ("example", "params"),
    [
        ("buy_and_hold.py", {"symbol": SYM}),
        ("dual_ma.py", {"fast": 3, "slow": 10}),
    ],
)
def test_example_runs_end_to_end(tmp_path: Path, example: str, params: dict) -> None:
    """示例策略在合成数据上完整跑通并产出实验留痕。"""
    store = make_market_store(tmp_path, symbols=(SYM,), days=40, base=10.0, drift=0.05)
    strategy_cls = load_strategy_class(EXAMPLES_DIR / example)
    config = BacktestConfig(
        start="2024-01-02",
        end="2024-03-01",
        benchmark=None,
        cost_model=zero_cost(),
        log_experiment=True,
        name=f"smoke-{example}",
    )
    result = BacktestEngine(strategy_cls(**params), config, store).run()
    assert result.run_id
    assert not result.nav.empty
    assert "sharpe" in result.metrics
    assert result.artifacts_dir is not None
    assert (result.artifacts_dir / "report.md").exists()
