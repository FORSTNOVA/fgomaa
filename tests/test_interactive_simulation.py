import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tests.test_manual_simulation import manual_fixture
from src.strategy.interactive_simulation import (InteractiveSimulation,InteractiveEvent,InteractiveRecording,SetupDraft,compile_events)


class TestInteractiveModel(unittest.TestCase):
    def test_compilation_tracks_cards_targets_and_turn_boundaries(self):
        events=[{'kind':'skill','actor':2,'skill':1,'target':1},{'kind':'cards'},
                {'kind':'np','actor':1},{'kind':'back'},{'kind':'master','skill':3,'swap':[2,4]},
                {'kind':'enemy','enemy':2},{'kind':'cards'},{'kind':'np','actor':1},{'kind':'attack'}]
        result=compile_events(events)
        self.assertEqual(result['interactive_turn'],2);self.assertEqual(result['interactive_phase'],'commands')
        self.assertEqual([a['kind'] for a in result['manual_turns'][0]['actions']],['skill','master','np'])
        self.assertEqual(result['manual_turns'][0]['actions'][-1]['enemy'],2)
        self.assertEqual(result['selected_nps'],[])

    def test_illegal_order_duplicates_and_spoofed_fields_rejected(self):
        for events in [[{'kind':'np','actor':1}], [{'kind':'attack'}],
            [{'kind':'cards'},{'kind':'skill','actor':1,'skill':1}],
            [{'kind':'cards'},{'kind':'np','actor':1},{'kind':'np','actor':1}],
            [{'kind':'cards'},{'kind':'np','actor':1},{'kind':'facecards'}]]:
            with self.assertRaises(ValueError):compile_events(events)
        for value in [{'kind':'skill','actor':True,'skill':1},{'kind':'master','skill':3,'swap':[2,7]},
                      {'kind':'np','actor':1,'enemy':2},{'kind':'cards','actor':1}]:
            with self.assertRaises(ValueError):InteractiveEvent.model_validate(value)

    def test_draft_allows_initial_empty_slots_but_enter_does_not(self):
        draft=SetupDraft(quest_id='fuyuki_x_a',party=[{'source':'owned','svt_id':0}]*5+
                         [{'source':'support','svt_id':0,'support':{}}])
        with self.assertRaises(ValueError):draft.ready_plan()

    def test_recordings_require_enter_and_valid_history_position(self):
        parser,plan=manual_fixture();draft={k:v for k,v in plan.items() if k!='turns'}
        root={'kind':'setup','label':'setup','draft':draft}
        for recording in [{'name':'x','entries':[root],'cursor':1},
                          {'name':'x','version':2,'entries':[root],'cursor':0},
                          {'name':'x','entries':[root,{'kind':'event','label':'skill','event':{'kind':'cards'}}],'cursor':0}]:
            with self.assertRaises(ValueError):InteractiveRecording.model_validate(recording)


class TestInteractiveNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.parser,plan=manual_fixture()
        cls.draft={k:v for k,v in plan.items() if k!='turns'}
        cls.engine=InteractiveSimulation(root=cls.tmp.name)
        cls.start=cls.engine.enter(cls.draft,cls.parser,version=1);cls.sid=cls.start['session_id']

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def replay(self,events):return self.engine.replay(self.sid,events)
    def player(self,result,slot):return next(p for p in result['scene']['players'] if p['slot']==slot)

    def test_skill_immediately_updates_np_cooldown_and_can_be_undone(self):
        initial=self.replay([])
        changed=self.replay([{'kind':'skill','actor':2,'skill':2,'target':3}])
        self.assertTrue(changed['scene']['accepted'])
        self.assertEqual(self.player(changed,3)['np'],20)
        self.assertGreater(self.player(changed,2)['skills'][1]['cooldown'],0)
        restored=self.replay([])
        self.assertEqual(restored['scene'],initial['scene'])
        self.assertEqual(self.player(restored,3)['np'],0)

    def test_attack_damage_refund_and_redo_are_deterministic(self):
        events=[{'kind':'cards'},{'kind':'np','actor':1},{'kind':'attack'}]
        result=self.replay(events)
        self.assertTrue(result['scene']['accepted']);self.assertEqual(result['scene']['wave'],2)
        self.assertGreater(result['scene']['last_attack']['damage'][0]['damage'],0)
        self.assertIn('np_gain',result['scene']['last_attack']['damage'][0])
        self.assertIn('overkill_hits',result['scene']['last_attack']['damage'][0])
        before=self.replay(events[:2])
        self.assertEqual(before['scene']['wave'],1);self.assertEqual(before['scene']['selected_nps'],[1])
        self.assertEqual(self.player(before,1)['np'],100)
        self.assertEqual(self.replay(events)['scene'],result['scene'])

    def test_bad_skill_and_unready_np_do_not_authorize_a_step(self):
        for events in [[{'kind':'skill','actor':4,'skill':1}],
                       [{'kind':'cards'},{'kind':'np','actor':2}],
                       [{'kind':'skill','actor':2,'skill':1},{'kind':'skill','actor':2,'skill':1}]]:
            self.assertFalse(self.replay(events)['scene']['accepted'])
        self.assertEqual(self.replay([])['scene'],self.start['scene'])

    def test_order_change_preserves_original_actor_identity(self):
        events=[{'kind':'master','skill':3,'swap':[2,4]}]
        swapped=self.replay(events)
        self.assertEqual(self.player(swapped,4)['position'],2)
        self.assertEqual(self.player(swapped,2)['position'],4)
        events.append({'kind':'skill','actor':4,'skill':1})
        self.assertTrue(self.replay(events)['scene']['accepted'])
        self.assertEqual(self.player(self.replay(events[:1]),4)['skills'][0]['cooldown'],0)

    def test_enter_snapshot_is_frozen_against_later_account_imports(self):
        before=self.replay([])
        old=self.parser.servants[100100].lv
        try:
            self.parser.servants[100100].lv=1
            self.assertEqual(self.replay([])['scene'],before['scene'])
        finally:self.parser.servants[100100].lv=old

    def test_saved_history_keeps_undo_redo_and_bound_setup(self):
        draft=SetupDraft.model_validate(self.draft).model_dump()
        entries=[{'kind':'setup','label':'编队','draft':draft},
                 {'kind':'enter','label':'进场','draft':draft,'session_id':self.sid,'scene':self.start['scene']},
                 {'kind':'event','label':'Attack','event':{'kind':'cards'},'scene':self.replay([{'kind':'cards'}])['scene']}]
        snapshot_path=self.engine.root/'sessions'/(self.sid+'.json')
        original=snapshot_path.read_bytes()
        legacy=json.loads(original)
        legacy['draft'].pop('mystic_code_lv',None)
        for slot in legacy['draft']['party']:
            slot.pop('owned_stats',None)
            slot.pop('owned_ce',None)
        try:
            self.engine.write(snapshot_path,legacy)
            saved=self.engine.save_recording({'name':'操作录制','entries':entries,'cursor':1})
        finally:
            snapshot_path.write_bytes(original)
        loaded=next(record for record in self.engine.recordings() if record['id']==saved['id'])
        self.assertEqual(loaded['cursor'],1);self.assertEqual(len(loaded['entries']),3)
        self.assertEqual(loaded['entries'][1]['scene'],self.start['scene'])
        bad=copy.deepcopy(entries);bad[1]['draft']['party'][0]['ce_id']=None
        with self.assertRaises(ValueError):self.engine.save_recording({'name':'伪造快照','entries':bad,'cursor':1})
        with self.assertRaises(ValueError):self.engine.replay('../escape',[])

    def test_victory_rejects_additional_actions(self):
        events=[{'kind':'cards'},{'kind':'np','actor':1},{'kind':'attack'},
            {'kind':'skill','actor':2,'skill':1},{'kind':'skill','actor':2,'skill':2,'target':1},
            {'kind':'skill','actor':3,'skill':1},{'kind':'skill','actor':3,'skill':2,'target':1},
            {'kind':'cards'},{'kind':'np','actor':1},{'kind':'attack'}]
        self.assertTrue(self.replay(events)['scene']['won'])
        self.assertFalse(self.replay(events+[{'kind':'cards'}])['scene']['accepted'])

    def test_battle_recording_saves_recognition_from_the_frozen_entry(self):
        enter={'kind':'enter','label':'进场','draft':self.draft,'session_id':self.sid,'scene':self.start['scene']}
        cards={'kind':'event','label':'Attack','event':{'kind':'cards'},'scene':self.replay([{'kind':'cards'}])['scene']}
        request={'name':'战斗录制','version':2,'entries':[enter,cards],'cursor':0}
        saved=self.engine.save_recording(request)
        loaded=next(record for record in self.engine.recordings() if record['id']==saved['id'])
        self.assertEqual(loaded,saved)
        self.assertEqual([entry['kind'] for entry in loaded['entries']],['enter','event'])
        self.assertEqual(loaded['cursor'],0)
        key=loaded['recognition']
        self.assertEqual(key['quest']['catalog_key'],self.draft['quest_id'])
        self.assertEqual((key['quest']['game_id'],key['quest']['phase']),
                         (self.start['metadata']['quest_id'],self.start['metadata']['phase']))
        self.assertEqual([(row['slot'],row['source'],row['svt_id']) for row in key['party']],
                         [(i+1,slot['source'],slot['svt_id']) for i,slot in enumerate(self.draft['party'])])
        self.assertEqual(key['party'][0]['ce']['ce_id'],9400340)
        self.assertEqual(key['mystic_code']['id'],20)
        for illegal_entries in ([enter,enter],[enter,{'kind':'setup','label':'编队','draft':self.draft}]):
            with self.assertRaisesRegex(ValueError,'后续只接受战斗操作'):
                self.engine.save_recording({**request,'entries':illegal_entries})


class TestInteractiveTimeline(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node unavailable')
    def test_cancel_before_post_acknowledgement_discards_completed_result(self):
        script="""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let acknowledge;const requests=[],messages=[];
const ctx={structuredClone,fetch:async(url,options)=>{
  requests.push(url);
  if(url.endsWith('/replay'))return await new Promise(resolve=>{acknowledge=()=>resolve({json:async()=>({success:true,request_id:'request'})});});
  if(url.endsWith('/stop'))return {json:async()=>({success:true,cancelled:false})};
  return {json:async()=>({report:{request_id:'request',client_revision:0,status:'completed',result:{scene:{accepted:true}}}})};
}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync('web/battlefield.js','utf8'),ctx);
vm.runInContext(fs.readFileSync('web/interactive.js','utf8')+`;
renderInteractiveBattle=()=>{};scheduleInteractiveSave=()=>{};
interactiveMessage=text=>messages.push(text);
interactiveTimeline.append({kind:'setup',draft:{party:[]}});
this.count=()=>interactiveTimeline.entries.length;`,Object.assign(ctx,{messages}));
(async()=>{
  vm.runInContext("battleFeedback={title:'old feedback'}",ctx);
  const pending=vm.runInContext("launchInteractive('/api/interactive/replay',{}, {kind:'event',event:{kind:'cards'}})",ctx);
  await vm.runInContext('stopInteractiveCalculation()',ctx);
  acknowledge();await pending;
  assert(requests.includes('/api/interactive/stop'));
  await vm.runInContext('pollInteractiveStatus()',ctx);
  assert.equal(ctx.count(),1);assert(messages.at(-1).includes('已中断'));
  assert.equal(vm.runInContext('battleFeedback',ctx),null);
  console.log('pending cancellation OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    @unittest.skipUnless(shutil.which('node'),'Node unavailable')
    def test_undo_redo_branch_and_snapshots_are_independent(self):
        script="""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const ctx={structuredClone};vm.createContext(ctx);
vm.runInContext(fs.readFileSync('web/interactive.js','utf8')+';this.Timeline=InteractiveTimeline;',ctx);
const t=new ctx.Timeline();t.append({kind:'setup',draft:{party:[1]}});
t.append({kind:'enter',draft:{party:[1]},session_id:'session',scene:{np:0}});
t.append({kind:'event',event:{kind:'skill'},scene:{np:50}});
assert.equal(t.state().scene.np,50);t.move(1);assert.equal(t.state().scene.np,0);
t.move(2);assert.equal(t.state().scene.np,50);
const state=t.state();state.scene.np=999;assert.equal(t.state().scene.np,50);
t.move(1);t.append({kind:'event',event:{kind:'cards'},scene:{np:0}});
assert.equal(t.entries.length,3);assert.equal(t.state().events[0].kind,'cards');
const reload=new ctx.Timeline(t.entries,1);assert.equal(reload.state().scene.np,0);
assert.equal(reload.entries.length,3);console.log('timeline OK');
"""
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
