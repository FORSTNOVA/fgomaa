import 'package:chaldea/app/battle/models/battle.dart';

// Present only for the version-3 interactive scenario. Other modes keep their rules.
class BattleEngineHooks {
  static final Expando<BattleEngineHooks> _bindings=Expando();
  static BattleEngineHooks? of(BattleData battle)=>_bindings[battle];
  static void bind(BattleData battle,BattleEngineHooks hooks)=>_bindings[battle]=hooks;
  int Function()? damageRoll;
  bool Function(int rate)? probability;
  Future<void> Function(String timing,BattleServantData? subject)? reaction;
  void Function(bool add,List<int> ids,int value)? battleValue;
  static Future<void> emit(BattleData battle,String timing,[BattleServantData? subject]) async {
    await of(battle)?.reaction?.call(timing,subject);
  }
}
