"""逐步战场的异步计算与本地录制接口。"""
import copy
import json
import threading
import uuid
from urllib.parse import parse_qs, urlparse
from src.strategy.interactive_simulation import InteractiveSimulation, SetupDraft, compile_events, valid_id
from src.strategy.manual_simulation import validate_ownership
from src.strategy.laplace import LaplaceService


def handle(handler,state,path,body=None):
    if body is None:
        try:
            if path=='/api/interactive/status':value={'report':getattr(state,'interactive_report',None)}
            elif path=='/api/interactive/recordings':value={'recordings':InteractiveSimulation().recordings()}
            elif path=='/api/interactive/visuals':
                from src.server.battle_assets import visual_metadata
                identity=parse_qs(urlparse(handler.path).query).get('session_id',[''])[0]
                value=visual_metadata(InteractiveSimulation(),identity)
            elif path=='/api/interactive/runtime':
                value=json.loads((LaplaceService().runtime/'manifest.json').read_text(encoding='utf-8'))
            else:handler._send_json({'success':False,'message':'接口不存在'},404);return
            handler._send_json({'success':True,**value})
        except Exception as exc:handler._send_json({'success':False,'message':str(exc)},400)
        return
    try:
        if not isinstance(body,dict):raise ValueError('请求须为JSON对象')
        if path=='/api/interactive/farm':
            if set(body)!={'recording_id','device_serial'} or not isinstance(body['device_serial'],str) or not body['device_serial']:
                raise ValueError('请选择录制和实机设备')
            from src.strategy.quest_catalog import catalog_sync
            from src.workflows.recorded_farm import RecordedFarmRunner
            recording_id=valid_id(body['recording_id'])
            with state.task_lock:
                if state.is_running or catalog_sync().running:
                    handler._send_json({'success':False,'message':'已有任务或关卡库同步正在运行'},409);return
                parser=copy.deepcopy(state.chaldea_parser)
                state.cancel_event.clear();state.is_running=True
            def farm():
                try:
                    report=RecordedFarmRunner(parser,state.device_mgr,log=lambda line:state.add_log(line,'info')).run(
                        recording_id,body['device_serial'],cancel_event=state.cancel_event)
                    state.add_log('录制实机刷本完成一场' if report['confirmed_clears'] else '录制实机刷本停止：'+report.get('error',''),
                                  'success' if report['confirmed_clears'] else 'error')
                except Exception as exc:
                    state.add_log('录制实机刷本停止：'+str(exc),'error')
                finally:
                    with state.task_lock:state.is_running=False
            threading.Thread(target=farm,daemon=True).start()
            handler._send_json({'success':True,'message':'已开始核对实机关卡和队伍，请查看运行日志。'})
            return
        if path=='/api/interactive/stop':
            if set(body)!={'request_id'}:raise ValueError('请指定模拟任务ID')
            with state.task_lock:
                matched=bool(getattr(state,'active_interactive_id','')) and state.active_interactive_id==body['request_id']
                if matched:state.cancel_event.set()
            handler._send_json({'success':True,'cancelled':matched});return
        if path=='/api/interactive/save':
            if set(body)!={'recording'}:raise ValueError('录制保存参数无效')
            with state.task_lock:value=InteractiveSimulation().save_recording(body['recording'])
            handler._send_json({'success':True,'saved':value});return
        if path not in ('/api/interactive/enter','/api/interactive/replay'):
            handler._send_json({'success':False,'message':'接口不存在'},404);return
        enter=path.endswith('/enter')
        allowed={'draft','client_revision'} if enter else {'session_id','events','client_revision'}
        if set(body)-allowed:raise ValueError('逐步模拟参数无效')
        revision=body.get('client_revision',0)
        if type(revision) is not int or revision<0:raise ValueError('编辑版本号无效')
        if enter:
            draft=SetupDraft.model_validate(body.get('draft'));validate_ownership(draft.ready_plan(),state.chaldea_parser)
        else:
            valid_id(body.get('session_id'))
            if not isinstance(body.get('events'),list) or len(body['events'])>600:raise ValueError('操作列表无效')
            compile_events(body['events'],version=0)
        from src.strategy.quest_catalog import catalog_sync
        with state.task_lock:
            if state.is_running or catalog_sync().running:
                handler._send_json({'success':False,'message':'已有任务或关卡库同步正在运行'},409);return
            parser=copy.deepcopy(state.chaldea_parser) if enter else None
            identity=uuid.uuid4().hex;state.active_interactive_id=identity
            state.cancel_event.clear();state.is_running=True
            state.interactive_report={'request_id':identity,'client_revision':revision,'status':'running'}
        def worker():
            report={'request_id':identity,'client_revision':revision,'status':'running'}
            try:
                engine=InteractiveSimulation()
                result=engine.enter(draft.model_dump(),parser,state.cancel_event) if enter else engine.replay(body['session_id'],body['events'],state.cancel_event)
                report.update(status='completed',result=result)
            except Exception as exc:report.update(status='cancelled' if state.cancel_event.is_set() else 'failed',error=str(exc))
            finally:
                if state.cancel_event.is_set():report.update(status='cancelled');report.pop('result',None)
                with state.task_lock:
                    state.interactive_report=report;state.active_interactive_id='';state.is_running=False
        threading.Thread(target=worker,daemon=True).start()
        handler._send_json({'success':True,'request_id':identity})
    except (ValueError,TypeError) as exc:handler._send_json({'success':False,'message':str(exc)},400)
    except Exception as exc:handler._send_json({'success':False,'message':str(exc)},500)
