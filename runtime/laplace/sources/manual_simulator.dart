import 'package:tuple/tuple.dart';
import 'package:chaldea/app/battle/models/battle.dart';
import 'package:chaldea/app/battle/interactions/_delegate.dart';
import 'package:chaldea/app/battle/utils/battle_logger.dart';
import 'simulator.dart';
import 'facecard_proof.dart';
import 'interactive_simulator.dart';

// Unlike automatic planning, manual simulation never reorders or adds actions.
Future<Map<String, dynamic>> simulateManual(Map<String, dynamic> request) async {
  if((request['interactive_version']??0)>=2)return simulateInteractive(request);
  if ((request['team'] as List).length != 6 ||
      (request['team'] as List).where((s) => s['is_friend'] == true).length != 1) {
    throw StateError('Manual party requires five owned servants and one support');
  }
  final battle = await initializeBattle(request, {'random':900});
  battle.options.simulateEnemy = false;
  final delegate = BattleDelegate()
    ..replaceMember = (front, back) async { throw StateError('请为换人技能明确设置换下和换上槽位'); }
    ..skillActSelect = (actor) async { throw StateError('此技能需要额外分支选择，当前手动模拟尚未适配'); }
    ..tdTypeChange = (actor, types) async { throw StateError('此角色需要额外宝具形态选择，当前手动模拟尚未适配'); };
  battle.delegate = delegate;
  final steps = <Map<String, dynamic>>[];
  final executed = <Map<String, dynamic>>[];
  String reason = '手动回合执行完毕，仍有敌人存活';
  Map<String,dynamic>? failure;
  int physical(int slot) => battle.onFieldAllyServants.indexWhere((s) => s?.startingPosition == slot);
  List<Map<String,dynamic>> state() => [for (final s in battle.nonnullPlayers)
    {'slot':s.startingPosition,'name':s.lBattleName,'np':s.np/100,'hp':s.hp}];
  void checkErrors() {
    final errors = battle.battleLogger.logs.where((l) => l.type == BattleLogType.error).toList();
    if (errors.isNotEmpty) throw StateError(errors.last.log);
    final unknown = battle.battleLogger.logs.where((l) => l.type == BattleLogType.debug &&
      RegExp(r'not implemented|unhandled|unsupported|unknown function',caseSensitive:false).hasMatch(l.log));
    if (unknown.isNotEmpty) throw StateError('当前战斗包含未实现机制：${unknown.first.log}');
  }
  final turns = request['manual_turns'] as List;
  for (int turn = 1; turn <= turns.length; turn++) {
    if (battle.isBattleWin) break;
    final wave = battle.waveCount;
    final step = <String,dynamic>{'turn':turn,'wave':wave,'before':state(),
      'enemies':[for(final e in battle.nonnullEnemies) {'name':e.lBattleName,'hp':e.hp}],
      'damage':<Map<String,dynamic>>[]};
    steps.add(step);
    int actionNumber = 0;
    try {
      final attacks = <CombatAction>[];
      bool attackPhase = false, facecards = false;
      int enemyTarget = 0;
      for (final raw in turns[turn-1]['actions'] as List) {
        actionNumber++;
        final action = Map<String,dynamic>.from(raw);
        final kind = action['kind'];
        if (attackPhase && kind != 'np') throw StateError('选卡之后不能再释放技能');
        if (action['enemy'] != null) battle.enemyTargetIndex = (action['enemy'] as int)-1;
        if (battle.enemyTargetIndex >= battle.onFieldEnemies.length ||
            battle.onFieldEnemies[battle.enemyTargetIndex] == null) throw StateError('指定敌方目标不在场');
        if (kind == 'skill' || kind == 'master') {
          final target = action['target'] as int?;
          battle.playerTargetIndex = target == null ? 0 : physical(target);
          if (battle.playerTargetIndex < 0) throw StateError('指定的技能目标不在前排');
          final skillIndex = (action['skill'] as int)-1;
          if (kind == 'skill') {
            final index = physical(action['actor'] as int);
            if (index < 0) throw StateError('技能施放者不在前排');
            final info = battle.onFieldAllyServants[index]!.skillInfoList[skillIndex];
            if (info.skill == null || info.chargeTurn > 0) throw StateError('技能未解锁或仍在冷却');
            if (!battle.onFieldAllyServants[index]!.canAttack() || !battle.canUseSvtSkillIgnoreCoolDown(index,skillIndex)) {
              throw StateError('角色无法行动、技能被封印或使用条件未满足');
            }
            if (info.skill!.functions.any((f) => f.funcType.name == 'replaceMember')) {
              throw StateError('从者换人技能需要独立选择，当前尚未适配');
            }
            await battle.activateSvtSkill(index,skillIndex);
          } else {
            if (skillIndex >= battle.masterSkillInfo.length || battle.masterSkillInfo[skillIndex].skill == null ||
                battle.masterSkillInfo[skillIndex].chargeTurn > 0) throw StateError('御主技能未配置或仍在冷却');
            if (!battle.canUseMysticCodeSkillIgnoreCoolDown(skillIndex)) throw StateError('御主技能使用条件未满足');
            final needsSwap = battle.masterSkillInfo[skillIndex].skill!.functions.any((f) => f.funcType.name == 'replaceMember');
            final swap = action['swap'] as List?;
            if (needsSwap != (swap != null)) throw StateError('所选御主技能的换人设置不匹配');
            if (swap != null) {
              final outgoing = swap[0] as int, incoming = swap[1] as int;
              if (physical(outgoing) < 0 || !battle.nonnullBackupPlayers.any((s) => s.startingPosition == incoming)) {
                throw StateError('换人必须是当前前排与后排各一人');
              }
              delegate.replaceMember = (front,back) async => Tuple2(
                front.firstWhere((s) => s?.startingPosition == outgoing)!,
                back.firstWhere((s) => s?.startingPosition == incoming)!);
              await battle.activateMysticCodeSkill(skillIndex);
              if (physical(incoming) < 0 || physical(outgoing) >= 0) throw StateError('换人未生效');
            } else {
              await battle.activateMysticCodeSkill(skillIndex);
            }
          }
          checkErrors();
          executed.add({'turn':turn,'action':actionNumber,...action,'after':state(),'status':'executed'});
        } else if (kind == 'np') {
          attackPhase = true;
          enemyTarget = battle.enemyTargetIndex;
          final index = physical(action['actor'] as int);
          if (index < 0) throw StateError('宝具角色不在前排');
          final actor = battle.onFieldAllyServants[index]!;
          if (actor.np < 10000 || !actor.canSelectNP(battle)) throw StateError('宝具NP不足100%或不可用');
          attacks.add(CombatAction(actor,actor.getNPCard()!));
        } else if (kind == 'facecards') {
          attackPhase = true;
          facecards = true;
        } else {
          throw StateError('未知手动操作');
        }
      }
      if (request['interactive'] == true && turn == turns.length && attacks.isEmpty && !facecards) {
        step['after'] = state();
        break;
      }
      final record = battle.recorder.records.length;
      if (facecards) {
        final proof = await proveAnyFacecards(battle);
        step['facecard_proof'] = proof;
        if (proof['all_triples'] != true) throw StateError('普攻不能保证任意发牌清场');
      } else {
        if (attacks.isEmpty || attacks.length > 3) throw StateError('宝具链必须为1至3张');
        step['np_before'] = [for(final a in attacks) {'slot':a.actor.startingPosition,'np':a.actor.np/100}];
        battle.enemyTargetIndex = enemyTarget;
        await battle.playerTurn(attacks);
      }
      checkErrors();
      step['damage'] = [for(final a in battle.recorder.records.skip(record).whereType<BattleAttackRecord>())
        for(final t in a.targets) {'actor_slot':a.attacker.startingPosition,'target':t.targetBefore.lBattleName,'hp':t.targetBefore.hp,
          'damage':t.result.totalDamage,'np_gain':t.result.totalNpGains/100,'overkill_hits':t.result.overkillCount}];
      step['after'] = state();
      step['wave_after'] = battle.waveCount;
      step['cleared'] = battle.isBattleWin || battle.waveCount > wave;
      for (final raw in turns[turn-1]['actions'] as List) {
        if (raw['kind'] == 'np' || raw['kind'] == 'facecards') {
          executed.add({'turn':turn,...Map<String,dynamic>.from(raw),'status':'attack_submitted'});
        }
      }
      if (battle.isBattleWin) reason = '$turn 回合模拟通关（仅己方行动）';
    } catch(error) {
      reason = error.toString();
      failure = {'turn':turn,'action':actionNumber,'message':reason};
      step['after'] = state();
      break;
    }
  }
  Map<String,dynamic>? scene;
  if (request['interactive'] == true) {
    try {
      if (battle.isBattleWin && (steps.length!=turns.length-1 || (turns.last['actions'] as List).isNotEmpty ||
          request['interactive_phase']!='commands' || (request['selected_nps'] as List).isNotEmpty || request['interactive_enemy']!=null)) {
        throw StateError('战斗已经结束，不能继续追加操作');
      }
      if (request['interactive_enemy'] != null && !battle.isBattleWin) {
        final enemy = (request['interactive_enemy'] as int)-1;
        if (enemy >= battle.onFieldEnemies.length || battle.onFieldEnemies[enemy] == null) throw StateError('指定敌方目标不在场');
        battle.enemyTargetIndex = enemy;
      }
      for (final slot in request['selected_nps'] as List) {
        final index = physical(slot as int);
        if (index < 0 || !battle.onFieldAllyServants[index]!.canSelectNP(battle)) throw StateError('所选宝具NP不足、被封印或角色不在前排');
      }
      checkErrors();
    } catch (error) {
      failure ??= {'message':error.toString()};
    }
    Map<String,dynamic> player(BattleServantData s,int position) => {
      'slot':s.startingPosition,'position':position,'name':s.lBattleName,'hp':s.hp,'max_hp':s.maxHp,'np':s.np/100,
      'np_name':s.getCurrentNP()?.name ?? '宝具','np_ready':position<=3 && s.canSelectNP(battle),
      'skills':[for(int i=0;i<s.skillInfoList.length && i<3;i++) {
        'num':i+1,'name':s.skillInfoList[i].lName,'cooldown':s.skillInfoList[i].chargeTurn,
        'ready':position<=3 && s.skillInfoList[i].skill!=null && s.skillInfoList[i].chargeTurn==0 && s.canAttack() && s.canUseSkillIgnoreCoolDown(battle,i)}],
      'buffs':[for(final b in s.battleBuff.getActiveList()) {
        'name':b.buff.name,'type':b.buff.type.name,'value':b.param,'count':b.count,'turns':b.logicTurn<0 ? -1 : (b.logicTurn+1)~/2}]
    };
    scene = {'wave':battle.waveCount,'turn':battle.isBattleWin ? steps.last['turn'] : request['interactive_turn'],
      'won':battle.isBattleWin,'phase':battle.isBattleWin ? 'won' : request['interactive_phase'],
      'selected_nps':request['selected_nps'],'enemy_target':battle.enemyTargetIndex+1,
      'players':[for(final entry in battle.onFieldAllyServants.asMap().entries) if(entry.value!=null) player(entry.value!,entry.key+1),
        for(final entry in battle.backupAllyServants.asMap().entries) if(entry.value!=null) player(entry.value!,entry.key+4)],
      'enemies':[for(final entry in battle.onFieldEnemies.asMap().entries) if(entry.value!=null) {
        'position':entry.key+1,'name':entry.value!.lBattleName,'hp':entry.value!.hp,'max_hp':entry.value!.maxHp}],
      'master_skills':[for(int i=0;i<battle.masterSkillInfo.length;i++) {
        'num':i+1,'name':battle.masterSkillInfo[i].lName,'cooldown':battle.masterSkillInfo[i].chargeTurn,
        'ready':battle.masterSkillInfo[i].skill!=null && battle.masterSkillInfo[i].chargeTurn==0 && battle.canUseMysticCodeSkillIgnoreCoolDown(i),
        'swap':battle.masterSkillInfo[i].skill?.functions.any((f)=>f.funcType.name=='replaceMember') ?? false}],
      'last_attack':steps.reversed.where((s)=>s.containsKey('wave_after')).firstOrNull,
      'accepted':failure==null};
  }
  return {'passed':battle.isBattleWin && failure == null,'message':reason,'steps':steps,'actions':executed,
    if(scene!=null) 'scene':scene,
    'failure':failure,'frontline':state(),'remaining_enemies':[for(final e in battle.nonnullEnemies) {'name':e.lBattleName,'hp':e.hp}],
    'battle_errors':battle.battleLogger.logs.where((l) => l.type == BattleLogType.error).map((l) => l.log).toList()};
}
