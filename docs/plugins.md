# 插件开发指南

SolidRockQuant 通过 Python **entry-points** 接入第三方数据源与因子：
安装你的插件包后，框架自动发现并注册，无需修改框架代码。

## 数据源插件

### 1. 实现 DataSource 子类

```python
# my_plugin/source.py
import pandas as pd
from solidrock.data.sources import DataSource, Capability, register_source
from solidrock.data.symbols import AssetType

@register_source
class MySource(DataSource):
    name = "my-source"
    capabilities = frozenset({Capability.BARS_DAILY_STOCK})

    def _fetch_bars_one(self, symbol, start, end, *, with_adj_factor):
        raw = ...  # 调用你的数据接口
        df = ...   # 映射为标准列：symbol/date/open/high/low/close/pre_close/volume/amount/adj_factor...
        return df
```

要求（详见 `solidrock/data/sources/base.py` 模块文档）：
- 符号统一为 `000001.SZ` 格式（`parse_symbol` 可解析）；
- 单位：volume=手、amount=元、pre_close=除权后口径；
- 依赖库**方法内惰性导入**，缺库抛 `SOURCE_UNAVAILABLE`（带安装 hint）；
- 网络调用经 `self._request(desc, fn, **kwargs)` 包装，异常自动转为
  `SOURCE_REQUEST_FAILED`（带 hint）。

### 2. 声明 entry-point

```toml
# my_plugin/pyproject.toml
[project.entry-points."solidrock.sources"]
my-source = "my_plugin.source"
```

`pip install my-plugin` 后即生效：

```python
from solidrock.data.sources import create_source, list_sources
src = create_source("my-source")     # 自动发现第三方插件
```

## 因子插件

```python
# my_plugin/factors.py
from solidrock.factors import Factor, FactorData, register_factor

@register_factor
class MyMomentum(Factor):
    params = {"n": 20}
    lookback = 20

    def compute(self, data: FactorData):
        hfq = data.hfq_close()
        return hfq / hfq.shift(int(self.params["n"])) - 1
```

```toml
[project.entry-points."solidrock.factors"]
my-momentum = "my_plugin.factors:MyMomentum"
```

使用：`create_factor("MyMomentum", n=10)`；CLI/MCP 的因子工具支持
`factor_name` 参数直接引用已注册因子（无需文件）。

## 调试

```python
from solidrock.plugins import discover_plugins
print(discover_plugins())   # {"sources": [...], "factors": [...]}
```

加载失败的插件在结果中标注 `(加载失败: ...)`，不影响其余插件。
