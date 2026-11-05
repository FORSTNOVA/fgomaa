const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('万圣节 90++ 在默认模拟列表可见，可用截图标题、等级或 ID 搜索', () => {
  const choice = {value:'', options:[], replaceChildren() { this.options = []; }, add(option) { this.options.push(option); }};
  const elements = {'battle-search':{value:''}, 'battle-kind':{value:'all'},
    'battle-choice':choice, 'battle-target':{}, 'battle-mode':{value:'auto'}};
  const target = {quest_id:'atlas_cn_94150109', game_id:94150109, phase:1, kind:'event',
    name:'Ｍ星之旅 对决！ 宇宙级艺术家！', war:'完结南瓜行星万圣节星球', spot:'第二仓储库',
    recommend_lv:'90++', navigation_supported:true, simulation_supported:true, supported:false};
  const context = vm.createContext({document:{getElementById:id=>elements[id]},
    Option:class { constructor(label,value) { Object.assign(this,{label,value}); } },
    quests:[...Array.from({length:301},(_,i)=>({quest_id:`daily_${i}`, kind:'daily',
      recommend_lv:'90+', simulation_supported:true, supported:true})), target]});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/battle.js'),'utf8'),context);
  vm.runInContext('battleCatalog = quests',context);
  for (const search of ['', 'M星之旅 对决! 宇宙级艺术家!', '等级９０＋＋', '94150109']) {
    elements['battle-search'].value = search;
    context.filterBattleChoices();
    if (search) assert.equal(choice.options.length,1);
    assert.match(choice.options.find(option=>option.value===target.quest_id).label,/Lv90\+\+/);
  }

  const html = fs.readFileSync(path.join(__dirname,'../web/index.html'),'utf8');
  assert.doesNotMatch(html.match(/<input[^>]+id="quest-supported-only"[^>]+>/)[0], /\schecked\b/);
  Object.assign(elements, {'quest-search':{value:'90++'}, 'quest-kind':{value:'event'},
    'quest-supported-only':{checked:false}, 'quest-choice':choice, 'quest-matches':{}});
  target.reason = '关卡未开放或已经结束';
  vm.runInContext(`const questCatalog = quests; ${html.slice(html.indexOf('function filterQuestChoices()'), html.indexOf('function addQuestToQueue()'))}`, context);
  context.filterQuestChoices();
  assert.equal(choice.options.length,1);
  assert.equal(choice.options[0].value,target.quest_id);
  assert.equal(choice.options[0].disabled,true);
  assert.match(choice.options[0].label,/关卡未开放或已经结束/);
});
