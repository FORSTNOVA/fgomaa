"""手动模拟 API：冻结输入、可取消任务、本地方案保存。"""
import copy
import json
import threading
import uuid
from urllib.parse import parse_qs

from src.core.path_helper import get_root_dir
from src.strategy.manual_simulation import ManualPlan, ManualPlanStore, ManualSimulation, inventory, support_metadata, validate_ownership


def validate_plan(value, parser):
    from src.workflows.battle_runner import simulation_catalog
    plan = ManualPlan.model_validate(value)
    row = next((r for r in simulation_catalog(parser) if r['quest_id'] == plan.quest_id),None)
    if not row or not row['simulation_supported']:
        raise ValueError('该关卡尚不能进行准确的普通战斗模拟')
    validate_ownership(plan,parser)
    return plan


def handle_get(handler, state, path, url):
    if not path.startswith('/api/manual/'):
        return False
    try:
        if path == '/api/manual/inventory':
            result = inventory(state.chaldea_parser)
        elif path == '/api/manual/support':
            identity = int(parse_qs(url.query).get('svt_id',['0'])[0])
            if identity < 1:raise ValueError('请选择有效助战角色')
            result = support_metadata(identity)
        elif path == '/api/manual/mystic-code':
            identity = int(parse_qs(url.query).get('id',['0'])[0])
            if identity not in state.chaldea_parser.mystic_codes:raise ValueError('没有该御主礼装的账号等级记录')
            from src.strategy.laplace import PublicData
            code = PublicData().get('CN','MC',identity)
            result = {'id':identity,'name':code['name'],'lv':state.chaldea_parser.mystic_codes[identity],
                      'skills':[{'name':r['name'],'num':r['num']} for r in code['skills']]}
        elif path == '/api/manual/plans':
            result = {'plans':ManualPlanStore().list()}
        elif path == '/api/manual/status':
            result = {'report':getattr(state,'manual_report',None)}
        else:
            handler._send_json({'success':False,'message':'接口不存在'},404)
            return True
        handler._send_json({'success':True,**result})
    except (ValueError,TypeError) as exc:
        handler._send_json({'success':False,'message':str(exc)},400)
    except Exception as exc:
        handler._send_json({'success':False,'message':str(exc)},500)
    return True


def handle_post(handler, state, path, body):
    if not path.startswith('/api/manual/'):
        return False
    try:
        if not isinstance(body,dict):raise ValueError('请求须为JSON对象')
        if path == '/api/manual/stop':
            if set(body) != {'request_id'} or not isinstance(body['request_id'],str):
                raise ValueError('请指定要中断的模拟任务')
            with state.task_lock:
                matched = bool(getattr(state,'active_manual_id','')) and state.active_manual_id == body['request_id']
                if matched:state.cancel_event.set()
            handler._send_json({'success':True,'cancelled':matched})
            return True
        if path == '/api/manual/plans/save':
            if set(body)-{'name','plan','id'}:raise ValueError('保存方案参数无效')
            plan = validate_plan(body.get('plan'),state.chaldea_parser)
            with state.task_lock:
                value = ManualPlanStore().save(body.get('name'),plan.model_dump(),body.get('id'))
            handler._send_json({'success':True,'saved':value})
            return True
        if path != '/api/manual/start':
            handler._send_json({'success':False,'message':'接口不存在'},404)
            return True
        if set(body)-{'plan','client_revision'}:raise ValueError('手动模拟参数无效')
        revision = body.get('client_revision',0)
        if type(revision) is not int or revision < 0:raise ValueError('无效编辑版本号')
        plan = validate_plan(body.get('plan'),state.chaldea_parser)
        from src.strategy.quest_catalog import catalog_sync
        with state.task_lock:
            if state.is_running or catalog_sync().running:
                handler._send_json({'success':False,'message':'请先中断当前任务并等待退出，再开始新的模拟'},409)
                return True
            parser = copy.deepcopy(state.chaldea_parser)
            identity = uuid.uuid4().hex
            state.active_manual_id = identity
            state.is_running = True
            state.cancel_event.clear()
            state.manual_report = {'request_id':identity,'client_revision':revision,'status':'running','mode':'manual'}
        def worker():
            report = dict(state.manual_report)
            folder = get_root_dir()/'.app_profile/manual_runs'/identity
            try:
                result = ManualSimulation().run(plan,parser,state.cancel_event)
                report.update(status='completed',result=result)
            except Exception as exc:
                report.update(status='cancelled' if state.cancel_event.is_set() else 'failed',error=str(exc))
            finally:
                if state.cancel_event.is_set():
                    report.update(status='cancelled')
                    report.pop('result',None)
                try:
                    folder.mkdir(parents=True,exist_ok=True)
                    (folder/'report.json').write_text(json.dumps({**report,'plan':plan.model_dump()},ensure_ascii=False,indent=2),encoding='utf-8')
                except OSError as exc:
                    report['save_error'] = str(exc)
                with state.task_lock:
                    state.manual_report = report
                    state.active_manual_id = ''
                    state.is_running = False
        threading.Thread(target=worker,daemon=True).start()
        handler._send_json({'success':True,'request_id':identity,'message':'手动模拟已开始，仅按本次输入顺序计算'})
    except (ValueError,TypeError) as exc:
        handler._send_json({'success':False,'message':str(exc)},400)
    except Exception as exc:
        handler._send_json({'success':False,'message':str(exc)},500)
    return True
