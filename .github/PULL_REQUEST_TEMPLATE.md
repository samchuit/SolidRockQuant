# Pull Request

## 变更说明

（做了什么、为什么）

## 类型

- [ ] feat（新功能）
- [ ] fix（修复）
- [ ] docs（文档）
- [ ] refactor / test / chore

## 自查清单

- [ ] `uv run ruff check src tests` 通过
- [ ] `uv run ruff format --check src tests` 通过
- [ ] `uv run mypy src/solidrock` 通过
- [ ] `uv run pytest -m "not network"` 全绿
- [ ] 新功能附带测试；面向 Agent 的改动已同步 skills/llms.txt/CHANGELOG
- [ ] 破坏性变更已在 CHANGELOG 中标注
