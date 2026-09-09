"""全局配置（pydantic-settings）.

优先级：环境变量 ``SOLIDROCK_*`` > 项目根 ``.env`` > 代码默认值。

费用默认值为 A 股常见参数（费率随政策与券商而变，回测时可全部覆盖），
仅作为合理起点，不构成任何交易建议。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SOLIDROCK_",
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- 存储 ---
    data_dir: Path = Path(".solidrock")  # 数据根目录，相对当前工作目录

    # --- 数据源 ---
    default_source: str = "akshare"
    tushare_token: str | None = None

    # --- 实盘（QMT / cfquant 桥接） ---
    live_account_id: str | None = None  # QMT 资金账号（SOLIDROCK_LIVE_ACCOUNT_ID）
    live_account_type: str = "STOCK"  # STOCK / CREDIT / FUTURE 等
    live_read_only: bool = True  # 只读模式：True 时禁止下单/撤单（安全默认，实盘下单显式关闭）

    # --- 交易费用默认值（M3 回测使用） ---
    commission_rate: float = 2.5e-4  # 佣金 万2.5
    commission_min: float = 5.0  # 单笔最低佣金（元）
    stamp_duty_rate: float = 5e-4  # 印花税，卖出 0.05%
    transfer_fee_rate: float = 1e-5  # 过户费 万0.1
    slippage_bps: float = 2.0  # 滑点（bps）

    def resolved_data_dir(self) -> Path:
        """数据根目录的绝对路径（相对路径基于当前工作目录解析）。"""
        return self.data_dir.expanduser().resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例。测试中用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
