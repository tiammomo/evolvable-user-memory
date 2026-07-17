# 记忆压缩与上下文投影

本项目中的压缩是从不可变 `RecallTrace` 生成有界、可归因、可重建的上下文投影，
不是覆盖 Observation、Evidence 或 MemoryRevision，也不是自动遗忘用户数据。

## 数据流

```text
权威 Revision + 双时间状态 + 当前策略
                  ↓
              RecallTrace
                  ↓
     ranked-extractive-v1
       或 exact-deduplicated-v1
                  ↓
      字符预算内的规范 JSON 文档
                  ↓
  source revision + 配置/源/结果 SHA-256
```

压缩只能读取同 Scope 的 Trace。它不会创建 Revision、Outcome 或 Utility 更新，也不会向
PostgreSQL、Milvus 或其他存储复制一份新的权威正文。PostgreSQL 模式重启后可以从已持久化
Trace 重新得到相同投影；内存模式在 Trace 生命周期内具有相同语义。

## 当前算法

### `ranked-extractive-v1`

按 Trace 中已经冻结的 rank 依次处理每条记忆，将 `key`、`value`、`context` 序列化为
规范 JSON 对象。只有整个对象和文档包络都能放入预算时才接纳；不会从中间截断一条事实。
如果高排名对象过长，算法会继续尝试后续更短对象，避免剩余预算浪费。

### `exact-deduplicated-v1`

先按规范 JSON 对象做精确去重，再执行相同预算选择。合并片段会保留所有源
record/revision、rank 和 score，因此去重不会丢失归因。首版有意不使用 embedding
相似度做模糊合并，避免把否定、条件或细微值差异错误压成同一事实。

两种算法都属于 extractive compression，不生成原 Trace 中不存在的新陈述。

## 输出与完整性

投影正文形如：

```json
{"memories":[{"context":{"time_of_day":"evening"},"key":"drink.preference","value":"decaf coffee"}]}
```

响应同时返回：

- Trace 的 `policy_id` / `policy_version`、`valid_at` / `known_at` 和创建时间；
- 每个片段的源 `record_id` / `revision_id`、原排名和得分；
- 源条目数、纳入数、遗漏数、原始/投影字符数和压缩比；
- `configuration_sha256`：算法、算法版本和预算；
- `source_sha256`：Scope、Trace、查询、上下文、策略、双时间和全部源条目；
- `projection_sha256`：配置摘要、源摘要、最终正文和片段归因。

相同 Trace、算法和预算必须产生完全相同的正文与摘要。投影没有独立的随机 ID 或生成时间，
避免把一次无状态重算误认为新的记忆事实。

## API

先执行 `/v1/recall` 获得 `trace_id`，再调用：

```bash
curl -X POST http://127.0.0.1:38089/v1/recall-contexts \
  -H 'content-type: application/json' \
  -d '{
    "tenant_id": "demo",
    "subject_id": "alice",
    "trace_id": "TRACE_ID",
    "algorithm": "ranked-extractive-v1",
    "max_characters": 2000
  }'
```

该端点需要独立的 `projection.compress` 动作。跨 Scope Trace 使用与资源不存在保持相同的
`404` 行为；预算范围是 64–100000 字符，未知算法或非法预算由请求 Schema 拒绝。

## 安全边界

- `value` 仍是不受信任的数据。JSON 转义能保持结构边界，但下游 LLM 仍应把整个文档放在
  明确的数据区，而不能把记忆正文当作系统指令。
- 字符预算不是模型 tokenizer 的 token 预算。调用具体模型前应预留协议和回答空间，并在
  集成层执行 tokenizer 复核。
- 压缩不会授权读取原始 Evidence，也不会放宽固定 relevance admission。
- 压缩比是投影正文与未压缩规范 JSON 的字符比，不代表数据库、向量索引或网络压缩率。
- 当前没有 LLM 摘要、模糊语义聚类、重要性删除或物理遗忘。

## 后续扩展规则

未来增加模型驱动摘要或语义聚类时，应继续使用独立 application port 和基础设施适配器，
并至少记录 `source_revision_ids`、Scope、双时间、模型 ID、prompt/算法版本、配置摘要、输出摘要
和重建状态。任何摘要都只能是可丢弃 Projection；PostgreSQL 权威 Revision、Outcome 归因、
权限、抑制、保留和删除规则不能由摘要模型修改。
