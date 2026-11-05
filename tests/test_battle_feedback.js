// 反馈只解释已确认场景，不生成战斗动作，也不改变历史输入。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const context=vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/battlefield.js'),'utf8'),context);
const plain=value=>JSON.parse(JSON.stringify(value));
const feedback=(...args)=>plain(context.buildBattleFeedback(...args));
function scene(){return {wave:1,players:[
  {slot:1,position:1,name:'同名角色',hp:1000,max_hp:1000,np:0,skills:[{num:1,name:'充能'}]},
  {slot:4,position:2,name:'同名角色',hp:2000,max_hp:2000,np:0,skills:[]}],
  enemies:[{position:1,name:'敌人',hp:5000,max_hp:5000}],master_skills:[{num:3,name:'换人'}],
  selected_cards:[{kind:'card',card:2},{kind:'np',actor:1}],hand:[{index:2,slot:4}],
  recent_attacks:[],ai_trace:[]};}
const attack=(turn=1,side='player')=>({kind:side,turn,wave:1,damage:[
  {actor_slot:side==='player'?4:1,side,actor:side==='player'?'同名角色':'敌人',
    target:side==='player'?'敌人':'同名角色',target_slot:side==='player'?1:4,damage:877,critical:false}]});

test('技能提示来自原槽位与技能名称，HP/NP差值不修改任何输入',()=>{
  const before=scene(),after=scene();after.players[1].np=20;
  before.recent_attacks=[attack()];after.recent_attacks=[attack()];
  after.ai_trace=[{type:'skill',wave:1,actor:'敌人',skill:'反应技能',activated:true}];
  const original=JSON.stringify([before,after]);
  const result=feedback(before,after,{kind:'skill',actor:1,skill:1,target:4},'技能');
  assert.equal(result.title,'原1号位 · 充能');assert.deepEqual(result.actors,[1]);
  assert.equal(result.target,4);assert.equal(result.players[4].np,20);
  assert.equal(result.hits.length,0);assert.match(result.notes[0],/原4号位/);
  assert(result.notes.some(note=>note.includes('反应技能')));
  assert.equal(JSON.stringify([before,after]),original);
});
test('滚动的四条攻击记录只显示新伤害，不重放上一回合',()=>{
  const before=scene(),after=scene();
  before.recent_attacks=[attack(1),attack(2),attack(3),attack(4)];
  after.recent_attacks=[attack(2),attack(3),attack(4),attack(5)];
  const result=feedback(before,after,{kind:'attack'},'确认三卡攻击');
  assert.equal(result.hits.length,1);assert.equal(result.hits[0].damage,877);
  assert.deepEqual(result.actors,[4,1]);assert.match(result.notes[0],/877/);
});
test('敌方技能与普攻分开显示，不重复前一步技能或己方伤害',()=>{
  const before=scene(),after=scene();before.recent_attacks=[attack()];
  after.recent_attacks=[attack(),{kind:'enemy',turn:1,wave:1,damage:[],action:'skill1'}];
  after.ai_trace=[{type:'skill',wave:1,actor:'敌人',skill:'领袖气质',activated:true}];
  const skill=feedback(before,after,{kind:'enemy_step'},'敌方行动');
  assert.match(skill.title,/领袖气质/);assert.equal(skill.hits.length,0);assert.deepEqual(skill.enemyActors,[1]);
  const attacked=structuredClone(after);attacked.recent_attacks.push(attack(1,'enemy'));attacked.players[1].hp-=877;attacked.players[1].np+=3.6;
  const result=feedback(after,attacked,{kind:'enemy_step'},'敌方行动');
  assert.equal(result.hits.length,1);assert.equal(result.hits[0].target_slot,4);
  assert.equal(result.players[4].hp,-877);assert.equal(result.players[4].np,3.6);
  assert.doesNotMatch(result.title,/领袖气质/);
});
test('回退与重做只报告状态恢复，不播放历史伤害或高亮旧攻击者',()=>{
  const before=scene(),after=scene();before.players[1].hp=1123;after.recent_attacks=[attack()];
  const result=feedback(before,after,null,'回退到第8步',true);
  assert.equal(result.players[4].hp,877);assert.equal(result.history,true);
  assert.deepEqual(result.hits,[]);assert.deepEqual(result.actors,[]);assert.deepEqual(result.enemyActors,[]);
  assert.match(result.notes[0],/不重新执行/);
});
test('进入下一波不把旧敌人的HP变化应用到新敌人，仍保留结算数字',()=>{
  const before=scene(),after=scene();after.wave=2;after.enemies[0].hp=8000;after.enemies[0].max_hp=8000;
  after.recent_attacks=[attack()];
  const result=feedback(before,after,{kind:'attack'},'攻击');
  assert.deepEqual(result.enemies,{});assert.equal(result.hits[0].wave,1);assert.equal(result.hits[0].damage,877);
});
test('同波替换单位不沿用旧HP；同名敌人缺少槽位时不猜行动位置',()=>{
  const before=scene(),after=scene();after.enemies[0].name='援军';
  after.enemies.push({position:2,name:'援军',hp:1000,max_hp:1000});
  after.ai_trace=[{type:'skill',wave:1,actor:'援军',skill:'支援'}];
  const result=feedback(before,after,{kind:'enemy_step'},'行动');
  assert.deepEqual(result.enemies,{});assert.deepEqual(result.enemyActors,[]);
});
test('御主换人显示原槽位；实际手牌按所属角色高亮',()=>{
  const before=scene(),after=scene();
  const result=feedback(before,after,{kind:'master',skill:3,swap:[1,4]},'御主技能');
  assert.equal(result.master,true);assert.match(result.title,/换人/);assert.match(result.notes[0],/原1号位.*原4号位/);
  assert.deepEqual(plain(context.battleActors(before,{kind:'card',card:2})),[4]);
});
test('旧版宝具录制与首次恢复可显示，超过100%的NP仍保留准确变化',()=>{
  const before=scene(),after=scene();delete before.selected_cards;delete after.selected_cards;
  before.selected_nps=[1];after.selected_nps=[1];before.players[0].np=100;after.players[0].np=200;
  after.last_attack={turn:1,wave:1,damage:attack().damage};delete before.recent_attacks;delete after.recent_attacks;
  const result=feedback(before,after,{kind:'attack'},'攻击');
  assert.equal(result.players[1].np,100);assert.equal(result.hits.length,1);assert.equal(result.hits[0].side,'player');
  assert.deepEqual(feedback(null,after,null,'载入录制',true).players,{});
});
test('HP不变但最大HP改变时，进度条从旧比例过渡到真实新比例',()=>{
  const before=scene(),after=scene();after.players[0].max_hp=2000;
  context.showBattleFeedback(before,after,{kind:'skill',actor:1,skill:1},'技能');
  const styles={},classes=[];
  const bar={getAttribute:()=> '2000',firstChild:{style:{width:'50%',setProperty:(key,value)=>styles[key]=value}},classList:{add:value=>classes.push(value)}};
  context.animateBattleGauge(bar,'player',1,'hp');
  assert.equal(styles['--gauge-from'],'100%');assert.equal(styles['--gauge-to'],'50%');
  assert.deepEqual(classes,['fgo-gauge-changing']);
});
