"""从已展开的准确自由关卡卡片读取阶段；不依据旧账号快照推测实机阶段。"""
import re
import unicodedata
import time
from pathlib import Path

import cv2
import numpy as np

from src.strategy.screen_detector import ScreenDetector


def quest_title_rows(ocr, name):
    def clean(value):
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))
    target = clean(name)
    words = sorted((sum(p[0] for p in box)/4, sum(p[1] for p in box)/4, clean(text))
                   for box,text,score in ocr if float(score)>=.55
                   and 1100<sum(p[0] for p in box)/4<1850
                   and 150<sum(p[1] for p in box)/4<800)
    rows = []
    for index, (_, y, text) in enumerate(words):
        for _, _, part in (word for word in words[index+1:] if abs(word[1]-y)<12):
            if not target.startswith(text) or text==target:
                break
            text += part
        if text==target and not any(abs(y-old)<12 for old in rows):
            rows.append(y)
    return rows


def read_card_progress(image, ocr, entry):
    phases=entry.get('phases',[])
    if phases not in ([1],[1,2,3]):
        raise ValueError('该关卡的首次攻略阶段布局尚未适配')
    clean=lambda t:re.sub(r'\s+','',unicodedata.normalize('NFKC',t))
    titles=quest_title_rows(ocr,entry['name'])
    if len(titles)!=1:
        raise ValueError('不能唯一确认目标关卡标题')
    y=titles[0]
    headers=[(round(sum(p[0] for p in b)/4),round(sum(p[1] for p in b)/4))
             for b,t,s in ocr if float(s)>=.55 and clean(t) in ('任务进行度','任务进行店')
             and 1320<sum(p[0] for p in b)/4<1570 and y+40<sum(p[1] for p in b)/4<y+110]
    if len(headers)!=1:
        raise ValueError('未唯一识别关卡任务进行度')
    x,hy=headers[0]
    arr=np.asarray(image.convert('RGB').resize((1920,1080)))
    roi=arr[hy+6:hy+76,x-112:x+105]
    hsv=cv2.cvtColor(roi,cv2.COLOR_RGB2HSV)
    gray=(hsv[:,:,1]<60)&(hsv[:,:,2]>60)&(hsv[:,:,2]<190)
    blue=(hsv[:,:,0]>=95)&(hsv[:,:,0]<=120)&(hsv[:,:,1]>100)&(hsv[:,:,2]>140)
    _,labels,stats,_=cv2.connectedComponentsWithStats((gray|blue).astype('uint8'))
    glyphs=[]
    for i,(gx,gy,w,h,area) in enumerate(stats[1:],1):
        arrow=40<=w<=58 and 55<=h<=70 and 1100<=area<=2400
        infinity=65<=w<=(100 if len(phases)==1 else 90) and 45<=h<=70 and 2000<=area<=4300
        if not (arrow or infinity) or not 10<=gx<=180:continue
        component=labels==i
        br=float(np.count_nonzero(blue&component))/int(area)
        gr=float(np.count_nonzero(gray&component))/int(area)
        if max(br,gr)<.7:raise ValueError('阶段标记颜色不明确')
        glyphs.append((int(gx),'infinity' if infinity else 'arrow',br>.7))
    glyphs.sort()
    expected=['arrow','arrow','infinity'] if len(phases)==3 else ['infinity']
    if [g[1] for g in glyphs]!=expected:
        raise ValueError('阶段标记数量或形状未通过核对')
    if len(phases)==3 and any(abs(g[0]-v)>9 for g,v in zip(glyphs,[20,59,115])):
        raise ValueError('阶段标记位置异常')
    states=[g[2] for g in glyphs]
    completed=sum(states)
    if states!=[True]*completed+[False]*(len(phases)-completed):
        raise ValueError('阶段标记顺序矛盾')
    aps=[int(m[1]) for b,t,s in ocr if float(s)>=.55
         and 1120<sum(p[0] for p in b)/4<1340 and y+60<sum(p[1] for p in b)/4<y+160
         and (m:=re.fullmatch(r'AP(\d+)',clean(t),re.I))]
    if len(aps)!=1:raise ValueError('未唯一识别目标卡片的实际AP')
    available=[int(m[1]) for b,t,s in ocr if float(s)>=.55
               and sum(p[0] for p in b)/4<650 and sum(p[1] for p in b)/4>930
               and (m:=re.search(r'行动力(\d+)/\d+',clean(t)))]
    return {'quest_id':entry['id'],'next_phase':phases[completed] if completed<len(phases) else None,
            'completed':completed==len(phases),'completed_phases':completed,
            'card_ap':aps[0],'available_ap':available[0] if len(available)==1 else None}


def inspect_free_quest(engine, key, entry):
    if engine.is_cancelled():raise RuntimeError('任务已取消')
    from src.strategy.event_navigation import resolve_spot
    entry=resolve_spot(entry)
    if not engine.navigate_to_free_quest(entry['war'],entry['spot'],entry['name'],
                                         inspect_only=True,quest_key=key):
        raise ValueError('未能打开目标关卡卡片')
    observations=[]
    for _ in range(2):
        if engine.is_cancelled():raise RuntimeError('任务已取消')
        image=engine.ctrl.screencap(force_fresh=True)
        ocr=ScreenDetector.run_ocr(image)
        short,canonical=ScreenDetector.identify_regional_map(ocr,image)
        if not engine.is_target_map(short,canonical,entry['war']):
            raise ValueError('实机章节与目标不一致')
        if entry.get('kind')!='event' and engine._named_free_quest_card(ocr,entry['name'],check_ap=False) is None:
            raise ValueError('未核对到准确自由关卡卡片')
        observation=read_card_progress(image,ocr,entry)
        folder=getattr(engine,'first_clear_observation_dir',None)
        if folder:
            folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
            file=folder/f"{entry['id']}_{time.time_ns()}.png";image.save(file)
            observation['image']=str(file)
        observations.append(observation)
    fields=('quest_id','next_phase','completed','completed_phases','card_ap')
    if any(observations[0][k]!=observations[1][k] for k in fields):
        raise ValueError('连续两帧的关卡阶段不一致')
    return observations[-1]
