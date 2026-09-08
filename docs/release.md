# 发布流程

## 前置（一次性）

1. **PyPI / TestPyPI 账号**：注册并启用 2FA；
2. **Trusted Publishing**（推荐，免 token）：
   - TestPyPI：账号设置 → Publishing → 添加 GitHub 项目，environment 填 `testpypi`；
   - PyPI：同样添加，environment 填 `pypi`；
3. **GitHub Environments**：仓库 Settings → Environments → 创建 `testpypi` 与 `pypi`（pypi 建议加手动审批保护）。

## 发布步骤

```bash
# 1. 发布前检查
pytest -m "not network"        # 全绿
ruff check src tests && mypy src/solidrock
uv build && uv pip install twine && twine check dist/*

# 2. 版本号：更新 pyproject.toml 的 version（去掉 dev0 后缀），提交
# 3. 打 tag 触发：build → twine check → TestPyPI 自动发布
git tag v0.1.0 && git push origin v0.1.0

# 4. TestPyPI 验证
pip install --index-url https://testpypi.python.org/simple \
    --extra-index-url https://pypi.org/simple "solidrock-quant[mcp,sources]"
srq --help && python -c "import solidrock"

# 5. 正式发布：GitHub Actions → Release 工作流 → Run workflow（选 pypi job）
```

## 版本约定

- 语义化版本：`0.x.y`；开发阶段后缀 `dev0`；
- v0.x 阶段 API 可能变动（在 CHANGELOG 中标注）；1.0 后遵守弃用流程（弃用先告警一个次版本）。
