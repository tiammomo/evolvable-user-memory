# 数据生命周期与安全清理

Memory 的删除单位是 `Scope = (tenant_id, subject_id)`，不是某张表或某个向量。权威事实在
PostgreSQL，Milvus 只是可重建投影；任何正式删除都必须同时覆盖 authority、outbox 和 projection。

## 应保留什么

- 当前 `MemoryRecord` 与不可变 `MemoryRevision`：用户偏好事实及其历史。
- 支撑事实的 Observation/Evidence/Candidate：用于修订解释和审计。
- RecallTrace、MemoryUsage 与 Outcome：用于证明实际暴露和有界效用学习。
- 治理模式为 `postgres` 时的 ProcessingGrant、SuppressionFence、Erasure receipt 与授权审计。
- migration、策略快照/激活和必要的 projection cursor。

空召回不是用户记忆。它可以按已批准的短期 trace 保留策略裁剪，但不能因此删除用户的
`personalization_enabled` 控制。测试身份必须使用独立 tenant/subject，不能复用真实用户 Scope。

## 正式 Scope 删除

使用治理 API，不直接按表执行 SQL：

```bash
curl --fail-with-body -X POST http://127.0.0.1:38089/v1/governance/erasures \
  -H 'Content-Type: application/json' \
  -d '{
    "tenant_id": "consumer-tenant",
    "subject_id": "subject-to-erase",
    "reason_code": "verified-retention-expiry",
    "idempotency_key": "erase:consumer-tenant:subject-to-erase:2026-07",
    "purpose": "privacy-governance"
  }'
```

生产删除要求可信 JWT、授权审批、ProcessingGrant/抑制策略和
`EMF_GOVERNANCE_MODE=postgres`。`development` 治理模式适合本机闭环，但其进程内回执不是持久
删除证明。调用方应保存 `request_id`，并读取 erasure receipt 验证三个 handler 均完成。

## 测试与备份

内置 `evolvable-memory-eval` 使用全新进程内状态，不污染 PostgreSQL。PostgreSQL 集成测试必须
使用 `EMF_TEST_DATABASE_URL` 指向独立测试库；不得把日常开发库作为测试目标。

删除前至少备份 PostgreSQL。Milvus 文档可由权威 Revision/Outbox 重建；恢复演练必须验证
PostgreSQL 恢复后 projection rebuild 能重新得到相同 Scope 数量和 digest。备份到期删除属于
单独的合规流程，在线 erasure 完成不等于历史备份已经到期物理销毁。

详细权限见[身份与权限设计](authorization.md)，投影恢复见[Milvus 投影指南](milvus-projection.md)，
治理操作见[治理运行手册](governance.md)。
