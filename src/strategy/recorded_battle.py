"""Compile a completed interactive recording to ordered live battle commands."""
from dataclasses import dataclass
import json

from src.strategy.interactive_simulation import InteractiveRecording, valid_id


@dataclass(frozen=True)
class RecordedAction:
    kind: str
    skill: int
    actor: int | None = None
    target: int | None = None
    swap: tuple[int, int] | None = None


@dataclass(frozen=True)
class RecordedTurn:
    wave: int
    turn: int
    actions: tuple[RecordedAction, ...]
    chain: tuple[int | None, ...]
    colors: tuple[str, ...]


def compile_recorded_battle(engine, recording_id):
    file = engine.root / 'recordings' / (valid_id(recording_id) + '.json')
    record = json.loads(file.read_text(encoding='utf-8'))
    recognition = record.pop('recognition', None)
    saved = InteractiveRecording.model_validate(record)
    if saved.version != 2 or not recognition:
        raise ValueError('请选择包含进场识别快照的新版战斗录制')
    entries = saved.entries[:saved.cursor + 1]
    first, last = entries[0], entries[-1]
    if not last.scene or not last.scene.get('won'):
        raise ValueError('请先录制到模拟通关，再从该历史位置启动实机刷本')
    path = engine.root / 'sessions' / (valid_id(first.session_id) + '.json')
    session = json.loads(path.read_text(encoding='utf-8'))
    from src.strategy.interactive_simulation import SetupDraft
    if SetupDraft.model_validate(session['draft']) != first.draft:
        raise ValueError('录制编队与进场快照不一致')
    request = session['payload']['request']
    if (recognition['quest']['game_id'], recognition['quest']['phase']) != (request['quest_id'], request['phase']):
        raise ValueError('录制关卡识别信息与进场快照不一致')
    party = request['team']
    if [(r['party_slot'], r['svt_id'], r.get('ce')) for r in party] != [
            (r['slot'], r['svt_id'], r.get('ce')) for r in recognition['party']]:
        raise ValueError('录制队伍识别信息与进场快照不一致')
    turns, actions = [], []
    before = first.scene
    for entry in entries[1:]:
        event = entry.event
        after = entry.scene
        if not before or not after or not after.get('accepted'):
            raise ValueError('录制缺少已确认的战斗状态')
        positions = {p['slot']:p['position'] for p in before['players'] if p['position'] > 0}

        def front(logical):
            slot = positions.get(logical)
            if slot not in (1, 2, 3):
                raise ValueError(f'录制第 {before["turn"]} 回合有无法核对的前排位置')
            return slot

        if event.kind in ('skill','master'):
            swap = None
            if event.swap:
                out, incoming = event.swap
                back = positions.get(incoming)
                if back not in (4, 5, 6):
                    raise ValueError('录制的换人目标不在后排')
                swap = front(out), back
            actions.append(RecordedAction(event.kind, event.skill,
                front(event.actor) if event.actor else None, front(event.target) if event.target else None, swap))
        elif event.kind == 'attack':
            attack = after.get('last_attack') or {}
            chain = tuple(front(card['slot']) if card['is_np'] else None for card in attack.get('chain', []))
            if len(chain) != 3:
                raise ValueError('录制的攻击需要完整的三卡指令')
            np_hit = next((hit for hit in attack.get('damage', []) if hit.get('is_np')), {})
            preferred = {1:'Arts', 2:'Buster', 3:'Quick'}.get(np_hit.get('card_type'), 'Arts')
            colors = (preferred, *(color for color in ('Arts','Buster','Quick') if color != preferred))
            turns.append(RecordedTurn(before['wave'], before['turn'], tuple(actions), chain, colors))
            actions.clear()
        elif event.kind not in ('cards','np','card','unselect','back','enemy_step','enemy_turn'):
            raise ValueError(f'实机执行尚未适配录制指令 {event.kind}')
        before = after
    if not turns or [turn.turn for turn in turns] != list(range(1, len(turns) + 1)):
        raise ValueError('录制缺少连续的战斗回合')
    return recognition, session, turns
