import 'package:tuple/tuple.dart';
import 'package:chaldea/app/battle/models/battle.dart';
import 'package:chaldea/app/battle/interactions/_delegate.dart';
import 'package:chaldea/app/battle/utils/battle_logger.dart';
import 'package:chaldea/models/models.dart';
import 'simulator.dart';
import 'replay_random.dart';
import 'enemy_ai.dart';

class InteractiveDeck {
  final BattleData battle;
  final ReplayRandom random;
  final int seed;
  List<int> hand=[],remaining=[];
  Set<int> liveSlots={};
  bool pendingRefill=false;
  int turn=-1,cycle=0,deals=0;
  InteractiveDeck(this.battle,this.seed):random=ReplayRandom(seed);
  bool sameSet(Iterable<int> a,Iterable<int> b)=>a.length==b.length && a.toSet().containsAll(b);
  void sync() {
    final alive={for(final s in [...battle.nonnullPlayers,...battle.nonnullBackupPlayers])s.startingPosition};
    pendingRefill=pendingRefill || liveSlots.any((slot)=>!alive.contains(slot));liveSlots=alive;
    if(!battle.isPlayerTurn || battle.isBattleWin || battle.nonnullPlayers.isEmpty)return;
    final pool=<int>[for(final s in battle.nonnullPlayers)
      if(!s.hasBuffNoProbabilityCheck(BuffAction.donotSelectCommandcard))
        for(final c in s.getCards())battle.cardDeckIndex(s,c)];
    final newTurn=turn!=battle.totalTurnCount;
    final refilled=pendingRefill || !sameSet(battle.currentCards,hand) && battle.cardDealt==5;
    if(!newTurn && !refilled)return;
    if(newTurn && hand.isNotEmpty && battle.fixCommandCard() && hand.every(pool.contains)) {
      turn=battle.totalTurnCount;return;
    }
    if(hand.isEmpty || refilled || remaining.isEmpty || remaining.any((id)=>!pool.contains(id))) {
      remaining=pool.toList();random.shuffle(remaining);cycle++;deals=0;
    }
    hand=remaining.take(5).toList();remaining.removeRange(0,hand.length);deals++;
    pendingRefill=false;
    battle.currentCards=hand.toSet();battle.remainingCards=remaining.toSet();battle.cardDealt=deals*5;
    turn=battle.totalTurnCount;
    if(hand.length!=5)throw StateError('无法生成完整五张手牌');
  }
  CombatAction action(int index) {
    if(index<1 || index>hand.length)throw StateError('指令卡不在本回合手牌中');
    final id=hand[index-1],actor=battle.onFieldAllyServants[id~/5];
    if(actor==null)throw StateError('指令卡角色已退场');
    final cards=actor.getCards().where((c)=>c.cardIndex==id%5).toList();
    if(cards.length!=1)throw StateError('指令卡形态与当前手牌不一致');
    return CombatAction(actor,cards.single);
  }
  Future<List<int>> starCounts() async {
    final r=ReplayRandom(seed ^ (turn*0x45d9f3b) ^ cycle);
    final bonus=[50,20,20,0,0];r.shuffle(bonus);
    final weights=<double>[];
    // Evaluate conditional gather buffs on a disposable copy, never the live state.
    battle.pushSnapshot();final view=battle.snapshots.removeLast();
    for(int i=1;i<=hand.length;i++) {
      final a=action(i),actor=view.onFieldAllyServants[hand[i-1]~/5]!;
      final modifier=1000+await actor.getBuffValue(view,BuffAction.starweight,card:a.cardData.copy());
      weights.add((actor.niceSvt!.criticalWeight*(modifier<0 ? 0:modifier)/1000)+bonus[i-1]);
    }
    final counts=List.filled(hand.length,0);
    for(int star=0;star<battle.criticalStars.floor().clamp(0,50);star++) {
      final available=[for(int i=0;i<counts.length;i++)if(counts[i]<10)i];
      if(available.isEmpty)break;
      final total=available.fold<double>(0,(v,i)=>v+weights[i]);
      int selected=available.last;
      if(total<=0)selected=available[r.nextInt(available.length)];
      else {double roll=r.nextDouble()*total;for(final i in available){roll-=weights[i];if(roll<0){selected=i;break;}}}
      counts[selected]++;
    }
    return counts;
  }
  bool critical(int index,int chance) => ReplayRandom(seed ^ (turn*0x119de1f3) ^ (hand[index-1]*0x45d9f3b)).nextInt(100)<chance;
}

Future<void> _interactiveSkill(BattleData battle,BattleDelegate delegate,Map<String,dynamic> action) async {
  int physical(int slot)=>battle.onFieldAllyServants.indexWhere((s)=>s?.startingPosition==slot);
  if(action['enemy']!=null)battle.enemyTargetIndex=(action['enemy'] as int)-1;
  if(battle.targetedEnemy==null)throw StateError('指定敌方目标不在场');
  final target=action['target'] as int?;
  battle.playerTargetIndex=target==null ? 0:physical(target);
  if(battle.playerTargetIndex<0)throw StateError('技能目标不在前排');
  final skill=(action['skill'] as int)-1;
  if(action['kind']=='skill') {
    final index=physical(action['actor'] as int);
    if(index<0)throw StateError('技能施放者不在前排');
    final actor=battle.onFieldAllyServants[index]!,info=actor.skillInfoList[skill];
    if(info.skill==null || info.chargeTurn>0)throw StateError('技能未解锁或仍在冷却');
    if(!actor.canAttack() || !battle.canUseSvtSkillIgnoreCoolDown(index,skill))throw StateError('角色不能行动、技能封印或条件不满足');
    if(info.skill!.functions.any((f)=>f.funcType.name=='replaceMember'))throw StateError('从者换人技能的分支选择尚未适配');
    await battle.activateSvtSkill(index,skill);
  } else {
    if(skill>=battle.masterSkillInfo.length || battle.masterSkillInfo[skill].skill==null ||
        battle.masterSkillInfo[skill].chargeTurn>0 || !battle.canUseMysticCodeSkillIgnoreCoolDown(skill))throw StateError('御主技能未配置、冷却或条件不满足');
    final needsSwap=battle.masterSkillInfo[skill].skill!.functions.any((f)=>f.funcType.name=='replaceMember');
    final swap=action['swap'] as List?;
    if(needsSwap!=(swap!=null))throw StateError('御主技能的换人设置不匹配');
    if(swap!=null) {
      final outgoing=swap[0] as int,incoming=swap[1] as int;
      if(physical(outgoing)<0 || !battle.nonnullBackupPlayers.any((s)=>s.startingPosition==incoming))throw StateError('换人需要前排和后排各一人');
      delegate.replaceMember=(front,back) async=>Tuple2(front.firstWhere((s)=>s?.startingPosition==outgoing)!,back.firstWhere((s)=>s?.startingPosition==incoming)!);
      await battle.activateMysticCodeSkill(skill);
      if(physical(incoming)<0 || physical(outgoing)>=0)throw StateError('换人未生效');
    } else {await battle.activateMysticCodeSkill(skill);}
  }
}

Future<Map<String,dynamic>> simulateInteractive(Map<String,dynamic> request) async {
  if((request['team'] as List).length!=6 || (request['team'] as List).where((s)=>s['is_friend']==true).length!=1)throw StateError('需要5名自有角色与1名助战');
  final seed=request['rng_seed'] as int,version=request['interactive_version'] as int;
  final ai=version>=3?EnemyAI(_mapAI(inputData['ai_data']),seed):null;
  final battle=await initializeBattle(request,{'random':900,'simulateEnemy':true,
    if(ai!=null)'setup':(BattleData b)=>ai.bind(b)});
  final deck=InteractiveDeck(battle,seed);
  final random=ReplayRandom(seed ^ 0x6d2b79f5);
  final delegate=BattleDelegate()
    ..replaceMember=(front,back)async{throw StateError('该换人效果需要明确的前后排选择');}
    ..skillActSelect=(actor)async{throw StateError('该技能需要尚未适配的分支选择');}
    ..tdTypeChange=(actor,types)async{throw StateError('该宝具需要尚未适配的形态选择');}
    ..ptRandom=(targets)async=>targets.isEmpty ? null:targets[random.nextInt(targets.length)];
  battle.delegate=delegate;
  final allPlayers={for(final s in [...battle.nonnullPlayers,...battle.nonnullBackupPlayers])s.startingPosition:s};
  if(ai!=null)allPlayers.addAll(ai.players);
  final selected=<Map<String,dynamic>>[],steps=<Map<String,dynamic>>[];
  final enemyCounts=<int,int>{};int enemyActions=0,attacks=0;
  String phase='commands';Map<String,dynamic>? failure,lastAttack;
  bool lost()=>ai?.outcome=='lost' || battle.nonnullPlayers.isEmpty && battle.nonnullBackupPlayers.isEmpty;
  bool won()=>ai?.outcome==null?battle.isBattleWin:ai!.outcome=='won';
  int physical(int slot)=>battle.onFieldAllyServants.indexWhere((s)=>s?.startingPosition==slot);
  void checkErrors() {
    final errors=battle.battleLogger.logs.where((l)=>l.type==BattleLogType.error ||
      (l.type==BattleLogType.debug && RegExp(r'not implemented|unhandled|unsupported|unknown function',caseSensitive:false).hasMatch(l.log))).toList();
    if(errors.isNotEmpty)throw StateError(errors.last.log);
  }
  List<Map<String,dynamic>> damageSince(int record)=>[for(final a in battle.recorder.records.skip(record).whereType<BattleAttackRecord>())
    for(final t in a.targets){'actor_slot':a.attacker.startingPosition,'actor':a.attacker.lBattleName,'side':a.attacker.isPlayer?'player':'enemy',
      'target':t.targetBefore.lBattleName,'target_slot':t.targetBefore.startingPosition,'hp':t.targetBefore.hp,'damage':t.result.totalDamage,
      'np_gain':t.result.totalNpGains/100,'def_np_gain':t.result.totalDefNpGains/100,'overkill_hits':t.result.overkillCount,
      if(ai!=null)'random_milli':t.damageParams.random,
      'card_type':a.card?.cardType,'is_np':a.card?.isTD??false,'critical':a.card?.critical??false}];
  List<BattleServantData> enemyCandidates()=>[for(final e in battle.nonnullEnemies)
    if(e.canAttack() && (enemyCounts[e.uniqueId]??0)<(e.niceEnemy?.ai?.maxActNum??1).clamp(0,3))e];
  Future<void> finishEnemy() async {
    if(!battle.isPlayerTurn && ai?.outcome==null)await battle.endEnemyActions();
    phase='commands';enemyCounts.clear();enemyActions=0;deck.sync();
  }
  Future<void> enemyStep() async {
    var candidates=enemyCandidates();
    if(enemyActions>=3 || candidates.isEmpty || battle.nonnullPlayers.isEmpty) {await finishEnemy();return;}
    if(ai!=null) {
      final required=candidates.fold<int>(0,(sum,e){final deficit=(e.niceEnemy?.ai?.minActNum??0)-(enemyCounts[e.uniqueId]??0);return sum+(deficit>0?deficit:0);});
      if(required>3-enemyActions)throw StateError('敌方最低行动次数无法在本回合内满足');
      if(required==3-enemyActions)candidates=candidates.where((e)=>(enemyCounts[e.uniqueId]??0)<(e.niceEnemy?.ai?.minActNum??0)).toList();
    }
    // Basic policy, not a reconstruction of quest-specific server AI.
    final total=candidates.fold<int>(0,(v,e)=>v+(e.niceEnemy?.ai?.actPriority??500).clamp(1,100000));
    int roll=random.nextInt(total);var enemy=candidates.last;
    for(final e in candidates){roll-=(e.niceEnemy?.ai?.actPriority??500).clamp(1,100000);if(roll<0){enemy=e;break;}}
    if(ai!=null && (enemy.niceEnemy?.ai?.aiId??0)>0) {
      final record=battle.recorder.records.length,turn=battle.totalTurnCount,wave=battle.waveCount;
      final decision=await ai.decide(enemy),oldPlayer=battle.playerTargetIndex,oldEnemy=battle.enemyTargetIndex;
      CombatAction? action;
      try {
        if(decision!=null)action=await ai.execute(decision,enemy);
        if(action!=null)await battle.playEnemyCard(action);
      } finally {battle.playerTargetIndex=oldPlayer;battle.enemyTargetIndex=oldEnemy;battle.updateTargetedIndex();}
      ai.committed(enemy,decision);checkErrors();
      enemyCounts[enemy.uniqueId]=(enemyCounts[enemy.uniqueId]??0)+1;enemyActions++;
      lastAttack={'kind':'enemy','turn':turn,'wave':wave,'damage':damageSince(record),'enemy_action':enemyActions,
        'actor':enemy.lBattleName,'is_np':action?.cardData.isTD??false,
        'action':decision?['aiAct']?['type']??'none'};steps.add(lastAttack!);
      if(enemyActions>=3 || enemyCandidates().isEmpty || battle.nonnullPlayers.isEmpty || ai.outcome!=null)await finishEnemy();
      return;
    }
    final taunts=battle.nonnullPlayers.where((s)=>s.hasBuffNoProbabilityCheck(BuffAction.hate)).toList();
    final targets=taunts.isEmpty ? battle.nonnullPlayers:taunts;
    final target=targets[random.nextInt(targets.length)];
    battle.playerTargetIndex=physical(target.startingPosition);battle.enemyTargetIndex=battle.onFieldEnemies.indexOf(enemy);
    CommandCardData? card;
    if(enemy.isNpFull() && !enemy.isNPSealed() && (enemy.getCurrentNP()==null || enemy.getCurrentNP()!.functions.isEmpty))throw StateError('敌方蓄力满，但没有完整宝具数据');
    if(enemy.canNP())card=enemy.getNPCard();
    if(card==null) {
      final svt=enemy.niceSvt;
      if(svt==null || svt.cardDetails.isEmpty)throw StateError('缺少敌方普通攻击数据');
      final critical=random.nextInt(1000)<(enemy.niceEnemy!.criticalRate+await enemy.getBuffValue(battle,BuffAction.criticalPoint)).clamp(0,1000);
      final int type;
      if(svt.cardDetails.containsKey(CardType.weak.value))type=critical && svt.cardDetails.containsKey(CardType.strength.value)?CardType.strength.value:CardType.weak.value;
      else {
        final types=svt.cards.where((t)=>svt.cardDetails.containsKey(t)&&!CardType.isExtra(t)).toList();
        if(types.isEmpty)throw StateError('敌方攻击卡型尚未适配');type=types[random.nextInt(types.length)];
      }
      card=CommandCardData(svtId:enemy.svtId,svtLimit:enemy.limitCount,uniqueId:enemy.uniqueId,cardType:type,
        cardDetail:svt.cardDetails[type]!,cardIndex:0,isTD:false,critical:critical,
        traits:ConstData.getCardInfo(type,1).individuality.toList());
    }
    final record=battle.recorder.records.length,turn=battle.totalTurnCount,wave=battle.waveCount;
    await battle.playEnemyCard(CombatAction(enemy,card));checkErrors();
    enemyCounts[enemy.uniqueId]=(enemyCounts[enemy.uniqueId]??0)+1;enemyActions++;
    lastAttack={'kind':'enemy','turn':turn,'wave':wave,'damage':damageSince(record),'enemy_action':enemyActions,
      'actor':enemy.lBattleName,'is_np':card.isTD,'target_slot':target.startingPosition};steps.add(lastAttack!);
    if(enemyActions>=3 || enemyCandidates().isEmpty || battle.nonnullPlayers.isEmpty)await finishEnemy();
  }
  try {checkErrors();} catch(error) {failure={'event':0,'message':error.toString()};}
  deck.sync();
  final events=request['interactive_events'] as List;
  for(int eventIndex=0;eventIndex<events.length && failure==null;eventIndex++) {
    final event=Map<String,dynamic>.from(events[eventIndex]);
    try {
      if(battle.isBattleWin || lost() || ai?.outcome!=null)throw StateError('战斗已经结束');
      final kind=event['kind'];
      if(kind=='enemy_step'||kind=='enemy_turn') {
        if(phase!='enemy')throw StateError('当前不在敌方阶段');
        do {await enemyStep();} while(kind=='enemy_turn' && phase=='enemy' && !lost());
      } else if(kind=='skill'||kind=='master') {
        if(phase!='commands')throw StateError('技能只能在己方技能阶段释放');
        await _interactiveSkill(battle,delegate,event);deck.sync();
      } else if(kind=='enemy') {
        if(phase!='commands' && phase!='cards')throw StateError('当前不能切换敌方目标');
        final index=(event['enemy'] as int)-1;
        if(index>=battle.onFieldEnemies.length || battle.onFieldEnemies[index]==null)throw StateError('敌方目标不在场');
        battle.enemyTargetIndex=index;
      } else if(kind=='cards') {
        if(phase!='commands')throw StateError('当前不能进入选卡');phase='cards';
      } else if(kind=='back') {
        if(phase!='cards')throw StateError('当前不在选卡阶段');phase='commands';selected.clear();
      } else if(kind=='unselect') {
        if(phase!='cards' || selected.isEmpty)throw StateError('没有已选指令卡');selected.removeLast();
      } else if(kind=='card'||kind=='np') {
        if(phase!='cards' || selected.length>=3)throw StateError('请在选卡阶段选择最多3张卡');
        if(selected.any((s)=>s['kind']==kind && s[kind=='np'?'actor':'card']==event[kind=='np'?'actor':'card']))throw StateError('不能重复选择同一张卡或宝具');
        if(kind=='np') {
          final index=physical(event['actor'] as int);
          if(index<0 || !battle.onFieldAllyServants[index]!.canSelectNP(battle))throw StateError('宝具NP不足、被封印或角色不在前排');
        } else {deck.action(event['card'] as int);}
        selected.add(event);
      } else if(kind=='attack') {
        if(phase!='cards' || selected.length!=3)throw StateError('请选择恰好3张普通卡或宝具');
        if(++attacks>30)throw StateError('最多模拟30回合');
        final starCounts=await deck.starCounts(),actions=<CombatAction>[];
        int? firstType;
        for(final choice in selected) {
          final CombatAction action;
          if(choice['kind']=='np') {
            final index=physical(choice['actor'] as int);
            if(index<0 || !battle.onFieldAllyServants[index]!.canSelectNP(battle))throw StateError('所选宝具不可用');
            final actor=battle.onFieldAllyServants[index]!;action=CombatAction(actor,actor.getNPCard()!);
          } else {action=deck.action(choice['card'] as int);}
          firstType??=action.cardData.cardType;
          if(choice['kind']=='card') {
            final handIndex=choice['card'] as int;
            final chance=(starCounts[handIndex-1]*10+(CardType.isQuick(firstType)?20:0)).clamp(0,100);
            action.cardData.critical=deck.critical(handIndex,chance);
          }
          actions.add(action);
        }
        final turn=battle.totalTurnCount,wave=battle.waveCount,record=battle.recorder.records.length;
        await battle.playerTurn(actions);checkErrors();
        lastAttack={'kind':'player','turn':turn,'wave':wave,'wave_after':battle.waveCount,'damage':damageSince(record),
          'chain_type':actions.first.cardData.chainType.name,
          'chain':[for(final a in actions){'slot':a.actor.startingPosition,'is_np':a.cardData.isTD,'card_type':a.cardData.cardType}],
          'cleared':battle.isBattleWin || battle.waveCount>wave};steps.add(lastAttack!);
        selected.clear();enemyActions=0;enemyCounts.clear();
        phase=battle.isPlayerTurn ? 'commands':'enemy';deck.sync();
      } else {throw StateError('此版本不支持该操作，请选择实际手牌');}
      checkErrors();
    } catch(error) {failure={'event':eventIndex+1,'message':error.toString()};break;}
  }
  if(won())phase='won';else if(lost())phase='lost';
  for(final s in [...battle.nonnullPlayers,...battle.nonnullBackupPlayers])allPlayers[s.startingPosition]=s;
  Map<String,dynamic> player(BattleServantData s) {
    final front=physical(s.startingPosition),back=battle.backupAllyServants.indexWhere((p)=>p?.startingPosition==s.startingPosition);
    final position=front>=0 ? front+1:back>=0 ? back+4:0;
    return {'slot':s.startingPosition,'position':position,'alive':position>0,'name':s.lBattleName,'hp':position==0?0:s.hp,'max_hp':s.maxHp,'np':s.np/100,
      'np_name':s.getCurrentNP()?.name??'宝具','np_id':s.getCurrentNP()?.id,
      'np_card_type':s.getNPCard()?.cardType,'np_ready':front>=0 && s.canSelectNP(battle),
      'skills':[for(int i=0;i<s.skillInfoList.length&&i<3;i++){'num':i+1,'name':s.skillInfoList[i].lName,'cooldown':s.skillInfoList[i].chargeTurn,
        'ready':front>=0 && s.skillInfoList[i].skill!=null && s.skillInfoList[i].chargeTurn==0 && s.canAttack() && s.canUseSkillIgnoreCoolDown(battle,i)}],
      'buffs':[for(final b in s.battleBuff.getActiveList()){'name':b.buff.name,'type':b.buff.type.name,'value':b.param,'count':b.count,'turns':b.logicTurn<0?-1:(b.logicTurn+1)~/2}]};
  }
  final stars=battle.isPlayerTurn && phase!='won' && phase!='lost' ? await deck.starCounts():<int>[];
  int? firstType;
  if(selected.isNotEmpty)firstType=selected.first['kind']=='np' ? battle.onFieldAllyServants[physical(selected.first['actor'] as int)]?.getNPCard()?.cardType:deck.action(selected.first['card'] as int).cardData.cardType;
  final hand=<Map<String,dynamic>>[];
  if(battle.isPlayerTurn && phase!='won' && phase!='lost')for(int i=1;i<=deck.hand.length;i++) {
    final a=deck.action(i);
    hand.add({'index':i,'deck_position':deck.hand[i-1]+1,'slot':a.actor.startingPosition,'name':a.actor.lBattleName,
      'card_index':a.cardData.cardIndex,'type':CardType.getName(a.cardData.cardType),'card_type':a.cardData.cardType,
      'can_act':a.actor.canCommandCard(a.cardData),'critical_chance':(stars[i-1]*10+(firstType!=null&&CardType.isQuick(firstType)?20:0)).clamp(0,100)});
  }
  final scene={'version':version,'rng_seed':seed,'wave':battle.waveCount,'turn':battle.totalTurnCount,'won':won(),'lost':lost(),'phase':phase,
    'players':allPlayers.values.map(player).toList(),'enemy_target':battle.enemyTargetIndex+1,
    'enemies':[for(final e in battle.onFieldEnemies.asMap().entries)if(e.value!=null){'position':e.key+1,'name':e.value!.lBattleName,
      'hp':e.value!.hp,'max_hp':e.value!.maxHp,'charge':e.value!.npLineCount,'charge_max':e.value!.niceEnemy?.chargeTurn??0,
      if(ai!=null)'buffs':[for(final b in e.value!.battleBuff.getActiveList()){'type':b.buff.type.name,'name':b.buff.name,'value':b.param}]}],
    'selected_nps':[for(final c in selected)if(c['kind']=='np')c['actor']],'selected_cards':selected,'hand':hand,
    'deck_cycle':deck.cycle,'deck_remaining':deck.remaining.length,'stars':battle.criticalStars,
    'enemy_actions':enemyActions,'enemy_policy':ai==null?'基础行动策略：普攻／满蓄力宝具，最多3次并遵守各敌人行动上限；不自动推断特殊AI技能或脚本。':
      '按冻结的关卡AI条件、优先级、权重、技能与脚本执行；逐目标均匀伤害乱数，未支持节点会报错停止。',
    if(ai!=null)'ai_trace':ai.trace.reversed.take(40).toList().reversed.toList(),
    if(ai!=null)'damage_rolls':ai.damageRolls,
    if(ai!=null)'battle_values':{for(final e in ai.values.entries)'${e.key}':e.value},
    'master_skills':[for(int i=0;i<battle.masterSkillInfo.length;i++){'num':i+1,'name':battle.masterSkillInfo[i].lName,
      'cooldown':battle.masterSkillInfo[i].chargeTurn,'ready':battle.masterSkillInfo[i].skill!=null && battle.masterSkillInfo[i].chargeTurn==0 && battle.canUseMysticCodeSkillIgnoreCoolDown(i),
      'swap':battle.masterSkillInfo[i].skill?.functions.any((f)=>f.funcType.name=='replaceMember')??false}],
    'last_attack':lastAttack,'recent_attacks':steps.reversed.take(4).toList().reversed.toList(),'accepted':failure==null};
  return {'passed':won()&&failure==null,'message':failure?['message']??(won()?'模拟通关':lost()?'模拟战败':'操作已完成'),
    'scene':scene,'failure':failure,'steps':steps};
}

Map<String,dynamic> _mapAI(dynamic value) {
  if(value is! Map)throw StateError('缺少冻结的AI数据');
  return Map<String,dynamic>.from(value);
}
