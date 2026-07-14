# 贡献指南

感谢参与 Evolvable User Memory。这个项目把语义正确性、隔离和可归因性放在功能数量之前。

## 开始前

1. 阅读 [快速开始](docs/getting-started.md)。
2. 阅读 [架构说明](docs/architecture.md) 中的依赖方向和核心不变量。
3. 对演化相关改动阅读 [演化安全模型](docs/evolution-safety.md)。

## 开发流程

```bash
uv sync
uv run pytest
```

进行小而聚焦的修改。不要顺便重构与目标无关的领域代码，也不要把基础设施类型引入 domain。

## 变更检查清单

- [ ] 有完整类型注解，领域值保持不可变。
- [ ] 所有有状态操作明确携带 tenant / subject Scope。
- [ ] 新时间值带时区并规范化为 UTC。
- [ ] 写入行为定义了幂等规则和内容冲突行为。
- [ ] 修正或演化追加版本，不改写历史。
- [ ] 召回和列表读取不修改 Belief 或 Utility。
- [ ] Outcome 能追溯到包含对应 Revision 的 RecallTrace。
- [ ] 正常路径、隔离、幂等和错误路径有测试。
- [ ] API Schema 包含字段说明和可执行示例。
- [ ] 前端动态用户内容没有通过 `innerHTML` 拼接。
- [ ] README 或对应文档已更新。

## 提交前门禁

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv build
```

所有检查必须通过。覆盖率不能低于 85%。

## 安全问题

不要在公开 issue、测试夹具或日志中提交真实用户证据、凭据或敏感 Scope 数据。涉及身份、隔离、删除或审计缺陷时，应先按项目维护者指定的私密渠道报告。
