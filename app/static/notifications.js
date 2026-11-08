(() => {
  "use strict";
  const byId = id => document.getElementById(id);
  const list = byId("notification-list");
  const preferenceKey = "capital-desktop-notifications";
  let nextBefore = null, newestSequence = 0, loading = false, desktopBusy = false, desktopFailure = "";
  let oldestSequence = null, filterVersion = 0;
  const deliveryLabels = {pending:"等待系統通知", claimed:"已領取派送；尚未確認提交", submitted:"已提交瀏覽器（不代表已顯示）", failed:"系統通知提交失敗", expired:"已超過系統通知時限", skipped_history:"歷史紀錄，不補發系統通知"};
  const enabled = () => { try { return localStorage.getItem(preferenceKey) === "true"; } catch { return false; } };
  const supported = () => "Notification" in window && window.isSecureContext;
  async function api(path, method = "GET", body) {
    const response = await fetch("/api/v1/notifications" + path, {method, headers:{"Content-Type":"application/json"}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `請求失敗 (${response.status})`);
    return data;
  }
  function status(message) { if(byId("notification-status")) byId("notification-status").textContent = message; }
  function desktopStatus() {
    const node = byId("desktop-status"); if (!node) return;
    node.textContent = desktopFailure || (!supported() ? "此瀏覽器不支援系統通知；可使用 Chrome／Edge 開啟本頁。" : Notification.permission === "denied" ? "瀏覽器已封鎖通知，請在網站權限設定調整。" : enabled() && Notification.permission === "granted" ? "已啟用：此瀏覽器將接收新的未讀提醒。" : "尚未啟用；網頁內通知不受影響。");
    byId("desktop-enable").disabled = !supported();
  }
  function item(node) {
    const article = document.createElement("article"); article.className = "panel notification-item" + (node.read ? "" : " unread");
    article.dataset.notificationId = node.id;
    article.dataset.snapshot = JSON.stringify(node);
    const header = document.createElement("header"), title = document.createElement("h2"), button = document.createElement("button");
    title.textContent = node.title; button.className = "secondary"; button.textContent = node.read ? "標為未讀" : "標為已讀";
    button.onclick = async () => { button.disabled = true; try { await api(`/${node.id}/read`, "PATCH", {read:!node.read}); await load(); } catch(error) {status(error.message);} finally{button.disabled=false;} };
    header.append(title, button);
    const date = document.createElement("time"); date.dateTime = node.created_at; date.textContent = new Date(node.created_at).toLocaleString("zh-TW");
    const message = document.createElement("p"); message.textContent = node.message;
    const delivery = document.createElement("div"); delivery.className = "notification-delivery"; delivery.textContent = (deliveryLabels[node.desktop_status] || node.desktop_status) + (node.desktop_error ? `：${node.desktop_error}` : "");
    article.append(header, date, message, delivery);
    const details = node.result?.details || [];
    if (details.length) { const disclosure = document.createElement("details"), summary = document.createElement("summary"); summary.textContent = "查看條件判斷"; disclosure.append(summary); details.forEach(detail => {const line=document.createElement("p");line.textContent=detail.reason;disclosure.append(line);});article.append(disclosure); }
    return article;
  }
  async function load(append = false, quiet = false) {
    if (loading) return; loading = true;
    const version = filterVersion;
    try {
      const params = new URLSearchParams({limit:list ? "30" : "1", unread_only:String(Boolean(byId("notification-unread")?.checked))});
      if (append && nextBefore) params.set("before", nextBefore);
      let data = await api("?" + params);
      const items = [...data.items];
      // Refresh through the oldest loaded row so new arrivals cannot displace history.
      while (list && !append && oldestSequence !== null && data.next_before && items.at(-1)?.sequence > oldestSequence) {
        params.set("before", data.next_before);
        data = await api("?" + params);
        items.push(...data.items);
      }
      if (version !== filterVersion) return;
      document.querySelectorAll("[data-notification-count]").forEach(node => {node.textContent = `${data.unread_count} 未讀`;});
      if (list) {
        const existing = new Map(Array.from(list.querySelectorAll("[data-notification-id]")).map(node => [node.dataset.notificationId, node]));
        const keep = new Set(items.map(node => node.id));
        list.querySelector(".notification-empty")?.remove();
        if (!append) {
          newestSequence = items[0]?.sequence || 0;
          existing.forEach((node, id) => { if (!keep.has(id)) node.remove(); });
        }
        let cursor = append ? null : list.firstElementChild;
        items.forEach(value => {
          let node = existing.get(value.id);
          if (!node || node.dataset.snapshot !== JSON.stringify(value)) {
            const replacement = item(value);
            if (node?.querySelector("details")?.open && replacement.querySelector("details")) replacement.querySelector("details").open = true;
            const focused = node?.contains(document.activeElement);
            if (node) { if (cursor === node) cursor = replacement; node.replaceWith(replacement); }
            node = replacement;
            if (focused) node.querySelector("button")?.focus({preventScroll:true});
          }
          if (node !== cursor) list.insertBefore(node, cursor);
          cursor = node.nextElementSibling;
        });
        oldestSequence = items.at(-1)?.sequence ?? (append ? oldestSequence : null);
        nextBefore = data.next_before; byId("notification-more").hidden = !nextBefore;
        byId("notification-read-all").disabled = !data.unread_count || !newestSequence;
        if (!list.childElementCount) {const empty=document.createElement("p");empty.className="panel notification-empty";empty.textContent="目前沒有通知。可建立測試通知，或等待提醒條件觸發。";list.append(empty);}
        status(`已自動更新 · ${data.unread_count} 則未讀`);
      }
    } catch(error) {status(`無法讀取通知：${error.message}`);} finally {loading=false;if(version !== filterVersion) load();}
  }
  async function dispatch() {
    if (desktopBusy || !enabled() || !supported() || Notification.permission !== "granted") return;
    desktopBusy = true;
    try {
      const claim = await api("/desktop/claim", "POST");
      if (!claim.notification) return;
      const n = claim.notification;
      let state = "submitted", errorText = null;
      try {
        const toast = new Notification(n.title, {body:n.message.slice(0, 400), tag:n.id});
        toast.onclick = () => { window.focus(); window.location.href = "/notifications"; toast.close(); };
      } catch(error) {state="failed";errorText=String(error.message).slice(0,500);desktopFailure=`系統通知失敗：${errorText}`;desktopStatus();}
      await api(`/${n.id}/desktop-ack`, "POST", {claim_token:claim.claim_token, status:state, error:errorText});
      await load(false, true);
    } catch(error) {desktopFailure=`系統通知連線失敗：${error.message}`;desktopStatus();} finally {desktopBusy=false;}
  }
  byId("desktop-enable")?.addEventListener("click", async () => {
    if(!supported()) return;
    try {const permission=await Notification.requestPermission();localStorage.setItem(preferenceKey,String(permission==="granted"));desktopFailure="";desktopStatus();await dispatch();} catch(error){desktopFailure=error.message;desktopStatus();}
  });
  byId("desktop-disable")?.addEventListener("click", () => {try{localStorage.setItem(preferenceKey,"false");desktopFailure="";}catch(error){desktopFailure=error.message;}desktopStatus();});
  byId("notification-test")?.addEventListener("click", async event => {event.target.disabled=true;try{await api("/test","POST");await load();await dispatch();}catch(error){status(error.message);}finally{event.target.disabled=false;}});
  byId("notification-read-all")?.addEventListener("click", async () => {try{await api("/read-all","POST",{through_sequence:newestSequence});await load();}catch(error){status(error.message);}});
  byId("notification-refresh")?.addEventListener("click", () => load());
  byId("notification-unread")?.addEventListener("change", () => {filterVersion++;oldestSequence=null;nextBefore=null;load();});
  byId("notification-more")?.addEventListener("click", () => load(true));
  window.addEventListener("storage", desktopStatus);
  desktopStatus(); load(); dispatch();
  setInterval(async () => {desktopStatus();await dispatch();await load(false,true);},5000);
  document.addEventListener("visibilitychange", () => {if(!document.hidden){desktopStatus();load(false,true);dispatch();}});
})();
