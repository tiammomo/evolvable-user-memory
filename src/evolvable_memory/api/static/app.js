"use strict";

const apiPort = document.querySelector('meta[name="api-port"]')?.content || "38089";
const API_BASE = `${window.location.protocol}//${window.location.hostname}:${apiPort}`;

const state = {
  scope: {
    tenantId: localStorage.getItem("emf.tenantId") || "demo",
    subjectId: localStorage.getItem("emf.subjectId") || "alice",
  },
  counts: { writes: 0, recalls: 0, outcomes: 0, results: 0 },
  lastTrace: null,
  activities: [],
  journey: { write: false, view: false, recall: false, outcome: false },
  scopeGeneration: 0,
  scopeControllers: new Set(),
  requestChannels: new Map(),
  idempotencyOperations: new Map(),
  healthController: null,
  storage: "unknown",
};

const viewCopy = {
  overview: ["记忆工作台", "MEMORY OVERVIEW"],
  capture: ["写入记忆", "CAPTURE EVIDENCE"],
  memories: ["当前记忆", "ACTIVE BELIEFS"],
  recall: ["记忆召回", "CONTEXTUAL RECALL"],
  architecture: ["系统架构", "FIVE-PLANE MODEL"],
};

const scoreLabels = {
  semantic: "词法匹配",
  context: "上下文",
  belief: "信念",
  utility: "效用",
  recency: "时效",
};

const ONBOARDING_STORAGE_KEY = "emf.onboarding.v1";
const ONBOARDING_DISMISSED_KEY = "emf.onboarding.dismissed";
const API_TIMEOUT_MS = 12000;
const HEALTH_TIMEOUT_MS = 3500;
const DIALOG_FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

const onboardingSteps = [
  {
    eyebrow: "01 · SCOPE",
    title: "先确认数据属于谁",
    description: "每次操作都必须明确租户和用户。默认的 demo / alice 适合本地体验，也能帮助你直观看到隔离边界。",
    points: [
      "tenant_id 隔离不同租户的数据。",
      "subject_id 标识租户内的具体用户。",
      "切换作用域后，列表、修订历史和召回都会重新读取。",
    ],
    note: "页面中的 Scope 只是开发合同；生产环境必须从可信认证上下文派生。",
  },
  {
    eyebrow: "02 · EVIDENCE",
    title: "从原始证据写入记忆",
    description: "记忆不是直接修改的一段文本。系统先保存用户原始表达，再形成带上下文和置信度的不可变修订。",
    points: [
      "稳定的记忆键描述要记住哪类事实。",
      "原始证据保持不变，后续修正不会覆盖它。",
      "上下文让同一偏好在不同场景中共存。",
    ],
    note: "第一次体验可以载入“晚间低因咖啡”示例，不需要自己设计字段。",
  },
  {
    eyebrow: "03 · BELIEF",
    title: "查看当前信念，而非改写历史",
    description: "“当前记忆”展示每条偏好的最新修订，同时保留完整版本链、证据数量和置信度。",
    points: [
      "刷新或打开列表是只读操作。",
      "修正会追加新 Revision，旧版本仍可追溯。",
      "租户或用户不匹配时无法读取其他作用域的数据。",
    ],
    note: "读取得再多，也不会让一条记忆自动变得更可信。",
  },
  {
    eyebrow: "04 · TRACE",
    title: "在当前语境中召回",
    description: "系统综合词法相关性、上下文、信念、效用与时效性排序，并为每次召回保存独立 Trace。",
    points: [
      "查询描述当前需要解决的问题。",
      "上下文用于匹配记忆成立的场景。",
      "评分分量解释结果为什么排在这里。",
    ],
    note: "召回只生成可审计投影和 Trace，不会改变信念或效用。",
  },
  {
    eyebrow: "05 · OUTCOME",
    title: "用真实结果完成学习闭环",
    description: "对召回结果提交“有帮助”或“无帮助”，Outcome 会引用刚才的 Trace，并更新该上下文中的效用。",
    points: [
      "反馈必须引用确实包含该 Revision 的 Trace。",
      "重复提交由幂等键保护，不会重复累计。",
      "效用来自可归因结果，而不是读取次数。",
    ],
    note: "点击下方按钮会载入示例，带你从第二步开始实际操作。",
  },
];

let onboardingStepIndex = 0;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function createElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function newId(prefix) {
  const randomPart = globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2);
  return `${prefix}:${Date.now()}:${randomPart}`;
}

function stableSerialize(value) {
  if (Array.isArray(value)) return `[${value.map((item) => stableSerialize(item)).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableSerialize(item)}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value);
}

function idempotencyKeyFor(operation, prefix, payload) {
  const fingerprint = stableSerialize(payload);
  const previous = state.idempotencyOperations.get(operation);
  if (previous?.fingerprint === fingerprint) return previous.key;
  const key = newId(prefix);
  state.idempotencyOperations.set(operation, { fingerprint, key });
  return key;
}

function shortId(value) {
  if (!value) return "—";
  return value.length > 13 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function getScope() {
  return {
    tenant_id: state.scope.tenantId,
    subject_id: state.scope.subjectId,
  };
}

function describeError(error) {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "发生了未知错误";
}

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

class ScopeChangedError extends Error {
  constructor() {
    super("作用域已切换，请求已取消");
    this.name = "ScopeChangedError";
  }
}

function isRequestCancelled(error) {
  return error?.name === "AbortError" || error instanceof ScopeChangedError;
}

async function api(path, options = {}) {
  const {
    timeoutMs = API_TIMEOUT_MS,
    signal: parentSignal,
    ...fetchOptions
  } = options;
  const controller = new AbortController();
  let timedOut = false;
  const forwardAbort = () => controller.abort();
  if (parentSignal?.aborted) controller.abort();
  else parentSignal?.addEventListener("abort", forwardAbort, { once: true });
  const timeout = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  const request = {
    ...fetchOptions,
    headers: { "content-type": "application/json", ...(fetchOptions.headers || {}) },
    signal: controller.signal,
  };
  try {
    const response = await fetch(`${API_BASE}${path}`, request);
    const contentType = response.headers.get("content-type") || "";
    const body = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      let message = `请求失败（HTTP ${response.status}）`;
      if (typeof body === "object" && body) {
        if (typeof body.detail === "string") message = body.detail;
        if (Array.isArray(body.detail)) {
          message = body.detail.map((item) => item.msg || "参数校验失败").join("；");
        }
      }
      throw new ApiError(message, response.status);
    }
    return body;
  } catch (error) {
    if (timedOut) throw new ApiError("请求超时，请确认 API 已启动后重试。", 408);
    throw error;
  } finally {
    globalThis.clearTimeout(timeout);
    parentSignal?.removeEventListener("abort", forwardAbort);
  }
}

async function scopedApi(path, options = {}, channel = "") {
  const generation = state.scopeGeneration;
  const controller = new AbortController();
  if (channel) state.requestChannels.get(channel)?.abort();
  if (channel) state.requestChannels.set(channel, controller);
  state.scopeControllers.add(controller);
  try {
    const result = await api(path, { ...options, signal: controller.signal });
    if (generation !== state.scopeGeneration) throw new ScopeChangedError();
    return result;
  } finally {
    state.scopeControllers.delete(controller);
    if (channel && state.requestChannels.get(channel) === controller) {
      state.requestChannels.delete(channel);
    }
  }
}

function cancelScopedRequests() {
  state.scopeGeneration += 1;
  for (const controller of state.scopeControllers) controller.abort();
  state.scopeControllers.clear();
  state.requestChannels.clear();
}

function setLoading(button, loading) {
  if (!button) return;
  button.disabled = loading;
  button.classList.toggle("is-loading", loading);
  button.setAttribute("aria-busy", String(loading));
}

function toast(title, message, type = "success") {
  const region = $("#toast-region");
  const item = createElement("div", `toast${type === "error" ? " is-error" : ""}`);
  item.setAttribute("role", type === "error" ? "alert" : "status");
  const mark = createElement("span", "toast-mark", type === "error" ? "!" : "✓");
  const copy = createElement("div");
  copy.append(createElement("strong", "", title), createElement("p", "", message));
  const close = createElement("button", "", "×");
  close.type = "button";
  close.setAttribute("aria-label", "关闭通知");
  close.addEventListener("click", () => item.remove());
  item.append(mark, copy, close);
  region.append(item);
  window.setTimeout(() => item.remove(), 5200);
}

function renderOnboardingStep() {
  const step = onboardingSteps[onboardingStepIndex];
  const position = onboardingStepIndex + 1;
  $("#onboarding-count").textContent = `第 ${position} 步，共 ${onboardingSteps.length} 步`;
  $("#onboarding-eyebrow").textContent = step.eyebrow;
  $("#onboarding-title").textContent = step.title;
  $("#onboarding-description").textContent = step.description;
  $("#onboarding-note").textContent = step.note;
  $("#onboarding-visual-number").textContent = String(position).padStart(2, "0");

  const points = $("#onboarding-points");
  points.replaceChildren(...step.points.map((point) => createElement("li", "", point)));

  const dots = $("#onboarding-dots");
  dots.replaceChildren();
  onboardingSteps.forEach((item, index) => {
    const dot = createElement("button", index === onboardingStepIndex ? "is-active" : index < onboardingStepIndex ? "is-done" : "");
    dot.type = "button";
    dot.setAttribute("aria-label", `转到第 ${index + 1} 步：${item.title}`);
    if (index === onboardingStepIndex) dot.setAttribute("aria-current", "step");
    dot.addEventListener("click", () => {
      onboardingStepIndex = index;
      renderOnboardingStep();
    });
    dots.append(dot);
  });

  $("#onboarding-prev").disabled = onboardingStepIndex === 0;
  $("#onboarding-next").textContent = onboardingStepIndex === onboardingSteps.length - 1 ? "载入示例并开始" : "下一步";
}

function showOnboarding(force = false) {
  if (
    !force
    && (
      localStorage.getItem(ONBOARDING_STORAGE_KEY) === "complete"
      || sessionStorage.getItem(ONBOARDING_DISMISSED_KEY) === "true"
    )
  ) return;
  onboardingStepIndex = 0;
  renderOnboardingStep();
  const dialog = $("#onboarding-dialog");
  if (!dialog.open) dialog.showModal();
}

function dismissOnboarding() {
  sessionStorage.setItem(ONBOARDING_DISMISSED_KEY, "true");
  const dialog = $("#onboarding-dialog");
  if (dialog.open) dialog.close();
}

function completeOnboarding() {
  localStorage.setItem(ONBOARDING_STORAGE_KEY, "complete");
  sessionStorage.removeItem(ONBOARDING_DISMISSED_KEY);
  const dialog = $("#onboarding-dialog");
  if (dialog.open) dialog.close();
}

function advanceOnboarding() {
  if (onboardingStepIndex < onboardingSteps.length - 1) {
    onboardingStepIndex += 1;
    renderOnboardingStep();
    return;
  }
  completeOnboarding();
  loadExample();
}

function updateStats() {
  $("#stat-writes").textContent = String(state.counts.writes);
  $("#stat-recalls").textContent = String(state.counts.recalls);
  $("#stat-outcomes").textContent = String(state.counts.outcomes);
  $("#stat-result-count").textContent = state.counts.recalls
    ? `最近一次返回 ${state.counts.results} 条结果`
    : "尚无召回结果";
  $("#stat-scope").textContent = state.scope.tenantId;
  $("#stat-subject").textContent = state.scope.subjectId;
}

function updateJourney() {
  $("#journey-scope").classList.add("is-done");
  $("#journey-scope small").textContent = `${state.scope.tenantId} / ${state.scope.subjectId}`;
  $("#journey-write").classList.toggle("is-done", state.journey.write);
  $("#journey-view").classList.toggle("is-done", state.journey.view);
  $("#journey-recall").classList.toggle("is-done", state.journey.recall);
  $("#journey-outcome").classList.toggle("is-done", state.journey.outcome);

  const completed = 1 + [state.journey.write, state.journey.view, state.journey.recall, state.journey.outcome].filter(Boolean).length;
  const isComplete = completed === 5;
  $("#journey-progress").textContent = `已完成 ${completed} / 5`;
  $(".quickstart-panel").classList.toggle("is-complete", isComplete);
  $("#quickstart-title").textContent = isComplete ? "恭喜，首条记忆闭环已完成" : "第一次使用？完成这五步";

  const action = $("#start-example");
  if (!state.journey.write) action.textContent = "载入示例并开始";
  else if (!state.journey.view) action.textContent = "下一步：查看记忆";
  else if (!state.journey.recall) action.textContent = "下一步：执行召回";
  else if (!state.journey.outcome) action.textContent = "下一步：提交反馈";
  else action.textContent = "重新查看新手引导";
}

function addActivity(kind, title, detail) {
  const symbols = { write: "+", recall: "↗", outcome: "✓", correction: "↻" };
  state.activities.unshift({ kind, title, detail, time: new Date() });
  state.activities = state.activities.slice(0, 8);
  renderActivities(symbols);
}

function renderActivities(symbols = { write: "+", recall: "↗", outcome: "✓", correction: "↻" }) {
  const list = $("#activity-list");
  list.replaceChildren();
  if (!state.activities.length) {
    const empty = createElement("div", "empty-activity");
    const mark = createElement("span");
    mark.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12h4l2-5 4 10 2-5h4" /></svg>';
    const copy = createElement("div");
    copy.append(
      createElement("strong", "", "等待第一条活动"),
      createElement("p", "", "写入、召回与反馈会显示在这里。"),
    );
    empty.append(mark, copy);
    list.append(empty);
    return;
  }
  for (const activity of state.activities) {
    const row = createElement("div", "activity-item");
    row.append(createElement("span", "", symbols[activity.kind] || "·"));
    const copy = createElement("div");
    copy.append(createElement("strong", "", activity.title), createElement("p", "", activity.detail));
    const time = createElement("time", "", activity.time.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }));
    row.append(copy, time);
    list.append(row);
  }
}

function goToView(name) {
  if (!viewCopy[name]) return;
  $$(".view").forEach((view) => view.classList.toggle("is-active", view.id === `view-${name}`));
  $$(".nav-item").forEach((item) => {
    const isActive = item.dataset.view === name;
    item.classList.toggle("is-active", isActive);
    if (isActive) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $("#page-title").textContent = viewCopy[name][0];
  $("#page-eyebrow").textContent = viewCopy[name][1];
  closeMobileMenu();
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  window.scrollTo({ top: 0, behavior });
  if (name === "memories") {
    if (state.journey.write) {
      state.journey.view = true;
      updateJourney();
    }
    loadMemories();
  }
}

function openMobileMenu() {
  $("#sidebar").classList.add("is-open");
  $("#mobile-scrim").classList.add("is-open");
  $("#mobile-menu").setAttribute("aria-expanded", "true");
}

function closeMobileMenu() {
  $("#sidebar").classList.remove("is-open");
  $("#mobile-scrim").classList.remove("is-open");
  $("#mobile-menu").setAttribute("aria-expanded", "false");
}

function addContextRow(containerId, key = "", value = "") {
  const container = document.getElementById(containerId);
  const row = createElement("div", "context-row");
  const keyInput = createElement("input");
  keyInput.type = "text";
  keyInput.className = "context-key";
  keyInput.placeholder = "上下文键";
  keyInput.value = key;
  keyInput.setAttribute("aria-label", "上下文键");
  const equals = createElement("span", "context-equals", "=");
  const valueInput = createElement("input");
  valueInput.type = "text";
  valueInput.className = "context-value";
  valueInput.placeholder = "值";
  valueInput.value = value;
  valueInput.setAttribute("aria-label", "上下文值");
  const remove = createElement("button", "remove-context");
  remove.type = "button";
  remove.setAttribute("aria-label", "移除上下文");
  remove.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12h12" /></svg>';
  remove.addEventListener("click", () => row.remove());
  row.append(keyInput, equals, valueInput, remove);
  container.append(row);
}

function readContext(containerId) {
  const context = {};
  for (const row of $$(`#${containerId} .context-row`)) {
    const key = $(".context-key", row).value.trim();
    const value = $(".context-value", row).value.trim();
    if (!key && !value) continue;
    if (!key || !value) throw new Error("上下文的键和值必须同时填写");
    if (Object.hasOwn(context, key)) throw new Error(`上下文键“${key}”重复`);
    context[key] = value;
  }
  return context;
}

function resetCaptureContext() {
  $("#capture-context").replaceChildren();
  addContextRow("capture-context", "time_of_day", "evening");
}

function prepareRecallExample(announce = true) {
  goToView("recall");
  const input = $("#recall-form input[name='query']");
  input.value = "晚上应该准备什么饮料？";
  input.focus();
  if (announce) toast("召回示例已准备", "保持晚间上下文，点击“开始召回”生成一条可审计 Trace。");
}

function focusOutcomeAction() {
  goToView("recall");
  const outcomeButton = $("[data-outcome-button]:not(:disabled)", $("#recall-results"));
  if (!outcomeButton) {
    prepareRecallExample(false);
    toast("先执行一次召回", "召回结果出现后，选择“有帮助”或“无帮助”完成闭环。");
    return;
  }
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  outcomeButton.scrollIntoView({ behavior, block: "center" });
  outcomeButton.focus({ preventScroll: true });
  toast("最后一步", "根据真实使用结果选择“有帮助”或“无帮助”。");
}

function runJourneyAction(action) {
  if (action === "scope") {
    $("#tenant-id").focus();
    toast("确认当前作用域", "首次体验保留 demo / alice 即可；修改后请点击“应用”。");
  } else if (action === "write") {
    loadExample();
  } else if (action === "view") {
    goToView("memories");
  } else if (action === "recall") {
    prepareRecallExample();
  } else if (action === "outcome") {
    focusOutcomeAction();
  }
}

function continueJourney() {
  if (!state.journey.write) runJourneyAction("write");
  else if (!state.journey.view) runJourneyAction("view");
  else if (!state.journey.recall) runJourneyAction("recall");
  else if (!state.journey.outcome) runJourneyAction("outcome");
  else showOnboarding(true);
}

function loadExample() {
  goToView("capture");
  const form = $("#memory-form");
  form.elements.key.value = "drink.preference";
  form.elements.value.value = "decaf coffee";
  form.elements.evidence_text.value = "晚上我只喝低因咖啡";
  form.elements.source.value = "conversation";
  form.elements.confidence.value = "0.92";
  $("#confidence-value").textContent = "0.92";
  $("#capture-context").replaceChildren();
  addContextRow("capture-context", "time_of_day", "evening");
  form.elements.key.focus();
  toast("示例已载入", "检查字段后点击“保存为记忆”，然后继续执行召回。 ");
}

function renderCaptureResult(data) {
  const panel = $("#capture-result");
  panel.classList.remove("is-hidden");
  const mark = createElement("div", "success-mark", "✓");
  const title = createElement("h3", "", data.idempotent_replay ? "已返回原有记忆" : "记忆已保存");
  const copy = createElement("p", "", data.idempotent_replay ? "幂等键匹配，未重复创建数据。" : `已生成第 ${data.sequence} 个不可变修订。`);
  const ids = createElement("div", "id-list");
  const fields = [
    ["Record", data.record_id],
    ["Revision", data.revision_id],
    ["Evidence", data.observation_id],
  ];
  for (const [label, value] of fields) {
    const row = createElement("div");
    const code = createElement("code", "", value);
    code.title = value;
    row.append(createElement("span", "", label), code);
    ids.append(row);
  }
  const next = createElement("button", "button button-dark result-next-action", "下一步：查看当前记忆");
  next.type = "button";
  next.addEventListener("click", () => goToView("memories"));
  panel.replaceChildren(mark, title, copy, ids, next);
}

async function submitMemory(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = $("button[type='submit']", form);
  const data = new FormData(form);
  try {
    setLoading(submit, true);
    const key = String(data.get("key") || "").trim();
    const value = String(data.get("value") || "").trim();
    const operationPayload = {
      ...getScope(),
      source: String(data.get("source") || "").trim(),
      key,
      value,
      context: readContext("capture-context"),
      evidence_text: String(data.get("evidence_text") || "").trim(),
      confidence: Number(data.get("confidence")),
    };
    const payload = {
      ...operationPayload,
      idempotency_key: idempotencyKeyFor("preference", "web:preference", operationPayload),
    };
    const result = await scopedApi(
      "/v1/preferences",
      { method: "POST", body: JSON.stringify(payload) },
      "preference-write",
    );
    state.counts.writes += result.idempotent_replay ? 0 : 1;
    state.journey.write = true;
    updateStats();
    updateJourney();
    renderCaptureResult(result);
    addActivity("write", "写入偏好记忆", `${key} = ${value}`);
    toast("记忆已保存", `已创建 Revision #${result.sequence} · ${shortId(result.revision_id)}`);
    loadMemories(true);
  } catch (error) {
    if (isRequestCancelled(error)) return;
    toast("写入失败", describeError(error), "error");
  } finally {
    if (!state.requestChannels.has("preference-write")) setLoading(submit, false);
  }
}

function renderMemoryLibrary(items) {
  const library = $("#memory-library");
  $("#memory-count").textContent = `${items.length} 条记忆`;
  library.replaceChildren();
  if (!items.length) {
    const empty = createElement("div", "empty-results");
    const rings = createElement("div", "empty-rings");
    rings.innerHTML = '<svg viewBox="0 0 48 48" aria-hidden="true"><path d="M14 12h22v26H14z" /><path d="M19 19h12m-12 6h9" /></svg>';
    const action = createElement("button", "button button-primary", "写入第一条记忆");
    action.type = "button";
    action.addEventListener("click", () => goToView("capture"));
    empty.append(
      rings,
      createElement("h3", "", "当前作用域还没有记忆"),
      createElement("p", "", "先写入一条带证据的偏好，随后可在这里查看、修正与追溯历史。"),
      action,
    );
    library.append(empty);
    return;
  }

  for (const item of items) {
    const card = createElement("article", "memory-card");
    const heading = createElement("div", "memory-card-heading");
    const identity = createElement("div");
    identity.append(createElement("span", "memory-key", item.key), createElement("h3", "", item.value));
    heading.append(identity, createElement("span", "revision-pill", `Revision #${item.sequence}`));

    const contexts = createElement("div", "context-chips");
    appendContextChips(contexts, item.context);

    const metrics = createElement("div", "memory-metrics");
    const confidence = createElement("div", "memory-confidence");
    const confidenceHeader = createElement("p");
    confidenceHeader.append(
      createElement("span", "", "信念置信度"),
      createElement("strong", "", item.confidence.toFixed(2)),
    );
    const track = createElement("div", "confidence-track");
    const fill = createElement("i");
    fill.style.width = `${Math.round(item.confidence * 100)}%`;
    track.append(fill);
    confidence.append(confidenceHeader, track);
    const evidence = createElement("div", "metric-copy");
    evidence.append(
      createElement("strong", "", String(item.evidence_count)),
      createElement("span", "", "关联证据"),
    );
    const support = createElement("div", "metric-copy");
    support.append(
      createElement("strong", "", String(item.support_count)),
      createElement("span", "", "支持次数"),
    );
    metrics.append(confidence, evidence, support);

    const footer = createElement("footer", "memory-card-footer");
    footer.append(createElement("span", "", `更新于 ${formatDate(item.recorded_at)}`));
    const actions = createElement("div");
    actions.append(
      makeMiniButton("修订历史", "", () => openHistory(item.record_id)),
      makeMiniButton("修正记忆", "", () => openCorrection(item)),
    );
    footer.append(actions);
    card.append(heading, contexts, metrics, footer);
    library.append(card);
  }
}

async function loadMemories(silent = false) {
  const refresh = $("#refresh-memories");
  const library = $("#memory-library");
  refresh.disabled = true;
  refresh.setAttribute("aria-busy", "true");
  library.setAttribute("aria-busy", "true");
  refresh.textContent = "读取中…";
  const params = new URLSearchParams({ tenant_id: state.scope.tenantId, subject_id: state.scope.subjectId });
  try {
    const items = await scopedApi(
      `/v1/preferences?${params}`,
      { method: "GET", headers: {} },
      "memories",
    );
    state.journey.write = items.length > 0;
    if (items.length > 0 && $("#view-memories").classList.contains("is-active")) state.journey.view = true;
    updateJourney();
    renderMemoryLibrary(items);
  } catch (error) {
    if (isRequestCancelled(error)) return;
    if (!silent) toast("读取记忆失败", describeError(error), "error");
    const errorState = createElement("div", "empty-results");
    errorState.append(
      createElement("h3", "", "暂时无法读取记忆"),
      createElement("p", "", describeError(error)),
    );
    library.replaceChildren(errorState);
  } finally {
    if (!state.requestChannels.has("memories")) {
      refresh.disabled = false;
      refresh.setAttribute("aria-busy", "false");
      library.setAttribute("aria-busy", "false");
      refresh.textContent = "刷新列表";
    }
  }
}

function appendContextChips(container, context) {
  const entries = Object.entries(context || {});
  if (!entries.length) {
    container.append(createElement("span", "context-chip", "通用上下文"));
    return;
  }
  for (const [key, value] of entries) {
    const chip = createElement("span", "context-chip");
    chip.append(document.createTextNode(`${key}: `), createElement("strong", "", value));
    container.append(chip);
  }
}

function renderScorePanel(item) {
  const panel = createElement("div", "score-panel");
  const total = createElement("div", "score-total");
  total.append(createElement("span", "", "综合得分"), createElement("strong", "", item.score.toFixed(3)));
  panel.append(total);
  const breakdown = createElement("div", "score-breakdown");
  for (const [key, label] of Object.entries(scoreLabels)) {
    const value = Number(item.breakdown[key] || 0);
    const row = createElement("div", "score-row");
    const track = createElement("div", "score-track");
    const fill = createElement("i");
    fill.style.width = `${Math.max(0, Math.min(100, value * 100))}%`;
    track.append(fill);
    row.append(createElement("span", "", label), track, createElement("b", "", value.toFixed(2)));
    breakdown.append(row);
  }
  panel.append(breakdown);
  return panel;
}

function makeMiniButton(label, className, onClick) {
  const button = createElement("button", `mini-button${className ? ` ${className}` : ""}`, label);
  button.type = "button";
  button.addEventListener("click", onClick);
  return button;
}

function renderRecallItem(item, traceId, index) {
  const card = createElement("article", "result-card");
  card.style.animationDelay = `${Math.min(index * 45, 220)}ms`;
  card.dataset.recordId = item.record_id;
  card.dataset.revisionId = item.revision_id;
  card.append(createElement("div", "rank-badge", String(item.rank).padStart(2, "0")));

  const main = createElement("div", "result-main");
  const key = createElement("div", "result-key");
  key.append(createElement("span"), document.createTextNode(item.key));
  main.append(key, createElement("h3", "", item.value));
  const chips = createElement("div", "context-chips");
  appendContextChips(chips, item.context);
  main.append(chips);

  const actions = createElement("div", "result-actions");
  const history = makeMiniButton("查看历史", "", () => openHistory(item.record_id));
  const correction = makeMiniButton("修正记忆", "", () => openCorrection(item));
  const positive = makeMiniButton("有帮助", "positive", () => recordOutcome(item, traceId, "helpful", card));
  const negative = makeMiniButton("无帮助", "negative", () => recordOutcome(item, traceId, "rejected", card));
  positive.dataset.outcomeButton = "true";
  negative.dataset.outcomeButton = "true";
  actions.append(history, correction, positive, negative);
  main.append(actions);
  card.append(main, renderScorePanel(item));
  return card;
}

function renderRecall(data) {
  state.lastTrace = data;
  const guidance = $("#recall-guidance");
  guidance.className = "next-step-banner is-hidden";
  guidance.textContent = "";
  const meta = $("#recall-meta");
  meta.classList.remove("is-hidden");
  const summary = createElement("div");
  summary.append(createElement("strong", "", `${data.items.length} 条记忆`), document.createTextNode(` · Policy v${data.policy_version}`));
  const trace = createElement("div");
  trace.append(document.createTextNode("Trace "), createElement("code", "", shortId(data.trace_id)), document.createTextNode(` · ${formatDate(data.created_at)}`));
  meta.replaceChildren(summary, trace);

  const results = $("#recall-results");
  results.replaceChildren();
  if (!data.items.length) {
    const empty = createElement("div", "empty-results");
    const ring = createElement("div", "empty-rings");
    ring.innerHTML = '<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="21" cy="21" r="10" /><path d="m29 29 8 8" /></svg>';
    empty.append(ring, createElement("h3", "", "没有达到阈值的记忆"), createElement("p", "", "尝试调整查询措辞或上下文；这次空结果仍有独立 Trace。"));
    results.append(empty);
    return;
  }
  data.items.forEach((item, index) => results.append(renderRecallItem(item, data.trace_id, index)));
  guidance.className = "next-step-banner";
  guidance.textContent = "下一步：根据真实使用结果选择“有帮助”或“无帮助”。只有这类引用当前 Trace 的反馈才会更新上下文效用。";
}

async function submitRecall(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = $("button[type='submit']", form);
  const results = $("#recall-results");
  const data = new FormData(form);
  const query = String(data.get("query") || "").trim();
  try {
    setLoading(submit, true);
    results.setAttribute("aria-busy", "true");
    const payload = {
      ...getScope(),
      query,
      context: readContext("recall-context"),
      limit: Number(data.get("limit")),
    };
    const result = await scopedApi(
      "/v1/recall",
      { method: "POST", body: JSON.stringify(payload) },
      "recall",
    );
    state.counts.recalls += 1;
    state.counts.results = result.items.length;
    state.journey.recall = true;
    updateStats();
    updateJourney();
    renderRecall(result);
    addActivity("recall", "执行上下文召回", `“${query}” · ${result.items.length} 条结果`);
  } catch (error) {
    if (isRequestCancelled(error)) return;
    toast("召回失败", describeError(error), "error");
  } finally {
    if (!state.requestChannels.has("recall")) {
      setLoading(submit, false);
      results.setAttribute("aria-busy", "false");
    }
  }
}

async function recordOutcome(item, traceId, kind, card) {
  const buttons = $$("[data-outcome-button]", card);
  buttons.forEach((button) => {
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  });
  try {
    const operationPayload = {
      ...getScope(),
      trace_id: traceId,
      revision_id: item.revision_id,
      kind,
      weight: 1,
      note: "submitted from memory console",
    };
    const operation = `outcome:${traceId}:${item.revision_id}`;
    const payload = {
      ...operationPayload,
      idempotency_key: idempotencyKeyFor(operation, "web:outcome", operationPayload),
    };
    const result = await scopedApi("/v1/outcomes", {
      method: "POST",
      body: JSON.stringify(payload),
    }, operation);
    state.counts.outcomes += result.idempotent_replay ? 0 : 1;
    state.journey.outcome = true;
    updateStats();
    updateJourney();
    const scorePanel = $(".score-panel", card);
    $(".utility-update", scorePanel)?.remove();
    scorePanel.append(createElement("div", "utility-update", `上下文效用已更新为 ${result.utility.mean.toFixed(3)}（样本权重 ${result.utility.sample_weight.toFixed(1)}）`));
    const feedback = kind === "helpful" ? "有帮助" : "无帮助";
    const selected = kind === "helpful" ? $(".positive", card) : $(".negative", card);
    selected.classList.add("is-selected");
    selected.textContent = `${feedback} ✓`;
    buttons.forEach((button) => button.setAttribute("aria-busy", "false"));
    const guidance = $("#recall-guidance");
    guidance.className = "next-step-banner is-complete";
    guidance.textContent = "反馈闭环已完成：这次 Outcome 已与召回 Trace 建立归因，并更新当前上下文中的效用。";
    addActivity("outcome", "记录可归因结果", `${item.key} · ${feedback}`);
    toast("反馈已记录", `效用均值更新为 ${result.utility.mean.toFixed(3)}`);
  } catch (error) {
    if (isRequestCancelled(error)) return;
    buttons.forEach((button) => {
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
    });
    toast("反馈失败", describeError(error), "error");
  }
}

function showDialog(dialog) {
  if (!dialog.open) dialog.showModal();
}

function trapDialogFocus(dialog, event) {
  if (event.key !== "Tab" || !dialog.open) return;
  const focusable = $$(DIALOG_FOCUSABLE_SELECTOR, dialog).filter(
    (element) => element.getClientRects().length > 0,
  );
  if (!focusable.length) {
    event.preventDefault();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  const active = document.activeElement;
  if (event.shiftKey && (active === first || !dialog.contains(active))) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
    event.preventDefault();
    first.focus();
  }
}

async function openHistory(recordId) {
  const modal = $("#history-modal");
  const content = $("#history-content");
  content.replaceChildren(createElement("div", "history-loading", "正在读取不可变修订链…"));
  showDialog(modal);
  const params = new URLSearchParams({ tenant_id: state.scope.tenantId, subject_id: state.scope.subjectId });
  try {
    const revisions = await scopedApi(
      `/v1/preferences/${encodeURIComponent(recordId)}/revisions?${params}`,
      { method: "GET", headers: {} },
      "history",
    );
    const timeline = createElement("div", "history-timeline");
    for (const revision of revisions) {
      const item = createElement("article", "history-item");
      item.append(createElement("span", "history-sequence", `#${revision.sequence}`));
      const body = createElement("div", "history-content");
      body.append(createElement("h3", "", revision.value), createElement("p", "", `记录于 ${formatDate(revision.recorded_at)} · Revision ${shortId(revision.id)}`));
      const meta = createElement("div", "history-meta");
      meta.append(
        createElement("span", "", `置信度 ${revision.confidence.toFixed(2)}`),
        createElement("span", "", `支持证据 ${revision.support_count}`),
        createElement("span", "", revision.supersedes_revision_id ? "替代上一版本" : "初始版本"),
      );
      body.append(meta);
      item.append(body);
      timeline.append(item);
    }
    content.replaceChildren(timeline);
  } catch (error) {
    if (isRequestCancelled(error)) return;
    content.replaceChildren(createElement("div", "history-loading", `读取失败：${describeError(error)}`));
  }
}

function openCorrection(item) {
  const form = $("#correction-form");
  state.idempotencyOperations.delete(`correction:${item.record_id}`);
  form.reset();
  form.elements.record_id.value = item.record_id;
  form.elements.expected_revision_id.value = item.revision_id;
  form.elements.value.value = item.value;
  showDialog($("#correction-modal"));
  window.setTimeout(() => form.elements.value.select(), 0);
}

async function submitCorrection(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = $("button[type='submit']", form);
  const data = new FormData(form);
  const recordId = String(data.get("record_id") || "");
  try {
    setLoading(submit, true);
    const operationPayload = {
      ...getScope(),
      source: String(data.get("source") || "").trim(),
      value: String(data.get("value") || "").trim(),
      evidence_text: String(data.get("evidence_text") || "").trim(),
      reason: String(data.get("reason") || "").trim(),
      expected_revision_id: String(data.get("expected_revision_id") || ""),
    };
    const operation = `correction:${recordId}`;
    const payload = {
      ...operationPayload,
      idempotency_key: idempotencyKeyFor(operation, "web:correction", operationPayload),
    };
    const result = await scopedApi(`/v1/preferences/${encodeURIComponent(recordId)}/corrections`, {
      method: "POST",
      body: JSON.stringify(payload),
    }, operation);
    state.counts.writes += result.idempotent_replay ? 0 : 1;
    state.journey.write = true;
    updateStats();
    updateJourney();
    addActivity("correction", "追加记忆修订", `${payload.value} · Revision #${result.sequence}`);
    $("#correction-modal").close();
    toast("修订已追加", `当前版本为 #${result.sequence}，重新召回即可查看。`);
    loadMemories(true);
  } catch (error) {
    if (isRequestCancelled(error)) return;
    toast("修正失败", describeError(error), "error");
  } finally {
    if (!state.requestChannels.has(`correction:${recordId}`)) setLoading(submit, false);
  }
}

function renderInitialRecallState() {
  const meta = $("#recall-meta");
  meta.className = "recall-meta is-hidden";
  meta.replaceChildren();
  const guidance = $("#recall-guidance");
  guidance.className = "next-step-banner is-hidden";
  guidance.textContent = "";
  const results = $("#recall-results");
  results.setAttribute("aria-busy", "false");
  const empty = createElement("div", "empty-results");
  const rings = createElement("div", "empty-rings");
  rings.innerHTML = '<svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="21" cy="21" r="10" /><path d="m29 29 8 8" /></svg>';
  empty.append(
    rings,
    createElement("h3", "", "输入一个问题，开始检索记忆"),
    createElement("p", "", "系统会综合词法相关性、上下文、信念、效用与时效性进行排序。"),
  );
  results.replaceChildren(empty);
}

function resetScopedViewState() {
  state.counts = { writes: 0, recalls: 0, outcomes: 0, results: 0 };
  state.lastTrace = null;
  state.activities = [];
  state.journey = { write: false, view: false, recall: false, outcome: false };
  state.idempotencyOperations.clear();

  for (const dialog of [$("#history-modal"), $("#correction-modal")]) {
    if (dialog.open) dialog.close();
  }
  $("#history-content").replaceChildren();
  $("#correction-form").reset();

  const memoryForm = $("#memory-form");
  memoryForm.reset();
  resetCaptureContext();
  $("#confidence-value").textContent = memoryForm.elements.confidence.value;
  const captureResult = $("#capture-result");
  captureResult.classList.add("is-hidden");
  captureResult.replaceChildren();

  const recallForm = $("#recall-form");
  recallForm.reset();
  $("#recall-context").replaceChildren();
  addContextRow("recall-context", "time_of_day", "evening");
  renderInitialRecallState();
  renderMemoryLibrary([]);
  $("#memory-library").setAttribute("aria-busy", "false");
  renderActivities();
  $("#toast-region").replaceChildren();

  setLoading($("button[type='submit']", memoryForm), false);
  setLoading($("button[type='submit']", recallForm), false);
  setLoading($("button[type='submit']", $("#correction-form")), false);
  const refresh = $("#refresh-memories");
  refresh.disabled = false;
  refresh.setAttribute("aria-busy", "false");
  refresh.textContent = "刷新列表";
  updateStats();
  updateJourney();
}

function applyScope() {
  const tenantId = $("#tenant-id").value.trim();
  const subjectId = $("#subject-id").value.trim();
  if (!tenantId || !subjectId) {
    toast("无法应用作用域", "租户和用户均不能为空。", "error");
    return;
  }
  if (tenantId === state.scope.tenantId && subjectId === state.scope.subjectId) {
    loadMemories();
    toast("作用域未变化", `仍在使用 ${tenantId} / ${subjectId}，已刷新当前记忆。`);
    return;
  }
  cancelScopedRequests();
  state.scope = { tenantId, subjectId };
  localStorage.setItem("emf.tenantId", tenantId);
  localStorage.setItem("emf.subjectId", subjectId);
  resetScopedViewState();
  loadMemories(true);
  toast("作用域已切换", `${tenantId} / ${subjectId}；旧作用域的页面状态与请求已清除。`);
}

async function checkHealth() {
  const dot = $("#status-dot");
  const label = $("#status-label");
  const detail = $("#status-detail");
  const retry = $("#retry-health");
  state.healthController?.abort();
  const controller = new AbortController();
  state.healthController = controller;
  dot.className = "status-dot";
  label.textContent = "正在连接";
  detail.textContent = "检查 API 状态…";
  retry.disabled = true;
  retry.setAttribute("aria-busy", "true");
  try {
    const health = await api("/health", {
      method: "GET",
      headers: {},
      signal: controller.signal,
      timeoutMs: HEALTH_TIMEOUT_MS,
    });
    if (state.healthController !== controller) return;
    dot.className = "status-dot is-online";
    label.textContent = "API 在线";
    updateStorageDisplay(health.storage);
    detail.textContent = `v${health.version} · ${health.storage === "postgres" ? "PostgreSQL" : "进程内存"}`;
  } catch (error) {
    if (isRequestCancelled(error)) return;
    dot.className = "status-dot is-offline";
    label.textContent = "API 离线";
    detail.textContent = describeError(error);
  } finally {
    if (state.healthController === controller) {
      state.healthController = null;
      retry.disabled = false;
      retry.setAttribute("aria-busy", "false");
    }
  }
}

function updateStorageDisplay(storage) {
  state.storage = storage;
  const persistent = storage === "postgres";
  $("#storage-title").textContent = persistent ? "PostgreSQL 权威存储" : "后端进程内存";
  $("#storage-description").textContent = persistent
    ? "记忆、修订、Trace 与 Outcome 持久化到 PostgreSQL；容器或进程重启后仍会保留。"
    : "当前不会写入浏览器、文件或数据库；重启后端后全部权威记忆都会清空。";
  $("#session-storage-note").textContent = persistent
    ? "当前使用 PostgreSQL，后端重启后数据保留"
    : "当前使用进程内存，后端重启后数据清空";
}

function bindEvents() {
  $$("[data-view]").forEach((button) => button.addEventListener("click", () => goToView(button.dataset.view)));
  $$("[data-go]").forEach((button) => button.addEventListener("click", () => goToView(button.dataset.go)));
  $$("[data-add-context]").forEach((button) => button.addEventListener("click", () => addContextRow(button.dataset.addContext)));
  $("#mobile-menu").addEventListener("click", openMobileMenu);
  $("#mobile-scrim").addEventListener("click", closeMobileMenu);
  $("#save-scope").addEventListener("click", applyScope);
  for (const input of [$("#tenant-id"), $("#subject-id")]) {
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") applyScope();
    });
  }
  $("#retry-health").addEventListener("click", checkHealth);
  $("#start-example").addEventListener("click", continueJourney);
  $("#load-example").addEventListener("click", loadExample);
  $("#continue-to-recall").addEventListener("click", () => prepareRecallExample());
  $$("[data-journey-action]").forEach((button) => button.addEventListener("click", () => runJourneyAction(button.dataset.journeyAction)));
  $("#open-onboarding").addEventListener("click", () => showOnboarding(true));
  $("#onboarding-close").addEventListener("click", dismissOnboarding);
  $("#onboarding-skip").addEventListener("click", dismissOnboarding);
  $("#onboarding-prev").addEventListener("click", () => {
    if (onboardingStepIndex > 0) onboardingStepIndex -= 1;
    renderOnboardingStep();
  });
  $("#onboarding-next").addEventListener("click", advanceOnboarding);
  $("#onboarding-dialog").addEventListener("close", () => {
    if (localStorage.getItem(ONBOARDING_STORAGE_KEY) !== "complete") {
      sessionStorage.setItem(ONBOARDING_DISMISSED_KEY, "true");
    }
  });
  $("#refresh-memories").addEventListener("click", () => loadMemories());
  $("#memory-form").addEventListener("submit", submitMemory);
  $("#memory-form").addEventListener("reset", () => {
    window.setTimeout(() => {
      resetCaptureContext();
      $("#confidence-value").textContent = $("#confidence").value;
      $("#capture-result").classList.add("is-hidden");
    }, 0);
  });
  $("#confidence").addEventListener("input", (event) => { $("#confidence-value").textContent = event.target.value; });
  $("#recall-form").addEventListener("submit", submitRecall);
  $$("[data-query-example]").forEach((button) => button.addEventListener("click", () => {
    const input = $("#recall-form input[name='query']");
    input.value = button.dataset.queryExample;
    input.focus();
  }));
  $("#correction-form").addEventListener("submit", submitCorrection);
  $$(".modal-close").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  $$("dialog").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener("keydown", (event) => trapDialogFocus(dialog, event));
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMobileMenu();
  });
}

function initialize() {
  $$('[data-backend-link]').forEach((link) => {
    link.href = `${API_BASE}${link.dataset.backendLink}`;
  });
  $("#tenant-id").value = state.scope.tenantId;
  $("#subject-id").value = state.scope.subjectId;
  resetCaptureContext();
  addContextRow("recall-context", "time_of_day", "evening");
  updateStats();
  updateJourney();
  bindEvents();
  checkHealth();
  loadMemories(true);
  const tourMode = new URLSearchParams(window.location.search).get("tour");
  if (tourMode !== "0") window.setTimeout(() => showOnboarding(tourMode === "1"), 420);
}

initialize();
