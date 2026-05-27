# Agent 工具调用与外部 Skill 系统开发计划

## Summary

项目后续采用“框架务实优先”的开发策略：框架只在能明显降低复杂度、提高可维护性或减少自研风险时使用；业务约束、安全边界和可测试行为仍由项目代码明确掌控。当前 `ResearchOrchestrator` 已基于 LangGraph `StateGraph` 重构，这一部分可以继续保留；但后续开发不强制所有能力都必须基于 LangGraph 或某个特定框架。

架构取向：

- 对稳定、可复用、框架擅长的流程编排，可继续使用 LangGraph。
- 对业务强约束、安全策略、错误码、预算、日志、脱敏、引用校验和 skill 沙箱边界，优先使用项目自研代码。
- 如果某个功能用框架会引入额外抽象、调试成本或测试不确定性，则直接用项目代码实现。

当前已完成的基础能力：

- `ResearchOrchestrator` 已切换为 LangGraph `StateGraph`，当前主流程为 `plan -> execute_task -> write_report`。
- `ToolCallingAgentRunner` 已提供通用工具调用循环，支持 native function calling、JSON fallback、工具预算、错误码、工具结果反馈和日志。
- `ToolRegistry` 已作为集中式业务工具注册入口，现有工具包括 `search_web`、`summarize_task`、`save_note`、`write_report`。

后续开发原则：

- 新的流程能力优先评估是否适合作为 LangGraph node 或普通服务函数；选择以代码清晰、测试稳定和后续扩展成本最低为准。
- 新的业务动作优先作为 `ToolRegistry` tool 增加。
- agent 的输出必须回到项目内 Pydantic 模型，不能让 LangGraph 或 LLM 原始消息泄露到 API 边界。
- 所有新增循环、分支或 agent 调用必须有明确上限、失败兜底和可测试状态。

## Target Architecture

当前推荐目标结构：

```text
ResearchOrchestrator
└── LangGraph StateGraph
    ├── plan
    ├── execute_task
    ├── judge_evidence
    ├── rewrite_queries
    ├── supplemental_search
    ├── write_report
    └── done

ToolRegistry
├── search_web
├── summarize_task
├── save_note
├── write_report
├── load_skill
├── judge_evidence
└── rewrite_queries

SkillRegistry
└── skills/<skill_name>/SKILL.md
```

边界：

- 如果使用 LangGraph node，则 node 只负责调度和状态更新，不直接绕过 `ToolRegistry` 执行业务工具。
- `ToolRegistry` 只负责注册、校验、执行、日志和脱敏，不负责全局流程分支。
- `ReportWriter` 只负责写报告和引用校验，不负责补充搜索调度。
- Skill 第一版只读取 `SKILL.md`，不执行 `scripts/`，不读取 skill 目录外文件。

## Phase 0: 当前编排基线固化

状态：已完成基础重构，后续只需补强文档和测试。

目标：确认当前研究主流程行为稳定，并冻结外部 API、事件和测试契约。当前实现使用 LangGraph，但验收重点是行为稳定，而不是框架本身。

开发内容：

- `ResearchOrchestrator` 使用 `StateGraph` 编排：
  - `plan`
  - `execute_task`
  - `write_report`
- 保留现有 API 返回结构：
  - `ResearchResult`
  - `TaskExecutionRecord`
  - `FinalReport`
  - SSE events
  - tool logs
- 保留现有工具调用运行层：
  - `ToolCallingAgentRunner`
  - `ToolCallingResearchExecutor`
  - `ToolRegistry`

验收指标：

- graph 编译结果是 LangGraph `CompiledStateGraph`。
- 原有 orchestrator mock flow 行为不变。
- SSE 事件顺序不回退。
- 后端完整测试通过。

## Phase 1: 流程状态与节点/服务约定整理

目标：让后续阶段能稳定扩展流程状态，而不是在节点或服务函数之间临时拼字段。

开发内容：

- 将当前 `_ResearchGraphState` 从 orchestrator 私有结构整理为可维护的状态模型或专用 TypedDict；如果后续发现 LangGraph 对某段逻辑收益不高，也允许同一状态模型被普通服务函数复用。
- 明确流程 state 字段：
  - `topic`
  - `max_tasks`
  - `planned_tasks`
  - `current_index`
  - `execution_records`
  - `completed_summaries`
  - `failed_tasks`
  - `sources`
  - `evidence_judgement`
  - `supplemental_queries`
  - `supplemental_rounds_used`
  - `report`
- 增加状态构造和来源去重 helper。
- graph node 或服务函数只返回自己修改的状态字段。
- 为 graph node / 服务函数命名和路由函数建立约定：
  - node 使用动词短语，例如 `judge_evidence`
  - route 使用 `route_after_<node>`
  - 所有循环 route 必须检查上限

验收指标：

- 当前 `plan -> execute_task -> write_report` 行为不变。
- 流程 state 字段有测试覆盖。
- 来源 URL 去重逻辑有单元测试。
- 后端完整测试通过。

## Phase 2: 外部 Skill 系统

目标：用户可从外部获取 skill，放入 `skills/` 后，agent 可以通过 `load_skill` 工具按需读取操作手册。

开发内容：

- 新增 `SkillRegistry`。
- 默认扫描项目根目录下的 `skills/`。
- 每个 skill 使用目录结构：

```text
skills/
└── <skill_name>/
    ├── SKILL.md
    ├── scripts/
    ├── references/
    └── assets/
```

- 第一版只要求 `SKILL.md` 必须存在。
- 第一版只读取：
  - `skills/<skill_name>/SKILL.md`
- 第一版不执行：
  - `scripts/`
  - 任意 shell
  - 任意外部命令
- 第一版不读取：
  - skill 目录外文件
  - 任意用户指定路径
- 新增业务工具 `load_skill`：
  - 参数：`skill_name`
  - 返回：skill 名称、手册内容、可选元信息
- `load_skill` 注册到 `ToolRegistry`，让所有 agent 通过同一份工具 definitions 看到它。

验收指标：

- `skills/foo/SKILL.md` 存在时，`load_skill(foo)` 能读取内容。
- skill 不存在时返回可测试错误码。
- `SKILL.md` 缺失时返回可测试错误码。
- `skill_name` 不能路径穿越，例如 `../` 必须拒绝。
- 不执行 `scripts/`。
- 不读取 skill 目录外文件。
- agent 可在工具调用循环中调用 `load_skill`，并继续完成原任务。
- README 说明 skill 目录格式和限制。
- 后端完整测试通过。

## Phase 3: TaskSummarizer 接入 Skill 能力

目标：`TaskSummarizer` 可以在需要时读取 skill，再完成总结；简单任务仍可直接总结。

开发内容：

- 保持公共行为不变：
  - 输入仍是 `ResearchTask + SearchResult[]`
  - 输出仍是 `TaskSummary`
- 将总结过程接入通用工具调用运行层，允许调用 `load_skill`。
- 不强制要求一定调用 skill。
- 如果 skill 加载失败，第一版采用回退普通总结，并记录工具失败日志。
- 总结输出仍需保留来源引用。
- 来源少于 3 条时，仍需标记 `资料不足` 或 `待验证`。

流程约束：

- `execute_task` 的外部行为不变。
- `TaskSummarizer` 的 skill 调用仍通过 `ToolRegistry`。
- 不在任务执行编排层硬编码 skill 选择逻辑。

验收指标：

- 简单任务不调用 skill 也能总结成功。
- 复杂任务可调用 `load_skill` 后总结成功。
- skill 不存在时回退普通总结，并记录原因。
- `TaskSummary` 结构不变。
- 原有 TaskSummarizer 测试继续通过。
- 工具日志包含 skill 调用记录。
- 后端完整测试通过。

## Phase 4: EvidenceJudge 节点

目标：所有任务走完后，在写最终报告前整体判断证据链是否足够。

开发内容：

- 新增 `EvidenceJudgement` 模型：
  - `is_sufficient: bool`
  - `confidence: high | medium | low`
  - `gaps: list[str]`
  - `rationale: str`
- 新增 `EvidenceJudge` agent。
- 新增业务工具 `judge_evidence`。
- 输入：
  - topic
  - completed summaries
  - sources
  - failed tasks
- 输出写入流程 state：
  - `evidence_judgement`
- 在当前编排层新增 evidence 判断步骤；优先作为 LangGraph node，如果实现更简单也可封装为普通服务函数后由 node 调用：
  - `judge_evidence`
- 位置：
  - `execute_task` 全部完成后
  - `write_report` 之前
- 判断失败时，不阻塞报告生成；流程 state 按证据不足处理。

验收指标：

- 证据充足时，`evidence_judgement.is_sufficient=True`。
- 证据不足时，输出明确 gaps。
- 判断结果可序列化并进入报告生成上下文。
- 判断失败时，不阻塞报告生成。
- 工具日志记录 evidence 判断结果。
- 后端完整测试通过。

## Phase 5: 证据不足时补充搜索一轮

目标：证据不足时，系统自动补充搜索，但严格限制成本和循环。

开发内容：

- 新增 `QueryRewriter` agent。
- 新增业务工具 `rewrite_queries`。
- 输入：
  - topic
  - evidence gaps
  - existing task queries
  - existing source summaries
  - `max_queries=2`
- 输出：
  - 最多 2 个新 query
- 在当前编排层新增补充搜索步骤；优先作为 LangGraph node，如果框架抽象增加复杂度，则保留清晰的普通服务函数并由编排层调用：
  - `rewrite_queries`
  - `supplemental_search`
- 新增流程 state：
  - `supplemental_queries`
  - `supplemental_rounds_used`
- 条件分支：
  - `judge_evidence` 充足 -> `write_report`
  - `judge_evidence` 不足且 `supplemental_rounds_used == 0` -> `rewrite_queries`
  - `judge_evidence` 不足且已补充过 -> `write_report`
- 补充搜索规则：
  - 最多 1 轮
  - 最多 2 个 query
  - 每个 query 调用一次 `search_web`
  - 结果按 URL 去重后合并到流程 state sources
- 补充搜索完成后重新进入 `judge_evidence`。
- 补充搜索失败不终止整体流程。

验收指标：

- 证据不足时最多生成 2 个 query。
- 最多只补充搜索 1 轮。
- 重复 URL 被去重。
- 补充搜索失败时仍生成报告。
- 补充后 evidence 充足时，报告不强制标记不足。
- 补充后仍不足时，报告必须明确提示“证据不足 / 待验证 / 仅供参考”。
- 不出现无限循环。
- 后端完整测试通过。

## Phase 6: ReportWriter 接收证据状态

目标：`ReportWriter` 只负责写报告，不负责调度上层 agent；但必须使用 evidence 状态影响报告表达。

开发内容：

- `ReportWriter.write` 输入增加 evidence 判断结果。
- 如果 evidence 不足，prompt 明确要求：
  - 标注证据不足
  - 区分高置信结论和待验证结论
  - 不夸大现有证据
- 保留现有 citation validator。
- 保留 fallback report。
- fallback report 也要包含 evidence 状态。
- `write_report` 步骤从 state 中读取：
  - `completed_summaries`
  - `sources`
  - `failed_tasks`
  - `evidence_judgement`

验收指标：

- 证据充足报告仍通过 citation validation。
- 证据不足报告包含明确风险提示。
- 缺少来源 URL、引用 marker 不一致等问题仍会被 validator 拦截。
- `ReportWriter` 不直接调用补充搜索。
- 报告生成失败时仍使用 fallback report。
- fallback report 包含 evidence 状态。
- 后端完整测试通过。

## Phase 7: 回归测试与文档

目标：把编排机制和业务工具机制固化为可维护行为，不把质量依赖绑定到某个框架名称上。

测试场景：

- 如果继续使用 LangGraph，graph 编译为 `CompiledStateGraph`。
- 如果继续使用 LangGraph，graph 包含预期节点和条件边。
- 如果某段流程改为普通服务函数，则必须有同等行为测试覆盖。
- 当前完整 mock research flow 仍通过。
- 所有 agent 共享完整业务工具 definitions。
- `load_skill` 正常读取外部 skill。
- `load_skill` 拒绝路径穿越。
- `load_skill` 不执行 scripts。
- TaskSummarizer 简单任务直接总结。
- TaskSummarizer 复杂任务调用 skill 后总结。
- EvidenceJudge 判断充足，不触发补充搜索。
- EvidenceJudge 判断不足，触发最多 2 个 query。
- 补充搜索只执行 1 轮。
- 补充搜索后仍不足，报告明确提示证据不足。
- 补充搜索失败，仍生成报告。
- native tools、JSON fallback、降级路径全部通过。
- 后端完整测试通过。

文档更新：

- README 增加：
  - 当前编排结构，以及哪些部分使用 LangGraph
  - agent 基础工具调用机制
  - 业务工具和认知 skill 的区别
  - `skills/<name>/SKILL.md` 目录约定
  - 第一版 skill 不执行 scripts
  - 证据不足时的补充搜索限制
- 开发文档增加：
  - 如何判断新增能力应放入 LangGraph node、普通服务函数还是业务工具
  - 如何新增 LangGraph node
  - 如何新增业务工具
  - 如何安装外部 skill
  - 如何调试 graph state 和工具调用日志

## Assumptions

- 后续开发以质量、清晰度、可测试性和维护成本为最高优先级，不强制绑定 LangGraph 或某个特定框架。
- 当前已基于 LangGraph 的主流程可以继续保留，除非后续证明它增加了明显复杂度。
- 所有 agent 都能看到全部业务工具，由 LLM 根据 `name` 和 `description` 自主选择。
- 后端仍保留强约束：只执行注册工具，参数必须通过 schema，调用次数必须在预算内。
- Skill 第一版只读 `SKILL.md`，不执行 `scripts/`。
- Skill 目录固定为 `skills/<skill_name>/SKILL.md`。
- 证据不足时最多补充搜索 1 轮，最多 2 个新 query。
- 证据充分性第一版只在最终报告前整体判断。
- 编排步骤失败必须有明确兜底策略，不能让单个非关键 agent 失败导致整个报告无法生成。
