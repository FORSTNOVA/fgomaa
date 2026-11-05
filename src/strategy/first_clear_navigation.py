"""首通视觉选关：橙色数字仅引导浏览，地图身份和准确标题授权选关。"""
import re
import time

import cv2
import numpy as np

from src.strategy.ordeal_navigation import normalize, OrdealNavigator
from src.strategy.screen_detector import GameScreen, ScreenDetector


def number_badges(image, ocr):
    """在1920×1080基准图中寻找橙底白边圆形数字，排除底栏礼物数量。"""
    rgb=np.asarray(image.convert('RGB').resize((1920,1080)))
    hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV)
    mask=cv2.inRange(hsv,np.array([5,130,180]),np.array([28,255,255]))
    contours,_=cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    result=[]
    for contour in contours:
        x,y,w,h=cv2.boundingRect(contour)
        if not (30<=w<=90 and 30<=h<=90 and .8<=w/h<=1.2 and 140<=y and y+h<890):continue
        area=cv2.contourArea(contour)
        if not .55<=area/(w*h)<=.88:continue
        cx,cy=x+w/2,y+h/2
        # 数字造成内孔，白边应位于橙色轮廓的外侧。
        yy,xx=np.ogrid[:1080,:1920]
        radius=(w+h)/4
        ring=((xx-cx)**2+(yy-cy)**2>(radius+1)**2)&((xx-cx)**2+(yy-cy)**2<(radius+6)**2)
        white=(hsv[:,:,1]<65)&(hsv[:,:,2]>210)
        if np.count_nonzero(white&ring)/max(1,np.count_nonzero(ring))<.45:continue
        digits=[normalize(t) for b,t,s in ocr if float(s)>=.55
                and x<=sum(p[0] for p in b)/4<=x+w and y<=sum(p[1] for p in b)/4<=y+h
                and re.fullmatch(r'[1-9][0-9]{0,2}',normalize(t))]
        if len(digits)!=1:
            # 直接调用文字识别器，避免孤立的“1”被文本检测器漏掉。
            readings=[]
            for scale in (.60,.66):
                dx,dy=round(w*scale/2),round(h*.66/2)
                crop=rgb[round(cy)-dy:round(cy)+dy,round(cx)-dx:round(cx)+dx]
                local,_=ScreenDetector.get_ocr().text_recognizer([crop[:,:,::-1].copy()])
                readings.append([normalize(t) for t,s in local if float(s)>=.4
                                 and re.fullmatch(r'[1-9][0-9]{0,2}',normalize(t))])
            digits=readings[0] if readings[0]==readings[1] else []
        if len(digits)==1:result.append({'x':round(cx),'y':round(cy),'count':int(digits[0])})
    return sorted(result,key=lambda b:(b['y'],b['x']))


def nearby_text(ocr,badge):
    return ' '.join(t for b,t,s in ocr if float(s)>=.55
                    and 950<sum(p[0] for p in b)/4<1850
                    and badge['y']-45<sum(p[1] for p in b)/4<badge['y']+170)


class FirstClearNavigator:
    def __init__(self,engine):
        self.engine=engine

    def select(self,candidates,max_steps=120):
        """只返回目录内的目标；没有可确认的目标时明确停止，不声称全部通关。"""
        engine=self.engine
        tried=set();exhausted=set();scans={};panners={};repeats={}
        folder=getattr(engine,'first_clear_observation_dir',None)
        for step in range(max_steps):
            if engine.is_cancelled():raise RuntimeError('任务已取消')
            image=engine.ctrl.screencap(force_fresh=True)
            screen,_,details=ScreenDetector.detect(image)
            ocr=details.get('ocr_results',[])
            # 大厅顶部轮播和底部AP计时会持续变化，不能作为列表是否移动的依据。
            signature=(screen.value,tuple((normalize(t),round(sum(p[0] for p in b)/80),round(sum(p[1] for p in b)/80))
                                         for b,t,s in ocr if float(s)>=.55
                                         and 140<sum(p[1] for p in b)/4<890
                                         and (screen!=GameScreen.MAIN_MENU or
                                              (950<sum(p[0] for p in b)/4<1850 and sum(p[1] for p in b)/4>430))))
            if folder:
                folder.mkdir(parents=True,exist_ok=True)
                image.save(folder/'selection_latest.png')
            if screen in (GameScreen.LOADING,GameScreen.UNKNOWN):
                time.sleep(.5);continue
            repeats[signature]=repeats.get(signature,0)+1
            if repeats[signature]>6:
                raise RuntimeError('首通导航画面重复且未推进，已停止；请检查角标或地图切层')
            if screen not in (GameScreen.MAIN_MENU,GameScreen.MAIN_RECORD,GameScreen.ORDEAL_SELECT,GameScreen.REGIONAL_MAP):
                raise RuntimeError(f'首通选关需要终端或地图，当前为{screen.value}')
            badges=number_badges(image,ocr)
            if screen==GameScreen.REGIONAL_MAP:
                short,canonical=details.get('map_name','未知地图'),details.get('current_map','未知地图')
                rows=[r for r in candidates if engine.is_target_map(short,canonical,r['war'])]
                context=('map',canonical)
                if rows and rows[0]['war_id'] not in exhausted:
                    # 已展开卡片（包括战后仍保留的卡片）优先核对，不需要重复点角标。
                    matches=[r for r in rows if any(normalize(t)==normalize(r['name']) for _,t,s in ocr if float(s)>=.55)
                             and engine._named_free_quest_card(ocr,r['name'],check_ap=False)]
                    if matches:
                        engine.log(f"[首通视觉选关] 已核对地图及关卡【{matches[0]['name']}】，读取阶段后模拟配队。")
                        return matches[0]
                    close=ScreenDetector.find_text_coordinate(ocr,'关闭',region=(0,0,350,150))
                    if close:
                        drawer=('drawer',canonical)
                        scans[drawer]=scans.get(drawer,0)+1
                        if scans[drawer]<=4:
                            engine.log('[首通卡片列表] 当前未见目录内的未通关卡片，向下扫描关卡抽屉。')
                            engine.ctrl.swipe(1500,790,1500,350,duration_ms=350);continue
                        engine.ctrl.click(*close,delay_after=.8);continue
                    for badge in badges:
                        token=(context,scans.get(context,0),round(badge['x']/24),round(badge['y']/24),badge['count'])
                        if token in tried:continue
                        tried.add(token)
                        scans[('drawer',canonical)]=0
                        engine.log(f"[首通地点角标] 未完成提示{badge['count']}，点击({badge['x']},{badge['y']})后核对关卡。")
                        engine.ctrl.click(badge['x'],badge['y'],delay_after=1.5)
                        break
                    else:
                        wid=rows[0]['war_id']
                        pan=panners.setdefault(wid,OrdealNavigator(engine,wid))
                        scans[context]=scans.get(context,0)+1
                        if scans[context]<=12:
                            pan.pan(ocr,None);continue
                        exhausted.add(wid)
                        engine.log(f'[首通地图跳过] 【{canonical}】有限扫描未找到可匹配的角标关卡，返回其他章节。')
                        self.back(ocr);continue
                    continue
                self.back(ocr);continue
            context=(screen.value,() if screen==GameScreen.MAIN_MENU else
                     tuple(normalize(t) for b,t,s in ocr if float(s)>=.55
                           and 950<sum(p[0] for p in b)/4 and sum(p[1] for p in b)/4<140))
            for badge in badges:
                if badge['x']<950:continue
                label=nearby_text(ocr,badge)
                rows=[r for r in candidates if r['war_id'] not in exhausted and
                      any(normalize(t) in normalize(label) for t in engine._get_war_search_tokens(r['war']))]
                # 主线记录是父入口；进入后必须重新匹配真实章节。
                parent=screen==GameScreen.MAIN_MENU and (
                    '主线记录' in label or ('主线物语' in label and
                    any(r['war_id']==310 and 310 not in exhausted for r in candidates)))
                if not rows and not parent:continue
                token=(context,normalize(label),badge['count'])
                if token in tried:continue
                tried.add(token)
                engine.log(f"[首通章节角标] 未完成提示{badge['count']}：{label}；进入后继续核对。")
                engine.ctrl.click(1450,min(870,badge['y']+70),delay_after=1.5)
                break
            else:
                # 先找列表顶部，再向下完整扫描；每个列表有独立进度。
                scan=scans.setdefault(context,{'direction':'top','seen':set(),'steps':0})
                scan['steps']+=1
                if signature in scan['seen'] or (scan['direction']=='top' and scan['steps']>=4):
                    if scan['direction']=='top':
                        scan.update(direction='down',seen=set())
                    else:
                        if screen==GameScreen.MAIN_MENU:
                            raise RuntimeError('角标扫描结束，仍有目录候选但未找到可确认的未通关入口；请检查截图或更新账号记录')
                        self.back(ocr);continue
                scan['seen'].add(signature)
                if scan['steps']>24:raise RuntimeError('章节列表扫描达到上限，未找到可核对的角标关卡')
                top=scan['direction']=='top'
                engine.log('[首通入口扫描] '+('定位列表顶部。' if top else '向下查找数字角标。'))
                engine.ctrl.swipe(1400,350 if top else 800,1400,800 if top else 350,duration_ms=400)
                continue
        raise RuntimeError('首通角标导航达到步骤上限，未核对到关卡')

    def back(self,ocr):
        point=next((p for text in ('管理室','关闭','返回') if
                    (p:=ScreenDetector.find_text_coordinate(ocr,text,region=(0,0,350,150))) is not None),None)
        if point is None:raise RuntimeError('未识别返回入口，停止首通导航')
        self.engine.ctrl.click(*point,delay_after=1)
