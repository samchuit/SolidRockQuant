"""ML 模型适配器：LightGBM / scikit-learn 薄封装.

统一 ``fit``/``predict``/``feature_importance`` 接口，
底层惰性导入 LightGBM（首选）或 sklearn GradientBoosting（回退）。
模型持久化由用户通过 LightGBM 原生 API 或 joblib 处理。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from solidrock.agent.errors import ErrorCode, err


class MLModel:
    """统一模型接口：封装 LightGBM/sklearn 回归器."""

    def __init__(self, estimator: Any = None, *, feature_names: list[str] | None = None) -> None:
        self.estimator = estimator
        self.feature_names = feature_names or []
        self._trained = False

    @classmethod
    def default(cls, *, n_estimators: int = 200, learning_rate: float = 0.05) -> MLModel:
        """默认模型：LightGBM 回归（若可用），否则 sklearn GradientBoosting."""
        try:
            import lightgbm as lgb

            return cls(lgb.LGBMRegressor(n_estimators=n_estimators, learning_rate=learning_rate, verbose=-1))
        except ImportError:
            pass
        try:
            from sklearn.ensemble import GradientBoostingRegressor

            return cls(GradientBoostingRegressor(n_estimators=n_estimators, learning_rate=learning_rate))
        except ImportError as exc:
            raise err(
                ErrorCode.SOURCE_UNAVAILABLE,
                "ML 模型依赖未安装",
                hint="pip install 'solidrock-quant[ml]' 或 pip install lightgbm scikit-learn",
            ) from exc

    def fit(self, X: pd.DataFrame, y: pd.Series) -> MLModel:
        self.feature_names = list(X.columns)
        self.estimator.fit(X.to_numpy(), y.to_numpy())
        self._trained = True
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        if not self._trained:
            raise err(ErrorCode.PARAM_INVALID, "模型尚未训练，请先 fit")
        missing = set(self.feature_names) - set(X.columns)
        if missing:
            raise err(
                ErrorCode.PARAM_INVALID,
                f"预测特征缺失：{sorted(missing)}",
                hint="训练与预测的特征列必须一致",
            )
        return self.estimator.predict(X[self.feature_names].to_numpy())

    @property
    def trained(self) -> bool:
        return self._trained

    def feature_importance(self) -> dict[str, float]:
        """特征重要性（归一化到 0~1）。"""
        if not self._trained:
            return {}
        model = self.estimator
        raw = None
        if hasattr(model, "feature_importances_"):
            raw = np.asarray(model.feature_importances_, dtype=float)
        elif hasattr(model, "coef_"):
            raw = np.abs(np.asarray(model.coef_, dtype=float))
        if raw is None or raw.sum() == 0:
            return dict.fromkeys(self.feature_names, 0.0)
        return {name: float(v) for name, v in zip(self.feature_names, raw / raw.sum(), strict=True)}
