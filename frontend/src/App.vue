<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";

type TaskStatus = "pending" | "running" | "completed" | "failed";

interface SearchResult {
  title: string;
  url: string;
  snippet: string;
  source: string;
}

interface ResearchTask {
  title: string;
  intent: string;
  query: string;
  status: TaskStatus;
}

interface TaskSummary {
  task_title: string;
  content: string;
  sources: SearchResult[];
}

interface FinalReport {
  title: string;
  markdown: string;
  sources: SearchResult[];
}

interface StreamMessage {
  message?: string;
  task?: ResearchTask;
  tasks?: ResearchTask[];
  task_title?: string;
  results?: SearchResult[];
  summary?: TaskSummary;
  report?: FinalReport;
  topic?: string;
}

interface LogEntry {
  type: "status" | "error";
  message: string;
  time: string;
}

const API_BASE_URL = normalizeApiBaseUrl(
  import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.DEV ? "/api" : "http://localhost:8000"),
);

const topic = ref("");
const maxTasks = ref(3);
const isRunning = ref(false);
const currentStatus = ref("就绪");
const errorMessage = ref("");
const tasks = ref<ResearchTask[]>([]);
const summaries = ref<TaskSummary[]>([]);
const searchResultsByTask = ref<Record<string, SearchResult[]>>({});
const report = ref<FinalReport | null>(null);
const logs = ref<LogEntry[]>([]);
const eventSource = ref<EventSource | null>(null);

const canStart = computed(() => topic.value.trim().length > 0 && !isRunning.value);
const allSources = computed(() => {
  const sources = report.value?.sources ?? Object.values(searchResultsByTask.value).flat();
  const seen = new Set<string>();
  return sources.filter((source) => {
    if (seen.has(source.url)) return false;
    seen.add(source.url);
    return true;
  });
});
const renderedReport = computed(() => (report.value ? renderMarkdown(report.value.markdown) : ""));

async function startResearch() {
  if (!canStart.value) return;
  resetState();
  isRunning.value = true;
  currentStatus.value = "正在连接";

  if (!(await ensureBackendAvailable())) {
    isRunning.value = false;
    return;
  }

  const params = new URLSearchParams({
    topic: topic.value.trim(),
    max_tasks: String(maxTasks.value),
  });
  const source = new EventSource(`${API_BASE_URL}/research/stream?${params}`);
  eventSource.value = source;

  source.addEventListener("status", (event) => handleStatus(parseEvent(event)));
  source.addEventListener("task", (event) => handleTask(parseEvent(event)));
  source.addEventListener("search_results", (event) => handleSearchResults(parseEvent(event)));
  source.addEventListener("summary", (event) => handleSummary(parseEvent(event)));
  source.addEventListener("report", (event) => handleReport(parseEvent(event)));
  source.addEventListener("error", (event) => handleAppError(parseEvent(event)));
  source.addEventListener("done", (event) => {
    const data = parseEvent(event);
    currentStatus.value = data.topic ? `已完成：${data.topic}` : "已完成";
    isRunning.value = false;
    addLog("status", "研究完成");
    closeStream();
  });
  source.onerror = (event) => {
    if (isSseMessageEvent(event)) return;
    if (!isRunning.value) return;
    handleTransportError("连接中断，请确认后端服务正在运行。");
    isRunning.value = false;
    closeStream();
  };
}

function stopResearch() {
  addLog("status", "已停止当前研究");
  currentStatus.value = "已停止";
  isRunning.value = false;
  closeStream();
}

function resetState() {
  errorMessage.value = "";
  tasks.value = [];
  summaries.value = [];
  searchResultsByTask.value = {};
  report.value = null;
  logs.value = [];
  closeStream();
}

function normalizeApiBaseUrl(url: string) {
  return url.replace(/\/$/, "");
}

async function ensureBackendAvailable() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 3000);

  try {
    const response = await fetch(`${API_BASE_URL}/health`, {
      signal: controller.signal,
    });
    if (response.ok) return true;
    handleTransportError(backendUnavailableMessage());
    return false;
  } catch {
    handleTransportError(backendUnavailableMessage());
    return false;
  } finally {
    window.clearTimeout(timeout);
  }
}

function backendUnavailableMessage() {
  if (API_BASE_URL === "/api") {
    return (
      "找不到后端服务。请确认后端已在 http://localhost:8000 运行，" +
      "再刷新或重新点击开始。"
    );
  }
  return `找不到后端服务。请确认 ${API_BASE_URL} 可以访问。`;
}

function closeStream() {
  eventSource.value?.close();
  eventSource.value = null;
}

function parseEvent(event: Event): StreamMessage {
  const message = event as MessageEvent<string>;
  try {
    return JSON.parse(message.data) as StreamMessage;
  } catch {
    return { message: message.data };
  }
}

function isSseMessageEvent(event: Event): event is MessageEvent<string> {
  return "data" in event;
}

function handleStatus(data: StreamMessage) {
  const message = data.message ?? "状态更新";
  currentStatus.value = message;
  addLog("status", message);
}

function handleTask(data: StreamMessage) {
  if (data.tasks) {
    tasks.value = data.tasks;
    return;
  }
  if (!data.task) return;
  const index = tasks.value.findIndex((task) => task.title === data.task?.title);
  if (index >= 0) {
    tasks.value.splice(index, 1, data.task);
  } else {
    tasks.value.push(data.task);
  }
}

function statusText(status: TaskStatus) {
  const labels: Record<TaskStatus, string> = {
    pending: "待执行",
    running: "执行中",
    completed: "已完成",
    failed: "失败",
  };
  return labels[status];
}

function handleSearchResults(data: StreamMessage) {
  if (!data.task_title || !data.results) return;
  searchResultsByTask.value = {
    ...searchResultsByTask.value,
    [data.task_title]: data.results,
  };
  addLog("status", `${data.task_title}: 获取到 ${data.results.length} 条来源`);
}

function handleSummary(data: StreamMessage) {
  if (!data.summary) return;
  const index = summaries.value.findIndex(
    (summary) => summary.task_title === data.summary?.task_title,
  );
  if (index >= 0) {
    summaries.value.splice(index, 1, data.summary);
  } else {
    summaries.value.push(data.summary);
  }
  addLog("status", `${data.summary.task_title}: 总结完成`);
}

function handleReport(data: StreamMessage) {
  if (!data.report) return;
  report.value = data.report;
  addLog("status", "报告已生成");
}

function handleAppError(data: StreamMessage) {
  const message = data.message ?? "研究流程发生错误";
  errorMessage.value = translateMessage(message);
  currentStatus.value = "任务遇到问题";
  addLog("error", translateMessage(message));
}

function handleTransportError(message: string) {
  errorMessage.value = message;
  currentStatus.value = "连接异常";
  addLog("error", message);
}

function translateMessage(message: string) {
  const translations: Record<string, string> = {
    "Tavily search returned no results.": "Tavily 没有返回搜索结果。",
    "Tavily search timed out.": "Tavily 搜索超时。",
    "Tavily search request failed.": "Tavily 搜索请求失败。",
    "TAVILY_API_KEY is required.": "缺少 TAVILY_API_KEY。",
    "DASHSCOPE_API_KEY is required for Bailian LLM calls.": "缺少 DASHSCOPE_API_KEY。",
    "Report must include a title, overview, conclusion, and references.":
      "报告生成器输出缺少标题、概述、总结或参考文献，系统会尝试使用兜底报告。",
  };
  return translations[message] ?? message;
}

function addLog(type: LogEntry["type"], message: string) {
  logs.value.push({
    type,
    message,
    time: new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date()),
  });
}

function renderMarkdown(markdown: string) {
  const escaped = markdown
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  return escaped
    .split(/\n{2,}/)
    .map((block) => {
      if (block.startsWith("# ")) return `<h1>${inlineMarkdown(block.slice(2))}</h1>`;
      if (block.startsWith("## ")) return `<h2>${inlineMarkdown(block.slice(3))}</h2>`;
      if (block.startsWith("### ")) return `<h3>${inlineMarkdown(block.slice(4))}</h3>`;
      if (block.startsWith("- ")) {
        const items = block
          .split("\n")
          .map((line) => `<li>${inlineMarkdown(line.replace(/^- /, ""))}</li>`)
          .join("");
        return `<ul>${items}</ul>`;
      }
      return `<p>${inlineMarkdown(block).replace(/\n/g, "<br>")}</p>`;
    })
    .join("");
}

function inlineMarkdown(text: string) {
  return text
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/\[(.*?)\]\((https?:\/\/.*?)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

onBeforeUnmount(closeStream);
</script>

<template>
  <main class="app-shell">
    <section class="workbench" aria-labelledby="research-title">
      <header class="topbar">
        <div>
          <p class="eyebrow">深度研究 Agent</p>
          <h1 id="research-title">研究工作台</h1>
        </div>
        <div class="status-pill" :data-state="isRunning ? 'running' : 'idle'">
          <span class="status-dot" />
          {{ currentStatus }}
        </div>
      </header>

      <section class="control-panel" aria-label="研究控制区">
        <form class="topic-form" @submit.prevent="startResearch">
          <label for="topic">研究主题</label>
          <div class="input-row">
            <input
              id="topic"
              v-model="topic"
              name="topic"
              type="text"
              maxlength="200"
              placeholder="输入你要研究的问题"
            />
            <select v-model.number="maxTasks" aria-label="最大任务数">
              <option :value="3">3 个任务</option>
              <option :value="4">4 个任务</option>
              <option :value="5">5 个任务</option>
            </select>
            <button type="submit" :disabled="!canStart">开始</button>
            <button v-if="isRunning" type="button" class="secondary" @click="stopResearch">
              停止
            </button>
          </div>
        </form>
        <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>
      </section>

      <div class="workspace-grid">
        <aside class="task-panel" aria-label="任务进度">
          <div class="panel-heading">
            <h2>TODO 规划器</h2>
            <span>{{ tasks.length }} 个任务</span>
          </div>
          <ol v-if="tasks.length" class="task-list">
            <li v-for="task in tasks" :key="task.title" class="task-item" :data-status="task.status">
              <div class="task-title-row">
                <strong>{{ task.title }}</strong>
                <span>{{ statusText(task.status) }}</span>
              </div>
              <p>{{ task.intent }}</p>
              <code>{{ task.query }}</code>
            </li>
          </ol>
          <p v-else class="empty-state">开始研究后，这里会显示规划出的 TODO 任务。</p>
        </aside>

        <section class="report-panel" aria-label="研究报告">
          <div class="panel-heading">
            <h2>报告生成器</h2>
            <span>{{ report ? "已生成" : "等待中" }}</span>
          </div>
          <article v-if="report" class="report-content" v-html="renderedReport" />
          <div v-else class="report-empty">
            <h3>Markdown 报告会显示在这里</h3>
            <p>系统会在收集已完成任务的总结和来源后生成最终报告。</p>
          </div>
        </section>
      </div>

      <section class="lower-grid">
        <section class="summary-panel" aria-label="任务总结">
          <div class="panel-heading">
            <h2>任务总结器</h2>
            <span>{{ summaries.length }} 条总结</span>
          </div>
          <div v-if="summaries.length" class="summary-list">
            <article v-for="summary in summaries" :key="summary.task_title" class="summary-item">
              <h3>{{ summary.task_title }}</h3>
              <p>{{ summary.content }}</p>
            </article>
          </div>
          <p v-else class="empty-state">每个任务完成搜索后，对应总结会显示在这里。</p>
        </section>

        <section class="source-panel" aria-label="来源链接">
          <div class="panel-heading">
            <h2>来源</h2>
            <span>{{ allSources.length }} 个链接</span>
          </div>
          <ul v-if="allSources.length" class="source-list">
            <li v-for="source in allSources" :key="source.url">
              <a :href="source.url" target="_blank" rel="noreferrer">{{ source.title }}</a>
              <p>{{ source.snippet }}</p>
              <span>{{ source.source }}</span>
            </li>
          </ul>
          <p v-else class="empty-state">SearchTool 返回的来源和报告参考链接会显示在这里。</p>
        </section>

        <section class="log-panel" aria-label="运行日志">
          <div class="panel-heading">
            <h2>运行日志</h2>
            <span>{{ logs.length }} 条记录</span>
          </div>
          <ol v-if="logs.length" class="log-list">
            <li v-for="(log, index) in logs" :key="`${log.time}-${index}`" :data-type="log.type">
              <time>{{ log.time }}</time>
              <span>{{ log.message }}</span>
            </li>
          </ol>
          <p v-else class="empty-state">状态、错误和进度更新会显示在这里。</p>
        </section>
      </section>
    </section>
  </main>
</template>
