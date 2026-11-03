"""奏章Ⅰ～Ⅳ：精确章节身份、终端/地球入口与地图拖动提示。"""
import re
import unicodedata

CHAPTERS = {
    402: ('奏章Ⅰ 虚数罗针内界 平面之月', ('虚数罗针内界', '平面之月')),
    403: ('奏章Ⅱ 不可逆废弃孔 伊底', ('不可逆废弃孔', '伊底')),
    404: ('奏章Ⅲ 新灵长后继战 原型肇始', ('新灵长后继战', '原型肇始')),
    405: ('奏章Ⅳ 人类裁决法庭 三位一体·梅塔特洛尼俄斯', ('人类裁决法庭', '三位一体', '梅塔特洛尼俄斯')),
}


def normalize(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold()


def chapter_id(text):
    value = normalize(text)
    matches = [wid for wid,(_,tokens) in CHAPTERS.items() if any(normalize(t) in value for t in tokens)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    for numeral,wid in [('IV',405),('III',404),('II',403),('I',402)]:
        if re.search(r'(?:奏章|ordealcall)'+numeral.casefold()+r'(?![iv])',value):
            return wid
    return None


def map_identity(ocr):
    headers = [text for box,text,score in ocr if float(score) >= .55
               and 950 <= sum(p[0] for p in box)/4 <= 1920
               and 0 <= sum(p[1] for p in box)/4 <= 170]
    wid = chapter_id(' '.join(headers))
    return (CHAPTERS[wid][0].split()[0],CHAPTERS[wid][0]) if wid else None


def is_selector(ocr):
    top = [normalize(text) for box,text,_ in ocr
           if sum(p[1] for p in box)/4 < 150 and sum(p[0] for p in box)/4 > 950]
    body = ''.join(text for _,text,_ in ocr)
    back = any(text in ('返回','关闭','管理室') and sum(p[0] for p in box)/4 < 350 and sum(p[1] for p in box)/4 < 150
               for box,text,_ in ocr)
    return back and 'ordealcall' in top and any(t in body for t in ('人理奠基盘','Rank','RANK'))


def banner_coordinate(ocr, wid):
    candidates = []
    for box,text,score in ocr:
        x,y = sum(p[0] for p in box)/4,sum(p[1] for p in box)/4
        if float(score) >= .55 and 900 <= x <= 1850 and 150 <= y <= 890:
            # 独特副标题优先于容易漏读/混淆的罗马数字，不使用泛称“奏章”。
            if any(normalize(token) in normalize(text) for token in CHAPTERS[wid][1]):
                candidates.append((x,y))
    if not candidates or max(y for _,y in candidates)-min(y for _,y in candidates) > 180:
        return None
    return tuple(int(sum(point[axis] for point in candidates)/len(candidates)) for axis in (0,1))


class OrdealNavigator:
    def __init__(self, engine, wid):
        self.engine = engine
        self.wid = wid
        self.phase = 'top'
        self.terminal_image = None
        self.still = 0
        self.war = None
        self.pan_count = 0

    def enter(self, ocr, *, selector=False):
        from src.strategy.screen_detector import ScreenDetector
        pos = banner_coordinate(ocr,self.wid)
        if pos:
            self.engine.log(f'[奏章入口] 已识别【{CHAPTERS[self.wid][0]}】，点击{pos}。')
            self.engine.ctrl.click(*pos,delay_after=2)
            return True
        if selector:
            back = next((point for text in ('返回','管理室')
                         if (point := ScreenDetector.find_text_coordinate(ocr,text,region=(0,0,350,150))) is not None),None)
            if back is None:
                return False
            self.engine.log('[奏章入口] 地球页当前未显示目标章节，返回终端重新定位。')
            self.engine.ctrl.click(*back,delay_after=1)
            return True
        return self.scan_entries(ocr)

    def scan_entries(self, ocr, *, label='奏章'):
        visible = [text for box,text,_ in ocr
                   if 950 <= sum(p[0] for p in box)/4 <= 1850 and 150 <= sum(p[1] for p in box)/4 <= 890]
        if not visible:
            self.engine.log(f'[{label}导航停止] 未读到章节入口列表。')
            return False
        from statistics import median
        from src.strategy.anchor_verifier import AnchorVerifier
        image = self.engine.ctrl.screencap(force_fresh=True)
        if self.terminal_image is not None:
            changes = [AnchorVerifier.compute_frame_diff(self.terminal_image,image,roi=(1050,y,1800,y+200))
                       for y in (200,425,650)]
            self.still = self.still + 1 if median(changes) < .006 else 0
        self.terminal_image = image
        if self.still >= 2:
            if self.phase == 'scan':
                self.engine.log(f'[{label}导航停止] 已扫描至列表底部，未找到目标章节。')
                return False
            self.phase = 'scan'
            self.still = 0
        self.engine.log(f'[{label}入口搜索] '+('先回到列表顶部。' if self.phase=='top' else '向下扫描章节入口。'))
        if self.phase == 'top':
            self.engine.ctrl.swipe(1400,350,1400,800,duration_ms=350)
        else:
            self.engine.ctrl.swipe(1400,800,1400,350,duration_ms=350)
        return True

    def pan(self, ocr, quest_key):
        """公开地点坐标只决定拖动方向；点击始终要求OCR读到真实地点。"""
        from src.strategy.quest_catalog import quest_binding
        from src.strategy.laplace import PublicData
        entry = quest_binding(quest_key) if quest_key else None
        if entry and entry.get('war_id') == self.wid:
            if self.war is None:
                self.war = PublicData().get('CN','war',self.wid)
            target = next((s for s in self.war['spots'] if s['id']==entry.get('spot_id')),None)
            if target:
                anchors = []
                for box,text,score in ocr:
                    x,y=sum(p[0] for p in box)/4,sum(p[1] for p in box)/4
                    if float(score)<.55 or not (80<x<1840 and 180<y<850):
                        continue
                    spots=[s for s in self.war['spots'] if s['mapId']==target['mapId']
                           and normalize(s['name'])==normalize(text) and s['name'] not in ('？？？','???')]
                    if spots and len({(s['mapId'],s['x'],s['y']) for s in spots})==1:
                        anchors.append((spots[0],x,y))
                if anchors:
                    anchor,x,y=min(anchors,key=lambda a:(a[0]['x']-target['x'])**2+(a[0]['y']-target['y'])**2)
                    dx,dy=target['x']-anchor['x'],target['y']-anchor['y']
                    if abs(dx)+abs(dy)>25:
                        scale_x,scale_y=2.,2.
                        for other,ox,oy in anchors:
                            if abs(other['x']-anchor['x'])>100:
                                scale=(ox-x)/(other['x']-anchor['x'])
                                if .3<scale<6:scale_x=scale
                            if abs(other['y']-anchor['y'])>70:
                                scale=(oy-y)/(other['y']-anchor['y'])
                                if .3<scale<6:scale_y=scale
                        end_x=int(max(250,min(1650,950-dx*scale_x)))
                        end_y=int(max(230,min(820,550-dy*scale_y)))
                        self.engine.log(f"[章节地图定位] 以已识别地标【{anchor['name']}】判断目标方向，拖动后重新识别。")
                        self.engine.ctrl.swipe(950,550,end_x,end_y,duration_ms=450)
                        return
        # 扩展螺旋扫描，避免旧四向循环回到同一位置而漏掉远处地点。
        index=self.pan_count
        leg=0
        while index >= leg//2+1:
            index-=leg//2+1
            leg+=1
        dx,dy=((600,0),(0,-350),(-600,0),(0,300))[leg%4]
        self.pan_count+=1
        self.engine.log(f'[章节地图搜索] 扩展扫描第{self.pan_count}次，继续OCR核对地点。')
        self.engine.ctrl.swipe(950,550,950+dx,550+dy,duration_ms=450)
