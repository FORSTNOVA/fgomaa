"""指定苹果恢复：赤铜按当前关卡AP缺口批量选择，提交一次并核对实际AP。"""
import re
import time
from pathlib import Path

import numpy as np
import cv2
from PIL import Image

from src.core.path_helper import get_resource_path
from src.strategy.screen_detector import ScreenDetector,GameScreen


APPLE_NAMES={'bronze':'赤铜果实','blue':'青铜果实','silver':'白银果实','gold':'黄金果实'}


class AppleLimitReached(ValueError):pass


def refill_count(apple,required_ap,available_ap):
    if available_ap>=required_ap:return 0
    return (required_ap-available_ap+9)//10 if apple=='bronze' else 1


def quantity_plus(image):
    """参考FGA的局部模板定位；模板来自本项目已核验的国服数量窗口。"""
    if image is None:raise ValueError('缺少数量按钮截图')
    with Image.open(get_resource_path('data/ui/ap_quantity_plus.png')) as template:
        pattern=cv2.cvtColor(np.array(template.convert('RGB')),cv2.COLOR_RGB2GRAY)
    region=cv2.cvtColor(np.array(image.convert('RGB'))[395:520,1305:1445],cv2.COLOR_RGB2GRAY)
    _,score,_,point=cv2.minMaxLoc(cv2.matchTemplate(region,pattern,cv2.TM_CCOEFF_NORMED))
    if score<.9:raise ValueError('未确认增加数量按钮，不调整或提交')
    return (1305+point[0]+pattern.shape[1]//2,395+point[1]+pattern.shape[0]//2)


def clean(text):return re.sub(r'\s+','',text)


def words(ocr,region):
    x1,y1,x2,y2=region
    return [(clean(t),sum(p[0] for p in b)/4,sum(p[1] for p in b)/4)
            for b,t,s in ocr if float(s)>=.55
            and x1<=sum(p[0] for p in b)/4<=x2 and y1<=sum(p[1] for p in b)/4<=y2]


def current_ap(ocr):
    values=[(int(m[1]),int(m[2])) for t,_,_ in words(ocr,(100,940,650,1060))
            if (m:=re.fullmatch(r'行动力(\d+)/(\d+)',t))]
    if len(values)!=1 or values[0][1]<=0:raise ValueError('未唯一识别实际行动力')
    return values[0]


def is_recovery_menu(ocr):
    top=[t for t,_,_ in words(ocr,(650,40,1300,155))]
    return '行动力回复' in top and '消耗道具回复行动力' in top


def apple_row(ocr,apple):
    if apple not in APPLE_NAMES:raise ValueError('只允许指定的苹果种类')
    if not is_recovery_menu(ocr):raise ValueError('不是行动力回复道具列表')
    names=[(x,y) for t,x,y in words(ocr,(650,180,1100,780)) if t==APPLE_NAMES[apple]]
    if len(names)!=1:return None
    x,y=names[0]
    counts=[int(m[1]) for t,_,_ in words(ocr,(1200,y-35,1470,y+35))
            if (m:=re.fullmatch(r'(\d+)个持有',t))]
    gains=[int(m[1]) for t,_,_ in words(ocr,(650,y+55,1450,min(y+125,855)))
           if '消耗1个' in t and (m:=re.search(r'\+(\d+)',t))]
    if len(counts)!=1 or len(gains)!=1:raise ValueError('不能核对所选苹果的库存或回复量')
    if counts[0]<=0:raise ValueError('所选苹果库存不足，不改用其他资源')
    return {'point':(round(x),round(y)),'inventory':counts[0],'gain':gains[0]}


def confirmation_open(ocr):
    return ('消耗果实回复行动力' in [t for t,_,_ in words(ocr,(600,50,1350,150))]
            or any(re.fullmatch(r'消耗\d+个'+re.escape(name),t)
                   for t,_,_ in words(ocr,(600,270,1400,360)) for name in APPLE_NAMES.values()))


def numeric_ap(image,region):
    """固定数字区按原方向独立重读，避免全屏OCR把冒号阴影拼成额外的1。"""
    crop=image.crop(region).convert('RGB');readings=[]
    for scale in (1,2):
        patch=np.array(crop.resize((crop.width*scale,crop.height*scale)))
        result,_=ScreenDetector.get_ocr().text_recognizer(patch)
        values=[(int(m[1]),int(m[2])) for text,score in result
                if float(score)>=.65 and (m:=re.fullmatch(r'(\d{1,4})/(\d{1,3})',clean(text)))]
        if len(values)!=1:raise ValueError('不能核对确认窗口的局部AP数字')
        readings.append(values[0])
    if readings[0]!=readings[1]:raise ValueError('确认窗口的两次局部AP识别不一致')
    return readings[0]


def button_point(ocr,label,region,center,threshold,image=None):
    points=[(round(sum(p[0] for p in b)/4),round(sum(p[1] for p in b)/4))
            for b,t,s in ocr if clean(t)==label and float(s)>=threshold
            and region[0]<=sum(p[0] for p in b)/4<=region[2]
            and region[1]<=sum(p[1] for p in b)/4<=region[3]]
    if len(points)>1:raise ValueError('苹果确认按钮不唯一')
    if not points and image is not None:
        x,y=center;crop=image.crop((x-90,y-35,x+90,y+35)).convert('RGB')
        for scale in (1,2):
            result,_=ScreenDetector.get_ocr().text_recognizer(np.array(crop.resize((180*scale,70*scale))))
            if len(result)!=1 or clean(result[0][0])!=label or float(result[0][1])<threshold:
                raise ValueError('苹果确认按钮的局部文字复核失败')
        points=[center]
    if not points:raise ValueError('未唯一确认苹果窗口的决定与取消按钮')
    return points[0]


def apple_confirmation(ocr,apple,image=None,expected_count=1):
    if apple not in APPLE_NAMES:raise ValueError('只允许指定的苹果种类')
    if type(expected_count) is not int or expected_count<1 or (apple!='bronze' and expected_count!=1):
        raise ValueError('仅赤铜果实允许按需批量使用')
    name=APPLE_NAMES[apple]
    def single(pattern,region):
        values=[m for t,_,_ in words(ocr,region) if (m:=re.fullmatch(pattern,t))]
        if len(values)!=1:raise ValueError('不能唯一核对苹果使用确认信息')
        return values[0]
    if '消耗果实回复行动力' in [t for t,_,_ in words(ocr,(600,50,1350,150))]:
        button_y=876
        headers=[t for t,_,_ in words(ocr,(650,160,1100,265))]
        if headers!=[name]:raise ValueError('确认窗口的苹果种类不符')
        count=int(single(re.escape(name)+r'的消耗数[:：](\d+)个',(650,500,1300,590))[1])
        before=single(r'现在行动力[:：](\d+)/(\d+)',(650,580,1300,650))
        after=single(r'回复后行动力[:：](\d+)/(\d+)',(650,660,1300,745))
        before_ap,max_ap=int(before[1]),int(before[2]);after_ap,after_max=int(after[1]),int(after[2])
        statement=[t for t,_,_ in words(ocr,(500,750,1450,820))]
        if f'是否执行消耗{name}来回复行动力？' not in statement:
            raise ValueError('确认文字不符，不提交苹果使用')
    else:
        button_y=846
        count=int(single(r'消耗(\d+)个'+re.escape(name),(600,270,1400,360))[1])
        gain=int(single(r'回复行动力(?:100|50)?%?[（(]\+(\d+)[）)]',(550,360,1450,415))[1])
        single(r'是否执行[？?]',(750,420,1150,475))
        single(r'当前行动力[:：].*',(650,520,1300,580))
        single(r'回复后行动力[:：].*',(650,580,1300,640))
        if image is None:raise ValueError('旧版确认窗口须局部复核AP数字')
        before_ap,max_ap=numeric_ap(image,(1025,520,1235,582))
        after_ap,after_max=numeric_ap(image,(1020,578,1280,640))
        if after_ap-before_ap!=gain:raise ValueError('确认窗口的回复量与AP变化不符')
    if count!=expected_count:raise ValueError('确认数量与计划不符，不使用果实')
    # 决定必须是唯一的完整按钮文字；取消的实机字形置信度略低，限定区域后允许0.5。
    buttons=[button_point(ocr,label,region,(x,button_y),threshold,image) for label,region,x,threshold in
             [('决定',(1000,800,1500,940),1260,.55),('取消',(400,800,950,940),660,.5)]]
    return {'before_ap':before_ap,'max_ap':max_ap,'after_ap':after_ap,
            'after_max_ap':after_max,'confirm':buttons[0],'cancel':buttons[1],'count':count}


class AppleRecovery:
    def __init__(self,controller,log_callback=None,cancel_event=None,report_dir=None):
        self.ctrl=controller;self.log=log_callback or (lambda t:None)
        self.cancel_event=cancel_event;self.report_dir=Path(report_dir) if report_dir else None

    def check_cancel(self):
        if self.cancel_event is not None and self.cancel_event.is_set():raise RuntimeError('任务已取消')

    def frame(self,label):
        self.check_cancel();image=self.ctrl.screencap(force_fresh=True)
        if self.report_dir:
            self.report_dir.mkdir(parents=True,exist_ok=True)
            image.save(self.report_dir/f'{time.time_ns()}_{label}.png')
        return image,ScreenDetector.run_ocr(image)

    def find_apple(self,apple):
        # 先看当前页，再回顶搜索；始终限制在已核对的恢复列表中拖动。
        for step in range(6):
            _,ocr=self.frame('menu')
            row=apple_row(ocr,apple)
            if row:return row
            self.check_cancel()
            if step<2:self.ctrl.swipe(1100,350,1100,745,duration_ms=300)
            elif step<5:self.ctrl.swipe(1100,745,1100,350,duration_ms=300)
        raise ValueError('恢复列表中未找到所选苹果')

    def close_menu(self,ocr):
        if is_recovery_menu(ocr):
            close=ScreenDetector.find_text_coordinate(ocr,'关闭',region=(700,860,1150,980))
            if close is None:raise ValueError('未确认恢复列表关闭按钮')
            self.check_cancel();self.ctrl.click(*close,delay_after=.5)

    def restore_once(self,apple,required_ap,on_submit,*,max_count=1):
        if apple not in APPLE_NAMES:raise ValueError('不允许使用苹果以外的资源')
        if type(max_count) is not int or not 1<=max_count<=999:raise ValueError('无效的剩余果实上限')
        self.check_cancel();image,ocr=self.frame('before')
        before,cap=current_ap(ocr)
        if before>=required_ap:return None
        screen,_,_=ScreenDetector.detect(image)
        if screen not in (GameScreen.MAIN_MENU,GameScreen.REGIONAL_MAP):
            raise ValueError('仅在已确认的大厅或关卡地图恢复体力')
        self.check_cancel()
        self.ctrl.click(282,1048,delay_after=.6)
        row=self.find_apple(apple)
        expected={'bronze':{10},'blue':{40},'silver':{cap//2,(cap+1)//2},'gold':{cap}}[apple]
        if row['gain'] not in expected:raise ValueError('所选苹果的回复量异常')
        self.check_cancel();self.ctrl.click(*row['point'],delay_after=.6)
        image,ocr=self.frame('confirmation');confirmation=apple_confirmation(ocr,apple,image)
        if (confirmation['max_ap']!=cap or confirmation['after_max_ap']!=cap
                or not before<=confirmation['before_ap']<=before+1
                or confirmation['after_ap']-confirmation['before_ap']!=row['gain']):
            raise ValueError('苹果确认窗口的AP变化不符')
        if confirmation['before_ap']>=required_ap:
            self.ctrl.click(*confirmation['cancel'],delay_after=.3)
            _,ocr=self.frame('cancelled');self.close_menu(ocr);return None
        count=refill_count(apple,required_ap,confirmation['before_ap'])
        if count>max_count:
            raise AppleLimitReached(f'本关需{count}个{APPLE_NAMES[apple]}，本次剩余上限{max_count}个，未使用')
        if count>row['inventory']:
            raise ValueError(f'本关需{count}个{APPLE_NAMES[apple]}，库存仅{row["inventory"]}个，未使用')
        if count>1:
            limits=[int(t) for t,_,_ in words(ocr,(1450,415,1580,500)) if re.fullmatch(r'\d+',t)]
            stocks=[int(m[1]) for t,_,_ in words(ocr,(1200,170,1480,250))
                    if (m:=re.fullmatch(r'(\d+)个持有',t))]
            if len(limits)!=1 or count>limits[0] or stocks!=[row['inventory']]:
                raise ValueError('数量窗口的库存或可用数量上限未通过核验，未使用')
            for selected in range(2,count+1):
                self.check_cancel();self.ctrl.click(*quantity_plus(image),delay_after=.3)
                # 每次只点一次加号，必须读到恰好+1才继续，不重试不明的点击。
                image,ocr=self.frame('quantity')
                updated=apple_confirmation(ocr,apple,image,expected_count=selected)
                if (updated['before_ap']!=confirmation['before_ap'] or updated['max_ap']!=cap
                        or updated['after_max_ap']!=cap
                        or updated['after_ap']-updated['before_ap']!=selected*row['gain']):
                    raise ValueError('调整数量后的AP或果实数量不符，未使用')
                confirmation=updated
        event={'apple':apple,'name':APPLE_NAMES[apple],'count':count,'inventory_before':row['inventory'],
               'before_ap':confirmation['before_ap'],'expected_after_ap':confirmation['after_ap'],'status':'pending'}
        self.check_cancel();on_submit(dict(event));self.check_cancel()
        self.ctrl.click(*confirmation['confirm'],delay_after=.6)
        # 只轮询结果，绝不重复点击决定。
        for _ in range(8):
            _,ocr=self.frame('after')
            try:actual,actual_cap=current_ap(ocr)
            except ValueError:continue
            if actual_cap==cap and confirmation['after_ap']<=actual<=confirmation['after_ap']+1:
                if confirmation_open(ocr):
                    self.ctrl._safe_sleep(.4);continue
                self.close_menu(ocr)
                closed_image,closed_ocr=self.frame('verified')
                verified,verified_cap=current_ap(closed_ocr)
                if verified_cap!=cap or not confirmation['after_ap']<=verified<=confirmation['after_ap']+1:
                    raise ValueError('关闭恢复窗口后AP未通过复核')
                if ScreenDetector.detect(closed_image)[0] not in (GameScreen.MAIN_MENU,GameScreen.REGIONAL_MAP):
                    raise ValueError('未确认恢复后已返回大厅或地图')
                self.log(f"[苹果恢复通过] {APPLE_NAMES[apple]} ×{count}，行动力 {confirmation['before_ap']}→{verified}。")
                return {**event,'status':'verified','after_ap':verified}
            self.ctrl._safe_sleep(.4)
        raise ValueError('苹果使用已提交但未确认AP增加，停止且不重复使用')
