# 快速开始

本指南从空环境开始，在 10 分钟内完成一条“写入 → 列表 → 召回 → 反馈 → 修正”的完整记忆闭环。

## 1. 检查环境

```bash
python --version
uv --version
```

Python 必须是 3.12 或更高版本。如果没有 `uv`，先按照 uv 官方安装说明完成安装。

## 2. 同步依赖

在项目根目录运行：

```bash
uv sync
```

该命令会创建或更新 `.venv`，并根据 `uv.lock` 安装固定版本依赖。

## 3. 启动两个进程

终端 A 启动后端：

```bash
uv run evolvable-memory
```

看到以下地址说明后端已启动：

```text
http://127.0.0.1:38089
```

终端 B 启动前端：

```bash
uv run evolvable-memory-frontend
```

看到以下地址说明前端已启动：

```text
http://127.0.0.1:33009
```

## 4. 验证服务

```bash
curl http://127.0.0.1:38089/health
```

期望响应：

```json
{"status":"ok","version":"0.1.0"}
```

还可以访问：

- 记忆工作台：<http://127.0.0.1:33009>
- OpenAPI 文档：<http://127.0.0.1:38089/docs>
- 服务发现：<http://127.0.0.1:38089/>

## 5. 在前端完成第一条闭环

### 确认作用域

页面右上角的 `tenant / subject` 是开发环境数据隔离边界。首次体验保留 `demo / alice` 即可。

> 这是开发合同，不是生产身份认证。生产环境必须从可信认证上下文派生 Scope。

### 写入偏好

1. 点击首页“使用示例开始”。
2. 检查自动填充的字段。
3. 点击“保存为记忆”。

系统会保存原始 Observation、EvidenceSpan 和 Candidate，再创建第一条 MemoryRevision。

### 查看当前记忆

进入“当前记忆”，确认能看到：

- `drink.preference`
- 当前值 `decaf coffee`
- 上下文 `time_of_day: evening`
- 修订号、置信度、支持次数和证据数量

刷新列表是只读操作，不会加强这条记忆。

### 执行召回

进入“记忆召回”，查询：

```text
晚上应该准备什么饮料？
```

保持上下文 `time_of_day = evening`。结果会展示综合分和五个评分分量，并产生独立的 `trace_id`。

### 提交结果

点击结果中的“有帮助”。这会把 `helpful` Outcome 与刚才的 Trace 关联，并更新该修订在晚间上下文中的效用。

如果只重复召回而不提交结果，信念与效用都不会变化。

### 追加修订

1. 在“当前记忆”点击“修正记忆”。
2. 把值改成 `herbal tea`。
3. 输入修正证据和原因。
4. 提交后打开“修订历史”。

历史中应同时存在旧值和新值；旧版本不会被覆盖。

## 6. 用脚本完成同样流程

后端运行时执行：

```bash
uv run python examples/first_memory.py
```

示例不依赖 `jq` 或额外 SDK，使用 Python 标准库调用写入、列表、召回和 Outcome API。

## 7. 停止与重新开始

在两个服务终端分别按 `Ctrl+C`。再次启动后端时，内存数据会清空，可以重新体验。

如果端口冲突或前端显示 API 离线，查看 [故障排查](troubleshooting.md)。
