"""数据源子包：基类、注册表与内置适配器.

导入本包即完成内置适配器注册；适配器对第三方库的导入是惰性的，
未安装 ``[sources]`` 扩展不影响核心包导入，只在真正取数时报
``SOURCE_UNAVAILABLE``（带安装 hint）。
"""

from solidrock.data.sources import akshare_source, tushare_source  # 导入即注册
from solidrock.data.sources.base import Capability, DataSource
from solidrock.data.sources.registry import (
    create_source,
    list_sources,
    register_source,
    registered_names,
)

__all__ = [
    "Capability",
    "DataSource",
    "akshare_source",
    "create_source",
    "list_sources",
    "register_source",
    "registered_names",
    "tushare_source",
]
