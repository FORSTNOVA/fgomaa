"""独立关卡模拟入口：依据账号从者与礼装配队，通过 Laplace 后才返回成功。"""

def prepare_quest(engine, plan):
    quest_name, war_name, spot_name = plan.name, plan.war, plan.spot
    quest_id = getattr(plan, "quest_id", "")
    callback = getattr(engine, "quest_progress_callback", None)
    if callback is not None:
        callback("simulating")
    if engine.team_builder:
        try:
            engine.current_recommendation = engine.team_builder.recommend_for_quest(
                target_quest=quest_name,
                war_name=war_name,
                spot_name=spot_name,
                quest_id=quest_id,
            )
            if not engine._ensure_laplace():
                return False
            engine.active_strategy = engine.current_recommendation.strategy
            engine.log(
                f"★ [关卡智能对策] 敌方职阶: {engine.current_recommendation.enemy_class_cn} | "
                f"首选打手: 1号位【{engine.current_recommendation.slot1_dps}】({engine.current_recommendation.slot1_class} {engine.current_recommendation.slot1_affinity}x克制) | "
                f"策略: {engine.current_recommendation.strategy.name}"
            )
        except Exception as e:
            engine.current_recommendation = None
            engine.active_strategy = None
            engine.log(f"[关卡停止] 智能配队推荐失败，不能沿用上一站策略: {e}")
            return False

    if not engine._ensure_laplace():
        return False
    return True
