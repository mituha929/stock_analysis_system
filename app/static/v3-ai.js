"use strict";
(() => {
  const clone = value => JSON.parse(JSON.stringify(value));
  const base = "/api/v1/ai/rules";
  const actionNames={single_stock_entry:'進場',single_stock_exit:'出場',single_stock_add:'加碼',single_stock_reduce:'減碼'};
  const isStockIntent = p => Boolean(p&&Object.hasOwn(actionNames,p.intent));
  const stockAnalysis = p => p.stock_analysis||p.entry_analysis||p.exit_analysis||p.add_analysis||p.reduce_analysis;
  async function rpc(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), path === "/execute" ? 330000 : 90000);
    try {
      const response = await fetch(base + path, {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),signal:controller.signal,cache:"no-store"});
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = result.detail;
        const error = new Error(Array.isArray(detail) ? detail.map(x => x.msg).join("；") : detail?.message || String(detail || `HTTP ${response.status}`));
        error.code = detail?.code; throw error;
      }
      return result;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("等待逾時；不代表伺服器工作已取消，請勿重複送出。執行請求不會自動重試。");
      throw error;
    } finally { clearTimeout(timer); }
  }

  // Small state machine also tested without DOM or external services.
  class PreviewFlow {
    constructor(call,changed=()=>{}) { this.call=call;this.changed=changed;this.generation=0;this.preview=null;this.editBase=null;this.busy=false;this.running=false;this.revocation=Promise.resolve(); }
    dirty() {
      this.generation++;
      const old=this.preview || this.editBase;
      this.preview=null;this.editBase=null;
      if (old?.preview_id) this.call("/preview/invalidate",{preview_id:old.preview_id}).catch(()=>{});
      this.changed("dirty");
    }
    beginEdit() {
      this.generation++;
      if (this.preview?.preview_id) {
        this.editBase=this.preview;this.preview=null;
        this.revocation=this.call("/preview/invalidate",{preview_id:this.editBase.preview_id,editable:true});
        // Save/revise awaits this promise; do not ignore a failed revocation.
        this.revocation.catch(()=>{});
      }
      this.changed("editing");
    }
    async resolve(action) {
      if (this.busy || this.running) return null;
      const version=this.generation;
      this.busy=true;this.changed("busy");
      try {
        const value=await action();
        if (version!==this.generation) {
          if (value.preview_id) this.call("/preview/invalidate",{preview_id:value.preview_id}).catch(()=>{});
          return null;
        }
        this.preview=value;this.editBase=null;this.changed("preview");return value;
      } finally { this.busy=false;this.changed("idle"); }
    }
    parse(body) { return this.resolve(()=>this.call("/preview",body)); }
    revise(rule) {
      const old=this.editBase;
      if (!old) return Promise.reject(new Error("請先修改門檻，或重新解析文字。"));
      const copy=clone(rule);
      return this.resolve(async()=>{await this.revocation;return this.call("/preview/revise",{preview_id:old.preview_id,revision:old.revision,rule:copy});});
    }
    async execute(body) {
      if (this.running || this.busy || !this.preview?.preview_id) throw new Error("尚無可執行的確認版本。");
      const draft=this.preview, version=this.generation;
      this.running=true;this.changed("running");
      try {
        const result=await this.call("/execute",{...body,preview_id:draft.preview_id,revision:draft.revision});
        this.preview=null;
        return {result,outdated:version!==this.generation};
      } catch(error) {
        if (error.code!=="scan_busy") this.dirty();
        throw error;
      } finally { this.running=false;this.changed("idle"); }
    }
  }
  window.CapitalAIPreviewFlow=PreviewFlow;

  document.addEventListener("DOMContentLoaded",()=>{
    const $=id=>document.getElementById(id);
    if (!$('ai-form')) return;
    const el=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
    let editedRule=null,lastText="";
    const message=(text,error=false)=>{$('ai-status').textContent=text;$('ai-status').classList.toggle("error",error);};
    const checks=()=>{$('ai-confirm-scope').checked=false;$('ai-confirm-assumptions').checked=false;};
    const flow=new PreviewFlow(rpc,event=>{
      if (["dirty","editing","preview"].includes(event)) checks();
      if (["dirty","editing"].includes(event)) $('ai-result').classList.add('hidden');
      if (event==="dirty") {$('ai-preview').classList.add('hidden');message("輸入／設定已變更，原確認失效，請重新解析。");}
      if (event==="editing") {message("門檻已修改，原確認失效；請按『套用修改』重新預覽。");$('ai-revise').classList.remove('hidden');}
      if (event==="preview") renderPreview(flow.preview);
      updateButtons();
    });
    function updateButtons() {
      const p=flow.preview;
      const expired=!p?.expires_at || Date.parse(p.expires_at)<=Date.now();
      $('ai-parse').disabled=flow.busy||flow.running;
      $('ai-revise').disabled=flow.busy||flow.running||!flow.editBase;
      $('ai-execute').disabled=flow.busy||flow.running||!p?.preview_id||expired||!$('ai-confirm-scope').checked||!$('ai-confirm-assumptions').checked;
      $('ai-execute').textContent=isStockIntent(p)?`確認${actionNames[p.intent]}條件並分析 ${stockAnalysis(p).symbol}`:'確認條件並開始選股';
    }
    function settings() {
      const kind=$('ai-universe').value;
      const symbols=kind==='symbols'?[...new Set($('ai-symbols').value.split(/[,，\s]+/).filter(Boolean).map(s=>s.toUpperCase()))]:[];
      const markets=[...document.querySelectorAll('input[name="ai-market"]:checked')].filter(e=>!e.disabled).map(e=>Number(e.value));
      if(!markets.length)throw new Error("請至少選擇一個市場。");
      if(kind==='symbols'&&!symbols.length)throw new Error("請輸入指定股票代號。");
      const sort=$('ai-sort').value==='risk'?[{field:'risk_score',direction:'asc'}]:$('ai-sort').value==='symbol'?[{field:'symbol',direction:'asc'}]:[{field:'matched_count',direction:'desc'},{field:'direction_score',direction:'desc'},{field:'risk_score',direction:'asc'}];
      return {universe:{kind,markets,symbols,category_nos:[]},history_interval:$('ai-interval').value,candle_table:$('ai-source').value,history_bars:Number($('ai-bars').value),limit:Number($('ai-limit').value),timeout_seconds:Number($('ai-timeout').value),sort};
    }
    function tree(node,parent) {
      const box=el('div',undefined,node.kind==='group'?'ai-rule-group':'ai-rule-leaf');
      box.append(el(node.kind==='group'?'strong':'p',node.label));
      for(const child of node.children||[])tree(child,box);
      parent.append(box);
    }
    function editor(node,path=[]) {
      if(node.rules){node.rules.forEach((child,i)=>editor(child,[...path,'rules',i]));return;}
      const value=node.value;
      if(typeof value!=='number'&&!Array.isArray(value))return;
      const label=el('label',`${node.field} ${node.operator}（修改後須重新預覽）`);
      const values=Array.isArray(value)?value:[value];
      values.forEach((v,index)=>{
        const input=el('input');input.type='number';input.step='any';input.value=String(v);
        input.setAttribute('aria-label',`${node.field} 門檻${index+1}`);
        input.addEventListener('input',()=>{flow.beginEdit();let target=editedRule;for(const key of path)target=target[key];if(Array.isArray(target.value))target.value[index]=input.value===''?null:Number(input.value);else target.value=input.value===''?null:Number(input.value);});
        label.append(input);
      });
      $('ai-editor').append(label);
    }
    function renderPreview(p) {
      $('ai-preview').classList.remove('hidden');$('ai-original').textContent=`本次需求：${p.original_text}`;
      const names={0:'上市',1:'上櫃',4:'興櫃',5:'上市零股',6:'上櫃零股'};
      const s=p.settings;
      const sortNames={matched_count:'符合條件數',direction_score:'技術方向分數',risk_score:'風險分數',symbol:'代號'};
      $('ai-scope').textContent=`範圍：${s.universe.markets.map(m=>names[m]).join('、')}；${s.universe.kind==='symbols'?s.universe.symbols.join('、'):s.universe.kind==='watchlist'?`固定自選股：${(p.resolved_watchlist||[]).join('、')}`:'選定市場'}；${{day:'日',week:'週',month:'月'}[s.history_interval]} K；資料表 ${s.candle_table}；分析 ${s.history_bars} 根，最多顯示 ${s.limit} 檔；排序 ${s.sort.map(x=>`${sortNames[x.field]||x.field}${x.direction==='asc'?'由小到大':'由大到小'}`).join(' → ')}`;
      if(isStockIntent(p)){const a=stockAnalysis(p);$('ai-scope').textContent=`本次僅分析 ${a.symbol||'尚未確認的代號'} 的${actionNames[p.intent]}條件；在所選市場核對商品名稱；${{day:'日',week:'週',month:'月'}[s.history_interval]} K，使用最近 ${s.history_bars} 根完整期間。觀察策略：${a.strategy.name}。${a.strategy.logic_description||''}${a.strategy.note}`;}
      $('ai-policy').textContent=`預覽時最近已完成期間至 ${p.data_policy.expected_period_end}。執行會再次核對完整期間與資料日期；歷史連續性不保證。`;
      $('ai-unresolved').replaceChildren();
      for(const issue of p.unresolved_conditions||[])$('ai-unresolved').append(el('p',issue));
      if(p.status==='needs_clarification'&&!isStockIntent(p)) {
        const note=el('p','請改寫完整技術條件；例如下列方向僅供觀察，不保證獲利。');$('ai-unresolved').append(note);
        [['近期均線觀察','股價高於MA20，且RSI不超過70'],['回檔觀察','股價高於MA60，且股價低於MA5']].forEach(([name,text])=>{const b=el('button',`將「${name}」範例填入輸入框`,'secondary');b.type='button';b.addEventListener('click',()=>replaceText(text));$('ai-unresolved').append(b);});
      }
      $('ai-explanation').replaceChildren();if(p.explanation)tree(p.explanation,$('ai-explanation'));
      if(isStockIntent(p)){
        const meaning=stockAnalysis(p).strategy.user_explanation;
        if(meaning){$('ai-explanation').append(el('h4','這套策略如何解讀'));for(const key of ['purpose','when_met','when_not_met'])$('ai-explanation').append(el('p',meaning[key]));}
      }
      $('ai-assumptions').replaceChildren();
      for(const a of p.assumptions||[])$('ai-assumptions').append(el('li',`「${a.source_text}」→ ${a.interpreted_as}`));
      if(!p.assumptions?.length)$('ai-assumptions').append(el('li','沒有模型列出的假設；仍請核對實際語意。'));
      $('ai-warnings').replaceChildren();for(const w of p.warnings||[])$('ai-warnings').append(el('p',w,'warning'));
      editedRule=clone(p.rule);$('ai-editor').replaceChildren();$('ai-editor').className='ai-thresholds';if(p.rule&&!isStockIntent(p))editor(editedRule);
      $('ai-revise').classList.add('hidden');$('ai-rule-json').textContent=JSON.stringify(p.rule,null,2);
      $('ai-expiry').textContent=p.expires_at?`確認版本 ${p.revision.slice(0,12)}，至 ${new Date(p.expires_at).toLocaleString('zh-TW',{timeZone:'Asia/Taipei'})} 有效；單次使用，重啟服務後失效。`:'尚未取得可執行版本。';
      message(p.status==='draft'?'請核對白話條件與全部假設，再勾選確認。':'有未解決條件，目前不能開始選股。',p.status!=='draft');
    }
    function renderResult(result) {
      if(isStockIntent(result)){renderEntryResult(result);return;}
      $('ai-detail-heading').textContent='排除與資料狀態';
      $('ai-result').classList.remove('hidden');$('ai-result-title').textContent=result.complete?'掃描完成（不代表每檔資料可用）':'部分完成：不是全市場排名';
      $('ai-result-summary').replaceChildren();
      $('ai-result-summary').append(el('p',`已處理 ${result.processed_count}/${result.universe_count} 檔；可分析 ${result.analyzed_count} 檔；符合 ${result.matched_count} 檔；顯示 ${result.returned_count} 檔；尚未處理 ${(result.remaining_symbols||[]).length} 檔。`));
      if(!result.complete)$('ai-result-summary').append(el('p','掃描尚未完成；排名僅涵蓋已處理且資料可用的股票。','warning'));
      if(result.truncated)$('ai-result-summary').append(el('p','已達顯示筆數限制，其他符合股票未在表格列出；這與掃描未完成不同。'));
      if(!result.items.length)$('ai-result-summary').append(el('p','目前顯示範圍沒有符合股票；不會自動放寬條件。請同時查看未處理及資料排除狀態。'));
      for(const w of result.warnings||[])$('ai-result-summary').append(el('p',w,'warning'));
      const written=value=>value?new Date(/(?:Z|[+-]\d\d:\d\d)$/.test(value)?value:value+'Z').toLocaleString('zh-TW',{timeZone:'Asia/Taipei',hour12:false}):'未記錄';
      const table=el('table'),head=el('tr');['排名','股票','收盤價','方向／風險分數','條件','K 線日期／寫入（臺北）'].forEach(t=>head.append(el('th',t)));table.append(head);
      for(const item of result.items||[]){const tr=el('tr');[item.rank,`${item.symbol} ${item.name}`,item.price,`${item.direction_score??'不足'}／${item.risk_score??'不足'}`,`${item.matched_count}/${item.total_rules}`,`${String(item.latest_candle_at).slice(0,10)}\n${written(item.latest_written_at)}`].forEach(t=>tr.append(el('td',String(t))));table.append(tr);const detail=el('tr',undefined,'ai-result-detail'),reason=el('td');reason.colSpan=6;reason.append(el('strong','符合原因與警告'));for(const text of item.reasons)reason.append(el('p',text));for(const text of item.warnings||[])reason.append(el('p',text,'warning'));detail.append(reason);table.append(detail);}
      $('ai-result-items').replaceChildren(table);$('ai-result-excluded').replaceChildren();
      const labels={missing_data:'無資料',insufficient_data:'資料不足',stale_data:'資料過期／個股狀態待確認',unverified_period:'完整期間未確認',not_matched:'不符合',prefiltered:'預篩排除',analysis_error:'分析錯誤',outside_universe:'不在範圍'};
      for(const item of result.excluded||[])$('ai-result-excluded').append(el('p',`${item.symbol}｜${labels[item.status]||item.status}｜${item.latest_candle_at||'無完整 K 線日期'}｜${item.reasons.join('；')}`));
      if(result.remaining_symbols?.length)$('ai-result-excluded').append(el('p',`尚未處理：${result.remaining_symbols.join('、')}`));
      $('ai-result-json').textContent=JSON.stringify(result,null,2);
    }
    function renderEntryResult(result) {
      $('ai-result').classList.remove('hidden');
      $('ai-detail-heading').textContent='觀察重點與資料狀態';
      $('ai-result-title').textContent=`${result.symbol} ${result.name||''}｜單股${actionNames[result.intent]}條件觀察`;
      $('ai-result-summary').replaceChildren();
      $('ai-result-summary').append(el('p',result.summary));
      const meaning=result.strategy.user_explanation;
      if(meaning){$('ai-result-summary').append(el('p',meaning.purpose));if(result.assessment==='conditions_met')$('ai-result-summary').append(el('p',meaning.when_met));else if(result.assessment==='conditions_not_met')$('ai-result-summary').append(el('p',meaning.when_not_met));}
      if(result.strategy.note)$('ai-result-summary').append(el('p',result.strategy.note));
      const quality={available:'可分析',missing_data:'沒有資料',outside_universe:'所選範圍查無商品',stale_data:'最新期間尚未到齊',unverified_period:'完整期間未確認',insufficient_data:'完整 K 線不足'};
      $('ai-result-summary').append(el('p',`資料狀態：${quality[result.data_status]||result.data_status}；應有期間至 ${result.data_policy.expected_period_end}；最新完整 K 線 ${String(result.latest_candle_at||'無').slice(0,10)}；實際 ${result.candle_count||0} 根。`));
      if(result.price!==undefined)$('ai-result-summary').append(el('p',`該根收盤價：${result.price}。策略：${result.strategy.name}；${result.strategy.logic_description||''}`));
      for(const warning of result.warnings||[])$('ai-result-summary').append(el('p',warning,'warning'));
      $('ai-result-items').replaceChildren();
      if(result.facts?.length){
        $('ai-result-items').append(el('h4','程式依行情計算的條件與補充資料'));
        const labels={matched:'符合',not_matched:'不符合',insufficient_data:'資料不足'};
        for(const fact of result.facts)$('ai-result-items').append(el('p',`${fact.kind==='strategy_condition'?(labels[fact.status]||fact.status)+'｜':''}${fact.label}`));
      }
      $('ai-result-excluded').replaceChildren();
      if(result.focus?.length){
        $('ai-result-excluded').append(el('h4',result.ai.status==='explained'?'AI 整理的觀察重點':'程式計算結果'));
        for(const id of result.focus){const fact=result.facts.find(f=>f.id===id);if(fact)$('ai-result-excluded').append(el('p',fact.label));}
      }
      $('ai-result-summary').append(el('p','判定以已確認策略及資料日期為準；歷史連續性仍以資料檢查結果為準。','hint'));
      $('ai-result-json').textContent=JSON.stringify(result,null,2);
    }
    function replaceText(text) {
      const old=$('ai-text').value.trim();
      if(old&&old!==text){$('ai-history-panel').classList.remove('hidden');$('ai-history').append(el('li',old));}
      $('ai-text').value=text;lastText=text;flow.dirty();$('ai-text').focus();
    }
    $('ai-form').addEventListener('input',()=>flow.dirty());
    $('ai-form').addEventListener('change',event=>{
      if(event.target.id==='ai-universe')$('ai-symbols').disabled=event.target.value!=='symbols';
      if(event.target.id==='ai-source'){
        const odd=event.target.value==='odd_lot_candle';document.querySelectorAll('input[name="ai-market"]').forEach(e=>{const isOdd=Number(e.value)>=5;e.disabled=isOdd!==odd;e.checked=isOdd===odd&&(odd||Number(e.value)!==4);});
        $('ai-source-note').textContent=odd?'此表對應零股商品範圍，但歷史來源不保證為純零股成交 K 線；需確認此限制。':'唯讀已儲存完整期間，不登入群益或回補。';
      }
      flow.dirty();
    });
    document.querySelectorAll('[data-ai-example]').forEach(b=>b.addEventListener('click',()=>replaceText(b.dataset.aiExample)));
    $('ai-form').addEventListener('submit',async event=>{event.preventDefault();try{if(!$('ai-form').reportValidity())return;const text=$('ai-text').value.trim();if(!text)throw new Error('請輸入完整條件。');if(lastText&&lastText!==text){$('ai-history-panel').classList.remove('hidden');$('ai-history').append(el('li',lastText));}lastText=text;flow.dirty();message('解析與驗證中；不會自動執行選股…');const config=settings();await flow.parse({text,history_interval:config.history_interval,settings:config});}catch(e){message(e.message,true);}});
    $('ai-revise').addEventListener('click',async()=>{try{message('驗證修改後的條件中…');await flow.revise(editedRule);}catch(e){message(e.message,true);}});
    ['ai-confirm-scope','ai-confirm-assumptions'].forEach(id=>$(id).addEventListener('change',updateButtons));
    $('ai-execute').addEventListener('click',async()=>{try{updateButtons();if($('ai-execute').disabled)return;message(isStockIntent(flow.preview)?'正在讀取單股完整 K 線並整理觀察證據；模型會於使用後卸載，首次載入可能較久。':'正在唯讀掃描 MySQL，請勿重複送出；沒有虛構百分比進度。');const answer=await flow.execute({text:$('ai-text').value.trim(),settings:settings(),confirmed:true,assumptions_confirmed:true});checks();if(answer.outdated){message('原確認版本已執行完成，但輸入已變更；未把舊結果套用至目前條件。');return;}renderResult(answer.result);message(isStockIntent(answer.result)?'單股觀察已完成，請查看資料狀態與條件；如需再次分析，請重新預覽及確認。':answer.result.complete?'掃描完成；如需再次執行，請重新預覽及確認。':'掃描部分完成，請查看尚未處理名單；不會自動重試。');}catch(e){message(e.message,true);}finally{updateButtons();}});
    setInterval(updateButtons,1000);
  });
})();
