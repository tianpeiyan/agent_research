import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../src/App.vue";

type Listener = (event: MessageEvent<string>) => void;

class MockEventSource {
  static instances: MockEventSource[] = [];

  url: string;
  onerror: (() => void) | null = null;
  close = vi.fn();
  listeners = new Map<string, Listener[]>();

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: Listener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type: string, data: unknown) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
    if (type === "error") this.onerror?.(event);
  }

  emitTransportError() {
    this.onerror?.(new Event("error"));
  }
}

describe("App", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(null, { status: 200 }))));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the research workspace controls", () => {
    const wrapper = mount(App);

    expect(wrapper.get("h1").text()).toBe("研究工作台");
    expect(wrapper.get("label").text()).toBe("研究主题");
    expect(wrapper.get("input").attributes("placeholder")).toBe(
      "输入你要研究的问题",
    );
    expect(wrapper.text()).toContain("TODO 规划器");
    expect(wrapper.text()).toContain("任务总结器");
    expect(wrapper.text()).toContain("报告生成器");
  });

  it("starts an SSE research run and renders streamed progress", async () => {
    const wrapper = mount(App);

    await wrapper.get("input").setValue("AI agent evaluation");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    const source = MockEventSource.instances[0];
    expect(fetch).toHaveBeenCalledWith("/api/health", expect.any(Object));
    expect(source.url).toContain("/api/research/stream?");
    expect(source.url).toContain("/research/stream?");
    expect(source.url).toContain("topic=AI+agent+evaluation");
    expect(source.url).toContain("max_tasks=3");

    source.emit("status", { message: "正在规划" });
    source.emit("task", {
      tasks: [
        {
          title: "Evaluation",
          intent: "Find criteria",
          query: "AI agent evaluation criteria",
          status: "pending",
        },
      ],
    });
    source.emit("task", {
      task: {
        title: "Evaluation",
        intent: "Find criteria",
        query: "AI agent evaluation criteria",
        status: "running",
      },
    });
    source.emit("search_results", {
      task_title: "Evaluation",
      results: [
        {
          title: "Source 1",
          url: "https://example.com/source-1",
          snippet: "Useful evidence.",
          source: "example.com",
        },
      ],
    });
    source.emit("summary", {
      summary: {
        task_title: "Evaluation",
        content: "Agents need grounded evaluation [1].",
        sources: [],
      },
    });
    source.emit("report", {
      report: {
        title: "AI agent evaluation",
        markdown:
          "# AI agent evaluation\n\n## Overview\nAgents need grounded evaluation [1].\n\n## References\n[1](https://example.com/source-1)",
        sources: [
          {
            title: "Source 1",
            url: "https://example.com/source-1",
            snippet: "Useful evidence.",
            source: "example.com",
          },
        ],
      },
    });
    source.emit("done", { topic: "AI agent evaluation" });
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain("Evaluation");
    expect(wrapper.text()).toContain("执行中");
    expect(wrapper.text()).toContain("获取到 1 条来源");
    expect(wrapper.text()).toContain("Agents need grounded evaluation");
    expect(wrapper.html()).toContain("<h1>AI agent evaluation</h1>");
    const links = wrapper.findAll('a[href="https://example.com/source-1"]');
    expect(links.some((link) => link.text() === "Source 1")).toBe(true);
    expect(source.close).toHaveBeenCalled();
  });

  it("shows application stream errors without treating them as connection drops", async () => {
    const wrapper = mount(App);

    await wrapper.get("input").setValue("AI agent evaluation");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    const source = MockEventSource.instances[0];
    source.emit("error", { message: "Tavily search returned no results." });
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain("Tavily 没有返回搜索结果。");
    expect(wrapper.text()).not.toContain("连接中断，请确认后端服务正在运行。");
    expect(wrapper.get('[role="alert"]').text()).toBe("Tavily 没有返回搜索结果。");
    expect(source.close).not.toHaveBeenCalled();
  });

  it("shows connection errors only for transport failures", async () => {
    const wrapper = mount(App);

    await wrapper.get("input").setValue("AI agent evaluation");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    const source = MockEventSource.instances[0];
    source.emitTransportError();
    await wrapper.vm.$nextTick();

    expect(wrapper.text()).toContain("连接中断，请确认后端服务正在运行。");
    expect(source.close).toHaveBeenCalled();
  });

  it("checks backend health before opening the research stream", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("connection refused"));
    const wrapper = mount(App);

    await wrapper.get("input").setValue("AI agent evaluation");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(MockEventSource.instances).toHaveLength(0);
    expect(wrapper.get('[role="alert"]').text()).toBe(
      "找不到后端服务。请确认后端已在 http://localhost:8000 运行，再刷新或重新点击开始。",
    );
  });
});
