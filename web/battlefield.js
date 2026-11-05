// 外观只读取战场状态；所有动作继续使用原有语义事件与历史时间线。
const battleVisuals=new Map();
const battleClasses={saber:'SABER',archer:'ARCHER',lancer:'LANCER',rider:'RIDER',caster:'CASTER',assassin:'ASSASSIN',berserker:'BERSERKER',ruler:'RULER',avenger:'AVENGER',alterEgo:'ALTER EGO',foreigner:'FOREIGNER',pretender:'PRETENDER',shielder:'SHIELDER',moonCancer:'MOON CANCER'};
const battleNumbers=['1st','2nd','3rd'];
let battleFeedback=null;

// 仅保存当前页面的显示反馈；不写入 scene、录制或引擎请求。
function freshBattleRows(before=[],after=[]) {
  for(let overlap=Math.min(before.length,after.length);overlap>0;overlap--)
    if(JSON.stringify(before.slice(-overlap))===JSON.stringify(after.slice(0,overlap)))return after.slice(overlap);
  return after;
}
function battleActors(scene,event) {
  if(!scene||!event)return [];
  if(event.kind==='skill'||event.kind==='np')return [event.actor];
  if(event.kind==='card')return [scene.hand?.find(c=>c.index===event.card)?.slot].filter(Boolean);
  if(event.kind==='attack')return [...new Set((scene.selected_cards||scene.selected_nps?.map(actor=>({kind:'np',actor}))||[])
    .map(c=>c.kind==='np'?c.actor:scene.hand?.find(h=>h.index===c.card)?.slot).filter(Boolean))];
  return [];
}
function buildBattleFeedback(before,after,event,label,history=false) {
  const rows=scene=>scene?.recent_attacks||(scene?.last_attack?[scene.last_attack]:[]);
  const attacks=history?[]:freshBattleRows(rows(before),rows(after));
  const trace=history?[]:freshBattleRows(before?.ai_trace||[],after.ai_trace||[]);
  const hits=attacks.flatMap(a=>(a.damage||[]).map(d=>({...d,side:d.side||(a.kind==='enemy'?'enemy':'player'),wave:a.wave??before?.wave})));
  const players={};
  for(const p of after.players) {
    const old=before?.players.find(row=>row.slot===p.slot);
    if(old)players[p.slot]={before:old,hp:p.hp-old.hp,np:Math.round((p.np-old.np)*100)/100};
  }
  const enemies={};
  if(before?.wave===after.wave)for(const e of after.enemies) {
    const old=before.enemies.find(row=>row.position===e.position&&row.name===e.name);
    if(old)enemies[e.position]={before:old,hp:e.hp-old.hp};
  }
  const actors=new Set(history?[]:battleActors(before,event));
  for(const hit of hits)if(hit.side==='player')actors.add(hit.actor_slot);
  const enemyActors=new Set();
  if(!history)for(const row of [...hits.filter(h=>h.side==='enemy'),...trace,...attacks.filter(a=>a.kind==='enemy')]) {
    if(row.wave!=null&&row.wave!==after.wave)continue;
    const matches=after.enemies.filter(e=>e.name===row.actor);
    const exact=matches.find(e=>e.position===row.actor_slot);
    if(exact||matches.length===1)enemyActors.add((exact||matches[0]).position);
  }
  const skill=event?.kind==='skill'?before?.players.find(p=>p.slot===event.actor)?.skills.find(s=>s.num===event.skill):
    event?.kind==='master'?before?.master_skills.find(s=>s.num===event.skill):null;
  let title=label||'操作已完成';
  if(skill)title=(event.kind==='master'?'御主':`原${event.actor}号位`)+` · ${skill.name}`;
  const enemySkill=trace.filter(row=>row.type==='skill'&&row.activated!==false).at(-1);
  if(enemySkill&&['enemy_step','enemy_turn'].includes(event?.kind))title=`${enemySkill.actor||'场地'} · ${enemySkill.skill||'技能生效'}`;
  const target=event?.target?after.players.find(p=>p.slot===event.target):null;
  const total=hits.reduce((sum,hit)=>sum+Math.max(0,hit.damage||0),0);
  const notes=[];
  if(history)notes.push('状态恢复，不重新执行战斗动作');
  else {
    if(target)notes.push(`目标：原${target.slot}号位 ${target.name}`);
    if(event?.swap)notes.push(`换下原${event.swap[0]}号位 → 换上原${event.swap[1]}号位`);
    if(enemySkill&&!['enemy_step','enemy_turn'].includes(event?.kind))notes.push(`反应：${enemySkill.actor||'场地'} · ${enemySkill.skill||'技能生效'}`);
    if(hits.length)notes.push(`${hits.length}次伤害结算 · 合计 ${total.toLocaleString()}`);
    if(!notes.length&&skill)notes.push('技能已生效');
  }
  return {title,notes,history,kind:history?'history':event?.kind,players,enemies,hits,actors:[...actors],enemyActors:[...enemyActors],
    master:!history&&event?.kind==='master',target:history?null:event?.target,startedAt:Date.now(),duration:2400};
}
function clearBattleFeedback(){battleFeedback=null;}
function showBattleFeedback(before,after,event,label,history=false) {
  battleFeedback=buildBattleFeedback(before,after,event,label,history);
}
function battleChangeText(change) {
  const signed=(value,digits=0)=>(value>0?'+':'−')+Math.abs(value).toLocaleString(undefined,{maximumFractionDigits:digits});
  return [change.hp?`HP ${signed(change.hp)}`:'',change.np?`NP ${signed(change.np,2)}%`:''].filter(Boolean).join(' · ');
}
function decorateBattleUnit(unit,side,slot,scene,popups=true) {
  unit.dataset.side=side;unit.dataset.slot=String(slot);
  const feedback=battleFeedback,elapsed=feedback?Date.now()-feedback.startedAt:Infinity;
  const pending=interactiveBusy?interactivePending?.event:null;
  if(side==='player'&&battleActors(scene,pending).includes(slot))unit.classList.add('fgo-pending-actor');
  if(!feedback||elapsed>=feedback.duration)return;
  unit.style.setProperty('--feedback-delay',`-${elapsed}ms`);
  if((side==='player'?feedback.actors:feedback.enemyActors).includes(slot))unit.classList.add('fgo-current-actor');
  if(side==='player'&&feedback.target===slot)unit.classList.add('fgo-skill-target');
  if(!popups)return;
  const hits=feedback.hits.filter(h=>side==='player'?h.side==='enemy'&&h.target_slot===slot:
    h.side==='player'&&h.wave===scene.wave&&h.target_slot===slot&&scene.enemies.some(e=>e.position===slot&&e.name===h.target));
  if(hits.length) {
    unit.classList.add('fgo-received-hit');
    const pop=manualElement('div','','fgo-damage-pop '+(side==='player'?'received':'dealt'));
    pop.setAttribute('aria-hidden','true');
    pop.append(manualElement('strong',hits.reduce((sum,h)=>sum+Math.max(0,h.damage||0),0).toLocaleString()));
    pop.append(manualElement('small',`${hits.some(h=>h.critical)?'CRITICAL · ':''}${hits.length}次伤害`));unit.append(pop);
  }
  const change=(side==='player'?feedback.players:feedback.enemies)[slot];
  if(change&&(change.hp||change.np)) {
    const chip=manualElement('div',(feedback.history?'恢复 ':'')+battleChangeText(change),'fgo-unit-change'+(change.hp<0?' loss':' gain'));
    chip.setAttribute('aria-hidden','true');
    unit.append(chip);
  }
}
function animateBattleGauge(bar,side,slot,kind) {
  const feedback=battleFeedback,elapsed=feedback?Date.now()-feedback.startedAt:Infinity;
  const change=(side==='player'?feedback?.players:feedback?.enemies)?.[slot];
  if(!change||elapsed>=700||!change[kind]&&(kind!=='hp'||change.before.max_hp===Number(bar.getAttribute('aria-valuemax'))))return bar;
  const old=change.before,max=kind==='np'?100:old.max_hp;
  bar.firstChild.style.setProperty('--gauge-from',Math.min(100,Math.max(0,old[kind]/Math.max(1,max)*100))+'%');
  bar.firstChild.style.setProperty('--gauge-to',bar.firstChild.style.width);
  bar.firstChild.style.setProperty('--gauge-delay',`-${elapsed}ms`);bar.classList.add('fgo-gauge-changing');return bar;
}

function loadBattleVisuals(identity) {
  if(battleVisuals.has(identity))return;
  battleVisuals.set(identity,{});
  fetch('/api/interactive/visuals?session_id='+encodeURIComponent(identity)).then(r=>r.json()).then(data=>{
    if(!data.success)return;
    battleVisuals.set(identity,data);
    if(interactiveTimeline.state().session_id===identity)renderInteractiveBattle(true);
  }).catch(()=>{}); // 素材失败不会中断战斗或写入操作历史。
}
function battleImage(url,className,alt='') {
  const wrap=manualElement('span','','fgo-image '+className);
  if(!url){wrap.classList.add('missing');return wrap;}
  const img=document.createElement('img');img.alt=alt;img.draggable=false;img.decoding='async';
  img.src='/api/battle-asset?url='+encodeURIComponent(url);
  img.onerror=()=>{wrap.classList.add('missing');img.remove();};
  wrap.append(img);return wrap;
}
function battleGauge(value,max,className,label) {
  const bar=manualElement('div','',className);bar.setAttribute('role','meter');
  bar.setAttribute('aria-label',label);bar.setAttribute('aria-valuemin','0');
  bar.setAttribute('aria-valuemax',String(Math.max(1,max)));bar.setAttribute('aria-valuenow',String(Math.min(Math.max(1,max),Math.max(0,value))));
  const fill=manualElement('span');fill.style.width=Math.min(100,Math.max(0,value/Math.max(1,max)*100))+'%';bar.append(fill);return bar;
}
function battleEffects(unit) {
  const effects=unit.buffs||[],detail=manualElement('details','','fgo-effects');
  detail.append(manualElement('summary',`状态效果 ${effects.length}`));
  if(!effects.length)detail.append(manualElement('div','无生效中的效果'));
  effects.forEach(b=>detail.append(manualElement('div',b.name+(b.turns!=null?' · '+(b.turns<0?'持续':b.turns+'回合'):'')+(b.count>=0?' · '+b.count+'次':''))));
  return detail;
}
function battleSkill(skill,owner,icon,scene,master=false) {
  const button=manualButton('',()=>interactivePrompt(master?'master':'skill',master?null:owner,skill.num,skill.name,master&&skill.swap));
  button.className='fgo-skill';button.setAttribute('aria-label',master?`御主技能${skill.num}`:`原${owner}号位技能${skill.num}`);
  button.title=skill.name+(skill.cooldown?' · 冷却 '+skill.cooldown+' 回合':'');
  button.append(battleImage(icon,'fgo-skill-icon'),manualElement('span',String(skill.num),'fgo-skill-number'));
  if(skill.cooldown)button.append(manualElement('strong',String(skill.cooldown),'fgo-cooldown'));
  button.disabled=interactiveBusy||!skill.ready||scene.phase!=='commands'||scene.won||scene.lost;
  return button;
}
function battleCommandCard(player,card,visual,scene,chain) {
  const np=!card,selected=chain.findIndex(s=>np?s.kind==='np'&&s.actor===player.slot:s.kind==='card'&&s.card===card.index);
  const rawType = player?.np_card_type ?? visual?.np_types?.[player?.np_name];
  const npType = {1:'Arts',2:'Buster',3:'Quick',arts:'Arts',buster:'Buster',quick:'Quick'}[String(rawType).toLowerCase()];
  const type=np?('np '+(npType?npType.toLowerCase():'unknown')):card.type.toLowerCase(),name=player?.name||card.name;
  const npLabel = npType ? `${{Arts:'蓝卡',Buster:'红卡',Quick:'绿卡'}[npType]} ${npType} · 宝具` : '宝具 · 色卡未确认';
  const button=manualButton('',()=>interactiveAction(np?{kind:'np',actor:player.slot}:{kind:'card',card:card.index},np?`选择宝具：${name}`:`选择普通卡${card.index}：${name} ${card.type}`));
  button.className='fgo-command '+type+(selected>=0?' selected':'');
  button.setAttribute('aria-label',np?`原${player.slot}号位 ${npLabel} ${player.np_name}`:`手牌${card.index} ${card.type}`);
  button.setAttribute('aria-pressed',String(selected>=0));button.title=np?player.np_name:`${name} · ${card.type} · 暴击 ${card.critical_chance}%`;
  button.append(battleImage(visual.command||visual.status||visual.face,'fgo-card-art'));
  if(np)button.append(manualElement('span',`NP ${player.np.toFixed(0)}% · NOBLE PHANTASM`,'fgo-card-caption'));
  else button.append(manualElement('span',`${card.critical_chance}% CRITICAL`,'fgo-critical'));
  button.append(manualElement('span',np?player.np_name:name,'fgo-card-name'),manualElement('strong',np?npLabel:card.type,'fgo-card-type'));
  if(!np&&!card.can_act)button.append(manualElement('span','行动受限','fgo-card-restricted'));
  if(selected>=0)button.append(manualElement('span',battleNumbers[selected],'fgo-order-badge'));
  button.disabled=interactiveBusy||scene.won||scene.lost||scene.phase!=='cards'||selected>=0||chain.length>=3||(np&&!player.np_ready);
  return button;
}
function renderFgoBattlefield(container,scene,state) {
  loadBattleVisuals(state.session_id);
  const visuals=battleVisuals.get(state.session_id)||{},v2=scene.version>=2,finished=scene.won||scene.lost;
  const chain=v2?(scene.selected_cards||[]):scene.selected_nps.map(actor=>({kind:'np',actor}));
  const front=scene.players.filter(p=>p.position>0&&p.position<=3).sort((a,b)=>a.position-b.position);
  const reserves=scene.players.filter(p=>!(p.position>0&&p.position<=3)).sort((a,b)=>(a.position||99)-(b.position||99));
  const visualFor=p=>visuals.players?.[p.slot]||{};
  const enemyVisual=e=>{
    const art=visuals.enemies?.[scene.wave]?.[e.position];
    return art&&(!art.names?.length||art.names.includes(e.name))?art:{};
  };
  const phase=scene.won?'战斗胜利':scene.lost?'战斗结束':scene.phase==='enemy'?'敌方行动':scene.phase==='cards'?'选择指令卡':'选择技能';
  const field=manualElement('section','','fgo-battle phase-'+scene.phase);field.setAttribute('aria-label','FGO 风格模拟战场');
  field.setAttribute('aria-busy',String(interactiveBusy));
  const top=manualElement('div','','fgo-topbar');
  const heading=manualElement('div','','fgo-location');heading.append(manualElement('span','BATTLE SIMULATOR','fgo-eyebrow'),manualElement('strong',visuals.quest_name||'模拟战场'));
  const counters=manualElement('div','','fgo-counters');counters.append(manualElement('span',`BATTLE  ${scene.wave} / ${visuals.waves||'—'}`),manualElement('span',`TURN  ${scene.turn}`));
  top.append(heading,counters);field.append(top);
  const enemyHud=manualElement('div','','fgo-enemy-hud');
  scene.enemies.forEach(enemy=>{
    const art=enemyVisual(enemy),chosen=scene.enemy_target===enemy.position;
    const box=manualElement('div','','fgo-enemy-status'+(chosen?' targeted':''));
    const target=manualButton('',()=>interactiveAction({kind:'enemy',enemy:enemy.position},`选择敌方${enemy.position}号：${enemy.name}`));
    target.className='fgo-enemy-target';target.disabled=interactiveBusy||finished||!(scene.phase==='commands'||v2&&scene.phase==='cards');
    target.setAttribute('aria-label',`选择敌方${enemy.position}号：${enemy.name}`);target.setAttribute('aria-pressed',String(chosen));
    target.append(manualElement('span',battleClasses[art.class_name]||'ENEMY','fgo-enemy-class'),manualElement('b',enemy.name),manualElement('span',chosen?'TARGET ◇':'◇','fgo-target-label'));
    box.append(target,animateBattleGauge(battleGauge(enemy.hp,enemy.max_hp,'fgo-hp-gauge','敌方'+enemy.position+'号 HP'),'enemy',enemy.position,'hp'),manualElement('div',`HP ${enemy.hp.toLocaleString()} / ${enemy.max_hp.toLocaleString()}`,'fgo-enemy-hp'));
    const charge=manualElement('div','','fgo-charge');charge.setAttribute('aria-label',`蓄力 ${enemy.charge||0}/${enemy.charge_max||0}`);
    for(let i=0;i<(enemy.charge_max||0);i++)charge.append(manualElement('span','◆',i<enemy.charge?'charged':''));
    box.append(charge);enemyHud.append(box);
  });field.append(enemyHud);
  const stage=manualElement('div','','fgo-stage');
  stage.append(manualElement('div','','fgo-horizon'),manualElement('div','','fgo-ground'));
  const enemies=manualElement('div','','fgo-enemy-figures');
  scene.enemies.forEach(enemy=>{
    const art=enemyVisual(enemy),unit=manualElement('div','','fgo-enemy-figure');
    unit.append(battleImage(art.figure||art.face,'fgo-enemy-art'+(art.figure?' has-figure':'')),manualElement('span',enemy.name,'fgo-figure-name'));
    decorateBattleUnit(unit,'enemy',enemy.position,scene);enemies.append(unit);
  });
  const allies=manualElement('div','','fgo-ally-figures');
  front.forEach(player=>{
    const art=visualFor(player),unit=manualElement('div','','fgo-ally-figure');
    unit.append(battleImage(art.figure||art.status||art.face,'fgo-servant-art'+(art.figure?' has-figure':'')),manualElement('span',player.name,'fgo-figure-name'));
    decorateBattleUnit(unit,'player',player.slot,scene);allies.append(unit);
  });
  stage.append(enemies,allies);
  const stageLabel=manualElement('div',interactiveBusy?'正在计算…':phase,'fgo-phase');stageLabel.setAttribute('role','status');stage.append(stageLabel);
  const feedback=battleFeedback,elapsed=feedback?Date.now()-feedback.startedAt:Infinity;
  if(feedback&&elapsed<feedback.duration&&['skill','master','attack','enemy_step','enemy_turn','history'].includes(feedback.kind)) {
    const toast=manualElement('div',feedback.title,'fgo-action-toast'+(feedback.history?' restored':''));
    toast.setAttribute('aria-hidden','true');
    toast.style.setProperty('--feedback-delay',`-${elapsed}ms`);stage.append(toast);
  }
  if(finished)stage.append(manualElement('div',scene.won?'VICTORY':'DEFEAT','fgo-result '+(scene.won?'won':'lost')));
  field.append(stage);
  const master=manualElement('div','','fgo-master');master.append(manualElement('span','MASTER'));
  if(interactiveBusy&&interactivePending?.event?.kind==='master')master.classList.add('fgo-pending-actor');
  if(feedback?.master&&elapsed<feedback.duration){master.classList.add('fgo-current-actor');master.style.setProperty('--feedback-delay',`-${elapsed}ms`);}
  scene.master_skills.forEach(skill=>master.append(battleSkill(skill,null,visuals.master_skills?.[skill.num],scene,true)));field.append(master);
  const bottom=manualElement('div','','fgo-bottom'),party=manualElement('div','','fgo-party-hud');
  front.forEach(player=>{
    const art=visualFor(player),unit=manualElement('div','','fgo-ally-status');
    const skills=manualElement('div','','fgo-skills');player.skills.forEach(skill=>skills.append(battleSkill(skill,player.slot,art.skills?.[skill.num],scene)));
    const identity=manualElement('div','','fgo-servant-identity');identity.append(battleImage(art.face||art.status,'fgo-mini-face'),manualElement('div',`${battleClasses[art.class_name]||'SERVANT'}${art.support?' · SUPPORT':''}\n${player.name}`));
    unit.append(skills,identity,manualElement('div',`HP  ${player.hp.toLocaleString()} / ${player.max_hp.toLocaleString()}`,'fgo-hp-text'),animateBattleGauge(battleGauge(player.hp,player.max_hp,'fgo-hp-gauge',`原${player.slot}号位 HP`),'player',player.slot,'hp'));
    unit.append(manualElement('div',`NP  ${player.np.toFixed(2)}%`,'fgo-np-text'),animateBattleGauge(battleGauge(player.np,100,'fgo-np-gauge'+(player.np>=100?' ready':''),`原${player.slot}号位 NP`),'player',player.slot,'np'));
    unit.append(manualElement('span',`原${player.slot}号位${art.lv?' · Lv.'+art.lv:''}`,'fgo-slot-label'));
    decorateBattleUnit(unit,'player',player.slot,scene,false);party.append(unit);
  });bottom.append(party);
  const attackZone=manualElement('div','','fgo-attack-zone');
  const attack=manualButton('',()=>interactiveAction({kind:'cards'},'Attack · 进入选卡'));attack.className='fgo-attack';
  attack.append(manualElement('strong','Attack'),manualElement('span','进入选卡'));
  attack.disabled=interactiveBusy||finished||scene.phase!=='commands';attack.setAttribute('aria-label','Attack · 进入选卡');
  attackZone.append(attack,manualElement('span',`★ ${Math.floor(scene.stars||0)} CRITICAL`,'fgo-star-count'));bottom.append(attackZone);field.append(bottom);
  container.append(field);

  if(feedback) {
    const receipt=manualElement('div','','fgo-action-receipt'+(feedback.history?' restored':''));receipt.setAttribute('role','status');receipt.setAttribute('aria-live','polite');
    receipt.append(manualElement('b',feedback.title),manualElement('span',feedback.notes.join(' · ')));
    const changes=Object.entries(feedback.players).filter(([,change])=>change.hp||change.np);
    if(changes.length)receipt.append(manualElement('span',changes.map(([slot,change])=>`原${slot}号 ${battleChangeText(change)}`).join(' ｜ '),'fgo-receipt-changes'));
    if(feedback.hits.length) {
      const hits=manualElement('div','','fgo-receipt-hits');
      // 保留逐目标数字，切换波次或角色退场后也不会把伤害贴到新单位上。
      feedback.hits.forEach(h=>hits.append(manualElement('span',`${h.actor||'原'+h.actor_slot+'号位'} → ${h.target}：${h.damage.toLocaleString()}${h.critical?' 暴击':''}`)));
      receipt.append(hits);
    }container.append(receipt);
  }

  if(scene.phase==='cards'&&!finished) {
    const selection=manualElement('section','','fgo-selection');selection.setAttribute('aria-label','指令卡选择');
    const title=manualElement('div','','fgo-selection-header');title.append(manualElement('b','选择指令卡'),manualElement('span',`按顺序选择三张 · ${chain.length} / 3`));selection.append(title);
    const nps=manualElement('div','','fgo-np-hand');front.forEach(p=>nps.append(battleCommandCard(p,null,visualFor(p),scene,chain)));selection.append(nps);
    if(v2) {
      const hand=manualElement('div','','fgo-command-hand');hand.id='interactive-hand';
      scene.hand.forEach(c=>hand.append(battleCommandCard(scene.players.find(p=>p.slot===c.slot),c,visuals.players?.[c.slot]||{},scene,chain)));selection.append(hand);
    }
    const order=manualElement('div','','fgo-chain');
    for(let i=0;i<3;i++) {
      const chosen=chain[i],player=scene.players.find(p=>p.slot===chosen?.actor),card=scene.hand?.find(c=>c.index===chosen?.card);
      order.append(manualElement('span',`${battleNumbers[i]}  ${chosen?(chosen.kind==='np'?(player?.name||chosen.actor+'号位')+' · 宝具':card?.name+' · '+card?.type):'待选择'}`,chosen?'filled':''));
    }selection.append(order);
    const actions=manualElement('div','','fgo-card-actions');
    function control(label,event,disabled=false,primary=false){const b=manualButton(label,()=>interactiveAction(event,label));b.disabled=interactiveBusy||disabled;b.classList.add(primary?'fgo-confirm':'fgo-secondary');actions.append(b);}
    control('返回技能',{kind:'back'});
    if(v2){control('撤销最近选卡',{kind:'unselect'},!chain.length);control('确认三卡攻击',{kind:'attack'},chain.length!==3,true);}
    else {control('任意三卡普攻',{kind:'facecards'},!!chain.length);control('释放所选宝具',{kind:'attack'},!chain.length,true);}
    selection.append(actions);container.append(selection);
  }
  if(scene.phase==='enemy'&&!finished) {
    const actions=manualElement('div','','fgo-enemy-actions');actions.append(manualElement('span','敌方回合 · 可逐步观察技能、攻击和受击 NP'));
    for(const [label,kind] of [['敌方行动一步','enemy_step'],['执行剩余敌方回合','enemy_turn']]) {
      const button=manualButton(label,()=>interactiveAction({kind},label));button.disabled=interactiveBusy;actions.append(button);
    }container.append(actions);
  }
  const reservesBox=manualElement('div','','fgo-reserves');reservesBox.append(manualElement('span','SUB MEMBER','fgo-eyebrow'));
  reserves.forEach(p=>{const unit=manualElement('div','',p.alive===false?'fgo-reserve fallen':'fgo-reserve');unit.append(battleImage(visualFor(p).face,'fgo-reserve-face'),manualElement('span',`${p.alive===false?'退场':'后排'} · 原${p.slot}号 ${p.name}\nHP ${p.hp.toLocaleString()} · NP ${p.np.toFixed(2)}%`));decorateBattleUnit(unit,'player',p.slot,scene);reservesBox.append(unit);});
  container.append(reservesBox);
  const details=manualElement('details','','fgo-battle-details');details.append(manualElement('summary','战场状态与计算记录'));
  details.append(manualElement('p',v2?scene.enemy_policy:'旧录制保留原有己方行动规则；重新进场启用随机手牌与敌方回合。','checkbox-desc'));
  if(v2)details.append(manualElement('p',`第${scene.deck_cycle}轮牌组 · 尚余${scene.deck_remaining}张 · 暴击星 ${Math.floor(scene.stars||0)}`,'manual-row-text'));
  for(const p of scene.players){const row=manualElement('div','','fgo-detail-unit');row.append(manualElement('b',`原${p.slot}号位 ${p.name}`),battleEffects(p));details.append(row);}
  for(const e of scene.enemies){const row=manualElement('div','','fgo-detail-unit');row.append(manualElement('b',`敌方${e.position}号 ${e.name}`),battleEffects(e));details.append(row);}
  for(const attack of (v2?scene.recent_attacks:scene.last_attack?[scene.last_attack]:[])||[]) {
    const table=manualElement('div','','fgo-damage-log manual-row-text');
    table.textContent=`${attack.kind==='enemy'?'敌方行动':'己方攻击'} · T${attack.turn}${attack.action?' · '+attack.action:''}\n`+(attack.damage||[]).map(d=>
      `${d.actor||d.actor_slot+'号位'}${d.is_np?' 宝具':''}${d.critical?' 暴击':''} → ${d.target}：伤害 ${d.damage.toLocaleString()} / HP ${d.hp.toLocaleString()}${d.random_milli!=null?'；倍率 '+(d.random_milli/1000).toFixed(3):''}；${d.side==='enemy'?'受击NP '+(d.def_np_gain||0).toFixed(2):'打击NP回收 '+d.np_gain.toFixed(2)}%；Overkill ${d.overkill_hits??0} hit`).join('\n');details.append(table);
  }
  if(scene.ai_trace?.length) {
    const trace=manualElement('details');trace.id='interactive-ai-trace';trace.append(manualElement('summary','AI 与脚本记录'));
    scene.ai_trace.forEach(row=>trace.append(manualElement('div',`T${row.turn} ${row.timing} · ${row.actor||'场地'} · ${row.node||''} · ${row.skill||row.action||row.type}${row.target?' → '+row.target:''}${row.script_id?' · 脚本 '+row.script_id:''}`,'manual-row-text')));details.append(trace);
  }container.append(details);
}
