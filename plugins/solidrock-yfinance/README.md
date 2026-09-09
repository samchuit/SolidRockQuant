# solidrock-yfinance

SolidRockQuant 官方 **yfinance 数据源插件**：接入海外股票日线（NYSE / NASDAQ / AMEX / HKEX / TSE / LSE）。

本包同时是[插件开发指南](https://github.com/samchuit/SolidRockQuant/blob/main/docs/plugins.md)的参考实现——它完全通过 entry-points 接入框架，不含任何框架侵入代码。

## 安装

```bash
pip install solidrock-yfinance      # 自动带上 solidrock-quant 与 yfinance
```

## 使用

```python
from solidrock.data.sources import create_source

src = create_source("yfinance")
df = src.fetch_bars(["AAPL.NASDAQ", "7203.TSE"], start="2024-01-01")
```

符号格式：`<代码>.<交易所>`，如 `AAPL.NASDAQ` / `BRK.A? 不支持` / `7203.TSE`。
`volume` 单位为**股**（海外惯例，非国内"手"）。

## 数据口径

- 原始价 + 后复权因子（`auto_adjust=False` 与 `auto_adjust=True` 各取一次，
  因子 = 后复权收盘 / 原始收盘，与框架口径一致）；
- 分钟线不支持（yfinance 1m 数据仅最近 30 天，后续版本评估）；
- 指数/外汇/加密货币暂不支持。
