# Memory、Context Engineering、Reflection 机制开发计划

## Summary

目标是在不破坏当前受控流水线的前提下，增加三类能力：

- **Research Memory**：相同高置信主题 30 天内可自动复用报告；相似主题只作为历史参考，仍走研究流程。
- **ContextBuilder**：统一控制 memory、历史报告、失败经验进入各 agent 上下文 的方式，使用字符预算、top-k、截断和来源标注，避免上下文膨胀。
- **Operational Reflection**：只记录确定性失败经验，例如 JSON 解析失败、工具参数错误、citation/report 格式错误，并用于后续对应 agent 的短规则提示。

默认选择：

- 存储：SQLite，路径默认 `data/memory.sqlite3`。
- 自动复用：仅限 normalized topic 完全相同、高置信、未过期、且用户没有要求最新信息的 research memory。
- 时效：默认 30 天；用户请求“最新/今天/当前/最近”等时不自动复用，只作为参考上下文。
- 上下文预算：字符预算，不引入 tokenizer、embedding 或向量库。
- Memory 不替代当前证据，不绕过 citation validator，不扩大任何 agent 的工具权限。

## Key Interfaces

新增配置：

- `MEMORY_ENABLED=true`
- `MEMORY_PATH=data/memory.sqlite3`
- `MEMORY_TTL_DAYS=30`
- `MEMORY_MAX_CONTEXT_CHARS=6000`
- `MEMORY_RESEARCH_TOP_K=3`
- `MEMORY_OPERATIONAL_TOP_K=5`

新增核心模型：

- `MemoryRecord`
  - `id`
  - `kind`: `research | operational`
  - `topic`
  - `normalized_topic`
  - `summary`
  - `content`
  - `sources`
  - `evidence_judgement`
  - `confidence`: `high | medium | low`
  - `tags`
  - `metadata`
  - `content_hash`
  - `use_count`
  - `created_at`
  - `updated_at`
  - `last_used_at`
  - `expires_at`
- `MemoryHit`
  - `record`
  - `score`
  - `match_type`: `exact | similar`
  - `risk`: `fresh | stale | low_confidence | insufficient_evidence`
- `MemoryDecision`
  - `mode`: `reuse | augment | ignore`
  - `reason_code`
  - `reusable_record`
  - `context_hits`

新增服务：

- `MemoryStore`
  - `save(record)`
  - `upsert(record)`
  - `search(topic, kind, limit)`
  - `find_reusable_research(topic, now, freshness_required)`
  - `mark_used(id)`
- `MemoryRetriever`
  - `normalize_topic(topic)`
  - `is_freshness_sensitive(topic)`
  - `decide(topic, now)`
- `ContextBuilder`
  - `build_planner_context(memory_hits)`
  - `build_evidence_context(memory_hits)`
  - `build_query_rewrite_context(memory_hits, operational_lessons)`
  - `build_report_context(memory_hits, operational_lessons)`
  - `enforce_budget(text, max_chars)`
- `ReflectionRecorder`
  - `record_tool_failure(...)`
  - `record_validation_failure(...)`
  - `record_agent_recovery(...)`

扩展 workflow state：

- `memory_decision`
- `memory_hits`
- `reused_memory_id`
- `memory_context`
- `operational_lessons`

保留现有 `NoteTool` JSONL notes。Memory 是可检索的长期复用层，notes 仍是任务执行产物记录，二者不互相替代。

## Phases

### Phase 0：现有契约固化

目标：先把 memory 接入前的基线行为固定住，防止后续改动掩盖回归。

验收指标：

- 当前完整 mock research flow 继续通过。
- 当前 LangGraph 节点和条件边测试继续通过。
- 当前 citation validator、fallback report、tool logs、SSE 顺序测试继续通过。
- 新增测试确认 `NoteTool` 行为不被 memory 取代或改变。

### Phase 1：Memory 基础设施

实现 SQLite `MemoryStore`、Pydantic memory 模型、配置项和基础 schema 初始化。

规则：

- 使用 Python 标准库 `sqlite3`，不新增数据库依赖。
- SQLite 初始化必须幂等。
- 写入 memory 前统一使用 `ResearchProgressTracker.sanitize()` 或等价脱敏 helper。
- 不保存原始 API key、Bearer token、secret、未脱敏异常堆栈、完整 LLM prompt。
- 以 `kind + normalized_topic + content_hash` 去重。

验收指标：

- 首次使用会创建 memory 数据库和表。
- 可保存、读取、按 `kind` 查询 memory。
- 可按 `normalized_topic` 做精确查询。
- 可过滤过期 memory。
- 重复写入相同内容只更新已有记录。
- 单元测试覆盖保存、查询、去重、过期过滤、脱敏和禁存敏感信息。

### Phase 2：Research Memory 写入

在研究完成后写入 research memory，但暂不读取、不影响主流程。

写入内容：

- topic 和 normalized topic。
- completed summaries。
- sources。
- evidence judgement。
- final report。
- failed tasks 摘要。
- 是否 fallback report。

置信度规则：

- 正常报告且 evidence `high` 或无不足：`confidence=high`。
- 正常报告但 evidence `medium`：`confidence=medium`。
- fallback report：最高 `confidence=medium`。
- evidence 不足：最高 `confidence=low`，不得作为自动复用候选。

验收指标：

- 完整研究结束后生成 research memory。
- fallback report 会写入 memory，但不会被高置信自动复用。
- evidence 不足 memory 写入为 low confidence。
- failed tasks 被记录为诊断信息，不进入高置信结论。
- memory 写入有可观测日志，建议使用 `progress.log_tool_call(stage="memory", tool_name="MemoryStore", ...)`。
- 后端完整测试通过。

### Phase 3：Memory Retrieval Gate：只做精确复用

在 `plan` 之前增加 memory lookup，但本阶段只处理“完全相同 topic 的高置信自动复用”。

规则：

- normalized topic 完全相同。
- memory 未过期。
- `confidence=high`。
- evidence 不足标记不存在。
- 用户 topic 不包含最新性意图。
- 命中后直接返回 cached `ResearchResult`，不执行 planner/search/evidence/report。
- cached `ResearchResult` 优先恢复原始 tasks/execution_records；如果旧记录缺少 tasks，则返回空 tasks 并在 tool log 标记 `memory_reuse_partial`。

验收指标：

- 同一 topic 第二次请求命中高置信 memory 时，不调用 planner/search/report。
- 复用路径仍发出 `report -> done` SSE。
- 复用路径的 `tool_logs` 包含 memory 命中记录。
- 用户要求“最新/今天/当前/最近”时，不自动复用。
- 过期、low confidence、evidence 不足 memory 不复用。
- 后端完整测试通过。

### Phase 4：ContextBuilder 与相似 memory 上下文

实现统一上下文构造，并开始把“相似但不可复用”的 research memory 作为参考注入 agent。

规则：

- 相似检索 v1 使用确定性轻量算法：normalized topic token overlap + substring match；不引入 embedding。
- 每个 agent 独立字符预算。
- research memory 默认 top 3。
- 单条 memory 摘要截断。
- memory 必须标注为“历史参考，不是当前证据”。
- ReportWriter 引用只能来自当前 report sources，不能引用 memory 中未进入当前 sources 的 URL。
- ContextBuilder 是唯一拼接 memory 上下文的入口，agent 不直接访问 MemoryStore。

接入范围：

- `TodoPlanner`：相似历史任务结构、历史 unresolved gaps。
- `EvidenceJudge`：历史 evidence 状态和 unresolved gaps。
- `QueryRewriter`：历史失败 query、未解决 gaps。
- `ReportWriter`：历史报告结构摘要，不提供历史 source citation marker。

验收指标：

- 相似 topic 会检索到 memory，但仍正常执行研究流程。
- prompt 中 memory 上下文不超过配置字符预算。
- memory 很多时仍只选 top-k。
- memory 不会绕过 citation validator。
- ReportWriter 不会引用未纳入当前 sources 的历史来源。
- 后端完整测试通过。

### Phase 5：Operational Reflection 记录

记录确定性失败经验，不做泛泛 LLM 自我总结，暂不注入 agent prompt。

记录来源：

- JSON fallback parse failure。
- tool 参数 schema failure。
- unregistered/forbidden tool。
- citation validator failure。
- ReportWriter 缺少 required sections、references、citation marker、source URL、证据不足提示。
- evidence judge 输出结构错误或执行失败。
- query rewrite 输出结构错误、空 query、重复 query。
- supplemental search 失败或无结果。

规则：

- 每类错误生成稳定 `problem_code`。
- lesson 使用项目内模板生成，不让 LLM 自由发挥。
- 同一 `agent + problem_code + scope` upsert，累计 `trigger_count`，更新 `last_seen_at`。
- 保存最近少量脱敏 example，不保存完整 prompt。
- lesson 必须脱敏。

验收指标：

- ReportWriter 缺少 References 后写入 operational memory。
- JSON parse 连续失败后写入 operational memory。
- tool 参数 schema 错误后写入 operational memory。
- 敏感 token/API key 不进入 memory。
- 重复错误只更新计数，不刷多条。
- 后端完整测试通过。

### Phase 6：Reflection 参与 Agent 行为

把 operational memory 通过 `ContextBuilder` 接入对应 agent。

优先顺序：

1. `ReportWriter`
2. `ToolCallingAgentRunner`
3. `QueryRewriter`
4. `TodoPlanner`

规则：

- 只注入与当前 `agent + scope` 匹配的短 lesson。
- operational lessons 默认 top 5。
- lesson 是约束提示，不覆盖系统安全边界。
- 反思不能扩大 agent 工具权限。
- 反思不能跳过 validator、schema 或 citation 检查。
- 如果 lesson 与当前系统约束冲突，以系统约束和代码校验为准。

验收指标：

- ReportWriter 曾缺少 References 后，下次 prompt 包含对应 lesson。
- ToolCallingAgentRunner 曾出现 JSON parse failure 后，下次 fallback prompt 包含更明确 JSON-only lesson。
- QueryRewriter 不重复生成已记录失败 query。
- 注入 reflection 后完整 mock research flow 仍通过。
- validator 仍能拦截同类错误。
- 后端完整测试通过。

### Phase 7：Memory 与 Reflection 清理策略

增加最小维护能力，避免 memory 无限膨胀。

规则：

- research memory 过期后默认不自动复用，但可作为 stale context 使用。
- operational memory 按 `trigger_count` 和 `last_seen_at` 排序。
- 提供内部 prune helper：删除超期 low-confidence research memory、过旧低频 operational memory。
- prune 不自动在每次请求执行；只作为服务函数和测试入口，后续再决定是否暴露管理 API。

验收指标：

- low-confidence 过期 research memory 可被 prune。
- 高频 operational lesson 不会被 prune。
- prune 不删除未过期 high-confidence research memory。
- 后端完整测试通过。

### Phase 8：回归、文档和评测集

新增固定回归场景：

- 相同 topic 自动复用 memory。
- 相似 topic 使用 memory context 但不跳过搜索。
- 过期 memory 不复用。
- 低置信 memory 不复用。
- 用户要求最新时不复用。
- ReportWriter 失败记录 reflection，下次 prompt 注入 lesson。
- memory 很多时 context 仍受预算控制。
- memory 中敏感信息被脱敏。
- citation validator 仍拦截错误引用。
- NoteTool notes 与 MemoryStore 各自行为稳定。

文档更新：

- README 增加 memory/reuse/reflection 行为说明。
- 开发指南增加 MemoryStore、MemoryRetriever、ContextBuilder、ReflectionRecorder 扩展规则。
- 说明 memory 只是历史参考，不是新证据，不绕过 citation validation。
- 说明 v1 不使用向量库、embedding、tokenizer。

验收指标：

- 后端完整测试通过。
- 新增 memory 相关测试全部通过。
- 文档与实际默认配置一致。

## Assumptions

- v1 不引入向量库、embedding 或 tokenizer。
- v1 不做用户画像、长期偏好学习、知识图谱。
- v1 不新增公开 HTTP API；memory 先作为内部能力接入 orchestrator。
- v1 不让所有 agent 自由调用 memory tool；memory 检索由 orchestrator/service 层和 ContextBuilder 受控注入。
- v1 自动复用只针对完全相同 normalized topic；相似问题只辅助上下文。
- v1 reflection 只来自确定性失败，不使用 LLM 生成开放式心得。
- v1 memory 不替代当前证据，不绕过工具白名单、预算、脱敏、schema 校验或 citation validator。
