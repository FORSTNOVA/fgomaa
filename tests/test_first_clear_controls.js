const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');

function form(prefix){
  const elements={};
  for(const [suffix,value] of [['limit','500'],['ap','100'],['apples','1'],['apple','bronze']]){
    elements[prefix+'-'+suffix]={value};
    elements[prefix+'-'+suffix+'-enabled']={checked:true};
    elements[prefix+'-'+suffix+'-off']={hidden:true};
  }
  elements[prefix+'-refill-enabled']={checked:false};
  const context=vm.createContext({document:{getElementById:id=>elements[id]}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../web/battle.js'),'utf8'),context);
  return {elements,context};
}

test('首通数量允许超过30，取消勾选传null，AP与果实限制独立',()=>{
  const {elements,context}=form('first-clear');
  assert.equal(vm.runInContext("resourceOptions('first-clear').max_quests",context),500);
  elements['first-clear-limit-enabled'].checked=false;
  vm.runInContext("updateResourceControls('first-clear')",context);
  assert.equal(elements['first-clear-limit'].disabled,true);
  assert.equal(vm.runInContext("resourceOptions('first-clear').max_quests",context),null);
  assert.equal(vm.runInContext("resourceOptions('first-clear').ap_budget",context),100);
  assert.equal(vm.runInContext("resourceOptions('first-clear').max_apples",context),0);
});

test('首通非法数量拒绝，指定关卡作战保留原有场次范围',()=>{
  const {elements,context}=form('first-clear');
  for(const value of ['0','-1','1.5','9007199254740992']){
    elements['first-clear-limit'].value=value;
    assert.throws(()=>vm.runInContext("resourceOptions('first-clear')",context));
  }
  assert.throws(()=>vm.runInContext("resourceOptions('battle')",form('battle').context));
});
