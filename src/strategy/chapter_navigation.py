"""第1.5部、第2部普通自由关卡：章节入口与地图身份核对。"""
import re
from src.strategy.ordeal_navigation import OrdealNavigator, normalize


# 不用“Lostbelt No.5”这种共用标题区分亚特兰蒂斯和奥林波斯。
CHAPTERS = {
    201: ('新宿', '亚种特异点Ⅰ 恶性隔绝魔境 新宿', ('恶性隔绝魔境', '新宿')),
    202: ('雅戈泰', '亚种特异点Ⅱ 传承地底世界 雅戈泰', ('传承地底世界', '雅戈泰')),
    203: ('下总国', '亚种平行世界 尸山血河舞台 下总国', ('尸山血河舞台', '下总国', '英灵剑豪七番胜负')),
    204: ('塞勒姆', '亚种特异点Ⅳ 禁忌降临庭园 塞勒姆', ('禁忌降临庭园', '塞勒姆', '异端塞勒姆')),
    301: ('阿纳斯塔西娅', 'Lostbelt No.1 永久冻土帝国 阿纳斯塔西娅', ('永久冻土帝国', '阿纳斯塔西娅')),
    302: ('诸神黄昏', 'Lostbelt No.2 无间冰焰世纪 诸神黄昏', ('无间冰焰世纪', '诸神黄昏')),
    303: ('人智统合真国', 'Lostbelt No.3 人智统合真国 SIN', ('人智统合真国', '红之月下美人', 'SIN')),
    304: ('由伽·刹多罗', 'Lostbelt No.4 创世灭亡轮回 由伽·刹多罗', ('创世灭亡轮回', '由伽·刹多罗', '由伽刹多罗')),
    305: ('亚特兰蒂斯', 'Lostbelt No.5 神代巨神海洋 亚特兰蒂斯', ('神代巨神海洋', '亚特兰蒂斯', '阿特兰蒂斯')),
    306: ('奥林波斯', 'Lostbelt No.5 星间都市山脉 奥林波斯', ('星间都市山脉', '奥林波斯')),
    307: ('平安京', '地狱界曼荼罗 平安京', ('地狱界曼荼罗', '平安京')),
    308: ('阿瓦隆·勒·菲', 'Lostbelt No.6 妖精圆桌领域 阿瓦隆·勒·菲', ('妖精圆桌领域', '阿瓦隆·勒·菲')),
    310: ('Traum', '死想显现界域 Traum', ('死想显现界域', 'Traum')),
    311: ('纳维·米克特兰', 'Lostbelt No.7 黄金树海纪行 纳维·米克特兰', ('黄金树海纪行', '纳维·米克特兰', '米克特兰')),
}
NAVIGATION_WARS = set(range(100,109)) | set(CHAPTERS) | {402,403,404,405}


def supports_navigation(entry):
    wid = entry.get('war_id')
    # 第七异闻带常驻章节的数据带eventId；只放行这一准确绑定，保留限时活动拦截。
    return wid in NAVIGATION_WARS and entry.get('event_id',0) in (0,80421 if wid==311 else 0)


def has_token(text, token):
    value, token = normalize(text), normalize(token)
    if token.isascii() and token.isalpha():
        return re.search(r'(?<![a-z])'+re.escape(token)+r'(?![a-z])', value) is not None
    return token in value


def chapter_id(text):
    matches = [wid for wid,(_,_,tokens) in CHAPTERS.items() if any(has_token(text,t) for t in tokens)]
    return matches[0] if len(matches) == 1 else None


def map_identity(ocr):
    # 只看右上章节标题；终端横幅、地名和关卡标题均不能证明当前地图身份。
    headers = [text for box,text,score in ocr if float(score) >= .55
               and 950 <= sum(p[0] for p in box)/4 <= 1920
               and 0 <= sum(p[1] for p in box)/4 <= 170]
    wid = chapter_id(' '.join(headers))
    return CHAPTERS[wid][:2] if wid else None


def banner_coordinate(ocr, wid):
    points = [(sum(p[0] for p in box)/4,sum(p[1] for p in box)/4)
              for box,text,score in ocr if float(score) >= .55
              and 900 <= sum(p[0] for p in box)/4 <= 1850
              and 150 <= sum(p[1] for p in box)/4 <= 890
              and any(has_token(text,t) for t in CHAPTERS[wid][2])]
    if not points or max(y for _,y in points)-min(y for _,y in points)>180:
        return None
    return tuple(round(sum(p[i] for p in points)/len(points)) for i in (0,1))


def record_number(text):
    text = normalize(text).replace('主线记录ii','主线记录2')
    match = re.search(r'主线记录(1\.5|[12])(?![\d.])',text)
    return match[1] if match else None


def record_part(ocr):
    headers = [text for box,text,score in ocr if float(score)>=.55 and sum(p[1] for p in box)/4<150]
    part = record_number(''.join(headers))
    if part:
        return part
    # 某些列表标题只显示“主线记录”，由可见的独特章节名核对分组。
    if not any('主线记录' in text for text in headers):
        return None
    ids = {chapter_id(text) for _,text,score in ocr if float(score)>=.55} - {None}
    parts = {'1.5' if wid < 300 else '2' for wid in ids}
    return parts.pop() if len(parts)==1 else None


def record_coordinate(ocr, part):
    points = []
    for box,text,score in ocr:
        x,y = sum(p[0] for p in box)/4,sum(p[1] for p in box)/4
        if float(score)<.55 or not (900<=x<=1850 and 150<=y<=890) or '主线记录' not in text:
            continue
        row = sorted((sum(p[0] for p in b)/4,t) for b,t,s in ocr if float(s)>=.55
                     and 900<=sum(p[0] for p in b)/4<=1850 and abs(sum(p[1] for p in b)/4-y)<55)
        if record_number(''.join(t for _,t in row))==part:
            points.append((1400,round(y)))
    return points[0] if len(points)==1 else None


class ChapterNavigator(OrdealNavigator):
    """共用有限列表扫描、地标坐标引导与扩展地图扫描。"""
    def __init__(self, engine, wid):
        super().__init__(engine,wid)
        self.context = None

    def enter(self, ocr, *, record=False):
        from src.strategy.screen_detector import ScreenDetector
        if self.context != record:
            self.context = record
            self.phase,self.still,self.terminal_image = 'top',0,None
        part = '1.5' if self.wid < 300 else '2'
        current_part = record_part(ocr) if record else None
        if record and current_part is not None and current_part != part:
            back = next((pos for text in ('关闭','返回','管理室') if
                         (pos := ScreenDetector.find_text_coordinate(ocr,text,region=(0,0,350,150))) is not None),None)
            if back is None:
                return False
            self.engine.log(f'[章节重定向] 当前为主线记录{current_part}，返回终端选择主线记录{part}。')
            self.engine.ctrl.click(*back,delay_after=1)
            return True
        pos = banner_coordinate(ocr,self.wid)
        if pos:
            self.engine.log(f'[章节入口] 已识别【{CHAPTERS[self.wid][1]}】，点击{pos}。')
            self.engine.ctrl.click(*pos,delay_after=2)
            return True
        if not record:
            pos = record_coordinate(ocr,part)
            if pos:
                self.engine.log(f'[主线记录入口] 点击第{part}部记录，进入后再次核对目标章节。')
                self.engine.ctrl.click(*pos,delay_after=2)
                return True
        return self.scan_entries(ocr,label='章节')
