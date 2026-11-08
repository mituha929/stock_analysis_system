// State-machine tests using the shipped script; no browser/network/DLL.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
async function main(){
  const code=fs.readFileSync('app/static/v3-ai.js','utf8');
  const ctx=vm.createContext({window:{},document:{addEventListener(){}},JSON,Date,AbortController,setTimeout,clearTimeout});
  vm.runInContext(code,ctx);const Flow=ctx.window.CapitalAIPreviewFlow;
  const preview={preview_id:'preview-id-for-test',revision:'revision-test',rule:{field:'rsi14',operator:'<=',value:70}};
  let calls=[],pending;
  const flow=new Flow(async(path,body)=>{calls.push({path,body});if(path==='/preview')return new Promise(r=>pending=r);return {invalidated:true};});
  const a=flow.parse({text:'old'});flow.dirty();pending({...preview});assert.equal(await a,null);
  assert.equal(flow.preview,null);assert(calls.some(c=>c.path==='/preview/invalidate'));
  assert(!calls.some(c=>c.path==='/execute'),'parse must never automatically execute');

  let resolveRun;
  calls=[];
  const f=new Flow(async(path,body)=>{calls.push({path,body});if(path==='/execute')return new Promise(r=>resolveRun=r);return {...preview};});
  await f.parse({text:'new'});const run=f.execute({confirmed:true,assumptions_confirmed:true});
  await assert.rejects(()=>f.execute({}),/尚無/);
  assert.equal(calls.filter(c=>c.path==='/execute').length,1);
  f.dirty();resolveRun({items:[]});assert.equal((await run).outdated,true,'input changes must not apply old results');

  calls=[];let releaseRevoke;
  const edit=new Flow(async(path,body)=>{calls.push({path,body});if(path==='/preview/invalidate')return new Promise(r=>releaseRevoke=r);if(path==='/preview/revise')return {...preview,preview_id:'revised'};return {...preview};});
  await edit.parse({});edit.beginEdit();assert.equal(edit.preview,null);
  assert.equal(calls.at(-1).body.editable,true);
  const revise=edit.revise({field:'rsi14',operator:'<=',value:60});
  await Promise.resolve();assert(!calls.some(c=>c.path==='/preview/revise'),'must await revocation before revising');
  releaseRevoke({invalidated:true});await revise;
  assert.equal(edit.preview.preview_id,'revised');assert.equal(calls.at(-1).body.rule.value,60);
  assert(!calls.some(c=>c.path==='/execute'));

  const busy=new Flow(async path=>{if(path==='/execute'){const e=new Error('busy');e.code='scan_busy';throw e;}return {...preview};});
  await busy.parse({});await assert.rejects(()=>busy.execute({}),/busy/);assert(busy.preview,'busy should not consume token');
  const fail=new Flow(async path=>{if(path==='/execute')throw new Error('database');return {...preview};});
  await fail.parse({});await assert.rejects(()=>fail.execute({}),/database/);assert.equal(fail.preview,null);

  let started=0,release;
  const duplicate=new Flow(async()=>{started++;return new Promise(r=>release=r);});
  const first=duplicate.parse({});await duplicate.parse({});assert.equal(started,1);release({...preview});await first;
  assert(!code.includes('.innerHTML'),'untrusted model/input must use text nodes, not HTML');
  const html=fs.readFileSync('app/static/v3.html','utf8');
  for(const id of ['ai-form','ai-preview','ai-result','ai-execute','ai-confirm-scope','ai-confirm-assumptions'])assert(html.includes(`id="${id}"`));
  assert(!html.match(/id="ai-interval"[^]*?<\/select>/)[0].includes('value="1m"'));
  // Exercise the actual DOM branch for single-stock preview/confirmation and
  // result rendering, including the response shape with no screener items.
  class Element {
    constructor(){this.children=[];this.events={};this._text='';this.value='';this.checked=false;this.disabled=false;this.classes=new Set();this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x),toggle:(x,on)=>on?this.classes.add(x):this.classes.delete(x)};}
    get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
    set textContent(value){this._text=String(value);this.children=[];}
    append(...children){this.children.push(...children);}
    replaceChildren(...children){this.children=children;this._text='';}
    addEventListener(name,fn){this.events[name]=fn;}
    setAttribute(){} focus(){} reportValidity(){return true;}
  }
  const elements=new Map(),node=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  let ready;const uiCalls=[];
  for(const [id,value] of Object.entries({'ai-universe':'markets','ai-source':'candles','ai-interval':'day','ai-bars':'360','ai-limit':'20','ai-timeout':'120','ai-sort':'default','ai-text':'我想要確認2454是否可以買進'}))node(id).value=value;
  const marketNodes=[0,1].map(n=>{const e=new Element();e.value=String(n);e.checked=true;return e;});
  const uiContext=vm.createContext({window:{},document:{
    addEventListener(name,fn){if(name==='DOMContentLoaded')ready=fn;},getElementById:node,
    createElement(){return new Element();},querySelectorAll(selector){return selector.includes('ai-market')?marketNodes:[];}},
    JSON,Date,AbortController,setTimeout,clearTimeout,setInterval(){},
    async fetch(url,options){
      uiCalls.push(url);const body=JSON.parse(options.body);let reply={invalidated:true};
      const sell=String(body.text||'').includes('賣出');
      const position=String(body.text||'').includes('加碼')?'加碼':String(body.text||'').includes('減碼')?'減碼':null;
      if(url.endsWith('/preview'))reply={...preview,intent:'single_stock_entry',entry_analysis:{symbol:'2454',strategy:{name:'20／60 根觀察',note:'需確認'}},
        settings:body.settings,original_text:body.text,data_policy:{expected_period_end:'2026-10-30'},unresolved_conditions:[],assumptions:[],warnings:[],status:'draft',expires_at:new Date(Date.now()+600000).toISOString()};
      if(url.endsWith('/preview')&&sell)reply={...reply,intent:'single_stock_exit',preview_id:'sell-preview-id',
        stock_analysis:{symbol:'2454',strategy:{name:'均線轉弱出場觀察',note:'需確認',logic_description:'兩組任一成立'}},
        explanation:{kind:'group',label:'任一符合（OR）',children:[{kind:'rule',label:'向下穿越 MA20'}]}};
      if(url.endsWith('/execute'))reply={intent:'single_stock_entry',symbol:'2454',name:'測試商品',complete:true,
        summary:'尚未全部符合觀察條件',data_status:'available',data_policy:{expected_period_end:'2026-10-30'},
        latest_candle_at:'2026-10-30',candle_count:90,price:123,strategy:{name:'20／60 根觀察'},warnings:[],
        facts:[{id:'condition_1',kind:'strategy_condition',status:'not_matched',label:'價格未高於均線'}],
        focus:['condition_1'],ai:{status:'explained'}};
      if(url.endsWith('/execute')&&sell)reply={...reply,intent:'single_stock_exit',summary:'目前未觸發已確認的出場觀察條件',
        strategy:{name:'均線轉弱出場觀察',logic_description:'兩組出場條件任一組成立即觸發'}};
      if(position){
        const strategy={name:`${position}觀察`,logic_description:'各項需同時成立',note:'未確認實際持股，不計算張數或比例',
          user_explanation:{purpose:`${position}目的說明`,when_met:'成立時仍需核對部位',when_not_met:'這套策略未成立'}};
        const intent=position==='加碼'?'single_stock_add':'single_stock_reduce';
        if(url.endsWith('/preview'))reply={...reply,intent,preview_id:`position-${intent}`,
          stock_analysis:{symbol:'2454',strategy},explanation:{kind:'rule',label:'確認觀察條件'}};
        if(url.endsWith('/execute'))reply={...reply,intent,strategy,assessment:'conditions_not_met',summary:`目前未符合${position}條件`};
      }
      return {ok:true,json:async()=>reply};
    }});
  vm.runInContext(code,uiContext);ready();
  await node('ai-form').events.submit({preventDefault(){}});
  assert(node('ai-scope').textContent.includes('本次僅分析 2454'));
  assert(node('ai-execute').disabled,'must explicitly confirm before reading data');
  assert(!uiCalls.some(p=>p.endsWith('/execute')));
  for(const id of ['ai-confirm-scope','ai-confirm-assumptions']){node(id).checked=true;node(id).events.change();}
  assert(!node('ai-execute').disabled);
  assert(node('ai-execute').textContent.includes('2454'));
  await node('ai-execute').events.click();
  assert(node('ai-result-title').textContent.includes('2454'));
  assert(node('ai-result-summary').textContent.includes('尚未全部符合'));
  assert(node('ai-result-items').textContent.includes('價格未高於均線'));
  assert.equal(uiCalls.filter(p=>p.endsWith('/execute')).length,1);
  node('ai-text').value='2454 是否可以賣出';
  await node('ai-form').events.submit({preventDefault(){}});
  assert(node('ai-scope').textContent.includes('出場'));
  assert(node('ai-explanation').textContent.includes('任一符合'));
  assert(node('ai-execute').textContent.includes('確認出場條件'));
  assert(node('ai-execute').disabled);
  assert.equal(uiCalls.filter(p=>p.endsWith('/execute')).length,1,'sell preview must not execute automatically');
  for(const id of ['ai-confirm-scope','ai-confirm-assumptions']){node(id).checked=true;node(id).events.change();}
  await node('ai-execute').events.click();
  assert(node('ai-result-title').textContent.includes('出場條件觀察'));
  assert(node('ai-result-summary').textContent.includes('未觸發'));
  assert(node('ai-result-summary').textContent.includes('任一組成立'));
  assert(!node('ai-result-summary').textContent.includes('三項條件需全部成立'));
  assert.equal(uiCalls.filter(p=>p.endsWith('/execute')).length,2);
  let executionCount=2;
  for(const word of ['加碼','減碼']){
    node('ai-text').value=`2454 是否可以${word}`;
    await node('ai-form').events.submit({preventDefault(){}});
    assert(node('ai-execute').textContent.includes(`確認${word}條件`));
    assert(node('ai-explanation').textContent.includes(`${word}目的說明`));
    assert(node('ai-execute').disabled);
    assert.equal(uiCalls.filter(p=>p.endsWith('/execute')).length,executionCount);
    for(const id of ['ai-confirm-scope','ai-confirm-assumptions']){node(id).checked=true;node(id).events.change();}
    await node('ai-execute').events.click();executionCount++;
    assert(node('ai-result-title').textContent.includes(`${word}條件觀察`));
    assert(node('ai-result-summary').textContent.includes(`目前未符合${word}條件`));
    assert(node('ai-result-summary').textContent.includes('不計算張數或比例'));
    assert(node('ai-result-summary').textContent.includes('這套策略未成立'));
  }
  assert.equal(uiCalls.filter(p=>p.endsWith('/execute')).length,4);
  console.log('Single-stock UI passed: exact symbol preview, explicit confirmation, one execute, and evidence/data-quality rendering without a screener result shape.');
  console.log('Sell UI passed: separate exit purpose, OR explanation, fresh confirmation and exit-specific results.');
  console.log('Add/reduce UI passed: separate intent and confirmation for each action, strategy meaning and position-size limitations.');
  console.log('AI frontend checks passed: stale replies, no auto-execute, single-flight/duplicate clicks, edit revocation, re-confirmation, stale result isolation, busy/failure states, safe text rendering and first-release scope.');
}
main().catch(e=>{console.error(e);process.exitCode=1;});
