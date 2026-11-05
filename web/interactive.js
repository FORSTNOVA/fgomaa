// 战斗时间线从成功进场开始，场景来自原生 Laplace。
class InteractiveTimeline {
  constructor(entries=[],cursor=entries.length-1,version=2,recordId=null) {
    this.entries=structuredClone(entries);this.cursor=cursor;
    this.version=version;this.recordId=recordId;
    if(cursor < -1 || cursor>=entries.length)throw new Error('无效历史位置');
  }
  append(entry) {
    if(this.cursor>=599)throw new Error('最多记录600步，请另存后开始新的录制');
    this.entries.splice(this.cursor+1);this.entries.push(structuredClone(entry));this.cursor++;
  }
  move(cursor) {if(cursor<0||cursor>=this.entries.length)return false;this.cursor=cursor;return true;}
  state() {
    let state={draft:null,session_id:null,events:[],scene:null,engine_hash:''};
    for(const entry of this.entries.slice(0,this.cursor+1)) {
      if(entry.kind==='setup')state={draft:entry.draft,session_id:null,events:[],scene:null,engine_hash:''};
      else if(entry.kind==='enter')state={draft:entry.draft,session_id:entry.session_id,events:[],scene:entry.scene,engine_hash:entry.engine_hash};
      else {state.events.push(entry.event);state.scene=entry.scene;state.engine_hash=entry.engine_hash;}
    }
    return structuredClone(state);
  }
}

let interactiveTimeline=new InteractiveTimeline(),interactiveRequest=null,interactiveRevision=0;
let interactivePending=null,interactiveApplying=false,interactivePolling=false,interactiveBusy=false,interactiveReturning=false;
let interactiveRecords=[],interactiveRuntimeHash='',interactiveAfterStop=null;
let interactiveSaveTimer=null,interactiveSaveQueue=Promise.resolve();
let interactiveStopRequested=false;

function isInteractiveMode(){return document.getElementById('battle-mode').value==='interactive';}
function interactiveDraft() {
  return {quest_id:document.getElementById('battle-choice').value,
    party:manualParty.map(manualSlotPayload),
    mystic_code_id:Number(document.getElementById('manual-mystic-code').value)||null,
    mystic_code_lv:manualMysticLevel()};
}
function interactiveMessage(text){document.getElementById('interactive-message').textContent=text;}
async function initInteractiveBattle() {
  const data=await fetch('/api/interactive/runtime').then(r=>r.json());
  interactiveRuntimeHash=data.kernel_sha256||'';
  interactiveConfigurationChanged();
  await interactiveSaveQueue;
  await refreshInteractiveRecordings();renderInteractiveBattle();
}
function interactiveConfigurationChanged() {
  if(!isInteractiveMode()||interactiveApplying||interactiveReturning||!manualInventory)return;
  const previous=interactivePending?.kind==='enter'?interactivePending.draft:interactiveTimeline.state().draft;
  if(!previous)return;
  const focused=document.activeElement;
  if(focused?.type==='number'&&!focused.checkValidity())return;
  let draft;
  try { draft=interactiveDraft(); } catch(error) { interactiveMessage(error.message); return; }
  if(JSON.stringify(interactiveDraftKey(previous))===JSON.stringify(interactiveDraftKey(draft)))return;
  interactiveRevision++;
  interactiveAfterStop=null;
  if(interactiveBusy)stopInteractiveCalculation();
  clearBattleFeedback();
  resetInteractiveTimeline();
  interactiveMessage('已更新配置。进入模拟战斗后开始新的录制。');
  renderInteractiveBattle();
}
function interactiveDraftKey(draft) {
  return [draft.quest_id,draft.party.map(manualSlotPayload),draft.mystic_code_id??null,draft.mystic_code_lv??null];
}
function resetInteractiveTimeline() {
  if(interactiveSaveTimer!==null)saveInteractiveRecording(false,true);
  interactiveTimeline=new InteractiveTimeline();
  document.getElementById('interactive-save-status').textContent='';
  renderInteractiveRecordings();
}
async function applyInteractiveDraft(draft) {
  interactiveApplying=true;
  try {
    manualParty=structuredClone(draft.party);
    document.getElementById('manual-support-slot').value=String(manualParty.findIndex(s=>s.source==='support')+1);
    const quest=document.getElementById('battle-choice');
    if(![...quest.options].some(o=>o.value===draft.quest_id))quest.add(new Option(battleCatalog.find(q=>q.quest_id===draft.quest_id)?.name||draft.quest_id,draft.quest_id));
    quest.value=draft.quest_id;updateBattleTarget();
    const mc=document.getElementById('manual-mystic-code');
    if(draft.mystic_code_id&&![...mc.options].some(o=>o.value===String(draft.mystic_code_id)))mc.add(new Option('已保存御主礼装 '+draft.mystic_code_id,String(draft.mystic_code_id)));
    mc.value=draft.mystic_code_id?String(draft.mystic_code_id):'';
    const level=document.getElementById('manual-mystic-level');if(level)level.value=draft.mystic_code_lv??'';
    syncManualMysticLevel();
    renderManualRoster();await loadManualMysticCode();
    const support=manualParty.find(s=>s.source==='support');
    if(support?.svt_id)await loadManualSupport(support);
  } finally{interactiveApplying=false;}
}
async function launchInteractive(endpoint,body,pending) {
  if(interactiveBusy){interactiveMessage('本步仍在计算，可点击中断或回退。');return;}
  clearBattleFeedback();
  interactiveBusy=true;interactivePending=pending;interactiveStopRequested=false;
  const revision=interactiveRevision;
  renderInteractiveBattle();
  try {
    const data=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,client_revision:revision})}).then(r=>r.json());
    if(!data.success)throw new Error(data.message);
    interactiveRequest=data.request_id;
    if(interactiveStopRequested||revision!==interactiveRevision)await stopInteractiveCalculation(revision!==interactiveRevision);
    else interactiveMessage('正在计算本步 NP、技能效果和战场状态…');
  } catch(error){
    interactiveBusy=false;interactivePending=null;interactiveStopRequested=false;
    if(revision===interactiveRevision)interactiveMessage(error.message);
    else {
      const msgEl=document.getElementById('interactive-message');
      if(msgEl&&msgEl.textContent.includes('后台计算正在取消中'))interactiveMessage('已恢复初始选人配置，已有战斗录制已保留。');
    }
    renderInteractiveBattle();
    if(interactiveAfterStop){const action=interactiveAfterStop;interactiveAfterStop=null;await action();}
  }
}
async function enterInteractiveBattle() {
  if(!isInteractiveMode()||interactiveApplying||interactiveReturning)return;
  try {
    if(manualSupportLoading)throw new Error('请等待助战资料读取完成');
    if(manualParty.some(s=>!s.svt_id))throw new Error('请配置5名自有角色与1名助战');
    const draft=interactiveDraft();
    await launchInteractive('/api/interactive/enter',{draft},{kind:'enter',draft,label:'进入模拟关卡'});
  }catch(error){interactiveMessage(error.message);}
}
async function returnInteractiveSelection() {
  if(!isInteractiveMode()||interactiveReturning)return;
  interactiveReturning=true;
  renderInteractiveBattle();
  try {
    const draft=interactivePending?.kind==='enter'?interactivePending.draft:interactiveTimeline.state().draft;
    if(!draft){
      document.getElementById('manual-team-card').open=true;
      return;
    }
    const wasBusy=interactiveBusy;
    interactiveRevision++;
    interactiveAfterStop=null;
    interactivePending=null;
    if(wasBusy)stopInteractiveCalculation(true).catch(()=>{});
    clearBattleFeedback();
    resetInteractiveTimeline();
    document.getElementById('manual-team-card').open=true;
    renderInteractiveBattle();
    await applyInteractiveDraft(draft);
    if(interactiveBusy){
      interactiveMessage('已恢复初始选人配置，已有战斗录制已保留；后台计算正在取消中…');
    }else{
      interactiveMessage('已恢复初始选人配置，已有战斗录制已保留。');
    }
    renderInteractiveBattle();
  }catch(error){
    interactiveMessage('已返回选人，部分资料读取失败：'+error.message);
  }finally{
    interactiveReturning=false;
    renderInteractiveBattle();
    document.getElementById('manual-team-card').scrollIntoView({block:'start'});
  }
}
async function interactiveAction(event,label) {
  const current=interactiveTimeline.state();
  if(!current.session_id||!current.scene){interactiveMessage('请先进入模拟战斗');return;}
  if(current.scene.won||current.scene.lost){interactiveMessage('本场已经结束，可回退或重新配置关卡。');return;}
  await launchInteractive('/api/interactive/replay',{session_id:current.session_id,events:[...current.events,event]},
    {kind:'event',event,label,session_id:current.session_id});
}
async function stopInteractiveCalculation(silent=false) {
  if(!interactiveBusy)return;
  interactiveStopRequested=true;
  const stopRevision=interactiveRevision;
  if(!silent)interactiveMessage('正在中断本次计算，保留上一步…');
  if(!interactiveRequest)return;
  try {
    const data=await fetch('/api/interactive/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:interactiveRequest})}).then(r=>r.json());
    if(!data.success&&!silent&&stopRevision===interactiveRevision)interactiveMessage(data.message||'中断请求未确认');
  } catch(error) {
    if(!silent&&stopRevision===interactiveRevision)interactiveMessage('中断请求失败：'+error.message);
  }

}
async function pollInteractiveStatus() {
  if(!interactiveRequest||interactivePolling)return;
  interactivePolling=true;
  try {
    const data=await fetch('/api/interactive/status').then(r=>r.json()),report=data.report;
    if(!report||report.request_id!==interactiveRequest||report.status==='running')return;
    const pending=interactivePending;
    interactiveRequest=null;interactivePending=null;interactiveBusy=false;
    const stopped=interactiveStopRequested;interactiveStopRequested=false;
    if(report.client_revision===interactiveRevision) {
      if(stopped)interactiveMessage('本次计算已中断，保留上一步。');
      else if(report.status==='completed') {
      const result=report.result;
      if(result.scene.accepted) {
        const entry={...pending,scene:result.scene,engine_hash:result.engine_hash};
        if(pending.kind==='enter'){
          resetInteractiveTimeline();
          entry.session_id=result.session_id;
          document.getElementById('manual-team-card').open=false;
        }
        if(pending.kind==='restore') {
          const target=interactiveTimeline.entries[interactiveTimeline.cursor];
          showBattleFeedback(target.scene,result.scene,null,'已恢复保存位置',true);
          target.scene=result.scene;target.engine_hash=result.engine_hash;
        } else {
          if(pending.kind==='event')showBattleFeedback(interactiveTimeline.state().scene,result.scene,pending.event,pending.label);
          interactiveTimeline.append(entry);
        }
        interactiveMessage(result.scene.won?'模拟关卡已完成，伤害与NP回收已记录。':result.scene.lost?'模拟战败，操作已保存；可回退调整战术。':'操作已记录，可继续下一步或回退。');
        scheduleInteractiveSave();
      } else interactiveMessage('本步未生效：'+(result.failure?.message||'操作不满足释放条件')+'。保留上一步状态。');
      } else interactiveMessage(report.status==='cancelled'?'计算已中断，保留上一步。':report.error);
    } else {
      const msgEl=document.getElementById('interactive-message');
      if(msgEl&&msgEl.textContent.includes('后台计算正在取消中'))interactiveMessage('已恢复初始选人配置，已有战斗录制已保留。');
    }
    renderInteractiveBattle();
    if(interactiveAfterStop){const action=interactiveAfterStop;interactiveAfterStop=null;await action();}
  }catch(error){
    const msgEl=document.getElementById('interactive-message');
    if(!msgEl||!msgEl.textContent.includes('已恢复初始选人配置'))interactiveMessage('查询状态失败：'+error.message);
  }
  finally{interactivePolling=false;}
}
async function moveInteractiveHistory(delta) {
  if(interactiveBusy) {
    const target=interactiveTimeline.cursor+(interactivePending?.kind==='restore'?delta:Math.max(0,delta));
    interactiveRevision++;interactiveAfterStop=()=>goInteractiveHistory(target);
    await stopInteractiveCalculation();return;
  }
  await goInteractiveHistory(interactiveTimeline.cursor+delta);
}
async function goInteractiveHistory(cursor) {
  if(interactiveBusy)return;
  const previous=interactiveTimeline.state(),previousCursor=interactiveTimeline.cursor;
  if(!interactiveTimeline.move(cursor))return;
  clearBattleFeedback();
  interactiveRevision++;
  const current=interactiveTimeline.state();await applyInteractiveDraft(current.draft);
  document.getElementById('manual-team-card').open=!current.scene;
  if(current.scene)showBattleFeedback(previous.session_id===current.session_id?previous.scene:null,current.scene,null,
    `${cursor<previousCursor?'回退':cursor>previousCursor?'重做':'恢复'}到第${cursor+1}步`,true);
  renderInteractiveBattle();scheduleInteractiveSave();
  if(current.session_id&&current.engine_hash!==interactiveRuntimeHash)await restoreInteractivePosition();
  else interactiveMessage(`已恢复到第${cursor+1}步，NP、冷却和血量一并恢复。`);
}
async function restoreInteractivePosition() {
  const current=interactiveTimeline.state();
  await launchInteractive('/api/interactive/replay',{session_id:current.session_id,events:current.events},{kind:'restore',label:'重放到当前历史步骤'});
}
function interactivePrompt(kind,actor,skill,name,swap=false) {
  const current=interactiveTimeline.state(),box=document.getElementById('interactive-prompt');box.close();box.replaceChildren();
  const line=manualElement('div','','manual-toolbar');line.append(manualElement('b',name));
  const front=current.scene.players.filter(p=>p.position>0&&p.position<=3),back=current.scene.players.filter(p=>p.position>3);
  const target=manualSelect(front.map(p=>[p.slot,`${p.name}（原${p.slot}号位）`]),front[0]?.slot,()=>{},'本次技能目标');
  line.append(target);
  let incoming;
  if(swap){incoming=manualSelect(back.map(p=>[p.slot,`${p.name}（原${p.slot}号位）`]),back[0]?.slot,()=>{},'换上角色');line.append(incoming);}
  line.append(manualButton(swap?'确认换人':'释放技能',()=>{
    const event={kind,skill};if(kind==='skill')event.actor=actor;
    if(swap)event.swap=[Number(target.value),Number(incoming.value)];else event.target=Number(target.value);
    box.close();box.replaceChildren();interactiveAction(event,swap?`御主技能${skill}：换下${target.value}号／换上${incoming.value}号`:`${actor?actor+'号位':'御主'}技能${skill} → ${target.value}号位`);
  }),manualButton('取消',()=>{box.close();box.replaceChildren();}));box.append(line);box.showModal();
}
function renderInteractiveBattle(preservePrompt=false) {
  const state=interactiveTimeline.state(),scene=state.scene,container=document.getElementById('interactive-scene');
  container.replaceChildren();const prompt=document.getElementById('interactive-prompt');
  if(!preservePrompt){prompt.close();prompt.replaceChildren();}
  document.getElementById('interactive-enter').disabled=interactiveBusy||interactiveApplying||interactiveReturning;
  const returnBtn=document.getElementById('interactive-return');
  if(returnBtn)returnBtn.disabled=interactiveReturning;
  document.getElementById('interactive-undo').disabled=interactiveTimeline.cursor<=0&&!interactiveBusy;
  document.getElementById('interactive-redo').disabled=interactiveTimeline.cursor>=interactiveTimeline.entries.length-1&&!interactiveBusy;
  const history=document.getElementById('interactive-history');history.replaceChildren();
  interactiveTimeline.entries.forEach((entry,i)=>{
    const button=manualButton(`${i+1}. ${entry.label}${i===interactiveTimeline.cursor?' ← 当前':''}`,()=>goInteractiveHistory(i));
    if(i===interactiveTimeline.cursor)button.classList.add('current-step');button.disabled=interactiveBusy;history.append(button);
  });
  if(!scene){container.append(manualElement('p','配置好关卡和编队后，点击「进入模拟战斗」开始录制。','checkbox-desc'));return;}
  renderFgoBattlefield(container,scene,state);
}
function scheduleInteractiveSave(){clearTimeout(interactiveSaveTimer);interactiveSaveTimer=setTimeout(()=>saveInteractiveRecording(false,true),250);}
async function saveInteractiveRecording(asNew=false,automatic=false) {
  clearTimeout(interactiveSaveTimer);
  interactiveSaveTimer=null;
  const timeline=interactiveTimeline;
  if(!timeline.entries.length){
    if(!automatic)interactiveMessage('请先进入模拟战斗。');
    return;
  }
  const name=document.getElementById('interactive-name').value.trim()||'逐步录制草稿';
  const recording={name,version:timeline.version,entries:structuredClone(timeline.entries),cursor:timeline.cursor};
  interactiveSaveQueue=interactiveSaveQueue.catch(()=>{}).then(async()=>{
    recording.id=asNew?null:timeline.recordId;
    const data=await fetch('/api/interactive/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({recording})}).then(r=>r.json());
    if(!data.success)throw new Error(data.message);
    timeline.recordId=data.saved.id;
    if(interactiveTimeline===timeline)document.getElementById('interactive-save-status').textContent=`${automatic?'草稿自动保存':'已保存'}：${name} · ${recording.entries.length}步，当前第${recording.cursor+1}步`;
    interactiveRecords=interactiveRecords.filter(r=>r.id!==data.saved.id);interactiveRecords.push(data.saved);renderInteractiveRecordings();
  }).catch(error=>{if(interactiveTimeline===timeline)document.getElementById('interactive-save-status').textContent='保存失败：'+error.message;});
  return interactiveSaveQueue;
}
function renderInteractiveRecordings(){const el=document.getElementById('interactive-recordings');el.replaceChildren(new Option('请选择录制',''));interactiveRecords.forEach(r=>el.add(new Option(r.name,r.id)));el.value=interactiveTimeline.recordId||'';}
async function refreshInteractiveRecordings(){const data=await fetch('/api/interactive/recordings').then(r=>r.json());if(!data.success)throw new Error(data.message);interactiveRecords=data.recordings;renderInteractiveRecordings();}
async function loadInteractiveRecording() {
  if(interactiveBusy||interactiveApplying||interactiveReturning){interactiveMessage('请等待当前操作完成或中断计算。');return;}
  const id=document.getElementById('interactive-recordings').value;if(!id)return;
  if(interactiveSaveTimer!==null)saveInteractiveRecording(false,true);
  await interactiveSaveQueue;
  if(interactiveBusy||interactiveApplying||interactiveReturning){interactiveMessage('请等待当前操作完成或中断计算。');return;}
  const recording=interactiveRecords.find(r=>r.id===id);if(!recording)return;
  clearBattleFeedback();
  interactiveTimeline=new InteractiveTimeline(recording.entries,recording.cursor,recording.version??1,recording.id);
  document.getElementById('interactive-name').value=recording.name;
  interactiveRevision++;const current=interactiveTimeline.state();await applyInteractiveDraft(current.draft);
  document.getElementById('manual-team-card').open=!current.scene;renderInteractiveBattle();
  if(current.session_id)await restoreInteractivePosition();else interactiveMessage('配置录制已载入，可继续配置或进入模拟战斗。');
}
async function startInteractiveFarm() {
  const recordingId=document.getElementById('interactive-recordings').value;
  const device=document.getElementById('battle-device').value;
  if(!recordingId||!device){interactiveMessage('请先选择已保存的通关录制和实机设备。');return;}
  if(interactiveBusy||interactiveApplying||interactiveReturning){interactiveMessage('请等待当前模拟操作完成。');return;}
  try {
    if(interactiveSaveTimer!==null)saveInteractiveRecording(false,true);
    await interactiveSaveQueue;
    const data=await fetch('/api/interactive/farm',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({recording_id:recordingId,device_serial:device})}).then(r=>r.json());
    interactiveMessage(data.message||'录制刷本请求失败。');
  }catch(error){interactiveMessage(error.message);}
}
