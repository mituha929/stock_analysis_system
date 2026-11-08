"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const state = { fields: [], alerts: [], notifications: [], screeners: [], deleteId: null };
let desktopBusy = false;

const PRESETS = {
  price_ma20: { field: "price", operator: ">", value: { field: "ma20" } },
  cross_ma20: { field: "price", operator: "cross_above", value: { field: "ma20" } },
  score: { combinator: "and", rules: [
    { field: "direction_score", operator: ">=", value: 65 },
    { field: "risk_score", operator: "<=", value: 60 },
  ] },
  volume: { combinator: "and", rules: [
    { field: "volume_ratio_20d", operator: ">=", value: 1.5 },
    { field: "rsi14", operator: "<=", value: 70 },
  ] },
};

function formatJSON(value) { return JSON.stringify(value, null, 2); }
function parseJSON(text, label = "Rule JSON") {
  try { return JSON.parse(text); }
  catch (error) { throw new Error(`${label} 格式錯誤：${error.message}`); }
}
function asNumber(value) { return value === "" ? null : Number(value); }
function fmtNumber(value) {
  return value === null || value === undefined ? "—" : new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 2 }).format(value);
}
function fmtTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-TW", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}
function splitValues(value) { return value.split(/[,，\s]+/).map(item => item.trim()).filter(Boolean); }

function node(tag, className, textValue) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (textValue !== undefined) element.textContent = textValue;
  return element;
}
function append(parent, ...children) { children.filter(Boolean).forEach(child => parent.append(child)); return parent; }
function button(textValue, className, action, dataset = {}) {
  const element = node("button", className, textValue);
  element.type = "button";
  element.dataset.action = action;
  Object.assign(element.dataset, dataset);
  return element;
}

function toast(message, tone = "ok") {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show ${tone}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = "toast"; }, 3500);
}

function errorText(error) {
  if (error instanceof Error) return error.message;
  return String(error || "發生未知錯誤");
}

async function api(path, options = {}) {
  const config = { ...options, headers: { ...(options.headers || {}) } };
  if (config.body && typeof config.body !== "string") {
    config.headers["Content-Type"] = "application/json";
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(path, config);
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }
  if (!response.ok) {
    const detail = payload?.detail;
    const message = Array.isArray(detail)
      ? detail.map(item => `${(item.loc || []).slice(1).join(".")}：${item.msg}`).join("；")
      : detail || (typeof payload === "string" ? payload : `HTTP ${response.status}`);
    throw new Error(message);
  }
  return payload;
}

function activity(action, status, detail) {
  const records = JSON.parse(sessionStorage.getItem("v3-activity") || "[]");
  records.unshift({ action, status, detail, at: new Date().toISOString() });
  sessionStorage.setItem("v3-activity", JSON.stringify(records.slice(0, 30)));
  renderActivity();
}

function renderActivity() {
  const target = $("#activity-log");
  const records = JSON.parse(sessionStorage.getItem("v3-activity") || "[]");
  target.replaceChildren();
  if (!records.length) { target.append(node("p", "empty", "尚無操作紀錄。")); return; }
  records.forEach(record => {
    const row = node("div", "activity-row");
    append(row,
      node("span", `activity-dot ${record.status}`),
      append(node("div"), node("b", "", record.action), node("p", "", record.detail)),
      node("time", "", fmtTime(record.at)),
    );
    target.append(row);
  });
}

const TITLES = {
  overview: "V3 功能總覽", rules: "規則測試", alerts: "提醒監控",
  notifications: "通知紀錄", screener: "選股與排序", saved: "儲存策略", ai: "AI 選股：預覽與確認",
};
function showView(view, updateHash = true) {
  if (!TITLES[view]) view = "overview";
  $$(".view").forEach(section => section.classList.toggle("active", section.dataset.page === view));
  $$(".nav-item").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  $("#page-title").textContent = TITLES[view];
  if (updateHash) history.replaceState(null, "", `#${view}`);
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (view === "alerts") loadAlerts();
  if (view === "notifications") loadNotifications();
  if (view === "saved") loadSaved();
}

function setBadge(element, status, textValue) {
  element.className = `status-badge ${status}`;
  element.textContent = textValue;
}

function renderRuleResult(result) {
  const target = $("#rule-summary");
  target.replaceChildren();
  const tone = result.status === "matched" ? "matched" : result.status === "insufficient_data" ? "unknown" : "not-matched";
  setBadge($("#rule-badge"), tone, result.status === "matched" ? "符合" : result.status === "insufficient_data" ? "資料不足" : "不符合");
  const stats = node("div", "result-stats");
  [["股票", `${result.symbol} ${result.name || ""}`], ["符合條件", `${result.matched_count}/${result.total_rules}`], ["K 線筆數", result.candle_count], ["資料日期", fmtTime(result.latest_candle_at)]].forEach(([label, value]) => {
    append(stats, append(node("div"), node("small", "", label), node("strong", "", String(value ?? "—"))));
  });
  target.append(stats);
  const list = node("div", "reason-list");
  (result.details || []).forEach(detail => {
    const item = node("div", `reason ${detail.status || (detail.matched ? "matched" : "not-matched")}`);
    append(item, node("b", "", detail.reason || detail.path), node("small", "", `實際 ${fmtNumber(detail.actual)} · 比較值 ${fmtNumber(detail.expected)}`));
    list.append(item);
  });
  if (!(result.details || []).length) list.append(node("p", "empty", "API 未回傳規則明細。"));
  target.append(list);
  (result.warnings || []).forEach(value => target.append(node("p", "warning", value)));
}

async function runRuleTest(event) {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  setBadge($("#rule-badge"), "working", "執行中");
  try {
    const symbol = $("#rule-symbol").value.trim().toUpperCase();
    const payload = { rule: parseJSON($("#rule-json").value), history_interval: $("#rule-interval").value, candle_table: $("#rule-table").value };
    const result = await api(`/api/v1/rules/test/${encodeURIComponent(symbol)}`, { method: "POST", body: payload });
    $("#rule-raw").textContent = formatJSON(result);
    renderRuleResult(result);
    activity("單股規則測試", "ok", `${symbol}：${result.status}，符合 ${result.matched_count}/${result.total_rules}`);
    toast("單股規則測試完成");
  } catch (error) {
    setBadge($("#rule-badge"), "error", "錯誤");
    $("#rule-summary").textContent = errorText(error);
    $("#rule-raw").textContent = errorText(error);
    activity("單股規則測試", "error", errorText(error));
    toast(errorText(error), "error");
  } finally { submit.disabled = false; }
}

async function loadFields(openDialog = false) {
  try {
    const result = await api("/api/v1/rules/fields");
    state.fields = result.fields || [];
    $("#metric-fields").textContent = state.fields.length;
    const target = $("#fields-list");
    target.replaceChildren();
    state.fields.forEach(field => {
      const card = node("article", "field-card");
      append(card, node("b", "", field.field), node("small", "", `${field.type} · ${field.operators.join("  ")}`));
      target.append(card);
    });
    if (openDialog) $("#fields-dialog").showModal();
    return result;
  } catch (error) { if (openDialog) toast(errorText(error), "error"); throw error; }
}

function alertPayload() {
  return {
    name: $("#alert-name").value.trim(), symbol: $("#alert-symbol").value.trim().toUpperCase(),
    source: $("#alert-source").value, enabled: true,
    check_interval_seconds: Number($("#alert-interval").value), cooldown_seconds: Number($("#alert-cooldown").value),
    quote_max_age_seconds: 60, history_interval: "day", candle_table: "candles",
    rule: parseJSON($("#alert-rule-json").value),
  };
}

function alertStateText(alert) {
  if (!alert.config.enabled) return "已停用";
  return ({ WAITING: "等待觸發", TRIGGERED: "已觸發", COOLDOWN: "冷卻中", RESET: "等待重置" })[alert.state] || alert.state;
}

function renderAlerts() {
  const target = $("#alerts-list");
  target.replaceChildren();
  if (!state.alerts.length) { target.append(node("p", "empty panel", "目前沒有提醒。可用左側表單建立第一個提醒。")); return; }
  state.alerts.forEach(alert => {
    const config = alert.config;
    const card = node("article", "panel item-card alert-card");
    const title = append(node("div", "item-title"), node("span", `state-light ${alert.config.enabled ? alert.state.toLowerCase() : "disabled"}`), append(node("div"), node("h3", "", config.name), node("p", "", `${config.symbol} · ${config.source.toUpperCase()} · 每 ${config.check_interval_seconds} 秒`)));
    const status = node("span", "status-badge idle", alertStateText(alert));
    const meta = node("div", "item-meta");
    [["最近檢查", fmtTime(alert.last_checked_at)], ["最近觸發", fmtTime(alert.last_triggered_at)], ["觸發次數", alert.trigger_count], ["上次判斷", alert.last_result?.status || "尚未建立基準"]].forEach(([label, value]) => append(meta, append(node("div"), node("small", "", label), node("b", "", String(value)) )));
    const actions = append(node("div", "item-actions"),
      button("立即檢查", "primary small", "check-alert", { id: alert.id }),
      button("清除基準", "secondary small", "reset-alert", { id: alert.id }),
      button(config.enabled ? "停用" : "啟用", "secondary small", "toggle-alert", { id: alert.id, enabled: String(!config.enabled) }),
      button("刪除", "danger-link", "delete-alert", { id: alert.id }),
    );
    append(card, append(node("div", "item-header"), title, status), meta);
    if (alert.last_error) card.append(node("p", "error-box", alert.last_error));
    if (alert.last_result?.reasons?.length) card.append(node("p", "reason-line", alert.last_result.reasons.join("；")));
    card.append(actions);
    target.append(card);
  });
}

async function loadAlerts(silent = false) {
  try {
    state.alerts = await api("/api/v1/alerts");
    renderAlerts();
    $("#metric-alerts").textContent = state.alerts.filter(item => item.config.enabled).length;
  } catch (error) { if (!silent) toast(errorText(error), "error"); }
}

async function createAlert(event) {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  try {
    const result = await api("/api/v1/alerts", { method: "POST", body: alertPayload() });
    $("#alert-raw").textContent = formatJSON(result);
    activity("建立提醒", "ok", `${result.config.symbol} · ${result.config.name}`);
    toast("提醒已建立，正在等待第一次基準檢查");
    await loadAlerts(true);
  } catch (error) { $("#alert-raw").textContent = errorText(error); activity("建立提醒", "error", errorText(error)); toast(errorText(error), "error"); }
  finally { submit.disabled = false; }
}

async function handleAlertAction(event) {
  const trigger = event.target.closest("[data-action]");
  if (!trigger) return;
  const { action, id } = trigger.dataset;
  if (!["check-alert", "reset-alert", "toggle-alert", "delete-alert"].includes(action)) return;
  const current = state.alerts.find(item => item.id === id);
  if (action === "delete-alert" && !window.confirm(`確定刪除提醒「${current?.config.name || id}」？`)) return;
  trigger.disabled = true;
  try {
    let result;
    if (action === "check-alert") result = await api(`/api/v1/alerts/${id}/check`, { method: "POST" });
    if (action === "reset-alert") result = await api(`/api/v1/alerts/${id}/reset`, { method: "POST" });
    if (action === "toggle-alert") result = await api(`/api/v1/alerts/${id}/enabled`, { method: "PATCH", body: { enabled: trigger.dataset.enabled === "true" } });
    if (action === "delete-alert") result = await api(`/api/v1/alerts/${id}`, { method: "DELETE" });
    $("#alert-raw").textContent = result ? formatJSON(result) : "HTTP 204 · 已刪除";
    const labels = { "check-alert": "立即檢查提醒", "reset-alert": "清除提醒基準", "toggle-alert": "切換提醒狀態", "delete-alert": "刪除提醒" };
    activity(labels[action], "ok", current?.config.name || id);
    toast(`${labels[action]}完成`);
    await Promise.all([loadAlerts(true), loadNotifications(true)]);
  } catch (error) { $("#alert-raw").textContent = errorText(error); activity("提醒操作", "error", errorText(error)); toast(errorText(error), "error"); }
  finally { trigger.disabled = false; }
}

function renderNotifications() {
  const target = $("#v3-notification-list");
  target.replaceChildren();
  $("#notification-count").textContent = state.notifications.length;
  $("#notification-updated").textContent = new Date().toLocaleTimeString("zh-TW", { hour12: false });
  if (!state.notifications.length) { target.append(node("p", "empty panel", "沒有符合篩選條件的通知。")); return; }
  state.notifications.forEach(item => {
    const card = node("article", `panel notification-card ${item.read ? "read" : "unread"}`);
    const icon = node("span", `notification-kind ${item.kind}`, item.kind === "test" ? "測" : "警");
    const body = append(node("div", "notification-body"), append(node("div", "notification-heading"), node("h3", "", item.title), node("time", "", fmtTime(item.created_at))), node("p", "", item.message), node("small", "", `${item.read ? "已讀" : "未讀"} · 系統通知 ${item.desktop_status || "—"}`));
    const toggle = button(item.read ? "標為未讀" : "標為已讀", "secondary small", "toggle-notification", { id: item.id, read: String(!item.read) });
    append(card, icon, body, toggle);
    target.append(card);
  });
}

async function loadNotifications(silent = false) {
  try {
    const unread = $("#only-unread")?.checked || false;
    const result = await api(`/api/v1/notifications?limit=30&unread_only=${unread}`);
    state.notifications = result.items || [];
    $("#notification-unread").textContent = result.unread_count;
    $("#metric-notifications").textContent = result.unread_count;
    renderNotifications();
    return result;
  } catch (error) { if (!silent) toast(errorText(error), "error"); throw error; }
}

function desktopSupported() { return "Notification" in window && window.isSecureContext; }
function desktopEnabled() { try { return localStorage.getItem("capital-desktop-notifications") === "true"; } catch { return false; } }
function updateDesktopStatus(message = "") {
  const target = $("#desktop-status");
  if (!target) return;
  const enabled = desktopEnabled();
  if (message) target.textContent = message;
  else if (!desktopSupported()) target.textContent = "此瀏覽器不支援系統通知，請使用 Chrome 或 Edge。";
  else if (Notification.permission === "denied") target.textContent = "瀏覽器已封鎖通知，請在網站權限中允許。";
  else if (enabled && Notification.permission === "granted") target.textContent = "已啟用：新未讀提醒會嘗試顯示 Windows 通知。";
  else target.textContent = "尚未啟用系統通知；網頁內通知仍會正常保存。";
  $("#desktop-enable").disabled = !desktopSupported();
}
async function dispatchDesktop() {
  if (desktopBusy || !desktopEnabled() || !desktopSupported() || Notification.permission !== "granted") return;
  desktopBusy = true;
  try {
    const claim = await api("/api/v1/notifications/desktop/claim", { method: "POST" });
    if (!claim.notification) return;
    const notification = claim.notification;
    let status = "submitted", error = null;
    try {
      const popup = new Notification(notification.title, { body: notification.message.slice(0, 400), tag: notification.id });
      popup.onclick = () => { window.focus(); popup.close(); showView("notifications"); };
    } catch (exception) { status = "failed"; error = String(exception.message).slice(0, 500); }
    await api(`/api/v1/notifications/${notification.id}/desktop-ack`, { method: "POST", body: { claim_token: claim.claim_token, status, error } });
    await loadNotifications(true);
    updateDesktopStatus(status === "submitted" ? "已提交一則系統通知；相同通知不會再次領取。" : `系統通知提交失敗：${error}`);
  } catch (error) { updateDesktopStatus(`系統通知連線失敗：${errorText(error)}`); }
  finally { desktopBusy = false; }
}

async function notificationAction(event) {
  const trigger = event.target.closest('[data-action="toggle-notification"]');
  if (!trigger) return;
  try {
    await api(`/api/v1/notifications/${trigger.dataset.id}/read`, { method: "PATCH", body: { read: trigger.dataset.read === "true" } });
    activity("更新通知狀態", "ok", trigger.dataset.read === "true" ? "標為已讀" : "標為未讀");
    await loadNotifications(true);
  } catch (error) { toast(errorText(error), "error"); }
}

function buildScreenerConfiguration() {
  const kind = $("#universe-kind").value;
  const markets = $$('[name="market"]:checked').map(input => Number(input.value));
  const universe = { kind, markets, symbols: kind === "symbols" ? splitValues($("#universe-symbols").value).map(value => value.toUpperCase()) : [], category_nos: splitValues($("#category-nos").value).map(Number) };
  const prefilter = {};
  const minPrice = asNumber($("#min-price").value), minVolume = asNumber($("#min-volume").value);
  if (minPrice !== null) prefilter.min_price = minPrice;
  if (minVolume !== null) prefilter.min_volume = minVolume;
  return {
    universe, prefilter, history_interval: "day", candle_table: "candles", history_bars: 360,
    rule: parseJSON($("#screen-rule-json").value), limit: Number($("#screen-limit").value), timeout_seconds: Number($("#screen-timeout").value),
    sort: [{ field: "matched_count", direction: "desc" }, { field: "direction_score", direction: "desc" }, { field: "risk_score", direction: "asc" }],
  };
}

function renderScreenRows(table, items, withVolume = true) {
  const tbody = $("tbody", table);
  tbody.replaceChildren();
  (items || []).forEach(item => {
    const row = document.createElement("tr");
    const values = withVolume
      ? [item.rank, `${item.symbol} ${item.name || ""}`, fmtNumber(item.price), fmtNumber(item.volume), fmtNumber(item.direction_score), fmtNumber(item.risk_score), `${item.matched_count}/${item.total_rules}`, fmtTime(item.latest_candle_at)]
      : [item.rank, `${item.symbol} ${item.name || ""}`, fmtNumber(item.price), fmtNumber(item.direction_score), fmtNumber(item.risk_score), (item.reasons || []).join("；")];
    values.forEach(value => row.append(node("td", "", String(value ?? "—"))));
    tbody.append(row);
  });
}

function renderScreenResult(result, prefix = "screen") {
  const badge = $(`#${prefix}-badge`);
  setBadge(badge, result.complete ? "matched" : "unknown", result.complete ? "掃描完成" : "部分完成");
  const summary = $(`#${prefix}-summary`);
  summary.replaceChildren();
  const stats = node("div", "result-stats");
  [["範圍", result.universe_count], ["已處理", result.processed_count], ["符合", result.matched_count], ["回傳", result.returned_count], ["耗時", `${result.elapsed_seconds ?? "—"} 秒`]].forEach(([label, value]) => append(stats, append(node("div"), node("small", "", label), node("strong", "", String(value ?? "—")))));
  summary.append(stats);
  if (!result.complete) summary.append(node("p", "warning", `掃描尚未完成，剩餘 ${(result.remaining_symbols || []).length} 檔。`));
  (result.warnings || []).forEach(value => summary.append(node("p", "warning", value)));
  const table = prefix === "screen" ? $("#screen-table") : $("#saved-table");
  table.classList.toggle("hidden", !(result.items || []).length);
  renderScreenRows(table, result.items, prefix === "screen");
}

async function runScreener(event) {
  event.preventDefault();
  const submit = event.submitter;
  submit.disabled = true;
  $("#screen-progress").classList.remove("hidden");
  setBadge($("#screen-badge"), "working", "掃描中");
  try {
    const result = await api("/api/v1/screener/run", { method: "POST", body: buildScreenerConfiguration() });
    $("#screen-raw").textContent = formatJSON(result);
    renderScreenResult(result);
    activity("執行選股", "ok", `處理 ${result.processed_count}/${result.universe_count}，符合 ${result.matched_count}`);
    toast("選股掃描完成");
  } catch (error) {
    setBadge($("#screen-badge"), "error", "錯誤");
    $("#screen-summary").textContent = errorText(error);
    $("#screen-raw").textContent = errorText(error);
    activity("執行選股", "error", errorText(error)); toast(errorText(error), "error");
  } finally { submit.disabled = false; $("#screen-progress").classList.add("hidden"); }
}

function applyConfiguration(config) {
  $("#universe-kind").value = config.universe.kind;
  $("#universe-symbols").value = (config.universe.symbols || []).join(",");
  $$('[name="market"]').forEach(input => { input.checked = (config.universe.markets || []).includes(Number(input.value)); });
  $("#category-nos").value = (config.universe.category_nos || []).join(",");
  $("#min-price").value = config.prefilter?.min_price ?? "";
  $("#min-volume").value = config.prefilter?.min_volume ?? "";
  $("#screen-limit").value = config.limit;
  $("#screen-timeout").value = config.timeout_seconds;
  $("#screen-rule-json").value = formatJSON(config.rule);
  updateUniverseForm();
}

function renderSaved() {
  const target = $("#saved-list");
  target.replaceChildren();
  if (!state.screeners.length) { target.append(node("p", "empty panel", "目前沒有儲存策略。先在「選股與排序」設定條件，再回到此頁儲存。")); return; }
  state.screeners.forEach(item => {
    const universe = item.configuration.universe;
    const scope = universe.kind === "symbols" ? universe.symbols.join(", ") : universe.kind === "watchlist" ? "目前自選股" : `市場 ${universe.markets.join(", ")}`;
    const card = node("article", "panel item-card saved-card");
    const title = append(node("div"), node("h3", "", item.name), node("p", "", item.description || "沒有說明"));
    const version = node("span", "revision", `rev ${item.revision}`);
    const meta = append(node("div", "item-meta"), append(node("div"), node("small", "", "股票範圍"), node("b", "", scope)), append(node("div"), node("small", "", "最多回傳"), node("b", "", String(item.configuration.limit))), append(node("div"), node("small", "", "更新時間"), node("b", "", fmtTime(item.updated_at))));
    const actions = append(node("div", "item-actions"), button("執行策略", "primary small", "run-saved", { id: item.id }), button("載入選股頁", "secondary small", "load-saved", { id: item.id }), button("編輯", "secondary small", "edit-saved", { id: item.id }), button("刪除", "danger-link", "delete-saved", { id: item.id }));
    append(card, append(node("div", "item-header"), title, version), meta, actions);
    target.append(card);
  });
}

async function loadSaved(silent = false) {
  try {
    const result = await api("/api/v1/screeners?limit=100&offset=0");
    state.screeners = result.items || [];
    $("#metric-saved").textContent = result.total;
    renderSaved();
    return result;
  } catch (error) { if (!silent) toast(errorText(error), "error"); throw error; }
}

function resetSavedForm() {
  $("#saved-form").reset(); $("#saved-id").value = ""; $("#saved-revision").value = "";
  $("#cancel-edit").classList.add("hidden"); $("#save-button-label").textContent = "儲存目前選股設定";
}

async function saveScreener(event) {
  event.preventDefault();
  const id = $("#saved-id").value;
  const body = { name: $("#saved-name").value.trim(), description: $("#saved-description").value.trim(), configuration: buildScreenerConfiguration() };
  if (id) body.expected_revision = Number($("#saved-revision").value);
  event.submitter.disabled = true;
  try {
    const result = await api(id ? `/api/v1/screeners/${id}` : "/api/v1/screeners", { method: id ? "PUT" : "POST", body });
    activity(id ? "更新儲存策略" : "建立儲存策略", "ok", `${result.name} · rev ${result.revision}`);
    toast(id ? "策略已更新" : "策略已儲存");
    resetSavedForm(); await loadSaved(true);
  } catch (error) { activity("儲存策略", "error", errorText(error)); toast(errorText(error), "error"); }
  finally { event.submitter.disabled = false; }
}

async function savedAction(event) {
  const trigger = event.target.closest("[data-action]");
  if (!trigger || !["run-saved", "load-saved", "edit-saved", "delete-saved"].includes(trigger.dataset.action)) return;
  const item = state.screeners.find(value => value.id === trigger.dataset.id);
  if (!item) return;
  if (trigger.dataset.action === "load-saved") { applyConfiguration(item.configuration); showView("screener"); toast("策略設定已載入選股頁"); return; }
  if (trigger.dataset.action === "edit-saved") {
    applyConfiguration(item.configuration);
    $("#saved-id").value = item.id; $("#saved-revision").value = item.revision; $("#saved-name").value = item.name; $("#saved-description").value = item.description;
    $("#cancel-edit").classList.remove("hidden"); $("#save-button-label").textContent = "更新策略"; window.scrollTo({ top: 0, behavior: "smooth" }); return;
  }
  if (trigger.dataset.action === "delete-saved") {
    state.deleteId = item.id; $("#delete-name").textContent = item.name; $("#confirm-dialog").showModal(); return;
  }
  trigger.disabled = true;
  $("#saved-run-result").classList.remove("hidden");
  setBadge($("#saved-run-badge"), "working", "執行中");
  try {
    const result = await api(`/api/v1/screeners/${item.id}/run`, { method: "POST" });
    renderScreenResult(result, "saved-run");
    activity("執行儲存策略", "ok", `${item.name}：符合 ${result.matched_count}`); toast("儲存策略執行完成");
  } catch (error) { setBadge($("#saved-run-badge"), "error", "錯誤"); $("#saved-run-summary").textContent = errorText(error); activity("執行儲存策略", "error", errorText(error)); toast(errorText(error), "error"); }
  finally { trigger.disabled = false; }
}

async function deleteSaved() {
  if (!state.deleteId) return;
  const current = state.screeners.find(item => item.id === state.deleteId);
  $("#confirm-delete").disabled = true;
  try {
    await api(`/api/v1/screeners/${state.deleteId}`, { method: "DELETE" });
    activity("刪除儲存策略", "ok", current?.name || state.deleteId); toast("策略已刪除");
    $("#confirm-dialog").close(); state.deleteId = null; await loadSaved(true);
  } catch (error) { toast(errorText(error), "error"); }
  finally { $("#confirm-delete").disabled = false; }
}

function updateUniverseForm() {
  const symbols = $("#universe-symbols");
  symbols.disabled = $("#universe-kind").value !== "symbols";
}

async function refreshOverview() {
  const results = await Promise.allSettled([
    api("/api/v1/health"), loadFields(false), loadAlerts(true), loadNotifications(true), loadSaved(true),
  ]);
  const health = results[0];
  const online = health.status === "fulfilled";
  $("#api-dot").className = `dot ${online ? "ok" : "error"}`;
  $("#api-status").textContent = online ? "API 連線正常" : "API 無法連線";
  if (!online) toast("API 無法連線，請確認服務是否啟動", "error");
}

function bindEvents() {
  $$('[data-view]').forEach(element => element.addEventListener("click", () => showView(element.dataset.view)));
  $("#start-validation").addEventListener("click", () => showView("rules"));
  $("#refresh-all").addEventListener("click", refreshOverview);
  $("#clear-log").addEventListener("click", () => { sessionStorage.removeItem("v3-activity"); renderActivity(); });
  $("#rule-form").addEventListener("submit", runRuleTest);
  $("#rule-preset").addEventListener("change", event => { $("#rule-json").value = formatJSON(PRESETS[event.target.value]); });
  $("#format-rule").addEventListener("click", () => { try { $("#rule-json").value = formatJSON(parseJSON($("#rule-json").value)); } catch (error) { toast(errorText(error), "error"); } });
  $("#load-rule-fields").addEventListener("click", () => loadFields(true));
  $("#alert-form").addEventListener("submit", createAlert); $("#refresh-alerts").addEventListener("click", () => loadAlerts()); $("#alerts-list").addEventListener("click", handleAlertAction);
  $("#refresh-notifications").addEventListener("click", () => loadNotifications()); $("#only-unread").addEventListener("change", () => loadNotifications()); $("#v3-notification-list").addEventListener("click", notificationAction);
  $("#create-test-notification").addEventListener("click", async () => { try { await api("/api/v1/notifications/test", { method: "POST" }); activity("建立測試通知", "ok", "通知已寫入 SQLite"); toast("測試通知已建立"); await loadNotifications(true); await dispatchDesktop(); } catch (error) { toast(errorText(error), "error"); } });
  $("#desktop-enable").addEventListener("click", async () => { if (!desktopSupported()) return; try { const permission = await Notification.requestPermission(); localStorage.setItem("capital-desktop-notifications", String(permission === "granted")); updateDesktopStatus(); await dispatchDesktop(); } catch (error) { updateDesktopStatus(errorText(error)); } });
  $("#desktop-disable").addEventListener("click", () => { try { localStorage.setItem("capital-desktop-notifications", "false"); } catch {} updateDesktopStatus(); });
  $("#mark-all-read").addEventListener("click", async () => { try { const result = await loadNotifications(true); const sequence = Math.max(0, ...(result.items || []).map(item => item.sequence)); if (!sequence) { toast("目前沒有通知"); return; } const changed = await api("/api/v1/notifications/read-all", { method: "POST", body: { through_sequence: sequence } }); activity("全部標為已讀", "ok", `更新 ${changed.updated_count} 則`); toast(`已更新 ${changed.updated_count} 則通知`); await loadNotifications(true); } catch (error) { toast(errorText(error), "error"); } });
  $("#screener-form").addEventListener("submit", runScreener); $("#universe-kind").addEventListener("change", updateUniverseForm); $("#copy-current-rule").addEventListener("click", () => { $("#screen-rule-json").value = $("#rule-json").value; toast("已套用規則測試內容"); });
  $("#saved-form").addEventListener("submit", saveScreener); $("#cancel-edit").addEventListener("click", resetSavedForm); $("#refresh-saved").addEventListener("click", () => loadSaved()); $("#saved-list").addEventListener("click", savedAction); $("#confirm-delete").addEventListener("click", deleteSaved);
  $$('[data-close-dialog]').forEach(element => element.addEventListener("click", () => element.closest("dialog").close()));
}

function init() {
  const initial = PRESETS.price_ma20;
  $("#rule-json").value = formatJSON(initial); $("#alert-rule-json").value = formatJSON(initial); $("#screen-rule-json").value = formatJSON(initial);
  bindEvents(); updateUniverseForm(); renderActivity(); updateDesktopStatus(); showView(location.hash.slice(1) || "overview", false); window.addEventListener("hashchange", () => showView(location.hash.slice(1), false)); refreshOverview();
  setInterval(() => { const active = $(".view.active")?.dataset.page; if (active === "notifications" || active === "overview") loadNotifications(true); }, 5000);
}

document.addEventListener("DOMContentLoaded", init);
