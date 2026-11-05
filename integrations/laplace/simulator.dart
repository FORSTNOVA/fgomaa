import 'package:tuple/tuple.dart';
import 'package:chaldea/app/battle/models/battle.dart';
import 'package:chaldea/app/battle/interactions/_delegate.dart';
import 'package:chaldea/app/battle/utils/battle_logger.dart';
import 'package:chaldea/models/models.dart';
import 'facecard_proof.dart';

Map<String, dynamic> inputData = {};
Map<String, dynamic> readData(String name) => Map<String, dynamic>.from(inputData[name] as Map);

Future<BattleData> initializeBattle(Map<String, dynamic> request, Map<String, dynamic> scenario) async {
  final team = <PlayerSvtData>[];
  for (final item in request['team'] as List) {
    final p = PlayerSvtData.id(item['svt_id'] as int)
      ..lv = item['lv'] as int
      ..tdLv = item['np_lv'] as int
      ..atkFou = item['atk_fou'] as int
      ..hpFou = item['hp_fou'] as int
      ..limitCount = (item['battle_chara_id'] ?? item['ascension']) as int
      ..skillLvs = [item['skill_lv1'], item['skill_lv2'], item['skill_lv3']].cast<int>()
      ..appendLvs = (item['append_levels'] as List).cast<int>();
    if (p.appendLvs.length != 5) throw StateError('Expected five append skill levels');
    final skillIds = (item['skill_ids'] as List).cast<int>();
    if (skillIds.length != 3) throw StateError('Expected three resolved skill IDs');
    p.skills = kActiveSkillNums.map((n) => skillIds[n - 1] == 0 ? null :
        p.svt!.groupedActiveSkills[n]!.firstWhere((s) => s.id == skillIds[n - 1])).toList();
    p.td = p.svt!.groupedNoblePhantasms[1]!.firstWhere((td) => td.id == item['np_id']);
    if (item['is_friend'] == true) p.supportType = SupportSvtType.friend;
    if (item['ce'] != null) {
      final ce = item['ce'] as Map;
      final model = db.gameData.craftEssencesById[ce['ce_id']];
      if (model == null) throw StateError('Missing CE ${ce['ce_id']}');
      p.equip1 = SvtEquipData(ce: model, lv: ce['lv'] as int, limitBreak: ce['is_mlb'] as bool);
    }
    team.add(p);
  }
  final battle = BattleData();
  battle.options.random = scenario['random'] as int;
  battle.options.simulateEnemy = scenario['simulateEnemy'] == true;
  final setup=scenario['setup'];
  if(setup!=null)(setup as void Function(BattleData))(battle);
  final mc = MysticCodeData()
    ..mysticCode = db.gameData.mysticCodes[request['mystic_code_id']]
    ..level = request['mystic_code_lv'] as int;
  final quest = QuestPhase.fromJson(readData(request['quest_data'] as String));
  if (quest.id != request['quest_id'] || quest.phase != request['phase'] ||
      quest.stages.isEmpty || quest.stages.length > 3 || quest.stages.any((s) => s.enemies.isEmpty)) {
    throw StateError('Quest identity or enemy data incomplete');
  }
  await battle.init(quest, team, mc);
  return battle;
}

Future<Map<String, dynamic>> simulate(Map<String, dynamic> request, Map<String, dynamic> scenario) async {
  final battle = await initializeBattle(request, scenario);
  final steps = <Map<String, dynamic>>[];
  final actions = <Map<String, dynamic>>[];
  final failedChargers = <String>{};
  final compiledTurns = <String, dynamic>{};
  List<Map<String, dynamic>> compiledSkills = [];
  String reason = 'Three turns ended without clearing';

  int physical(int logical) => battle.onFieldAllyServants.indexWhere((s) => s?.startingPosition == logical);
  bool forbidden(int slot, int skill, int wave) => scenario['guards'] == true &&
      (request['runtime_forbidden']['$wave'] as List).any((pair) => pair[0] == slot && pair[1] == skill);

  Future<void> cast(int slot, int skill, int? target, int wave, {bool dynamic = false}) async {
    final index = dynamic ? slot - 1 : physical(slot);
    if (index < 0) throw StateError('Servant $slot not on field');
    final logical = battle.onFieldAllyServants[index]!.startingPosition;
    if (forbidden(logical, skill, wave)) {
      if (scenario['strict'] == true) throw StateError('Validated strategy contains a forbidden skill');
      actions.add({'wave': wave, 'slot': index + 1, 'skill': skill, 'status': 'runtime_guard_skipped'});
      return;
    }
    final info = battle.onFieldAllyServants[index]!.skillInfoList[skill - 1];
    if (info.chargeTurn > 0) {
      if (scenario['strict'] == true) throw StateError('Validated strategy attempts a skill on cooldown');
      actions.add({'wave': wave, 'slot': index + 1, 'skill': skill, 'status': 'cooldown', 'remaining': info.chargeTurn});
      return;
    }
    final targetIndex = target == null ? physical(1) : physical(target);
    if (targetIndex < 0) throw StateError('Target $target not on field');
    battle.playerTargetIndex = targetIndex;
    await battle.activateSvtSkill(index, skill - 1);
    compiledSkills.add({'servant': logical, 'skill': skill, 'target': target});
    actions.add({'wave': wave, 'slot': index + 1, 'skill': skill, 'status': dynamic ? 'dynamic' : 'script'});
  }

  for (int turn = 1; turn <= 3; turn++) {
    final wave = battle.waveCount;
    final directive = request['strategy']['turns']['$turn'] as Map?;
    if (directive == null) {
      reason = '缺少第 $turn 回合技能方案';
      break;
    }
    compiledSkills = [];
    final skills = directive['skills'] as List;
    final master = directive['master_skills'] as List;
    final compiledTurn = <String, dynamic>{'skills': compiledSkills, 'master_skills': master,
      'np_servant': directive['np_servant'], 'attack_mode': 'np'};
    compiledTurns['$turn'] = compiledTurn;
    final swaps = master.where((s) => s['swap'] != null).toList();
    if (swaps.isNotEmpty) {
      final swap = swaps.single;
      final outgoing = swap['swap'][0] as int, incoming = swap['swap'][1] as int;
      for (final s in skills.where((s) => s['servant'] == outgoing)) {
        await cast(s['servant'], s['skill'], s['target'], wave);
      }
      battle.delegate = BattleDelegate()..replaceMember = (front, back) async =>
          Tuple2(front.firstWhere((s) => s?.startingPosition == outgoing)!,
                 back.firstWhere((s) => s?.startingPosition == incoming)!);
      await battle.activateMysticCodeSkill((swap['skill'] as int) - 1);
      if (physical(incoming) < 0) throw StateError('Order change failed');
      for (final s in skills.where((s) => s['servant'] != outgoing)) {
        await cast(s['servant'], s['skill'], s['target'], wave);
      }
    } else {
      for (final s in skills) {
        await cast(s['servant'], s['skill'], s['target'], wave);
      }
    }
    for (final m in master.where((s) => s['swap'] == null)) {
      battle.playerTargetIndex = physical((m['target'] as int?) ?? 1);
      await battle.activateMysticCodeSkill((m['skill'] as int) - 1);
    }
    final enemies = battle.nonnullEnemies.map((e) => {'name': e.lBattleName, 'hp': e.hp}).toList();
    final normalRecord = battle.recorder.records.length;
    final wantsNormal = directive['attack_mode'] == 'facecards';
    Map<String, dynamic>? normalAttempt;
    if (wantsNormal || (request['allow_facecards'] == true && scenario['strict'] != true)) {
      final proof = await proveAnyFacecards(battle);
      normalAttempt = proof;
      if (proof['all_triples'] == true) {
        compiledTurn['attack_mode'] = 'facecards';
        compiledTurn['np_servant'] = null;
        final attacks = battle.recorder.records.skip(normalRecord).whereType<BattleAttackRecord>();
        steps.add({'turn': turn, 'wave': wave, 'attack_mode': 'facecards', 'np_servant': null,
          'cleared': true,
          'np_before_attack': 0, 'np_after_turn': 0, 'wave_after': battle.waveCount, 'enemies': enemies,
          'facecard_proof': proof, 'damage': [for (final a in attacks) for (final t in a.targets)
            {'target': t.targetBefore.lBattleName, 'hp': t.targetBefore.hp,
             'damage': t.result.totalDamage, 'np_gain': 0, 'overkill_hits': t.result.overkillCount}]});
        if (battle.isBattleWin) { reason = '$turn 回合模拟通关'; break; }
        continue;
      } else if (wantsNormal) {
        reason = '第 $turn 回合普攻不能保证任意发牌清场';
        break;
      }
    }
    final dps = battle.onFieldAllyServants[physical(directive['np_servant'] as int)]!;
    final plannedNp = dps.np;
    if (scenario['dynamic'] == true && request['battle_mode'] != 'rotation' && dps.np < 10000) {
      for (final pair in [[3, 2], [2, 2], [3, 1], [2, 1], [1, 2], [3, 3], [2, 3]]) {
        if (dps.np >= 10000) break;
        final actor = battle.onFieldAllyServants[pair[0] - 1];
        if (actor == null) continue;
        final slot = actor.startingPosition;
        final key = '$slot-${pair[1]}';
        if (failedChargers.contains(key) || forbidden(slot, pair[1], wave)) continue;
        if (actor.skillInfoList[pair[1] - 1].chargeTurn > 0) continue;
        final before = dps.np;
        await cast(slot, pair[1], 1, wave, dynamic: true);
        if (dps.np == before) failedChargers.add(key);
      }
    }
    final step = <String, dynamic>{'turn': turn, 'wave': wave, 'np_after_script': plannedNp / 100,
      'attack_mode': 'np', 'np_servant': directive['np_servant'],
      'np_before_attack': dps.np / 100,
      'enemies': battle.nonnullEnemies.map((e) => {'name': e.lBattleName, 'hp': e.hp}).toList()};
    if (normalAttempt != null) step['facecard_attempt'] = normalAttempt;
    steps.add(step);
    if (dps.np < 10000) {
      reason = '第 $turn 回合 NP 不足：${dps.np / 100}%';
      break;
    }
    final recordIndex = battle.recorder.records.length;
    await battle.playerTurn([CombatAction(dps, dps.getNPCard()!)]);
    final attacks = battle.recorder.records.skip(recordIndex).whereType<BattleAttackRecord>();
    step['damage'] = [for (final a in attacks) for (final t in a.targets)
      {'target': t.targetBefore.lBattleName, 'hp': t.targetBefore.hp,
       'damage': t.result.totalDamage, 'np_gain': t.result.totalNpGains / 100,
       'overkill_hits': t.result.overkillCount}];
    step['np_after_turn'] = dps.np / 100;
    step['wave_after'] = battle.waveCount;
    step['cleared'] = battle.isBattleWin || battle.waveCount > wave;
    step['instant_death_clear'] = step['cleared'] == true && (step['damage'] as List).isEmpty;
    if (battle.isBattleWin) {
      reason = '$turn 回合模拟通关';
      break;
    }
    if (battle.waveCount == wave) {
      reason = '第 $turn 回合伤害不足，仍有敌人存活';
      break;
    }
  }
  final result = <String, dynamic>{...scenario, 'success': battle.isBattleWin, 'stop_reason': reason,
    'steps': steps, 'actions': actions,
    'compiled_strategy': {...Map<String, dynamic>.from(request['strategy'] as Map), 'turns': compiledTurns},
    'battle_errors': battle.battleLogger.logs.where((l) => l.type == BattleLogType.error).map((l) => l.log).toList(),
    'debug_messages': battle.battleLogger.logs.where((l) => l.type == BattleLogType.debug).map((l) => l.log).toSet().toList()};
  print('SCENARIO_RESULT ${scenario['name']} ${result['success']} $reason');
  return result;
}
