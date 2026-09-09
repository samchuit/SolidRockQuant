# 参与贡献

感谢关注 SolidRockQuant！项目处于早期快速迭代阶段，以下指南帮助你的 PR 快速合入。

## 开发环境

```bash
git clone https://github.com/samchuit/SolidRockQuant.git
cd SolidRockQuant
uv venv --python 3.12
uv sync --all-extras        # 严格按 uv.lock 安装（含 sources/mcp/report）
uv run pytest -m "not network"
```

## 质量门槛（CI 会在 3.10–3.12 矩阵上执行）

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/solidrock
uv run pytest -m "not network"
```

四项全绿是合入的必要条件。

## 工作流

1. Fork → 创建特性分支（`feat/xxx` 或 `fix/xxx`）；
2. 新功能必须附带测试；修复请先写一个能复现问题的失败测试；
3. 面向 Agent 的改动（工具/错误码/信封）请同步更新：
   - `src/solidrock/agent/skills/`（操作指南）
   - `llms.txt` 与 `docs/mcp-setup.md`（工具清单）
   - `CHANGELOG.md`；
4. 提交信息使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)（feat/fix/docs/refactor/test/chore）。

## 设计约定（详见 docs/design.md）

- 错误必须是 `SolidRockError`：稳定错误码 + **可执行的** hint；
- 引擎默认防前视（next_open）；任何放宽都需要显式参数并在报告中标注；
- 数据落库一律为原始价 + 复权因子；
- 新工具的 docstring 就是 LLM 看到的说明书，必须自包含。

## 提交反馈

- Bug 报告请附：版本号、最小复现代码、完整报错（含错误码与 hint）；
- 设计讨论建议先开 Issue 再动手，避免方向性返工。
