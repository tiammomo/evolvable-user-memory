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
  journey: { write: false, recall: false, outcome: false },
};

const viewCopy = {
  overview: ["记忆工作台", "MEMORY OVERVIEW"],
  capture: ["写入记忆", "CAPTURE EVIDENCE"],
  memories: ["当前记忆", "ACTIVE BELIEFS"],
  recall: ["记忆召回", "CONTEXTUAL RECALL"],
  architecture: ["系统架构", "FIVE-PLANE MODEL"],
};

const scoreLabels = {
  semantic: "语义",
  context: "上下文",
  belief: "信念",
  utility: "效用",
  recency: "时效",
};

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

async function api(path, options = {}) {
  const request = {
    ...options,
    headers: { "content-type": "application/json", ...(options.headers || {}) },
  };
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
}

function setLoading(button, loading) {
  if (!button) return;
  button.disabled = loading;
  button.classList.toggle("is-loading", loading);
}

function toast(title, message, type = "success") {
  const region = $("#toast-region");
  const item = createElement("div", `toast${type === "error" ? " is-error" : ""}`);
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
  $("#journey-recall").classList.toggle("is-done", state.journey.recall);
  $("#journey-outcome").classList.toggle("is-done", state.journey.outcome);
}

function addActivity(kind, title, detail) {
  const symbols = { write: "+", recall: "↗", outcome: "✓", correction: "↻" };
  state.activities.unshift({ kind, title, detail, time: new Date() });
  state.activities = state.activities.slice(0, 8);
  const list = $("#activity-list");
  list.replaceChildren();
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
  $$(".nav-item").forEach((item) => item.classList.toggle("is-active", item.dataset.view === name));
  $("#page-title").textContent = viewCopy[name][0];
  $("#page-eyebrow").textContent = viewCopy[name][1];
  closeMobileMenu();
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "memories") loadMemories();
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
  panel.replaceChildren(mark, title, copy, ids);
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
    const payload = {
      ...getScope(),
      source: String(data.get("source") || "").trim(),
      idempotency_key: newId("web:preference"),
      key,
      value,
      context: readContext("capture-context"),
      evidence_text: String(data.get("evidence_text") || "").trim(),
      confidence: Number(data.get("confidence")),
    };
    const result = await api("/v1/preferences", { method: "POST", body: JSON.stringify(payload) });
    state.counts.writes += result.idempotent_replay ? 0 : 1;
    state.journey.write = true;
    updateStats();
    updateJourney();
    renderCaptureResult(result);
    addActivity("write", "写入偏好记忆", `${key} = ${value}`);
    toast("记忆已保存", `已创建 Revision #${result.sequence} · ${shortId(result.revision_id)}`);
    loadMemories(true);
  } catch (error) {
    toast("写入失败", describeError(error), "error");
  } finally {
    setLoading(submit, false);
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
  const originalLabel = refresh.textContent;
  refresh.disabled = true;
  refresh.textContent = "读取中…";
  const params = new URLSearchParams({ tenant_id: state.scope.tenantId, subject_id: state.scope.subjectId });
  try {
    const items = await api(`/v1/preferences?${params}`, { method: "GET", headers: {} });
    state.journey.write = items.length > 0;
    updateJourney();
    renderMemoryLibrary(items);
  } catch (error) {
    if (!silent) toast("读取记忆失败", describeError(error), "error");
    const library = $("#memory-library");
    const errorState = createElement("div", "empty-results");
    errorState.append(
      createElement("h3", "", "暂时无法读取记忆"),
      createElement("p", "", describeError(error)),
    );
    library.replaceChildren(errorState);
  } finally {
    refresh.disabled = false;
    refresh.textContent = originalLabel;
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
}

async function submitRecall(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = $("button[type='submit']", form);
  const data = new FormData(form);
  const query = String(data.get("query") || "").trim();
  try {
    setLoading(submit, true);
    const payload = {
      ...getScope(),
      query,
      context: readContext("recall-context"),
      limit: Number(data.get("limit")),
    };
    const result = await api("/v1/recall", { method: "POST", body: JSON.stringify(payload) });
    state.counts.recalls += 1;
    state.counts.results = result.items.length;
    state.journey.recall = true;
    updateStats();
    updateJourney();
    renderRecall(result);
    addActivity("recall", "执行上下文召回", `“${query}” · ${result.items.length} 条结果`);
  } catch (error) {
    toast("召回失败", describeError(error), "error");
  } finally {
    setLoading(submit, false);
  }
}

async function recordOutcome(item, traceId, kind, card) {
  const buttons = $$("[data-outcome-button]", card);
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const result = await api("/v1/outcomes", {
      method: "POST",
      body: JSON.stringify({
        ...getScope(),
        trace_id: traceId,
        revision_id: item.revision_id,
        kind,
        idempotency_key: newId("web:outcome"),
        weight: 1,
        note: "submitted from memory console",
      }),
    });
    state.counts.outcomes += result.idempotent_replay ? 0 : 1;
    state.journey.outcome = true;
    updateStats();
    updateJourney();
    const scorePanel = $(".score-panel", card);
    $(".utility-update", scorePanel)?.remove();
    scorePanel.append(createElement("div", "utility-update", `上下文效用已更新为 ${result.utility.mean.toFixed(3)}（样本权重 ${result.utility.sample_weight.toFixed(1)}）`));
    const feedback = kind === "helpful" ? "有帮助" : "无帮助";
    addActivity("outcome", "记录可归因结果", `${item.key} · ${feedback}`);
    toast("反馈已记录", `效用均值更新为 ${result.utility.mean.toFixed(3)}`);
  } catch (error) {
    buttons.forEach((button) => { button.disabled = false; });
    toast("反馈失败", describeError(error), "error");
  }
}

function showDialog(dialog) {
  if (!dialog.open) dialog.showModal();
}

async function openHistory(recordId) {
  const modal = $("#history-modal");
  const content = $("#history-content");
  content.replaceChildren(createElement("div", "history-loading", "正在读取不可变修订链…"));
  showDialog(modal);
  const params = new URLSearchParams({ tenant_id: state.scope.tenantId, subject_id: state.scope.subjectId });
  try {
    const revisions = await api(`/v1/preferences/${encodeURIComponent(recordId)}/revisions?${params}`);
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
    content.replaceChildren(createElement("div", "history-loading", `读取失败：${describeError(error)}`));
  }
}

function openCorrection(item) {
  const form = $("#correction-form");
  form.reset();
  form.elements.record_id.value = item.record_id;
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
    const payload = {
      ...getScope(),
      source: String(data.get("source") || "").trim(),
      idempotency_key: newId("web:correction"),
      value: String(data.get("value") || "").trim(),
      evidence_text: String(data.get("evidence_text") || "").trim(),
      reason: String(data.get("reason") || "").trim(),
    };
    const result = await api(`/v1/preferences/${encodeURIComponent(recordId)}/corrections`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.counts.writes += result.idempotent_replay ? 0 : 1;
    state.journey.write = true;
    updateStats();
    updateJourney();
    addActivity("correction", "追加记忆修订", `${payload.value} · Revision #${result.sequence}`);
    $("#correction-modal").close();
    toast("修订已追加", `当前版本为 #${result.sequence}，重新召回即可查看。`);
    loadMemories(true);
  } catch (error) {
    toast("修正失败", describeError(error), "error");
  } finally {
    setLoading(submit, false);
  }
}

function applyScope() {
  const tenantId = $("#tenant-id").value.trim();
  const subjectId = $("#subject-id").value.trim();
  if (!tenantId || !subjectId) {
    toast("无法应用作用域", "租户和用户均不能为空。", "error");
    return;
  }
  state.scope = { tenantId, subjectId };
  state.journey = { write: false, recall: false, outcome: false };
  localStorage.setItem("emf.tenantId", tenantId);
  localStorage.setItem("emf.subjectId", subjectId);
  updateStats();
  updateJourney();
  loadMemories(true);
  toast("作用域已切换", `${tenantId} / ${subjectId}`);
}

async function checkHealth() {
  const dot = $("#status-dot");
  const label = $("#status-label");
  const detail = $("#status-detail");
  try {
    const health = await api("/health", { method: "GET", headers: {} });
    dot.className = "status-dot is-online";
    label.textContent = "API 在线";
    detail.textContent = `v${health.version} · In-memory store`;
  } catch (error) {
    dot.className = "status-dot is-offline";
    label.textContent = "API 离线";
    detail.textContent = describeError(error);
  }
}

function bindEvents() {
  $$("[data-view]").forEach((button) => button.addEventListener("click", () => goToView(button.dataset.view)));
  $$("[data-go]").forEach((button) => button.addEventListener("click", () => goToView(button.dataset.go)));
  $$("[data-add-context]").forEach((button) => button.addEventListener("click", () => addContextRow(button.dataset.addContext)));
  $("#mobile-menu").addEventListener("click", openMobileMenu);
  $("#mobile-scrim").addEventListener("click", closeMobileMenu);
  $("#save-scope").addEventListener("click", applyScope);
  $("#start-example").addEventListener("click", loadExample);
  $("#load-example").addEventListener("click", loadExample);
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
  $$("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  }));
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
}

initialize();
