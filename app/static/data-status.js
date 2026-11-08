const $ = (id) => document.getElementById(id);
const controls = ["markets", "intervals", "target-bars", "resume", "reload", "refresh", "prepare-all", "retry", "pause", "continue"];
let statusLoading = false;
let workerLoading = false;
let actionBusy = false;
let statusQueued = false;
let dayGapsLoading = false;
let dayGapsOffset = 0;
let dayGapsTotal = 0;
let dayGapsMarkets = null;
const openRowDetails = new Set();

function params(includeAction = false) {
  const result = new URLSearchParams({ markets: $("markets").value, intervals: $("intervals").value });
  if (includeAction) {
    if ($("target-bars").value.trim()) result.set("target_bars", $("target-bars").value);
    result.set("resume", $("resume").value);
  }
  return result;
}

function dateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString("zh-TW", { hour12: false });
}

function formatFetchDuration(seconds) {
  return Number.isFinite(seconds) && seconds >= 0 ? `${seconds.toFixed(2)} 秒／檔` : "—";
}

function updateControls() {
  // A slow read must never disable the pause/resume controls.
  controls.forEach((id) => { $(id).disabled = id === "reload" ? statusLoading : actionBusy; });
}

function statusMessage(message, error = false) {
  $("operation-status").textContent = message;
  $("operation-status").classList.toggle("error", error);
}

function td(text, className = "") {
  const cell = document.createElement("td");
  cell.textContent = text;
  if (className) cell.className = className;
  return cell;
}

function rowDetails(key, columnCount, entries) {
  const detailRow = document.createElement("tr");
  detailRow.className = "row-details";
  detailRow.id = `detail-${encodeURIComponent(key)}`;
  detailRow.hidden = !openRowDetails.has(key);
  const content = td("");
  content.colSpan = columnCount;
  const list = document.createElement("dl");
  list.className = "row-details-grid";
  entries.forEach(([label, value]) => {
    const group = document.createElement("div");
    const title = document.createElement("dt");
    const description = document.createElement("dd");
    title.textContent = label;
    description.textContent = value == null || value === "" ? "—" : String(value);
    group.append(title, description);
    list.appendChild(group);
  });
  content.appendChild(list);
  detailRow.appendChild(content);
  const cell = td("");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary row-details-toggle";
  button.textContent = "詳細資訊";
  button.setAttribute("aria-controls", detailRow.id);
  button.setAttribute("aria-expanded", String(!detailRow.hidden));
  button.addEventListener("click", () => {
    detailRow.hidden = !detailRow.hidden;
    button.setAttribute("aria-expanded", String(!detailRow.hidden));
    if (detailRow.hidden) openRowDetails.delete(key);
    else {
      openRowDetails.add(key);
      if (openRowDetails.size > 100) openRowDetails.delete(openRowDetails.values().next().value);
    }
  });
  cell.appendChild(button);
  return { cell, detailRow };
}

function renderSchedule(schedule) {
  const badge = $("schedule-state");
  const labels = { disabled: "未啟用", waiting: "等待中", queued: "每日更新已排入", waiting_worker: "等待目前工作釋放，稍後重試", waiting_connection: "等待群益連線", waiting_database: "等待資料庫", waiting_memory: "等待記憶體恢復", waiting_backfill: "缺口等待下輪驗收", running: "執行中", paused: "已暫停", completed: "已完成", skipped: "已略過", error: "錯誤", unavailable: "未啟動" };
  badge.textContent = schedule.state === "waiting_review" ? "空資料待查證" : labels[schedule.state] || schedule.state;
  badge.className = `badge ${schedule.state === "completed" ? "good" : schedule.state === "error" ? "bad" : "warn"}`;
  const rolling = schedule.refresh_bars_by_interval && Object.keys(schedule.refresh_bars_by_interval).length > 0;
  const targets = rolling ? schedule.refresh_bars_by_interval : schedule.target_bars_by_interval || {};
  const target = (interval) => `${interval}:${targets[interval] || schedule.target_bars}`;
  const dailyIntervals = schedule.intervals.filter((interval) => !["week", "month"].includes(interval));
  const weeklyIntervals = schedule.intervals.filter((interval) => interval === "week");
  const monthlyIntervals = schedule.intervals.filter((interval) => interval === "month");
  const cadenceText = [
    dailyIntervals.length ? `每日交易日 ${dailyIntervals.map(target).join(", ")}` : "",
    weeklyIntervals.length ? `${schedule.aggregate_week_month_from_day ? "每日由日K重算（含本週暫定）" : "每週最後交易日"} ${weeklyIntervals.map(target).join(", ")}` : "",
    monthlyIntervals.length ? `${schedule.aggregate_week_month_from_day ? "每日由日K重算（含本月暫定）" : "每月最後交易日"} ${monthlyIntervals.map(target).join(", ")}` : "",
  ].filter(Boolean).join("；");
  $("schedule-detail").textContent = schedule.enabled
    ? `${schedule.timezone} 每日 ${schedule.local_time} 檢查；市場 ${schedule.markets.join(",")}；${rolling ? "日常回抓目標" : "初始化目標"}：${cadenceText}；${rolling ? "依實際回傳根數達標，不截短既有資料；初始化／重抓目標另計" : "日常只補缺少區間"}`
    : "排程目前關閉；可在 .env 設定 CAPITAL_DAILY_BACKFILL_ENABLED=true 後重啟 API。";
  $("schedule-next").textContent = dateTime(schedule.next_run_at);
  $("schedule-last").textContent = dateTime(schedule.last_finished_at || schedule.last_started_at);
  $("schedule-message").textContent = schedule.last_message || "—";
}

function renderScheduleLogs(logs) {
  const body = $("schedule-log-rows");
  body.replaceChildren();
  const statusLabels = { running: "執行中", paused: "暫停（工作保留等待）", waiting_review: "空資料待查證", completed: "完成", error: "失敗", skipped: "略過" };
  const stageLabels = {
    syncing_board_instruments: "同步整張商品",
    syncing_odd_lot_instruments: "同步零股商品",
    waiting_backfill: "慢速補齊等待中",
    planning_kline: "建立 K 線工作",
    backfilling_kline: "更新 K 線",
    connection_paused: "連線恢復失敗，等待續跑",
    connection: "等待群益連線恢復",
    database: "等待資料庫恢復",
    memory: "記憶體保護暫停",
    manual: "安全暫停",
    no_job_history: "缺少首次回補",
    kline_failed: "K 線部分失敗",
    interrupted: "API 中斷",
    completed: "完成",
  };
  if (!logs.length) {
    const row = document.createElement("tr");
    const cell = td("尚無自動更新紀錄");
    cell.colSpan = 7;
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }
  logs.forEach((log, index) => {
    const row = document.createElement("tr");
    const statusCell = td(statusLabels[log.status] || log.status, `log-${log.status}`);
    const productText = `整張 ${log.board_instrument_count}／零股 ${log.odd_lot_instrument_count}`;
    const jobText = `排入 ${log.queued_jobs}／處理 ${log.processed_jobs}／成功 ${log.completed_jobs}／不足 ${log.insufficient_jobs}／失敗 ${log.failed_jobs}`;
    row.append(
      td(dateTime(log.started_at)), td(dateTime(log.updated_at)), statusCell, td(stageLabels[log.stage] || log.stage),
      td(productText), td(jobText),
    );
    const detail = rowDetails(`log:${log.id || `${log.started_at}:${index}`}`, 7, [
      ["訊息", log.message], ["錯誤訊息", log.error_message],
    ]);
    row.appendChild(detail.cell);
    body.append(row, detail.detailRow);
  });
}

function formatDayGapCounts(audit) {
  const products = audit.unresolved_symbols == null ? "待查商品數尚未統計" : `待查 ${audit.unresolved_symbols} 檔`;
  return `${products}／${audit.unresolved_dates} 個日期`;
}

function renderWorker(worker) {
  const workerLabels = { preparing: "完整重抓準備中", queued: "已排入續跑", planning: "正在重建工作計畫", prepared: "工作已準備", running: "執行中", paused: "已暫停", waiting_connection: "等待群益連線", waiting_trading_day: "等待下一交易日", waiting_backfill: "慢速補齊等待中", completed: "完成", error: "工作失敗", limited: "已達本次工作數上限" };
  $("worker-state").textContent = `${workerLabels[worker.state] || "等待工作"}${worker.pause_requested ? "；已請求安全暫停" : ""}${worker.pause_reason ? `：${worker.pause_reason}` : ""}`;
  if (worker.state === "waiting_review") $("worker-state").textContent = "空資料待查證（不代表完整）；" + (worker.pause_reason || "");
  $("worker-detail").textContent = worker.current_symbol
    ? `${worker.current_requery ? "完整範圍重抓" : worker.current_refresh ? "日常最新根數回抓" : worker.queue_phase === "history" ? "較舊歷史補齊" : worker.queue_phase === "catchup" ? "長缺口慢速補齊" : "最新缺口優先"}；市場 ${worker.current_market}／${worker.current_symbol}／${worker.current_interval}；查詢 ${worker.window_start}～${worker.window_end}；已提交斷點 ${worker.checkpoint || "—"}；完成區間 ${worker.windows || 0}；完成工作 ${worker.processed || 0}；等待補齊 ${worker.deferred || 0}${worker.next_retry_at ? `；下輪 ${dateTime(worker.next_retry_at)}` : ""}`
    : worker.state === "prepared" ? `已準備 ${worker.queued_jobs || 0} 筆工作，尚未抓取；請按「繼續回補」。既有 K 線保留。` : "每個查詢區間完整寫入後保存進度；暫停不會刪除既有資料。";
  const group = worker.group_plan;
  if (worker.empty_review) $("worker-detail").textContent += `；${worker.empty_review.reason}`;
  if (worker.pending_review) $("worker-detail").textContent += worker.pending_review_symbols != null && worker.pending_review_dates != null
    ? `；待查 ${worker.pending_review_symbols} 檔／${worker.pending_review_dates} 個日期${worker.pending_review_periods ? `；另 ${worker.pending_review_periods} 個週月期間` : ""}`
    : `；${worker.pending_review} 筆空資料待查紀錄（非商品數）`;
  if (worker.day_integrity_active) $("worker-detail").textContent = `日 K 完整性驗收；市場 ${worker.current_market}／${worker.current_symbol || "—"}；已檢查 ${worker.day_integrity_checked || 0} 檔${worker.day_gap_date ? `；本次缺口 ${worker.day_gap_date}，查詢 ${worker.window_start}～${worker.window_end}` : ""}`;
  if (worker.day_first) $("worker-detail").textContent += worker.priority_phase === "day"
    ? "；日 K 正常抓取優先，待補齊留到所有週期正常抓取之後"
    : worker.priority_phase === "other" ? "；其他週期正常抓取中，日 K 待補齊不阻擋" : worker.priority_phase === "recovery"
      ? `；所選週期正常抓取輪已結束，正在處理／等待補齊${worker.day_pending ? `（日 K ${worker.day_pending} 筆工作）` : ""}` : "";
  if (worker.day_integrity) {
    const audit = worker.day_integrity;
    $("worker-detail").textContent += `；日 K 驗收：已檢查 ${audit.checked_symbols} 檔，完整目標 ${audit.complete_symbols} 檔，可用歷史不足且已核實 ${audit.available_history_symbols} 檔，${formatDayGapCounts(audit)}`;
  }
  const maintenance = worker.day_integrity_maintenance;
  $("day-maintenance-detail").textContent = maintenance
    ? `休市日日 K 驗收：${maintenance.active && worker.worker_active ? "執行中" : maintenance.state === "completed" ? "最近一輪已完成" : "等待／未完成"}；截止 ${maintenance.as_of_date || "—"}；已檢查 ${maintenance.day_integrity_checked || 0} 檔${maintenance.current_symbol ? `；市場 ${maintenance.current_market}／${maintenance.current_symbol}` : ""}${maintenance.day_integrity ? `；${formatDayGapCounts(maintenance.day_integrity)}` : ""}${maintenance.pause_reason ? `；${maintenance.pause_reason}` : ""}。一般抓取計畫與斷點保留。`
    : worker.day_integrity_schedule_reason || "交易日照常更新；完整驗收安排於休市日。";
  if (worker.preparation_pending) $("worker-detail").textContent = `已取得獨占工作鎖；準備進度 ${worker.preparation_done || 0}/${worker.preparation_total || 35} 個市場／週期；全部成功後自動開始。可按安全暫停。`;
  if (worker.current_target_bars) $("worker-detail").textContent += `；${worker.current_refresh ? "本輪回抓" : "本工作補齊"}目標 ${worker.current_target_bars} 根（非儲存上限）`;
  if (worker.current_symbol && worker.listing_policy) {
    const listing = worker.listing_policy;
    $("worker-detail").textContent += listing.history_start
      ? `；適用掛牌邊界 ${listing.history_start}（含當日；週／月保留該期）`
      : listing.reason === "existing_history_precedes_listing" ? "；既有掛牌前行情優先保留，不以掛牌日截斷"
      : "；原市場掛牌邊界尚未核實，保留歷史探查";
    if (worker.listing_history_shortfall) $("worker-detail").textContent += `；掛牌歷史不足：${worker.stored_bars || 0}/${worker.current_target_bars || "—"} 根，不冒充完整目標`;
  }
  if (worker.current_aggregate_from_day) $("worker-detail").textContent += `；由完整日K範圍重算，含當期暫定週／月；同期資料覆寫${worker.source_window_start ? `；來源日K ${worker.source_window_start}～${worker.source_window_end}` : ""}`;
  if (worker.data_failure) {
    const failure = worker.data_failure;
    $("worker-detail").textContent += `；最近來源資料異常 ${failure.symbol}/${failure.interval}：同區間 ${failure.count}/${failure.limit} 次，${failure.failed ? "已隔離該工作" : "保留斷點等待重試"}`;
  }
  if (worker.history_exclusions?.length) {
    const periods = worker.history_exclusions.map(rule => `${rule.symbol}/${rule.interval} ${rule.start_date}～${rule.end_date}`).join("、");
    $("worker-detail").textContent += `；已明確排除 ${periods}（不寫入、不計入根數，保留排除紀錄）`;
  }
  if (group) $("worker-detail").textContent += `；族群最舊檢查日 ${group.oldest_checked_through || "尚無"}（不強迫全族群從此日重抓）；已確認停牌 ${group.suspended_symbols || 0}；未確認長缺口 ${group.unknown_lagging_symbols || 0}`;
  if (worker.suspensions) $("worker-detail").textContent += `；停復牌來源 ${worker.suspensions.state}${worker.suspensions.state === "partial" ? "（部分來源無法更新，不依過期名單跳過）" : ""}`;
  const memoryUpdatedAt = worker.pause_kind === "memory" ? worker.memory_observed_at || worker.updated_at : worker.updated_at;
  $("worker-memory").textContent = `記憶體：程序配置 ${worker.memory_private_mb ?? "—"} MB／實體使用 ${worker.memory_working_set_mb ?? "—"} MB；最近更新 ${dateTime(memoryUpdatedAt)}`;
  if (worker.memory_resume_mb) $("worker-memory").textContent += `；恢復須低於 ${worker.memory_resume_mb} MB 並持續穩定；階段 ${worker.memory_stage || "—"}`;
  if (worker.native_worker?.enabled) {
    const native = worker.native_worker;
    $("worker-memory").textContent += `；群益背景程序 PID ${native.pid ?? "—"}／${native.private_mb ?? "—"} MiB；輪替門檻 ${native.recycle_mb} MiB；狀態 ${native.phase}；已輪替 ${native.recycle_count || 0} 次${native.recycle_due ? "（等待當前區間提交後輪替）" : ""}${native.last_error ? `；${native.last_error}` : ""}`;
    if (Object.keys(native.subscription_errors || {}).length) $("worker-memory").textContent += `；訂閱未恢復：${Object.keys(native.subscription_errors).join("、")}`;
  }
  if (worker.pause_kind === "memory") {
    $("worker-memory").textContent += `；冷卻至 ${dateTime(worker.memory_retry_after)}；低水位觀察開始 ${dateTime(worker.memory_resume_below_since)}`;
    const readiness = !worker.memory_observed_at ? "尚未取得恢復觀察結果"
      : worker.memory_resume_ready === true ? `已符合（仍須符合${worker.manual_run ? "群益連線及未手動暫停；手動回補休市日亦可" : "交易日、群益連線及未手動暫停等續跑條件"}）` : "尚未符合";
    $("worker-memory").textContent += `；記憶體恢復條件：${readiness}`;
  }
  const memoryBody = $("memory-rows");
  memoryBody.replaceChildren();
  [...(worker.memory_samples || [])].reverse().forEach((sample) => {
    const row = document.createElement("tr");
    [dateTime(sample.at),sample.stage,sample.private_mb ?? "—",sample.working_set_mb ?? "—",sample.delta_mb ?? "—",sample.gc ? "是" : "否"].forEach((value) => row.appendChild(td(String(value))));
    memoryBody.appendChild(row);
  });
}

function render(data) {
  let active = 0, tracked = 0, activeJobs = 0, failed = 0;
  const activeMarkets = new Set();
  const body = $("status-rows");
  const failures = $("failures");
  body.replaceChildren();
  failures.replaceChildren();
  data.items.forEach((item) => {
    if (!activeMarkets.has(item.market_no)) { activeMarkets.add(item.market_no); active += item.active_instruments; }
    tracked += item.tracked_symbols;
    activeJobs += item.job_counts.pending + item.job_counts.running;
    failed += item.job_counts.failed;
    const row = document.createElement("tr");
    if (item.job_counts.failed) row.className = "has-failure";
    const phases = item.pending_phases || {};
    const known = ["latest", "catchup", "history"].reduce((sum, phase) => sum + (phases[phase] || 0), 0);
    const unknown = phases.unknown ?? Math.max(0, item.job_counts.pending - known);
    [
      [item.market_name], [item.interval], [`${item.tracked_symbols} / ${item.active_instruments}`, "number"],
      [item.job_counts.completed, "number"], [phases.latest || 0, "number"],
      [item.job_counts.insufficient_history, "number"], [item.job_counts.running, "number"],
      [item.data_through || "—"],
    ].forEach(([value, className]) => row.appendChild(td(String(value), className)));
    row.children[7].title = "有 K 線商品各自最後標記日的最小值，未包含完全無資料商品；週月標記是期間起始日，不代表已完成或所有商品同日更新。";
    const updateCell = td("", "status-update-cell");
    const checked = document.createElement("div");
    checked.textContent = `已檢查至：${item.checked_through || "—"}（${item.checked_symbols || 0}/${item.active_instruments}）`;
    const updated = document.createElement("div");
    updated.className = "subtle";
    updated.textContent = `最近狀態：${dateTime(item.latest_status_at)}`;
    updateCell.append(checked, updated);
    row.appendChild(updateCell);
    const detail = rowDetails(`market:${item.market_no}:${item.interval}`, 10, [
      ["長缺口等待", phases.catchup || 0], ["歷史等待", phases.history || 0],
      ["待分類", unknown], ["執行", item.job_counts.running], ["失敗", item.job_counts.failed],
      ["平均抓取時間", `${formatFetchDuration(item.average_fetch_seconds)}（樣本 ${item.average_fetch_sample_count || 0} 檔）`],
      ["空資料待查證", item.job_counts.waiting_review || 0],
      ["無 K 線商品", `${item.data_missing_symbols || 0} 檔`],
      ["資料表狀態", item.storage_ready ? "已就緒" : "資料表未完整"],
      ["最近失敗商品", item.latest_failure_symbol], ["最近失敗時間", dateTime(item.latest_failure_at)],
      ["失敗原因", item.latest_failure_error],
    ]);
    row.appendChild(detail.cell);
    body.append(row, detail.detailRow);
    if (item.latest_failure_error) {
      const block = document.createElement("div");
      block.className = "failure-item";
      const title = document.createElement("strong");
      title.textContent = `${item.market_name} ${item.interval} / ${item.latest_failure_symbol || "未知商品"}`;
      const detail = document.createElement("code");
      detail.textContent = `${dateTime(item.latest_failure_at)} — ${item.latest_failure_error}`;
      block.append(title, document.createElement("br"), detail);
      failures.appendChild(block);
    }
  });
  if (!data.items.length) {
    const row = document.createElement("tr");
    const cell = td(data.database_error ? "資料庫狀態尚未取得，請查看上方錯誤。" : data.database_refreshing ? "首次資料庫快照建立中；操作按鈕與回補控制仍可使用。" : "沒有符合條件的狀態資料");
    cell.colSpan = 10; row.appendChild(cell); body.appendChild(row);
  }
  $("active-total").textContent = active.toLocaleString("zh-TW");
  $("tracked-total").textContent = tracked.toLocaleString("zh-TW");
  $("active-jobs").textContent = activeJobs.toLocaleString("zh-TW");
  $("failed-total").textContent = failed.toLocaleString("zh-TW");
  $("failure-panel").hidden = failures.childElementCount === 0;
  $("generated-at").textContent = `查詢時間：${dateTime(data.generated_at)}`;
  renderSchedule(data.schedule);
  renderScheduleLogs(data.schedule_logs || []);
}

async function jsonRequest(url, options = {}, timeoutMs = 20000) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", ...options, signal: controller.signal });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `${response.status} ${response.statusText}`);
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("要求逾時；這不代表背景工作已停止，請查看回補進度，勿重複送出。");
    throw error;
  } finally { window.clearTimeout(timeout); }
}

async function loadWorker() {
  if (workerLoading) return;
  workerLoading = true;
  try {
    const worker = await jsonRequest("/api/v1/backfill/control", {}, 5000);
    renderWorker(worker);
    $("control-status").textContent = worker.control_version >= 7 ? "已載入互斥重抓與自動啟動；手動回補休市日亦可。補齊目標不是儲存上限。" : "目前 API 尚未載入互斥重抓與自動啟動；請先安全暫停，再自行安排重啟 API。";
  } catch (error) { $("control-status").textContent = `回補控制狀態讀取失敗：${error.message}`; }
  finally { workerLoading = false; }
}

async function loadStatus({ quiet = false, force = false } = {}) {
  if (statusLoading) { if (!quiet) statusQueued = true; return; }
  statusLoading = true;
  const selected = params().toString();
  const begin = performance.now();
  updateControls();
  $("query-status").textContent = "正在讀取資料庫狀態；暫停／繼續按鈕仍可使用…";
  try {
    const data = await jsonRequest(`/api/v1/backfill/status?${selected}${force ? "&refresh=true" : ""}`);
    if (selected === params().toString()) {
      render(data);
      const database = data.database_query_seconds == null ? "伺服器尚未提供查詢分項" : !data.database_cache_hit ? "尚未完成首次查詢" : `資料庫 ${data.database_query_seconds.toFixed(3)} 秒；共用 ${data.database_cache_age_seconds.toFixed(1)} 秒前的快照`;
      $("query-status").textContent = data.database_error ? `背景資料庫查詢失敗：${data.database_error}；保留上次結果，稍後重試。` : `頁面回應 ${((performance.now()-begin)/1000).toFixed(2)} 秒；${data.database_refreshing ? "資料庫快照在背景更新中；顯示已取得的結果。" : "快照已就緒。"} 最近完成的資料庫查詢：${database}。`;
    }
  } catch (error) {
    $("query-status").textContent = `資料庫狀態讀取失敗：${error.message}`;
  } finally {
    statusLoading = false;
    updateControls();
    if (statusQueued || selected !== params().toString()) { statusQueued = false; void loadStatus({ quiet: true }); }
  }
}

function renderDayGaps(data) {
  const body = $("day-gap-rows");
  body.replaceChildren();
  const marketNames = {0:"上市",1:"上櫃",4:"興櫃",5:"上市零股",6:"上櫃零股"};
  const states = {unresolved:"待查證／補抓",failed_data:"資料異常隔離",resolved:"已補回",confirmed_no_bar:"已核實無 K",pending_review:"待人工查證"};
  const reasons = {api_missing_confirmed:"已有成交證據但缺 K：API 缺漏調查",trade_activity_unconfirmed:"尚無成交證據：待查交易活動（未判定 API 缺漏）"};
  for (const gap of data.items || []) {
    const row = document.createElement("tr");
    [marketNames[gap.market_no] || gap.market_no, gap.symbol, gap.candle_date,
      states[gap.status] || gap.status, `${reasons[gap.reason] || gap.reason}${gap.source ? `；${gap.source}` : ""}`,
      gap.attempt_count, dateTime(gap.next_retry_at)].forEach(value => row.appendChild(td(String(value))));
    body.appendChild(row);
  }
  const shown = new Set((data.items || []).map(gap => `${gap.candle_table}/${gap.market_no}/${gap.symbol}`));
  for (const audit of data.assessments || []) {
    if (shown.has(`${audit.candle_table}/${audit.market_no}/${audit.symbol}`)) continue;
    const row = document.createElement("tr");
    [marketNames[audit.market_no] || audit.market_no, audit.symbol, `${audit.search_start}～${audit.as_of_date}`,
      audit.status, JSON.stringify(audit.audit_json || {}), "—", "待驗收"].forEach(value => row.appendChild(td(String(value))));
    body.appendChild(row);
  }
  const summary = (data.summary || []).map(item => `${marketNames[item.market_no] || item.market_no} ${item.status}：${item.symbols} 檔（截止 ${item.as_of_date}）`).join("；");
  $("day-gaps-status").textContent = !data.storage_ready ? "驗收清單尚未建立；需由新版本續跑日 K，不能把未檢查當成完整。"
    : data.assessment_total === 0 && (data.summary || []).length === 0 && data.total === 0
      ? "目前沒有驗收紀錄（清單已清空或尚未驗收）；尚未重新驗收，不代表資料完整。"
    : `待核日期共 ${data.total} 筆；顯示 ${data.items?.length ? data.offset+1 : 0}～${data.offset+(data.items?.length || 0)}。${summary}`;
}

async function loadDayGaps(offset = 0) {
  if (dayGapsLoading) return;
  dayGapsLoading = true;
  ["day-gaps-load", "day-gaps-prev", "day-gaps-next"].forEach(id => { $(id).disabled = true; });
  const markets = $("markets").value;
  dayGapsMarkets = markets;
  try {
    const data = await jsonRequest(`/api/v1/backfill/day-integrity?${new URLSearchParams({markets,limit:200,offset})}`);
    if (markets !== $("markets").value) {
      $("day-gaps-status").textContent = "市場篩選已變更，請重新查詢日 K 缺口。";
      return;
    }
    dayGapsOffset = data.offset;
    dayGapsTotal = Math.max(data.total, data.assessment_total || 0);
    renderDayGaps(data);
  } catch (error) { $("day-gaps-status").textContent = `日 K 驗收清單讀取失敗：${error.message}`; }
  finally {
    dayGapsLoading = false;
    $("day-gaps-load").disabled = false;
    $("day-gaps-prev").disabled = dayGapsOffset <= 0;
    $("day-gaps-next").disabled = dayGapsOffset+200 >= dayGapsTotal;
  }
}

async function runAction(path, confirmation) {
  if (confirmation && !window.confirm(confirmation)) return;
  if (actionBusy) return;
  actionBusy = true;
  updateControls();
  statusMessage("工作建立中，請勿重複按下按鈕…");
  try {
    const data = await jsonRequest(`${path}?${params(true)}`, { method: "POST" });
    statusMessage(data.message || "操作已受理。");
    if (data.control) renderWorker(data.control);
    // Do not keep buttons disabled while the unrelated SQL refresh runs.
    void loadWorker();
    void loadStatus({ quiet: true });
  } catch (error) {
    statusMessage(`操作失敗：${error.message}`, true);
  } finally {
    actionBusy = false;
    updateControls();
  }
}

async function prepareAllBackfill() {
  if (actionBusy) return;
  const confirmation = "確定重抓全部市場與週期，並在準備成功後直接開始嗎？\n\n市場：上市、上櫃、興櫃、上市零股、上櫃零股。\n週期：1m、5m、15m、60m、day、week、month。\n不採用上方篩選與自訂根數；使用現有商品主檔。\n保留既有K線，相同時間鍵更新，不清空資料庫。\n\n後端會獨占 API／腳本共用工作鎖，其他回補執行中即拒絕啟動；全部範圍準備成功才抓取。休市日可抓已完成期間，仍保留記憶體與連線保護。";
  if (!window.confirm(confirmation)) return;
  actionBusy = true;
  updateControls();
  try {
    statusMessage("正在確認回補是否已停止…");
    const worker = await jsonRequest("/api/v1/backfill/control", {}, 5000);
    if (worker.control_version < 7 || !Number.isFinite(worker.control_version)) throw new Error("API 尚未載入互斥重抓與自動啟動；請先安全暫停並安排重啟 API，再使用此按鈕。");
    if (worker.worker_active || worker.preparation_pending || (worker.resume_pending || ["running","queued","planning","preparing"].includes(worker.state)) && !worker.pause_requested) {
      throw new Error("已有回補執行或排隊中；請先按「安全暫停」，等待工作停止後再試。");
    }
    // A single backend orchestration owns the OS lease across every group
    // and the first fetch. Frontend checks alone cannot prevent CLI races.
    const data = await jsonRequest("/api/v1/backfill/regrab-all", { method: "POST" });
    statusMessage(data.message || "完整重抓已受理；全部準備成功後自動開始，請查看目前回補進度。");
    if (data.control) renderWorker(data.control);
  } catch (error) {
    statusMessage(`完整重抓未確認啟動：${error.message} 請查看目前回補進度；勿重複送出。`, true);
  } finally {
    actionBusy = false;
    updateControls();
    void loadWorker();
    void loadStatus({ quiet: true, force: true });
  }
}

$("reload").addEventListener("click", () => { void loadWorker(); void loadStatus({ force: true }); });
$("markets").addEventListener("change", () => loadStatus());
$("intervals").addEventListener("change", () => loadStatus());
$("refresh").addEventListener("click", () => runAction("/api/v1/backfill/refresh-stale", "確定要檢查並背景更新所選市場與週期嗎？執行前必須已連線群益。"));
$("prepare-all").addEventListener("click", prepareAllBackfill);
$("retry").addEventListener("click", () => runAction("/api/v1/backfill/retry-failed", "只會重新排入目前仍失敗的最新工作。確定繼續嗎？"));
$("pause").addEventListener("click", () => runAction("/api/v1/backfill/pause"));
$("continue").addEventListener("click", () => runAction("/api/v1/backfill/resume"));
$("day-gaps-load").addEventListener("click", () => loadDayGaps(0));
$("day-gaps-prev").addEventListener("click", () => loadDayGaps(dayGapsMarkets === $("markets").value ? Math.max(0, dayGapsOffset-200) : 0));
$("day-gaps-next").addEventListener("click", () => loadDayGaps(dayGapsMarkets === $("markets").value ? dayGapsOffset+200 : 0));
loadWorker();
loadStatus();
window.setInterval(loadWorker, 5000);
// Background snapshot refreshes use this same cadence, without extra polling.
window.setInterval(() => loadStatus({ quiet: true }), 15000);
