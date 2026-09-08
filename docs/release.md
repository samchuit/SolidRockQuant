# 发布流程

## 发布记录

| 版本 | 日期 | TestPyPI | PyPI |
|------|------|----------|------|
| v0.2.0 | 2026-09-09 | 通过 Trusted Publishing 自动发布 | 待手动触发 |

## 前置（一次性）

1. **PyPI / TestPyPI 账号**：注册并启用 2FA；
2. **Trusted Publishing**（免 token）：
   - 登录 https://test.pypi.org → Account Settings → Publishing →
     **Add a new pending publisher**：
     - PyPI project name: `solidrock-quant`
     - Owner: `samchuit`，Repository: `SolidRockQuant`
     - Workflow: `release.yml`，Environment: **留空**
   - PyPI 正式站同样添加一份；
3. 正式发布如需人工审批，在 GitHub 仓库 Settings→Environments 创建 `pypi`
   环境并加必选审批人（工作流的 pypi job 已改为 workflow_dispatch 手动触发）。

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
pip install --index-url https://test.pypi.org/simple \
    --extra-index-url https://pypi.org/simple "solidrock-quant[mcp,sources]"
srq --help && python -c "import solidrock"

# 5. 正式发布：GitHub Actions → Release 工作流 → Run workflow（选 pypi job）
```

## 版本约定

- 语义化版本：`0.x.y`；开发阶段后缀 `dev0`；
- v0.x 阶段 API 可能变动（在 CHANGELOG 中标注）；1.0 后遵守弃用流程（弃用先告警一个次版本）。
