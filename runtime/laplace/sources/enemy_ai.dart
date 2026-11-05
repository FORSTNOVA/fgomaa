import 'package:chaldea/app/battle/models/battle.dart';
import 'package:chaldea/models/models.dart';
import 'package:chaldea/models/gamedata/individuality.dart';
import 'engine_hooks.dart';
import 'replay_random.dart';

Map<String,dynamic> _map(dynamic value)=>Map<String,dynamic>.from(value as Map);
List<int> _ints(dynamic value)=>((value??[]) as List).cast<int>();

class _Mind {
  int base,thinking,acts=0,turnActs=0,lastId=0;
  _Mind(this.base):thinking=base;
}

class EnemyAI {
  final Map<String,dynamic> data;
  final ReplayRandom choices,damage,chance;
  late BattleData battle;
  final Map<int,_Mind> minds={};
  final Map<String,_Mind> fields={};
  final Map<int,BattleServantData> players={};
  final Map<int,Map<int,BattleServantData>> waveEnemies={};
  final Map<int,int> values={};
  final List<Map<String,dynamic>> trace=[],damageRolls=[];
  final Map<String,List<Map<String,dynamic>>> groups={};
  final Set<String> _stack={},_firstScripts={};
  int _turn=-1,playerSkills=0,masterSkills=0,totalSkills=0,totalMaster=0,totalNPs=0;
  int _effects=0;String _timing='';String? outcome;
  EnemyAI(this.data,int seed):choices=ReplayRandom(seed ^ 0x3c6ef372),damage=ReplayRandom(seed ^ 0xa54ff53a),chance=ReplayRandom(seed ^ 0x510e527f) {
    for(final item in (data['collections'] as Map).entries) {
      final kind=item.key.toString().split(':').first,c=_map(item.value);
      for(final raw in [...c['mainAis'] as List,...(c['relatedAis']??[]) as List]) {
        final row=_map(raw),key='$kind:${row['id']}';
        if((row['probability'] as int)>0) {
          if(kind=='field'&&!const [-6,1,2,3,4,5,6,9].contains(row['timing']))fail('未支持场地触发时机 ${row['timing']}',row);
          if(kind=='svt'&&(row['actNumInt'] as int)<0&&!const [-1,-2,-3,-4,-5,-6,-7,-8,-9,-10,-11,-12,-13,-14,-15,-16,-17,-18,-19,-201,-401,-501,-1401,-1901].contains(row['actNumInt']))fail('未支持从者AI触发时机 ${row['actNumInt']}',row);
        }
        final rows=groups.putIfAbsent(key,()=>[]);
        if(!rows.any((r)=>r['idx']==row['idx']))rows.add(row);
      }
    }
  }
  Never fail(String message,[Map<String,dynamic>? row])=>throw StateError('AI${row==null?'':'[${row['id']}:${row['idx']}]'} $message');
  void log(String type,{BattleServantData? actor,Map<String,dynamic>? row,Map<String,dynamic> extra=const {}}) {
    if(++_effects>2048)fail('行动/脚本超过2048步，可能存在循环',row);
    trace.add({'type':type,'wave':battle.waveCount,'turn':battle.totalTurnCount,'timing':_timing,
      if(actor!=null)'actor':actor.lBattleName,if(row!=null)'node':'${row['id']}:${row['idx']}',...extra});
  }
  void bind(BattleData b) {
    battle=b;
    final hooks=BattleEngineHooks()
      ..damageRoll=(){
        final min=ConstData.constants.attackRateRandomMin,max=ConstData.constants.attackRateRandomMax;
        if(min<0 || max<=min)fail('伤害随机常量无效');
        final value=min+damage.nextInt(max-min);
        damageRolls.add({'turn':b.totalTurnCount,'wave':b.waveCount,'value':value});return value;
      }
      ..probability=((int rate)=>rate>=1000 || (rate>0 && chance.nextInt(1000)<rate))
      ..reaction=react
      ..battleValue=(add,ids,value){
        if(ids.isEmpty)fail('战斗变量没有ID');
        for(final id in ids)values[id]=(add?(values[id]??0):0)+value;
        log('battle_value',extra:{'ids':ids,'value':value,'add':add});
      };
    BattleEngineHooks.bind(b,hooks);
  }
  void sync() {
    for(final s in [...battle.nonnullPlayers,...battle.nonnullBackupPlayers])players[s.startingPosition]=s;
    for(final s in battle.nonnullEnemies)waveEnemies.putIfAbsent(battle.waveCount,()=>{})[s.uniqueId]=s;
    if(_turn!=battle.totalTurnCount) {
      _turn=battle.totalTurnCount;playerSkills=0;masterSkills=0;
      for(final mind in minds.values)mind.turnActs=0;
    }
  }
  _Mind mind(BattleServantData actor) {
    final id=actor.niceEnemy?.ai?.aiId??0;
    final m=minds.putIfAbsent(actor.uniqueId,()=>_Mind(id));
    if(m.base!=id){m.base=id;m.thinking=id;}
    return m;
  }
  bool traits(List<int> actual,List<int> expected)=>Individuality.checkSignedIndivPartialMatch(self:actual,signedTarget:expected);
  List<BattleServantData> side(BattleServantData? actor,bool allies,{bool all=false}) {
    final player=actor?.isPlayer??false;
    return (allies==player) ? [...battle.nonnullPlayers,if(all)...battle.nonnullBackupPlayers]
      :[...battle.nonnullEnemies,if(all)...battle.nonnullBackupEnemies];
  }
  bool condition(Map<String,dynamic> row,BattleServantData? actor,_Mind m) {
    final name=row['cond']??'none',v=_ints(row['vals']);
    int val([int i=0]) {if(i>=v.length)fail('条件$name缺少参数',row);return v[i];}
    final allies=side(actor,true),opponents=side(actor,false);
    final turn=_timing=='field:turnStart'?battle.turnCount+1:battle.turnCount;
    final totalTurn=_timing=='field:turnStart'?battle.totalTurnCount+1:battle.totalTurnCount;
    bool higher(num a)=>a>=val();
    bool lower(num a)=>a<=val();
    bool modulo(int number){final n=val();if(n<=0)fail('周期必须大于0',row);return number%n==(v.length>1?v[1]:0);}
    bool result;
    switch(name) {
      case 'none':result=true;
      case 'hpHigher':if(actor==null)fail('HP条件缺少施放者',row);result=actor.hp*1000>=actor.maxHp*val();
      case 'hpLower':if(actor==null)fail('HP条件缺少施放者',row);result=actor.hp*1000<=actor.maxHp*val();
      case 'turn':result=turn==val();
      case 'turnMultiple':result=modulo(turn);
      case 'turnHigher':result=higher(turn);
      case 'turnLower':result=lower(turn);
      case 'fieldturn':case 'totalTurnEqual':result=totalTurn==val();
      case 'fieldturnMultiple':result=modulo(totalTurn);
      case 'totalTurnHigher':result=higher(totalTurn);
      case 'totalTurnLower':result=lower(totalTurn);
      case 'actcount':result=m.acts==val();
      case 'actcountMultiple':result=modulo(m.acts);
      case 'actcountThisturn':result=m.turnActs==val();
      case 'turnAndActcountThisturn':result=turn==val() && m.turnActs==val(1);
      case 'beforeActId':result=m.lastId==val();
      case 'beforeNotActId':result=m.lastId!=val();
      case 'checkSelfNpturn':result=actor?.npLineCount==val();
      case 'checkSelfNpturnHigher':result=higher(actor?.npLineCount??0);
      case 'checkSelfNpturnLower':result=lower(actor?.npLineCount??0);
      case 'checkPtLowerNpturn':case 'checkPtLowerTdturn':result=allies.any((s)=>s.npLineCount<=val());
      case 'checkOpponentHeightNpgauge':result=opponents.any((s)=>s.np>=val());
      case 'checkSelfHigherNpgauge':result=higher(actor?.np??0);
      case 'checkPtHigherNpgauge':result=allies.any((s)=>s.np>=val());
      case 'countAlivePt':result=allies.where((s)=>s.hp>0).length==val();
      case 'countAliveOpponent':result=opponents.where((s)=>s.hp>0).length==val();
      case 'countAlivePtAll':result=side(actor,true,all:true).where((s)=>s.hp>0).length==val();
      case 'countAliveOpponentAll':result=side(actor,false,all:true).where((s)=>s.hp>0).length==val();
      case 'countPtRestHigher':result=higher(side(actor,true,all:true).length);
      case 'countPtRestLower':result=lower(side(actor,true,all:true).length);
      case 'countOpponentRestHigher':result=higher(side(actor,false,all:true).length);
      case 'countOpponentRestLower':result=lower(side(actor,false,all:true).length);
      case 'starHigher':result=higher(battle.criticalStars);
      case 'starLower':result=lower(battle.criticalStars);
      case 'checkPtHpHigher':result=allies.any((s)=>s.hp*1000>=s.maxHp*val());
      case 'checkPtHpLower':result=allies.any((s)=>s.hp*1000<=s.maxHp*val());
      case 'checkOpponentHpHigher':result=opponents.any((s)=>s.hp*1000>=s.maxHp*val());
      case 'checkOpponentHpLower':result=opponents.any((s)=>s.hp*1000<=s.maxHp*val());
      case 'checkSelfIndividuality':result=actor!=null&&traits(actor.getTraits(),v);
      case 'checkPtIndividuality':result=allies.any((s)=>traits(s.getTraits(),v));
      case 'checkOpponentIndividuality':result=opponents.any((s)=>traits(s.getTraits(),v));
      case 'checkPtAllIndividuality':result=allies.isNotEmpty&&allies.every((s)=>traits(s.getTraits(),v));
      case 'checkOpponentAllIndividuality':result=opponents.isNotEmpty&&opponents.every((s)=>traits(s.getTraits(),v));
      case 'checkSelfBuffIndividuality':case 'checkSelfBuffActiveAndPassiveIndividuality':
        result=actor!=null&&traits(actor.getBuffTraits(),v);
      case 'checkSelfNotBuffIndividuality':result=actor!=null&&!traits(actor.getBuffTraits(),v);
      case 'checkPtBuffIndividuality':case 'checkPtBuffActiveAndPassiveIndividuality':result=allies.any((s)=>traits(s.getBuffTraits(),v));
      case 'checkOpponentBuffIndividuality':case 'checkOpponentBuffActiveAndPassiveIndividuality':result=opponents.any((s)=>traits(s.getBuffTraits(),v));
      case 'checkPtAllBuffIndividuality':result=allies.isNotEmpty&&allies.every((s)=>traits(s.getBuffTraits(),v));
      case 'checkOpponentAllBuffIndividuality':result=opponents.isNotEmpty&&opponents.every((s)=>traits(s.getBuffTraits(),v));
      case 'existIndividualityOpponentFront':case 'existIndividualityOpponentCenter':case 'existIndividualityOpponentBack':
        final index=name.endsWith('Front')?0:name.endsWith('Center')?1:2;
        final list=actor?.isPlayer==true?battle.onFieldEnemies:battle.onFieldAllyServants;
        result=index<list.length&&list[index]!=null&&traits(list[index]!.getTraits(),v);
      case 'checkUseSkillThisturn':result=playerSkills>0;
      case 'checkMasterSkillThisturn':result=masterSkills>0;
      case 'countPlayerSkillHigher':result=higher(totalSkills);
      case 'countPlayerSkillLower':result=lower(totalSkills);
      case 'countPlayerSkillEqual':result=totalSkills==val();
      case 'countPlayerSkillHigherIncludeMasterSkill':result=higher(totalSkills+totalMaster);
      case 'countPlayerSkillLowerIncludeMasterSkill':result=lower(totalSkills+totalMaster);
      case 'countPlayerSkillEqualIncludeMasterSkill':result=totalSkills+totalMaster==val();
      case 'countPlayerNpHigher':result=higher(totalNPs);
      case 'countPlayerNpLower':result=lower(totalNPs);
      case 'countPlayerNpEqual':result=totalNPs==val();
      case 'countMasterSkillHigherThisTurn':result=higher(masterSkills);
      case 'countMasterSkillLowerThisTurn':result=lower(masterSkills);
      case 'countMasterSkillEqualThisTurn':result=masterSkills==val();
      case 'checkBattleValueAbove':result=(values[val()]??0)>=val(1);
      case 'checkBattleValueEqual':result=(values[val()]??0)==val(1);
      case 'checkBattleValueNotEqual':result=(values[val()]??0)!=val(1);
      case 'checkBattleValueBelow':result=(values[val()]??0)<=val(1);
      case 'checkBattleValueBetween':case 'checkBattleValueNotBetween':
        final number=values[val()]??0;result=number>=val(1)&&number<=val(2);if(name.endsWith('NotBetween'))result=!result;
      case 'commonReleaseId':
        final list=(data['releases'] as Map)['${val()}'];if(list==null)fail('缺少commonRelease ${val()}',row);
        final releases=(list as List).map((r)=>CommonRelease.fromJson(_map(r))).toList();
        final answer=CommonRelease.check(releases,(r){
          if(r.condType.name=='none')return true;
          if(r.condType.name=='questClear'||r.condType.name=='questClearPhase') {
            final record=(data['quest_records'] as Map)['${r.condId}'];
            if(record==null&&data['quest_records_complete']!=true)fail('账号缺少解锁条件记录',row);
            return record!=null && ((record['clearNum']??0)>0 || (record['questPhase']??0)>r.condNum);
          }
          fail('未支持解锁条件 ${r.condType.name}',row);
        });
        if(answer==null)fail('解锁条件无法确认',row);result=answer;
      default:fail('未支持条件 $name，参数$v',row);
    }
    return row['condNegative']==true ? !result:result;
  }
  BaseSkill? skill(Map<String,dynamic> act,BattleServantData? actor) {
    final type=act['type'];
    if(const ['skill1','skill2','skill3'].contains(type)) {
      if(actor==null)return null;final i=int.parse(type.toString().substring(5))-1;
      return i<actor.skillInfoList.length?actor.skillInfoList[i].skill:null;
    }
    if(act['skill']!=null)return BaseSkill.fromJson(_map(act['skill']));
    return db.gameData.baseSkills[act['skillId']];
  }
  List<int> availableSkills(BattleServantData actor) => [for(int i=0;i<actor.skillInfoList.length;i++)
    if(actor.skillInfoList[i].skill!=null && actor.canAttack() && actor.canUseSkillIgnoreCoolDown(battle,i) &&
      BattleSkillInfoData.checkSkillScript(battle,actor,actor.skillInfoList[i].skill!.script,actor.skillInfoList[i].skillLv))i];
  bool available(Map<String,dynamic> act,BattleServantData? actor,bool reactive) {
    final type=act['type'];
    if(type=='skillRandom')return actor!=null&&availableSkills(actor).isNotEmpty;
    if(const ['skill1','skill2','skill3','skillId','skillIdCheckbuff','gimmickSkill'].contains(type)) {
      if(skill(act,actor)==null)return false;
      return reactive || actor==null || actor.canAttack()&&!actor.hasBuffNoProbabilityCheck(BuffAction.donotSkill);
    }
    if(type=='noblePhantasm')return actor!=null&&actor.canNP();
    return true;
  }
  Map<String,dynamic>? choose(String kind,_Mind m,BattleServantData? actor,bool reactive,List<int> timings,{int? fieldTiming}) {
    final rows=groups['$kind:${m.thinking}'];if(rows==null)fail('缺少$kind AI集合 ${m.thinking}');
    final candidates=<Map<String,dynamic>>[];
    for(final row in rows) {
      final n=row['actNumInt'] as int;
      if(fieldTiming!=null) {if(row['timing']!=fieldTiming)continue;}
      else if(reactive) {if(!timings.contains(n))continue;}
      else if(n!= -1 && n!=0 && n!=m.turnActs+1 && !timings.contains(n))continue;
      if((row['probability'] as int)<=0)continue;
      if(condition(row,actor,m) && available(_map(row['aiAct']),actor,reactive))candidates.add(row);
    }
    if(candidates.isEmpty)return null;
    final priority=candidates.map((r)=>r['priority'] as int).reduce((a,b)=>a>b?a:b);
    final tier=candidates.where((r)=>r['priority']==priority).toList()..sort((a,b)=>(a['idx'] as int).compareTo(b['idx'] as int));
    final total=tier.fold<int>(0,(v,r)=>v+(r['probability'] as int));
    int roll=choices.nextInt(total);
    for(final row in tier){roll-=row['probability'] as int;if(roll<0)return row;}return tier.last;
  }
  void advance(_Mind m,Map<String,dynamic> row) {
    final a=_ints(row['avals']);
    if(a.length>1&&a[1]!=0)fail('未支持的AI跳转标志 ${a[1]}',row);
    if(a.isNotEmpty&&a.first>0)m.thinking=a.first;
  }
  Future<Map<String,dynamic>?> decide(BattleServantData actor) async {
    sync();final m=mind(actor);if(m.base==0)return null;
    if(actor.isNpFull()&&!actor.isNPSealed()&&(actor.getCurrentNP()==null||actor.getCurrentNP()!.functions.isEmpty))fail('敌方蓄力满，但没有完整宝具数据');
    for(int hop=0;hop<64;hop++) {
      Map<String,dynamic>? row;
      if(actor.canNP()) {
        row=choose('svt',m,actor,true,[-9]);
        row??={'id':0,'idx':0,'aiAct':{'id':0,'type':'noblePhantasm','target':'random'}};
      }
      row??=choose('svt',m,actor,false,[]);
      if(row==null)return actor.canNP()?{'id':0,'idx':0,'aiAct':{'id':0,'type':'noblePhantasm','target':'random'}}:null;
      if(row['aiAct']['type']!='changeThinking')return row;
      final before=m.thinking;advance(m,row);log('thinking',actor:actor,row:row,extra:{'to':m.thinking});
      if(before==m.thinking)fail('changeThinking没有目标',row);
    }
    fail('AI跳转超过64层，可能存在循环');
  }
  BattleServantData? target(Map<String,dynamic> act,BattleServantData? actor,List<BattleServantData> pool) {
    var candidates=pool.where((s)=>s.hp>0).toList();
    final individuality=_ints(act['targetIndividuality']);
    if(individuality.isNotEmpty)candidates=candidates.where((s)=>traits(s.getTraits(),individuality)).toList();
    if(candidates.isEmpty)return null;
    final mode=act['target']??'random';
    if(mode=='revenge')return candidates.where((s)=>s.uniqueId==actor?.lastHitBy?.uniqueId).firstOrNull??candidates[choices.nextInt(candidates.length)];
    if(const ['front','center','back'].contains(mode)) {
      final index=mode=='front'?0:mode=='center'?1:2;
      return candidates.where((s)=>s.fieldIndex==index).firstOrNull;
    }
    if(mode=='random'||mode=='none')return candidates[choices.nextInt(candidates.length)];
    num score(BattleServantData s)=>mode=='npturnLower'?s.npLineCount:mode=='npgaugeHigher'?s.np:s.hp/s.maxHp;
    if(const ['hpHigher','hpLower','npturnLower','npgaugeHigher'].contains(mode)) {
      candidates.sort((a,b)=>score(a).compareTo(score(b)));
      if(mode=='hpHigher'||mode=='npgaugeHigher')candidates=candidates.reversed.toList();
      final best=score(candidates.first),ties=candidates.where((s)=>score(s)==best).toList();return ties[choices.nextInt(ties.length)];
    }
    if(mode=='individualityActive')return candidates[choices.nextInt(candidates.length)];
    fail('未支持目标规则 $mode');
  }
  Future<CombatAction?> execute(Map<String,dynamic> row,BattleServantData? actor,{bool reactive=false}) async {
    final act=_map(row['aiAct']),type=act['type'] as String;
    final parameters=_map(act['script']??{});
    if(parameters.isNotEmpty && type!='battleScript' && !const ['message','messageGroup','playMotion','loadMovie','playMovie'].contains(type))fail('未支持AI行动参数 ${parameters.keys.join(',')}',row);
    if(type=='skillIdCheckbuff')fail('skillIdCheckbuff的额外检查尚未支持',row);
    if(type=='changeThinking') {if(actor==null)fail('脚本changeThinking缺少思考对象',row);advance(mind(actor),row);return null;}
    if(type=='battleScript') {final id=act['script']?['battleScriptId'];if(id is! int||id<=0)fail('战斗脚本没有ID',row);await script(id,actor);return null;}
    if(const ['none','message','messageGroup','playMotion','overwriteFieldMotion','overwriteAddUniqueCamera','loadMovie','playMovie','waitFinishMovie'].contains(type)) {
      log('presentation',actor:actor,row:row,extra:{'action':type});return null;
    }
    if(const ['battleEnd','allBattleEnd','battleEndNotRelatedSurvivalStatus','battleEndNotRelatedSurvivalStatusInstantly','loseEnd'].contains(type)) {
      outcome=type=='loseEnd'?'lost':'won';
      if(outcome=='won'){battle.onFieldEnemies.fillRange(0,battle.onFieldEnemies.length,null);battle.backupEnemies.fillRange(0,battle.backupEnemies.length,null);battle.curStage=null;battle.waveCount=battle.niceQuest!.stages.map((s)=>s.wave).reduce((a,b)=>a>b?a:b);}
      else {battle.onFieldAllyServants.fillRange(0,battle.onFieldAllyServants.length,null);battle.backupAllyServants.fillRange(0,battle.backupAllyServants.length,null);}
      log('outcome',actor:actor,row:row,extra:{'result':outcome});return null;
    }
    if(type=='skillRandom') {
      if(actor==null)fail('随机技能缺少施放者',row);final options=availableSkills(actor);
      if(options.isEmpty)fail('随机技能没有可用技能',row);
      final chosen=options[choices.nextInt(options.length)];
      return execute({...row,'aiAct':{...act,'type':'skill${chosen+1}'}},actor,reactive:reactive);
    }
    if(const ['skill1','skill2','skill3','skillId','skillIdCheckbuff','gimmickSkill'].contains(type)) {
      final model=skill(act,actor);if(model==null||model.functions.isEmpty)fail('AI技能数据不完整',row);
      final ally=target(act,actor,side(actor,true)),opponent=target(act,actor,side(actor,false));
      final oldPlayer=battle.playerTargetIndex,oldEnemy=battle.enemyTargetIndex;
      final isPlayer=actor?.isPlayer??false;
      if(ally!=null){if(isPlayer)battle.playerTargetIndex=ally.fieldIndex;else battle.enemyTargetIndex=ally.fieldIndex;}
      if(opponent!=null){if(isPlayer)battle.enemyTargetIndex=opponent.fieldIndex;else battle.playerTargetIndex=opponent.fieldIndex;}
      final level=(act['skillLv'] as int?)??(type.startsWith('skill')&&type.length==6&&actor!=null?actor.skillInfoList[int.parse(type.substring(5))-1].skillLv:1);
      final info=BattleSkillInfoData(model,type:actor==null?SkillInfoType.fieldAi:SkillInfoType.svtAi,skillLv:level);
      try {
        final activated=await info.activate(battle,activator:actor,defaultToPlayer:isPlayer,preferredEnemy:opponent);
        log('skill',actor:actor,row:row,extra:{'skill_id':model.id,'skill':model.name,'target':opponent?.lBattleName,'activated':activated});
      } finally {battle.playerTargetIndex=oldPlayer;battle.enemyTargetIndex=oldEnemy;battle.updateTargetedIndex();}
      return null;
    }
    if(!const ['attack','attackA','attackB','attackQ','attackACritical','attackBCritical','attackQCritical','attackCritical','noblePhantasm'].contains(type))fail('未支持行动 $type',row);
    if(reactive)fail('反应时机中的攻击行动尚未支持',row);
    if(actor==null)fail('攻击缺少施放者',row);
    final candidates=side(actor,false),taunts=candidates.where((s)=>s.hasBuffNoProbabilityCheck(BuffAction.hate)).toList();
    final victim=target(act,actor,taunts.isEmpty?candidates:taunts);if(victim==null)fail('AI攻击没有合法目标',row);
    battle.enemyTargetIndex=actor.fieldIndex;battle.playerTargetIndex=victim.fieldIndex;
    CommandCardData card;
    if(type=='noblePhantasm') {
      final model=actor.getNPCard();if(model==null||!actor.canNP()||actor.getCurrentNP()!.functions.isEmpty)fail('AI宝具不可用或数据不完整',row);
      if(act['noblePhantasmId']!=null&&act['noblePhantasmId']!=actor.getCurrentNP()!.id)fail('AI指定宝具版本尚未匹配',row);
      if(act['noblePhantasmLv']!=null&&act['noblePhantasmLv']!=actor.tdLv)fail('AI指定宝具等级尚未匹配',row);
      if(act['noblePhantasmOc']!=null&&act['noblePhantasmOc']!=10000)fail('AI指定OC尚未支持',row);
      card=model;
    } else {
      final svt=actor.niceSvt;if(svt==null)fail('缺少敌方攻击卡型',row);
      int? requested=type.contains('attackA')?CardType.arts.value:type.contains('attackB')?CardType.buster.value:type.contains('attackQ')?CardType.quick.value:null;
      final crit=type.endsWith('Critical') || choices.nextInt(1000)<(actor.niceEnemy!.criticalRate+await actor.getBuffValue(battle,BuffAction.criticalPoint)).clamp(0,1000);
      final int color;
      if(requested!=null){if(!svt.cardDetails.containsKey(requested))fail('指定卡型不存在',row);color=requested;}
      else if(svt.cardDetails.containsKey(CardType.weak.value))color=crit&&svt.cardDetails.containsKey(CardType.strength.value)?CardType.strength.value:CardType.weak.value;
      else {final colors=svt.cards.where((c)=>svt.cardDetails.containsKey(c)&&!CardType.isExtra(c)).toList();if(colors.isEmpty)fail('没有普通攻击卡',row);color=colors[choices.nextInt(colors.length)];}
      card=CommandCardData(svtId:actor.svtId,svtLimit:actor.limitCount,uniqueId:actor.uniqueId,cardType:color,
        cardDetail:svt.cardDetails[color]!,cardIndex:0,critical:crit,traits:ConstData.getCardInfo(color,1).individuality.toList());
    }
    log('attack',actor:actor,row:row,extra:{'target':victim.lBattleName,'is_np':card.isTD,'critical':card.critical});
    return CombatAction(actor,card);
  }
  Future<void> script(int id,BattleServantData? actor) async {
    final key='script:$id';if(!_stack.add(key)||_stack.length>32)fail('脚本循环或嵌套超过32层：$id');
    try {
      final raw=(data['scripts'] as Map)['$id'];if(raw==null)fail('缺少战斗脚本$id');
      final rows=(raw as List).map(_map).toList()..sort((a,b){final v=(a['playOrder']??0).compareTo(b['playOrder']??0);return v!=0?v:(a['idx']??0).compareTo(b['idx']??0);});
      for(final row in rows) {
        final detail=_map(row['script']??{}),first='$id:${row['idx']}:${actor?.uniqueId??0}';
        if(detail['isOnlyFirst']==1&&!_firstScripts.add(first))continue;
        final action=row['battleScriptAction'];log('script',actor:actor,extra:{'script_id':id,'idx':row['idx'],'action':action});
        if(const ['wait','cutIn','moveCamera','message','playVoice','normalSpeed','resumeSpeed'].contains(action))continue;
        if(action!='aiAct')fail('未支持战斗脚本命令 $action，脚本$id');
        if(detail['aiScript']!=null && (detail['aiScript'] as Map).isNotEmpty)fail('脚本$id含未支持aiScript参数');
        final actId=detail['aiActId'],definition=(data['acts'] as Map)['$actId'];if(definition==null)fail('脚本$id缺少aiAct $actId');
        final type=detail['type']??0;List<BattleServantData?> sources;
        if(type==0)sources=[actor];else if(type==1)sources=[null];
        else if(type==2)sources=battle.nonnullEnemies;
        else if(type==3)sources=battle.nonnullPlayers;
        else fail('脚本$id含未支持施放者类型$type');
        final trait=detail['individuality'];
        if(trait is int && trait!=0)sources=sources.where((s)=>s!=null&&traits(s.getTraits(),[trait])).toList();
        for(final source in sources)await execute({'id':id,'idx':row['idx'],'aiAct':definition},source,reactive:true);
      }
    } finally {_stack.remove(key);}
  }
  Future<void> reactionGroup(String kind,_Mind m,BattleServantData? actor,List<int> timings,{int? fieldTiming}) async {
    if(m.base==0)return;
    for(int hop=0;hop<64;hop++) {
      final row=choose(kind,m,actor,true,timings,fieldTiming:fieldTiming);if(row==null)return;
      final old=m.thinking;
      if(row['aiAct']['type']!='changeThinking'){await execute(row,actor,reactive:true);advance(m,row);return;}
      advance(m,row);
      log('thinking',actor:actor,row:row,extra:{'to':m.thinking});if(old==m.thinking)fail('反应AI跳转没有目标',row);
    }
    fail('反应AI跳转超过64层');
  }
  Future<void> react(String timing,BattleServantData? subject) async {
    if(outcome!=null)return;
    sync();final previous=_timing;_timing=timing;
    final key='$timing:${subject?.uniqueId??0}';if(!_stack.add(key)||_stack.length>32)fail('AI反应递归循环：$timing');
    try {
      if(timing=='player_skill'){playerSkills++;totalSkills++;}
      if(timing=='master_skill'){masterSkills++;totalMaster++;}
      if(timing=='player_np')totalNPs++;
      final fieldTiming={'field:waveStart':1,'field:turnStart':2,'player_start':3,'player_end':4,'enemy_start':5,'enemy_end':6,'field:beforeWaveStartAnimation':9,'dead':-6}[timing];
      if(fieldTiming!=null)for(final field in battle.curStage?.fieldAis??<FieldAi>[]) {
        final m=fields.putIfAbsent('${battle.waveCount}:${field.id}',()=>_Mind(field.id));
        await reactionGroup('field',m,null,[],fieldTiming:fieldTiming);
      }
      final timings={
        'wave_start':[-8],'turn_start':[-12],'player_action_start':[-13],
        'player_skill':[-3],'master_skill':[-3],'player_action_end':[-7],
        'combo_end':[-201,-2],'enemy_start':[-401,-4],'enemy_end':[-501,-5],
        'entry':[-1401,-14],'dead':[-6],'before_dead':[-16],
        'before_resurrection':[-15],'shift_before':[-1901,-19],'shift_after':[-17,-10],
        'before_wave':[-18],'np_target':[-11],
      }[timing];
      if(timings==null)return;
      final specific=const ['wave_start','turn_start','player_action_start','shift_before','shift_after','before_dead','dead','before_resurrection','np_target'].contains(timing);
      final actors=timing=='before_wave'?(waveEnemies[battle.waveCount]?.values.toList()??<BattleServantData>[]):
        specific ? (subject?.isEnemy==true?[subject!]:<BattleServantData>[]) : [...battle.nonnullEnemies];
      for(final actor in actors) {
        if(outcome!=null)break;
        final m=mind(actor);
        // Priority reactions precede their ordinary counterpart.
        for(final n in timings)await reactionGroup('svt',m,actor,[n]);
      }
    } finally {_stack.remove(key);_timing=previous;}
  }
  void committed(BattleServantData actor,Map<String,dynamic>? row) {
    final m=mind(actor);m.acts++;m.turnActs++;if(row!=null){m.lastId=row['aiAct']['id']??0;advance(m,row);}
  }
}
