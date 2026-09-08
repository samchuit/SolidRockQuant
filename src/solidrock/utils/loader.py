"""从用户 Python 文件加载指定基类的子类（策略/因子等组件共用）."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TypeVar

from solidrock.agent.errors import ErrorCode, err

T = TypeVar("T")


def load_class_from_file(path: str | Path, base: type[T], base_label: str) -> type[T]:
    """加载文件中定义的（且只定义一个）``base`` 子类.

    - 文件不存在 / 语法错误 → ``PARAM_INVALID`` / ``STRATEGY_INVALID``；
    - 没有或多个子类 → ``STRATEGY_INVALID``（带 hint）。
    """
    path = Path(path)
    if not path.exists():
        raise err(
            ErrorCode.PARAM_INVALID,
            f"{base_label} 文件不存在：{path}",
            hint=f"传入 .py 文件的绝对路径；文件内需定义一个 {base.__name__} 子类",
        )
    module_name = f"srq_component_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise err(ErrorCode.STRATEGY_INVALID, f"无法加载文件：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    candidates = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, base) and obj is not base and obj.__module__ == module_name
    ]
    if len(candidates) == 0:
        raise err(
            ErrorCode.STRATEGY_INVALID,
            f"文件中没有定义 {base.__name__} 子类：{path}",
            hint=f"from solidrock import {base.__name__}; class My({base.__name__}): ...",
        )
    if len(candidates) > 1:
        names = [c.__name__ for c in candidates]
        raise err(
            ErrorCode.STRATEGY_INVALID,
            f"文件定义了多个 {base.__name__} 子类：{names}",
            hint="每个文件只放一个组件类",
        )
    return candidates[0]
