// Headless behavior checks of the real page script; no network/browser/DLL.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

async function main() {
  const elements = new Map();
  function element(id = "") {
    return { id, value: ["markets", "intervals"].includes(id) ? "all" : id === "target-bars" ? "1200" : "1",
      textContent: "", disabled: false, hidden: false, children: [], handlers: {},
      classList: { toggle() {} }, append(...children) { this.children.push(...children); },
      appendChild(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
      get childElementCount() { return this.children.length; },
      addEventListener(name, handler) { this.handlers[name] = handler; } };
  }
  const calls = [];
  let resolveStatus;
  const timers = new Map();
  const intervalTimers = new Map();
  let nextTimer = 0;
  let controlFixture = { control_version: 7, state: "paused", worker_active: false, pause_requested: true };
  let prepareCount = 0;
  let failPrepareAt = 0;
  let holdFirstPrepare = false;
  let releasePrepare;
  const response = (data) => ({ ok: true, status: 200, json: async () => data });
  const context = vm.createContext({
    document: { getElementById(id) { if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id); }, createElement: () => element() },
    URLSearchParams, AbortController, Date, performance,
    window: { confirm: () => true, setInterval(fn, ms) { intervalTimers.set(ms, fn); return ms; },
      setTimeout(fn, ms) { timers.set(++nextTimer, { fn, ms }); return nextTimer; },
      clearTimeout(id) { timers.delete(id); } },
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url.startsWith("/api/v1/backfill/status")) return new Promise((resolve) => { resolveStatus = resolve; });
      if (url.startsWith("/api/v1/backfill/resume")) return response({ message: "已排入續跑", control: { state: "queued" } });
      if (url === "/api/v1/backfill/regrab-all") {
        prepareCount += 1;
        if (prepareCount === failPrepareAt) throw new Error("模擬準備失敗");
        const result = response({ message: "完整重抓已受理；準備成功後直接開始", control: {state:"preparing",preparation_pending:true,preparation_done:0,preparation_total:35} });
        if (holdFirstPrepare && prepareCount === 1) return new Promise((resolve) => { releasePrepare = () => resolve(result); });
        return result;
      }
      return response(controlFixture);
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../app/static/data-status.js"), "utf8"), context);
  await new Promise(setImmediate);
  assert.equal(elements.get("reload").disabled, true);
  assert.equal(elements.get("continue").disabled, false, "慢查詢不能鎖住繼續按鈕");
  assert.equal(elements.get("pause").disabled, false);
  await vm.runInContext('runAction("/api/v1/backfill/resume")', context);
  assert.equal(elements.get("operation-status").textContent, "已排入續跑");
  assert.equal(elements.get("continue").disabled, false, "POST 完成後不能等待 SQL 才解除按鈕");
  assert.equal(calls.filter((call) => call.url.startsWith("/api/v1/backfill/status")).length, 1);
  resolveStatus(response({ items: [], generated_at: "2026-10-15T00:00:00Z", schedule_logs: [],
    database_query_seconds: 0.2, database_cache_hit: true, database_cache_age_seconds: 1,
    database_refreshing: false, schedule: { state: "disabled", enabled: false, intervals: [] } }));
  await new Promise(setImmediate);
  await new Promise(setImmediate);
  assert.equal(elements.get("operation-status").textContent, "已排入續跑", "狀態刷新不能蓋掉操作結果");
  assert.match(elements.get("query-status").textContent, /快照已就緒/);
  assert.equal(elements.get("reload").disabled, false);
  assert.equal(calls.filter((call) => call.options.method === "POST").length, 1);
  assert.deepEqual([...intervalTimers.keys()].sort((a, b) => a - b), [5000, 15000],
    "保留回補控制每 5 秒、資料表格每 15 秒更新");
  for (const cacheHit of [false, true]) {
    const statusCalls = calls.filter((call) => call.url.startsWith("/api/v1/backfill/status")).length;
    const refresh = intervalTimers.get(15000)();
    resolveStatus(response({ items: [], generated_at: "2026-10-20T00:00:00Z", schedule_logs: [],
      database_query_seconds: cacheHit ? 9.359 : 0, database_cache_hit: cacheHit,
      database_cache_age_seconds: cacheHit ? 20 : 0, database_refreshing: true,
      schedule: { state: "disabled", enabled: false, intervals: [] } }));
    await refresh;
    assert.equal(calls.filter((call) => call.url.startsWith("/api/v1/backfill/status")).length, statusCalls + 1);
    assert.match(elements.get("query-status").textContent, /資料庫快照在背景更新中/);
    assert.equal(timers.size, 0, "首次與已有快照的背景更新都不可排入額外輪詢；要求逾時計時器仍須清除");
    assert.equal(elements.get("pause").disabled, false);
    assert.equal(elements.get("continue").disabled, false);
  }
  const workerCalls = calls.filter((call) => call.url === "/api/v1/backfill/control").length;
  const statusCalls = calls.filter((call) => call.url.startsWith("/api/v1/backfill/status")).length;
  await intervalTimers.get(5000)();
  assert.equal(calls.filter((call) => call.url === "/api/v1/backfill/control").length, workerCalls + 1);
  assert.equal(calls.filter((call) => call.url.startsWith("/api/v1/backfill/status")).length, statusCalls,
    "控制狀態輪詢不能額外刷新資料表格");
  assert.equal(vm.runInContext("formatFetchDuration(47.126)",context),"47.13 秒／檔");
  assert.equal(vm.runInContext("formatFetchDuration(0)",context),"0.00 秒／檔");
  assert.equal(vm.runInContext("formatFetchDuration(null)",context),"—");
  assert.equal(vm.runInContext("formatFetchDuration(undefined)",context),"—");
  context.exclusionWorker = {state:"running",current_symbol:"TSLQ",current_interval:"week",
    history_exclusions:[{symbol:"TSLQ",interval:"week",start_date:"2017-12-04",end_date:"2017-12-10"}]};
  vm.runInContext("renderWorker(exclusionWorker)",context);
  assert.match(elements.get("worker-detail").textContent,/已明確排除 TSLQ\/week 2017-12-04～2017-12-10/);
  assert.match(elements.get("worker-detail").textContent,/不寫入、不計入根數/);
  vm.runInContext('renderWorker({state:"running",current_symbol:"2330",history_exclusions:[]})',context);
  assert.doesNotMatch(elements.get("worker-detail").textContent,/已明確排除/);
  vm.runInContext('renderWorker({state:"waiting_backfill",day_first:true,priority_phase:"day",day_pending:3})',context);
  assert.match(elements.get("worker-detail").textContent,/日 K 正常抓取優先，待補齊留到所有週期正常抓取之後/);
  vm.runInContext('renderWorker({state:"running",day_first:true,priority_phase:"other"})',context);
  assert.match(elements.get("worker-detail").textContent,/其他週期正常抓取中，日 K 待補齊不阻擋/);
  vm.runInContext('renderWorker({state:"waiting_backfill",day_first:true,priority_phase:"recovery",day_pending:3})',context);
  assert.match(elements.get("worker-detail").textContent,/所選週期正常抓取輪已結束.*日 K 3 筆工作/);
  vm.runInContext('renderWorker({state:"waiting_review",pending_review:10,pending_review_symbols:1,pending_review_dates:10,pending_review_periods:2})',context);
  assert.match(elements.get("worker-detail").textContent,/待查 1 檔／10 個日期；另 2 個週月期間/);
  assert.doesNotMatch(elements.get("worker-detail").textContent,/10 檔/);
  vm.runInContext('renderWorker({day_integrity:{checked_symbols:1,complete_symbols:0,available_history_symbols:0,blocked_symbols:0,unresolved_symbols:1,unresolved_dates:10}})',context);
  assert.match(elements.get("worker-detail").textContent,/待查 1 檔／10 個日期/);
  assert.equal(vm.runInContext('formatDayGapCounts({blocked_symbols:0,unresolved_dates:10})',context),
    "待查商品數尚未統計／10 個日期","舊快照的排程 blocked 數不能冒充待查商品數");
  vm.runInContext('renderWorker({state:"running",day_integrity_scheduled:false,day_integrity_schedule_reason:"交易日，不執行完整驗收"})',context);
  assert.match(elements.get("day-maintenance-detail").textContent,/交易日，不執行完整驗收/);
  vm.runInContext('renderWorker({state:"waiting_backfill",worker_active:true,day_integrity_maintenance:{active:true,state:"running",as_of_date:"2026-10-23",day_integrity_checked:20,current_symbol:"2330",current_market:0}})',context);
  assert.match(elements.get("day-maintenance-detail").textContent,/休市日日 K 驗收：執行中/);
  assert.match(elements.get("day-maintenance-detail").textContent,/截止 2026-10-23；已檢查 20 檔/);
  assert.match(elements.get("day-maintenance-detail").textContent,/一般抓取計畫與斷點保留/);
  vm.runInContext('renderWorker({state:"completed",worker_active:false,day_integrity_maintenance:{active:true,state:"running"}})',context);
  assert.doesNotMatch(elements.get("day-maintenance-detail").textContent,/驗收：執行中/,
    "重開後的舊 active 旗標不能冒充正在執行");
  assert.deepEqual([...intervalTimers.keys()].sort((a,b)=>a-b),[5000,15000],"休市日驗收顯示不新增輪詢");
  const item={market_no:0,market_name:"上市",interval:"60m",tracked_symbols:1,active_instruments:1,
    storage_ready:true,coverage_percent:100,job_counts:{pending:0,running:0,completed:1,failed:0,insufficient_history:0},
    average_fetch_seconds:47.126,average_fetch_sample_count:1};
  context.durationData={items:[item],generated_at:"2026-10-15T00:00:00Z",schedule_logs:[],schedule:{state:"disabled",enabled:false,intervals:[]}};
  vm.runInContext("render(durationData)",context);
  const row=elements.get("status-rows").children[0];
  assert.equal(row.children.length,17);
  assert.equal(row.children[15].textContent,"47.13 秒／檔（樣本 1 檔）");
  assert.match(row.children[15].title,/樣本 1 檔/);
  delete item.average_fetch_seconds; delete item.average_fetch_sample_count;
  vm.runInContext("render(durationData)",context);
  assert.equal(elements.get("status-rows").children[0].children[15].textContent,"—");
  item.job_counts.pending=4;
  item.pending_phases={latest:1,catchup:1,history:2,unknown:0};
  item.checked_through="2026-10-15";item.checked_symbols=1;
  item.data_missing_symbols=1;item.unfinished_symbols=1;
  vm.runInContext("render(durationData)",context);
  const classified=elements.get("status-rows").children[0];
  assert.deepEqual(classified.children.slice(6,10).map(cell=>cell.textContent),["1","1","2","0"]);
  assert.match(classified.children[2].textContent,/無 K 線 1 檔/);
  assert.match(classified.children[12].textContent,/未結束期間 K/);
  assert.match(classified.children[13].textContent,/2026-10-15/);
  elements.get("target-bars").value="";
  assert.equal(vm.runInContext('params(true).has("target_bars")',context),false,"留空不可偷送 1200 蓋過週月目標");
  elements.get("target-bars").value="700";
  assert.equal(vm.runInContext('params(true).get("target_bars")',context),"700");
  item.interval="week";item.target_bars=560;
  vm.runInContext("render(durationData)",context);
  assert.match(elements.get("status-rows").children[0].children[1].textContent,/week.*560/);
  item.interval="month";item.target_bars=100;
  vm.runInContext("render(durationData)",context);
  assert.match(elements.get("status-rows").children[0].children[1].textContent,/month.*100/);
  context.workerFixture={state:"waiting_backfill",deferred:2,current_symbol:"2330",current_market:0,
    current_interval:"day",queue_phase:"history",checkpoint:"2025-01-01",next_retry_at:"2026-10-15T15:00:00Z",
    group_plan:{oldest_checked_through:"2026-10-14",suspended_symbols:1,unknown_lagging_symbols:2},
    suspensions:{state:"partial"}};
  vm.runInContext("renderWorker(workerFixture)",context);
  assert.match(elements.get("worker-state").textContent,/慢速補齊等待中/);
  assert.match(elements.get("worker-detail").textContent,/較舊歷史補齊/);
  assert.match(elements.get("worker-detail").textContent,/不強迫全族群/);
  assert.match(elements.get("worker-detail").textContent,/不依過期名單跳過/);
  context.workerFixture.memory_samples=[{stage:"after_write_release",private_mb:2500,working_set_mb:2400,delta_mb:10,gc:false}];
  context.workerFixture.memory_resume_mb=3072;
  vm.runInContext("renderWorker(workerFixture)",context);
  assert.equal(elements.get("memory-rows").children.length,1);
  assert.match(elements.get("worker-memory").textContent,/3072/);

  context.workerFixture.pause_kind="memory";
  context.workerFixture.memory_resume_ready=false;
  vm.runInContext("renderWorker(workerFixture)",context);
  assert.match(elements.get("worker-memory").textContent,/尚未取得恢復觀察結果/);
  context.workerFixture.memory_observed_at="2026-10-18T00:00:00Z";
  vm.runInContext("renderWorker(workerFixture)",context);
  assert.match(elements.get("worker-memory").textContent,/記憶體恢復條件：尚未符合/);
  context.workerFixture.memory_resume_ready=true;
  context.workerFixture.pause_requested=true;
  const postsBeforeMemoryRender=calls.filter(call=>call.options.method==="POST").length;
  vm.runInContext("renderWorker(workerFixture)",context);
  assert.match(elements.get("worker-memory").textContent,/記憶體恢復條件：已符合/);
  assert.match(elements.get("worker-memory").textContent,/仍須符合交易日、群益連線及未手動暫停/);
  assert.match(elements.get("worker-state").textContent,/已請求安全暫停/);
  assert.equal(calls.filter(call=>call.options.method==="POST").length,postsBeforeMemoryRender,"記憶體顯示就緒不可自動 POST 續跑");

  const preparePosts = () => calls.filter(call => call.url === "/api/v1/backfill/regrab-all");
  const postCount = () => calls.filter(call => call.options.method === "POST").length;
  let before = calls.length;
  context.window.confirm = () => false;
  await vm.runInContext("prepareAllBackfill()",context);
  assert.equal(calls.length,before,"取消確認不能送出任何要求");
  context.window.confirm = () => true;
  controlFixture = {control_version:5,state:"paused"};
  before=postCount();
  await vm.runInContext("prepareAllBackfill()",context);
  assert.equal(postCount(),before,"舊API不可把忽略 force 的增量準備當成完整重抓");
  assert.match(elements.get("operation-status").textContent,/尚未載入/);
  controlFixture = {control_version:7,state:"running",worker_active:true};
  await vm.runInContext("prepareAllBackfill()",context);
  assert.equal(postCount(),before,"執行中的工作不得被重抓準備重設");
  assert.match(elements.get("operation-status").textContent,/安全暫停/);

  controlFixture={control_version:7,state:"paused",worker_active:false,pause_requested:true};
  elements.get("markets").value="0";elements.get("intervals").value="day";elements.get("target-bars").value="777";
  prepareCount=0;holdFirstPrepare=true;
  const resumeCalls=calls.filter(call=>call.url.startsWith("/api/v1/backfill/resume")).length;
  const preparing=vm.runInContext("prepareAllBackfill()",context);
  await new Promise(setImmediate);
  assert.equal(elements.get("prepare-all").disabled,true);
  await vm.runInContext("prepareAllBackfill()",context);
  assert.equal(prepareCount,1,"重複點擊不得再準備另一批");
  releasePrepare(); await preparing;
  assert.equal(preparePosts().length,1);
  for (const call of preparePosts()) {
    assert.equal(call.url.includes("?"),false,"完整重抓依系統週期設定，不偷用篩選欄位");
    assert.equal(call.options.method,"POST");
  }
  assert.equal(calls.filter(call=>call.url.startsWith("/api/v1/backfill/resume")).length,resumeCalls,"由後端鎖內接續，前端不可額外送 resume");
  assert.match(elements.get("operation-status").textContent,/準備成功後直接開始/);
  assert.match(elements.get("worker-detail").textContent,/獨占工作鎖.*0\/35/);
  assert.equal(elements.get("prepare-all").disabled,false);
  holdFirstPrepare=false;prepareCount=0;failPrepareAt=1;
  const beforeFailure=preparePosts().length;
  await vm.runInContext("prepareAllBackfill()",context);
  assert.equal(preparePosts().length-beforeFailure,1,"失敗後不可自動重送");
  assert.match(elements.get("operation-status").textContent,/未確認啟動.*模擬準備失敗/);
  assert.equal(calls.filter(call=>call.url.startsWith("/api/v1/backfill/resume")).length,resumeCalls);
  context.workerFixture.current_requery=true;
  vm.runInContext("renderWorker(workerFixture)",context);
  assert.match(elements.get("worker-detail").textContent,/完整範圍重抓/);
  context.workerFixture.current_requery=false;
  context.workerFixture.current_refresh=true;
  context.workerFixture.current_target_bars=10;
  vm.runInContext("renderWorker(workerFixture)",context);
  assert.match(elements.get("worker-detail").textContent,/日常最新根數回抓/);
  assert.match(elements.get("worker-detail").textContent,/本輪回抓目標 10/);
  context.scheduleFixture={enabled:true,state:"waiting",timezone:"Asia/Taipei",local_time:"18:30",
    markets:[0],intervals:["1m","5m","15m","60m","day","week","month"],target_bars:1200,
    target_bars_by_interval:{"1m":1200,"5m":1200,"15m":1200,"60m":1200,day:1200,week:560,month:100},
    refresh_bars_by_interval:{"1m":1200,"5m":240,"15m":60,"60m":20,day:10,week:5,month:2}};
  vm.runInContext("renderSchedule(scheduleFixture)",context);
  assert.match(elements.get("schedule-detail").textContent,/日常回抓目標/);
  assert.match(elements.get("schedule-detail").textContent,/1m:1200.*5m:240.*15m:60.*60m:20.*day:10.*week:5.*month:2/);
  assert.match(elements.get("schedule-detail").textContent,/初始化／重抓目標另計/);
  context.scheduleFixture.aggregate_week_month_from_day=true;
  vm.runInContext("renderSchedule(scheduleFixture)",context);
  assert.match(elements.get("schedule-detail").textContent,/每日由日K重算（含本週暫定）.*week:5/);
  assert.match(elements.get("schedule-detail").textContent,/每日由日K重算（含本月暫定）.*month:2/);
  assert.doesNotMatch(elements.get("schedule-detail").textContent,/每週最後交易日|每月最後交易日/);
  context.workerFixture.current_aggregate_from_day=true;
  context.workerFixture.source_window_start="2026-09-01";
  context.workerFixture.source_window_end="2026-10-20";
  vm.runInContext("renderWorker(workerFixture)",context);
  assert.match(elements.get("worker-detail").textContent,/完整日K範圍重算.*同期資料覆寫.*2026-09-01～2026-10-20/);
  const callsBeforeGapRender=calls.length;
  context.gapFixture={storage_ready:true,total:1,offset:0,summary:[{market_no:0,status:"unresolved",symbols:1,as_of_date:"2026-10-20"}],
    items:[{market_no:0,symbol:"2330",candle_date:"2026-10-19",status:"unresolved",reason:"missing_bar",attempt_count:2,next_retry_at:null}]};
  vm.runInContext('renderDayGaps(gapFixture)',context);
  assert.match(elements.get("day-gaps-status").textContent,/待核日期共 1 筆/);
  assert.equal(elements.get("day-gap-rows").children[0].children[2].textContent,"2026-10-19");
  assert.equal(elements.get("day-gap-rows").children[0].children[3].textContent,"待查證／補抓");
  context.gapFixture={storage_ready:false,items:[],total:0,offset:0};
  vm.runInContext('renderDayGaps(gapFixture)',context);
  assert.match(elements.get("day-gaps-status").textContent,/未檢查當成完整/);
  context.gapFixture={storage_ready:true,assessment_total:0,summary:[],items:[],assessments:[],total:0,offset:0};
  vm.runInContext('renderDayGaps(gapFixture)',context);
  assert.equal(elements.get("day-gap-rows").children.length,0);
  assert.match(elements.get("day-gaps-status").textContent,/尚未重新驗收，不代表資料完整/);
  assert.equal(calls.length,callsBeforeGapRender,"缺口顯示不可自動抓取、登入或排入新的輪詢");
  assert.deepEqual([...intervalTimers.keys()].sort((a,b)=>a-b),[5000,15000]);
  console.log("Frontend checks passed: slow read does not block controls, single in-flight read, resume feedback persists, no duplicate POST.");
  console.log("Polling checks passed: 5-second control and 15-second status updates remain; no extra polling during initial or cached snapshot refresh.");
  console.log("Status checks passed: 17 columns, recovery-last phases, distinct product/date counts, sample count, checked dates, memory telemetry and old API fallback.");
  console.log("Regrab-all checks passed: confirmation, version/active-worker guards, one backend orchestration, no duplicate clicks/retries, no frontend resume race.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
