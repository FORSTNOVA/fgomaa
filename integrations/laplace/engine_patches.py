"""Reproducible, checked hooks for the pinned Chaldea source; no legacy behavior change."""
from pathlib import Path


def apply_engine_patches(source):
    base=Path(source)/'lib/app/battle'

    def replace(relative,old,new,count=1):
        path=base/relative;text=path.read_text(encoding='utf-8')
        if new in text:return
        actual=text.count(old)
        if actual!=count:raise RuntimeError(f'Chaldea hook anchor changed: {relative} ({actual}, expected {count})')
        path.write_text(text.replace(old,new),encoding='utf-8')

    replace('../api/atlas.dart', '  const AtlasApi._();', """  const AtlasApi._();
  static Map<String, dynamic>? frozenCommonReleases;
  static Map<String, dynamic> frozenReleaseQuestRecords = {};""")
    release_method = '  static Future<List<CommonRelease>?> commonRelease(int releaseId, {Region region = Region.jp, Duration? expireAfter}) {'
    replace('../api/atlas.dart', release_method, release_method + """
    final frozen = frozenCommonReleases?['${region.upper}/$releaseId'];
    if (frozen is List) {
      return Future.value(frozen.map((row) => CommonRelease.fromJson(Map<String, dynamic>.from(row as Map))).toList());
    }""")
    replace('functions/function_executor.dart',
        '        case .questClearPhase || .questClear:\n          return true;',
        """        case .questClearPhase || .questClear:
          if (AtlasApi.frozenCommonReleases == null) return true;
          final record = AtlasApi.frozenReleaseQuestRecords['${release.condId}'] as Map?;
          if (record == null) throw StateError('Missing release quest record ${release.condId}');
          return (record['clearNum'] as int? ?? 0) > 0 &&
              (release.condType != CondType.questClearPhase || (record['questPhase'] as int? ?? 0) >= release.condNum);
        case .none:
          return true;""")
    replace('functions/function_executor.dart',
        '          // for battle, focus on FALSE, so regard unknown(null) as true',
        """          if (AtlasApi.frozenCommonReleases != null) {
            throw StateError('Unsupported common release condition ${release.condType}');
          }
          // for battle, focus on FALSE, so regard unknown(null) as true""")

    for relative in ['functions/damage.dart','models/battle.dart','models/ai.dart','models/svt_entity.dart','functions/function_executor.dart']:
        path=base/relative;text=path.read_text(encoding='utf-8')
        line="import 'package:chaldea/fgomaa/engine_hooks.dart';\n"
        if line not in text:path.write_text(line+text,encoding='utf-8')
    replace('functions/damage.dart','..random = battleData.options.random',
        '..random = BattleEngineHooks.of(battleData)?.damageRoll?.call() ?? battleData.options.random')
    replace('models/battle.dart','    final curResult = options.threshold <= activationRate;',
        '    final seeded = BattleEngineHooks.of(this)?.probability;\n    if (seeded != null) return seeded(activationRate);\n    final curResult = options.threshold <= activationRate;')
    methods={
        'simpleAiAct(BattleData battleData, AiTiming timing)':'field:${timing.name}',
        'reactionWaveStart(BattleData battleData, BattleServantData actor)':'wave_start',
        'afterTurnPlayerEnd(BattleData battleData, BattleServantData actor)':'shift_after',
        'reactionTurnStart(BattleData battleData, BattleServantData actor)':'turn_start',
        'reactionPlayerActionStart(BattleData battleData, BattleServantData actor)':'player_action_start',
    }
    for signature,timing in methods.items():
        old='  Future<void> '+signature+' async {'
        subject='null' if signature.startswith('simpleAiAct') else 'actor'
        new=old+f"\n    if (BattleEngineHooks.of(battleData) != null) {{ await BattleEngineHooks.emit(battleData,'{timing}',{subject});return; }}"
        replace('models/ai.dart',old,new)
    old='  Future<void> actTurnStart(BattleData battleData) async {'
    replace('models/ai.dart',old,old+"\n    if (BattleEngineHooks.of(battleData) != null) { await BattleEngineHooks.emit(battleData,'field:turnStart');return; }")
    for method,timing in [('_endPlayerTurn','player_end'),('_endEnemyTurn','enemy_end')]:
        old=f'  Future<void> {method}() async {{'
        replace('models/battle.dart',old,old+f"\n    await BattleEngineHooks.emit(this,'{timing}');")
    old='  Future<bool> _nextWave() async {'
    replace('models/battle.dart',old,old+"\n    await BattleEngineHooks.emit(this,'before_wave');")
    old='''        await svt.startOfMyTurn(this);
      }
    });
  }

  Future<void> _endEnemyTurn()'''
    replace('models/battle.dart',old,old.replace('    });\n  }',"    });\n    await BattleEngineHooks.emit(this,'enemy_start');\n  }"))
    # A stage-3 hook sits after native turn-start buffs and card-deck maintenance.
    old='''  Future<bool> _nextWave() async {'''
    # Install at the end of _nextTurn using its following declaration as the boundary.
    path=base/'models/battle.dart';text=path.read_text(encoding='utf-8')
    marker="    await BattleEngineHooks.emit(this,'player_start');"
    if marker not in text:
        start=text.index('  Future<void> _nextTurn() async {');end=text.index('  Future<bool> _nextWave()',start)
        chunk=text[start:end];at=chunk.rfind('\n  }')
        if at<0:raise RuntimeError('Missing _nextTurn end')
        chunk=chunk[:at]+"\n"+marker+chunk[at:];text=text[:start]+chunk+text[end:];path.write_text(text,encoding='utf-8')
    replace('models/battle.dart','          await svt.activateSkill(this, skillIndex);',
        "          await svt.activateSkill(this, skillIndex);\n          await BattleEngineHooks.emit(this,'player_skill',svt);")
    replace('models/battle.dart','          await skillInfo.activate(this);\n          recorder.skill(',
        "          await skillInfo.activate(this);\n          await BattleEngineHooks.emit(this,'master_skill');\n          recorder.skill(")
    replace('models/battle.dart','                recorder.endPlayerCard(actor, actualCard);',
        "                recorder.endPlayerCard(actor, actualCard);\n                await BattleEngineHooks.emit(this,'player_action_end',actor);")
    replace('models/battle.dart','''                  actualCard.np = actor.np;
                  await actor.activateNP(this, actualCard, extraOvercharge);''',
        '''                  actualCard.np = actor.np;
                  await BattleEngineHooks.emit(this,'np_target',targetedEnemy);
                  await actor.activateNP(this, actualCard, extraOvercharge);
                  await BattleEngineHooks.emit(this,'player_np',actor);''')
    replace('models/battle.dart','        // end player turn',
        "        await BattleEngineHooks.emit(this,'combo_end');\n        // end player turn")
    replace('models/battle.dart','''      final actor = actorList[i]!;
      if (actor.hp > 0''','''      final actor = actorList[i]!;
      if (actor.hp <= 0 && !actor.hasNextShift(this)) await BattleEngineHooks.emit(this,'before_dead',actor);
      if (actor.hp > 0''')
    replace('models/battle.dart','        await actor.death(this);',
        "        await actor.death(this);\n        if (BattleEngineHooks.of(this) != null && actor.hp > 0) continue;")
    replace('models/svt_entity.dart','  Future<void> death(BattleData battleData) async {',
        "  Future<void> death(BattleData battleData) async {\n    await BattleEngineHooks.emit(battleData,'dead',this);")
    replace('models/svt_entity.dart','    await activateBuff(battleData, BuffAction.functionEntry);',
        "    await activateBuff(battleData, BuffAction.functionEntry);\n    await BattleEngineHooks.emit(battleData,'entry',this);")
    replace('models/svt_entity.dart','    niceEnemy = nextShift;',
        "    await BattleEngineHooks.emit(battleData,'shift_before',this);\n    niceEnemy = nextShift;")
    replace('models/svt_entity.dart','    if (gutsToApply != null) {',
        "    if (gutsToApply != null) {\n      await BattleEngineHooks.emit(battleData,'before_resurrection',this);")
    replace('functions/function_executor.dart','        case FuncType.fixCommandcard:',
        '''        case FuncType.addBattleValue:
        case FuncType.setBattleValue:
          final hook=BattleEngineHooks.of(battleData)?.battleValue;
          if(hook==null) { battleData.battleLogger.debug('not implemented battle value');break; }
          hook(function.funcType==FuncType.addBattleValue,function.vals,dataVals.Value ?? 0);
          for(final target in targets) {battleData.setFuncResult(target.uniqueId,true);}
          break;
        case FuncType.fixCommandcard:''')
