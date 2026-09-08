---
name: srq-data-update
description: 用 SolidRockQuant 更新本地行情数据、交易日历与数据快照。当用户要求拉数据、更新行情、准备回测数据时使用。
---

# SolidRockQuant 数据更新

1. `get_data_overview` 先看已有什么（symbols/行数/日期范围/快照）。
2. 补数据：`fetch_bars(symbols=[...], start="YYYY-MM-DD", end=今天)` —— 增量幂等，重复调用安全；回测 prep 时 start 要覆盖回测起点往前 250 个交易日。
3. 交易日历：首次使用或跨年时 `get_trading_calendar(update=true)`。
4. 严肃实验前创建数据快照：让用户运行 `srq data snapshot create <tag>`（近零拷贝、只读）。回测结果记录快照名，复现实验 = 同一快照。
5. 搜索标的：`search_instruments(query="茅台")` 或按代码 `query="600519"`。

## 数据口径

- 存储为原始价 + 复权因子（hfq = close × adj_factor）；volume 单位=手；amount 单位=元；pre_close 为除权后口径。
- 东财主通道不可达时自动切新浪；北交所无新浪数据。
- Tushare 需要环境变量 SOLIDROCK_TUSHARE_TOKEN，部分接口需 2000 积分。
