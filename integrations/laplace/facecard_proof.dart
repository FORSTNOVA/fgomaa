import 'package:chaldea/app/battle/models/battle.dart';
import 'package:chaldea/app/battle/utils/battle_logger.dart';

// Restrict this proof to flat modifiers. On-hit skills, transformations and other
// state-dependent mechanisms stay on the NP path rather than being guessed.
const _flatBuffs = {
  'upAtk', 'downAtk', 'upAttack', 'downAttack', 'upCommandall', 'downCommandall', 'upDamage', 'downDamage',
  'addDamage', 'subDamage', 'upNpdamage', 'downNpdamage', 'upCriticaldamage', 'downCriticaldamage',
  'upTolerance', 'downTolerance', 'upGrantstate', 'downGrantstate', 'avoidState',
  'downCriticalRateDamageTaken', 'downCriticalStarDamageTaken', 'upCriticalrate',
  'upStarweight', 'downStarweight', 'upDropnp', 'downDropnp', 'upDropstar', 'downDropstar',
  'upDefence', 'downDefence', 'upMaxhp', 'downMaxhp', 'regainNp', 'regainHp',
  'upGrantInstantdeath', 'upDamagedropnp', 'guts', 'invincible', 'avoidance', 'avoidInstantdeath',
};

bool _negative(BuffData b) => b.buff.type.name.startsWith('down') || b.buff.type.name == 'subDamage';

Future<Map<String, dynamic>> proveAnyFacecards(BattleData battle) async {
  final wave = battle.waveCount;
  final unsupported = {for (final a in battle.nonnullActors)
    for (final b in a.battleBuff.validBuffs)
      if (!_flatBuffs.contains(b.buff.type.name)) b.buff.type.name};
  if (battle.nonnullEnemies.isEmpty || battle.nonnullEnemies.length > 3 ||
      battle.nonnullEnemies.any((e) => e.hp > 5000) || battle.fieldBuffs.isNotEmpty ||
      battle.curStage?.fieldAis.isNotEmpty == true ||
      unsupported.isNotEmpty) {
    return {'all_triples': false, 'reason': '超出保守普攻校验范围',
      'unsupported_buffs': unsupported.toList()};
  }
  final pool = <(int, int)>[];
  for (int actor = 0; actor < battle.onFieldAllyServants.length; actor++) {
    final svt = battle.onFieldAllyServants[actor];
    if (svt == null) continue;
    for (int card = 0; card < svt.getCards().length; card++) {
      pool.add((actor, card));
    }
  }
  if (pool.length < 3) return {'all_triples': false};
  battle.pushSnapshot();
  final saved = battle.snapshots.removeLast();
  final logs = List<BattleLog>.from(battle.battleLogger.logs);
  void restore() {
    battle.snapshots..clear()..add(saved);
    battle.popSnapshot();
    // popSnapshot transfers its lists rather than cloning them. Clone once
    // before each trial so no attack can mutate the reusable saved state.
    battle.pushSnapshot();
    battle.popSnapshot();
    battle.battleLogger.logs..clear()..addAll(logs);
  }
  Future<void> play(List<int> picks) async {
    final actions = <CombatAction>[];
    for (final pick in picks) {
      final (actor, card) = pool[pick];
      final svt = battle.onFieldAllyServants[actor]!;
      actions.add(CombatAction(svt, svt.getCards()[card]));
    }
    battle.criticalStars = 0;
    await battle.playerTurn(actions);
  }
  final started = DateTime.now();
  int tested = 0;
  final minimumNp = {for (final actor in battle.nonnullPlayers) actor.startingPosition: 999999};
  final actorCount = battle.nonnullPlayers.length;
  for (int a = 0; a < pool.length; a++) {
    for (int b = 0; b < pool.length; b++) {
      if (b == a) continue;
      for (int c = 0; c < pool.length; c++) {
        if (c == a || c == b) continue;
        restore();
        await play([a, b, c]);
        tested++;
        if ((!battle.isBattleWin && battle.waveCount == wave) ||
            battle.nonnullPlayers.length != actorCount ||
            battle.battleLogger.logs.any((l) => l.type == BattleLogType.error) ||
            DateTime.now().difference(started).inMilliseconds > 8000) {
          restore();
          return {'all_triples': false, 'tested': tested,
            'reason': DateTime.now().difference(started).inMilliseconds > 8000 ? '校验超时' : '存在无法清场的选卡组合'};
        }
        for (final actor in battle.nonnullPlayers) {
          final previous = minimumNp[actor.startingPosition]!;
          if (actor.np < previous) minimumNp[actor.startingPosition] = actor.np;
        }
      }
    }
  }
  restore();
  await play([0, 1, 2]);
  // Carry a pessimistic state into the next wave: take minimum guaranteed NP,
  // discard stars, remove positive count-limited buffs, and retain the
  // strongest pre-card negative count-limited effects. This prevents the
  // representative triple from granting benefits other hands would not give.
  for (final actor in battle.nonnullPlayers) {
    actor.np = minimumNp[actor.startingPosition]!;
    actor.battleBuff.originalActiveList.removeWhere((b) => b.count >= 0 && !_negative(b));
    actor.battleBuff.originalPassiveList.removeWhere((b) => b.count >= 0 && !_negative(b));
    final before = saved.onFieldAllyServants.where((s) => s?.startingPosition == actor.startingPosition).firstOrNull;
    if (before != null) {
      for (final debuff in before.battleBuff.validBuffs.where((b) => b.count >= 0 && _negative(b))) {
        actor.battleBuff.originalActiveList.removeWhere((b) => b.addOrder == debuff.addOrder);
        actor.battleBuff.originalPassiveList.removeWhere((b) => b.addOrder == debuff.addOrder);
        actor.battleBuff.originalActiveList.add(debuff.copy()..logicTurn = -1);
      }
    }
  }
  battle.criticalStars = 0;
  return {'all_triples': true, 'tested': tested, 'deck_cards': pool.length,
          'critical': false, 'carryover': 'lower_bound'};
}
