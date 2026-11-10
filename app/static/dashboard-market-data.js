(() => {
  "use strict";
  const byId = id => document.getElementById(id);
  const form = byId("market-data-form");
  const codeInput = byId("market-data-code");
  const marketInput = byId("market-data-market");
  const periodInput = byId("market-data-period");
  const scopeInput = byId("market-data-scope");
  const scopeLabel = byId("market-data-scope-label");
  const status = byId("market-data-status");
  const result = byId("market-data-result");
  const views = [...byId("market-data-views").querySelectorAll("[data-view]")];
  const stockControls = byId("market-data-stock-controls");
  const marketControls = byId("market-data-market-controls");
  const stockPattern = /^(?:[0-9]{4,6}|[0-9]{5}[A-Z])$/;
  let view = "stock";
  const datasets = {stock: "institutional_stock", market: "index"};
  let controller;
  let selectedStock = "";

  const fieldLabels = {
    foreign_net_shares_excluding_foreign_dealers: "外資及陸資買賣超（不含外資自營商）",
    investment_trust_net_shares: "投信買賣超",
    dealer_net_shares: "自營商買賣超",
    financing_previous_balance: "融資前日餘額",
    financing_balance: "融資今日餘額",
    short_previous_balance: "融券前日餘額",
    short_balance: "融券今日餘額",
    revenue: "當月營收",
    previous_month_revenue: "上月營收",
    previous_year_same_month_revenue: "去年同月營收",
    mom_percent: "月增率",
    yoy_percent: "年增率",
    year_to_date_revenue: "本年累計營收",
    previous_year_to_date_revenue: "去年同期累計營收",
    year_to_date_yoy_percent: "累計年增率",
    open: "開盤指數",
    high: "最高指數",
    low: "最低指數",
    volume: "市場成交股數",
    turnover: "市場成交金額",
    trades: "市場成交筆數",
  };
  const units = {shares: "股", lots: "張", TWD: "新台幣元", TWD_thousand: "新台幣仟元", "TWD/share": "元／股", percent: "%"};
  const markets = {TWSE: "上市", TPEx: "上櫃"};
  const scopes = {stocks: "股票", securities: "有價證券", published: "證交所統計範圍"};

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function formatAmount(value) {
    if (value == null) return "—";
    const parts = /^(-?)(\d+)(\.\d+)?$/.exec(String(value));
    return parts ? parts[1] + parts[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (parts[3] || "") : String(value);
  }

  function makeTable(headers, caption) {
    const wrap = element("div", null, "market-data-table-wrap");
    const table = element("table", null, "market-data-table");
    table.append(element("caption", caption));
    const head = table.createTHead().insertRow();
    headers.forEach(([name, className]) => {
      const th = element("th", name, className);
      th.scope = "col";
      head.append(th);
    });
    wrap.append(table);
    result.append(wrap);
    return table.createTBody();
  }

  function render(payload) {
    result.replaceChildren();
    const rows = payload.data;
    const market = markets[payload.market];
    if (!rows.length) {
      status.textContent = "目前沒有符合條件的本機報表。";
      return;
    }
    if (payload.dataset === "index") {
      const row = rows[0];
      const {close, change, ...details} = row.values;
      const movement = Number(change);
      status.textContent = `${payload.name} · ${payload.period} 收盤`;
      const summary = element("div", null, "market-data-index");
      summary.append(element("strong", `${formatAmount(close)} 點`));
      if (change != null) {
        summary.append(element("span", `漲跌 ${movement > 0 ? "+" : ""}${formatAmount(change)} 點`,
          movement > 0 ? "positive" : movement < 0 ? "negative" : "neutral"));
      }
      result.append(summary);
      renderFields(details, row.unit, payload.symbol ? "指數日 K" : "盤後資料");
      result.append(element("p", `來源：${payload.source}${payload.symbol ? `（${payload.symbol}）` : ""}${payload.stale ? " · 暫時無法更新，顯示上次取得的資料" : ""}`, "market-data-meta"));
      return;
    }
    const marketView = payload.dataset === "institutional_market";
    const title = marketView ? scopes[payload.scope] : `${rows[0].stock_name}（${payload.stock_code}）`;
    status.textContent = [title, market, payload.period].filter(Boolean).join(" · ");

    if (marketView) {
      const body = makeTable([
        ["法人分類"], ["買進（元）", "market-data-number"], ["賣出（元）", "market-data-number"],
        ["官方淨額（元）", "market-data-number"], ["差額旗標"],
      ], "金額為新台幣元");
      for (const row of rows) {
        const tr = body.insertRow();
        tr.append(element("th", row.institution_type));
        tr.firstChild.scope = "row";
        for (const key of ["buy_amount", "sell_amount", "net_amount"]) {
          tr.append(element("td", formatAmount(row.values[key]), "market-data-number"));
        }
        const flag = element("td");
        if (row.net_difference_flag) {
          const value = formatAmount(row.net_difference_twd);
          const label = element("span", `${value.startsWith("-") ? "" : "+"}${value} 元`, "market-data-flag");
          label.title = "差額為買進減賣出再減官方淨額";
          flag.append(label);
        } else {
          flag.textContent = "—";
        }
        tr.append(flag);
      }
    } else {
      for (const row of rows) {
        const basis = row.period_type === "year_to_date" ? "年初至本季末累計"
          : row.period_type === "point_in_time" ? "期末餘額"
          : payload.dataset === "revenue" && row.scope != null ? (row.scope === "1" ? "外國公司" : "國內公司")
          : "報表數值";
        renderFields(row.values, row.unit, market ? basis : `${markets[row.market]} · ${basis}`);
      }
    }
    const sources = new Set(rows.map(row => row.source.url.includes("mops") ? "公開資訊觀測站"
      : row.market === "TWSE" ? "臺灣證券交易所" : "證券櫃檯買賣中心"));
    const coverage = rows.every(row => row.is_complete) ? "覆蓋已核對" : "覆蓋未驗證";
    result.append(element("p", `來源：${[...sources].join("、")} · ${coverage}`, "market-data-meta"));
  }

  function renderFields(values, fieldUnits, caption) {
    const body = makeTable([["欄位"], ["數值", "market-data-number"], ["單位"]], caption);
    for (const [key, value] of Object.entries(values)) {
      const tr = body.insertRow();
      const label = element("th", fieldLabels[key] || key);
      label.scope = "row";
      const unit = fieldUnits[key];
      tr.append(label, element("td", formatAmount(value), "market-data-number"),
        element("td", units[unit] || unit || "—", "market-data-unit"));
    }
  }

  function updatePeriods(periods, selected) {
    periodInput.replaceChildren();
    for (const period of periods) periodInput.add(new Option(period, period));
    if (!periods.length) periodInput.add(new Option("—", ""));
    periodInput.value = selected || "";
    periodInput.disabled = !periods.length;
  }

  async function load(period = "") {
    controller?.abort();
    controller = undefined;
    const dataset = datasets[view];
    const market = view === "market";
    const code = market ? "" : codeInput.value.trim().toUpperCase();
    if (!market) {
      codeInput.value = code;
      if (selectedStock !== code) period = "";
      selectedStock = code;
    }
    result.replaceChildren();
    result.setAttribute("aria-busy", "false");
    updatePeriods([], null);
    status.classList.remove("error");
    if (!market && !stockPattern.test(code)) {
      status.textContent = "請輸入有效的台股代號。";
      return;
    }
    const active = new AbortController();
    controller = active;
    const params = new URLSearchParams({dataset});
    if (dataset === "institutional_market") params.set("scope", scopeInput.value);
    if (period) params.set("period", period);
    scopeLabel.hidden = dataset !== "institutional_market" || marketInput.value !== "TPEx";
    const path = market ? `market-summary/${marketInput.value}` : `market-data/${encodeURIComponent(code)}`;
    status.textContent = "載入資料…";
    result.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(`/api/local/dashboard/${path}?${params}`, {signal: active.signal});
      const payload = await response.json();
      if (controller !== active) return;
      if (!response.ok) {
        throw new Error(response.status === 404 ? "請載入新版 API 後再重新整理頁面。"
          : typeof payload.detail === "string" ? payload.detail : "暫時無法讀取報表。");
      }
      scopeLabel.hidden = dataset !== "institutional_market" || payload.market !== "TPEx";
      updatePeriods(payload.periods, payload.period);
      render(payload);
    } catch (error) {
      if (controller !== active || error.name === "AbortError") return;
      status.textContent = error.message || "暫時無法讀取報表。";
      status.classList.add("error");
    } finally {
      if (controller === active) result.setAttribute("aria-busy", "false");
    }
  }

  form.addEventListener("submit", event => {
    event.preventDefault();
    load();
  });
  marketInput.addEventListener("change", () => load());
  periodInput.addEventListener("change", () => load(periodInput.value));
  scopeInput.addEventListener("change", () => load());
  for (const [name, controls] of [["stock", stockControls], ["market", marketControls]]) {
    const buttons = [...controls.querySelectorAll("[data-dataset]")];
    buttons.forEach(button => button.addEventListener("click", () => {
      datasets[name] = button.dataset.dataset;
      buttons.forEach(item => item.setAttribute("aria-pressed", String(item === button)));
      load();
    }));
  }
  for (const button of views) {
    button.addEventListener("click", () => {
      view = button.dataset.view;
      stockControls.hidden = view !== "stock";
      marketControls.hidden = view !== "market";
      views.forEach(item => item.setAttribute("aria-pressed", String(item === button)));
      load();
    });
  }
  byId("analyze").addEventListener("click", () => {
    const code = byId("symbol").value.trim().toUpperCase();
    if (stockPattern.test(code) && code !== codeInput.value) {
      codeInput.value = code;
      if (view === "stock") load();
    }
  });
  load();
})();
