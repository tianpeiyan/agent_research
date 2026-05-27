---
name: research-task-summarizer
description: Summarize a single research task from search results into concise Markdown with source citations. Use when an agent needs to produce a TaskSummary from ResearchTask plus SearchResult evidence, especially when evidence is sparse, conflicting, or must clearly mark 资料不足 / 待验证.
---

# Research Task Summarizer

## Workflow

1. Identify the task intent before writing conclusions.
2. Use only the provided sources; do not introduce unsupported facts.
3. Preserve citation markers like `[1]` next to claims that depend on sources.
4. If fewer than 3 sources are provided, explicitly mark the summary as `资料不足` or `待验证`.
5. If sources conflict, separate confirmed findings from unresolved claims.
6. Keep the summary concise enough for a final report writer to reuse directly.

## Output Shape

Return Markdown only for the summary content. Prefer this structure:

```markdown
### Key Findings
- Finding with citation [1].
- Another finding with citation [2].

### Evidence Limits
资料不足 / 待验证: explain the specific limitation.
```

## Quality Rules

- Every factual bullet should cite at least one source marker.
- Do not cite source numbers that were not provided.
- Do not include a references section; the caller keeps source objects separately.
- Do not claim the evidence is complete when the source count is below 3.
- Do not mention this skill or the tool call in the summary.
