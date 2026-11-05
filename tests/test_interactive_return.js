const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=process.env.INTERACTIVE_WEB||path.join(__dirname,'../web');
const clone=v=>JSON.parse(JSON.stringify(v));
const draft={quest_id:'test_quest',party:Array.from({length:6},(_,i)=>({source:i===2?'support':'owned',svt_id:100+i,ce_id:200,
  owned_stats:{lv:100,np_id:304801},owned_ce:{lv:100,is_mlb:true},support:{lv:90,np_id:123},support_ce:{lv:80,is_mlb:false}})),mystic_code_id:20,mystic_code_lv:7};
function harness(){
  const elements=new Map();
  const node=()=>({value:'',options:[],textContent:'',open:false,disabled:false,children:[],
    add(o){this.options.push(o);},append(...v){this.children.push(...v);},replaceChildren(...v){this.children=v;},
    close(){this.open=false;},scrollIntoView(){},focus(){},classList:{add(){}}});
  const get=id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id);};
  get('battle-mode').value='interactive';get('interactive-name').value='return-test';
  const calls=[],saved=[];let report=null,ack=null,stopFails=false;
  const ctx=vm.createContext({structuredClone,setTimeout,clearTimeout,console,document:{getElementById:get},
    Option:function(text,value){this.text=text;this.value=value;},manualParty:[],manualInventory:{},manualSupportLoading:false,battleCatalog:[],
    manualSlotPayload:value=>structuredClone(value),manualMysticLevel:()=>Number(get('manual-mystic-level').value),
    updateBattleTarget(){},syncManualMysticLevel(){},renderManualRoster(){},loadManualMysticCode:async()=>{},loadManualSupport:async()=>{},
    clearBattleFeedback(){},showBattleFeedback(){},renderFgoBattlefield(){},manualElement:node,manualButton:node,
    fetch:async(url,options)=>{
      const body=options?JSON.parse(options.body):null;calls.push({url,body});
      if(url.endsWith('/enter')||url.endsWith('/replay'))return new Promise(resolve=>{ack=()=>resolve({json:async()=>({success:true,request_id:'req'})});});
      if(url.endsWith('/stop')){if(stopFails)throw Error('offline');return {json:async()=>({success:true,cancelled:true})};}
      if(url.endsWith('/save')){saved.push(body.recording);return {json:async()=>({success:true,saved:{...body.recording,id:'saved'}})};}
      return {json:async()=>({success:true,report})};
    }});
  vm.runInContext(fs.readFileSync(path.join(source,'interactive.js'),'utf8'),ctx);
  const html=fs.readFileSync(path.join(source,'index.html'),'utf8');
  const button=html.match(/<button\b[^>]*onclick="([^"]+)"[^>]*>[^<]*返回选人[^<]*<\/button>/);
  assert(button,'返回选人按钮不存在');
  const run=code=>vm.runInContext(code,ctx);
  ctx.entryDraft=structuredClone(draft);
  return {ctx,get,calls,saved,run,back:()=>run(button[1]),ack:()=>ack(),report:value=>{report=value;},failStop:()=>{stopFails=true;}};
}
function seed(h){h.run(`interactiveTimeline.append({kind:'enter',draft:entryDraft,session_id:'session',scene:{accepted:true,players:[{slot:1,position:6,hp:1}],selected_cards:[{kind:'np',actor:1}]}});scheduleInteractiveSave();`);}
test('返回恢复进场配置且保留录制；再次进场使用恢复后的配置',async()=>{
  const h=harness();seed(h);await h.back();await h.run('interactiveSaveQueue');
  assert.deepEqual(clone(h.ctx.manualParty),draft.party);
  assert.equal(h.get('battle-choice').value,draft.quest_id);assert.equal(Number(h.get('manual-mystic-level').value),7);
  assert.equal(h.get('manual-team-card').open,true);assert.equal(h.run('interactiveTimeline.state().scene'),null);
  assert.equal(h.saved.length,1);assert.equal(h.saved[0].entries[0].scene.players[0].hp,1);
  const entering=h.run('enterInteractiveBattle()');h.ack();await entering;
  assert.deepEqual(clone(h.calls.find(c=>c.url.endsWith('/enter')).body.draft),draft);
});
for(const pendingEnter of [false,true])test(`计算中返回阻止迟到结果重新进场（${pendingEnter?'进场未应答':'战斗步骤'}）`,async()=>{
  const h=harness();if(!pendingEnter)seed(h);
  const task=h.run(pendingEnter?`launchInteractive('/api/interactive/enter',{draft:entryDraft},{kind:'enter',draft:entryDraft})`:
    `launchInteractive('/api/interactive/replay',{}, {kind:'event',event:{kind:'cards'}})`);
  await h.back();assert.equal(h.get('manual-team-card').open,true);assert.equal(h.run('interactiveBusy'),true);
  assert.equal(h.run('interactiveTimeline.state().scene'),null);
  h.ack();await task;assert(h.calls.some(c=>c.url.endsWith('/stop')));
  h.report({request_id:'req',client_revision:0,status:'completed',result:{scene:{accepted:true},session_id:'late'}});
  await h.run('pollInteractiveStatus()');await h.run('interactiveSaveQueue');
  assert.equal(h.run('interactiveBusy'),false);assert.equal(h.run('interactiveTimeline.entries.length'),0);
  assert.deepEqual(clone(h.ctx.manualParty),draft.party);
});
test('中断网络失败仍能返回，等待原任务结束前不允许新计算',async()=>{
  const h=harness();seed(h);
  const task=h.run(`launchInteractive('/api/interactive/replay',{}, {kind:'event',event:{kind:'cards'}})`);
  h.ack();await task;h.failStop();await h.back();
  assert.equal(h.get('manual-team-card').open,true);assert.equal(h.run('interactiveBusy'),true);
  assert.equal(h.get('interactive-enter').disabled,true);
  h.report({request_id:'req',client_revision:0,status:'completed',result:{scene:{accepted:true}}});
  await h.run('pollInteractiveStatus()');await h.run('interactiveSaveQueue');
  assert.equal(h.run('interactiveTimeline.state().scene'),null);assert.equal(h.run('interactiveBusy'),false);
});
test('保留原有计算中回退功能',async()=>{
  const h=harness();seed(h);
  h.run(`interactiveTimeline.append({kind:'event',event:{kind:'cards'},scene:{accepted:true},engine_hash:''});`);
  const task=h.run(`launchInteractive('/api/interactive/replay',{}, {kind:'event',event:{kind:'attack'}})`);
  h.ack();await task;await h.run('moveInteractiveHistory(-1)');
  h.report({request_id:'req',client_revision:0,status:'cancelled'});
  await h.run('pollInteractiveStatus()');
  assert.equal(h.run('interactiveAfterStop'),null);
  assert.equal(h.run('interactiveTimeline.cursor'),1);
  await h.run('saveInteractiveRecording(false,true)');
});
