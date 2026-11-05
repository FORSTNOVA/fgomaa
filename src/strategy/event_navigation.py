"""按活动ID单独适配的普通周回关卡；不自动推进活动剧情。"""
from functools import lru_cache
import cv2
import numpy as np
from src.strategy.ordeal_navigation import OrdealNavigator,normalize


HALLOWEEN_WAR = 9195
HALLOWEEN_EVENT = 80580
TITLE = '完结南瓜行星万圣节星球'
FARM_QUESTS = frozenset(range(94150101,94150110))
EVENT_WARS = {HALLOWEEN_WAR}


def chapter_id(text):
    value=normalize(text)
    return HALLOWEEN_WAR if TITLE in value or ('万圣节2026' in value and '终结' in value) else None


def supports_navigation(entry):
    return (entry.get('war_id')==HALLOWEEN_WAR and entry.get('event_id')==HALLOWEEN_EVENT
            and entry.get('id') in FARM_QUESTS and entry.get('after_clear')=='repeatLast')


def resolve_spot(entry):
    """该周回本解锁时地图已改名，导航时仍需在实机找到目标卡片。"""
    if supports_navigation(entry) and entry.get('spot_id')==919501:
        return {**entry,'spot':'新亚马逊平原'}
    return entry


@lru_cache(maxsize=1)
def map_features():
    from src.core.path_helper import get_resource_path
    with np.load(get_resource_path('data/navigation/halloween_2026_map.npz'),allow_pickle=False) as data:
        return data['points'].copy(),data['descriptors'].copy()


def matches_map_image(image):
    """活动没有章节标题图，使用公开地图背景特征核对，兼容拖动/遮挡。"""
    if image is None:
        return False
    frame=cv2.resize(np.asarray(image.convert('RGB')),(960,540),interpolation=cv2.INTER_AREA)
    gray=cv2.cvtColor(frame,cv2.COLOR_RGB2GRAY)
    mask=np.zeros_like(gray);mask[70:465,:]=255
    keypoints,descriptors=cv2.SIFT_create(nfeatures=1800).detectAndCompute(gray,mask)
    if descriptors is None or len(keypoints)<24:
        return False
    try:
        points,reference=map_features()
    except (OSError,ValueError,KeyError):
        return False
    matches=cv2.BFMatcher(cv2.NORM_L2).knnMatch(reference,descriptors,k=2)
    good=[a for pair in matches if len(pair)==2 for a,b in [pair] if a.distance<.7*b.distance]
    if len(good)<24:
        return False
    source=np.float32([points[m.queryIdx] for m in good])
    target=np.float32([keypoints[m.trainIdx].pt for m in good])
    # 资源PNG尺寸与游戏mapImageH可不同，允许受限的横纵独立缩放。
    transform,inliers=cv2.estimateAffine2D(source,target,method=cv2.RANSAC,ransacReprojThreshold=2.5)
    if transform is None or inliers is None:
        return False
    inliers=inliers.ravel().astype(bool)
    if inliers.sum()<18 or inliers.mean()<.55:
        return False
    width,height=np.ptp(source[inliers],axis=0)
    sx,sy=float(transform[0,0]),float(transform[1,1])
    return bool(width>=200 and height>=100 and width*height>=30000 and .35<sx<3.5 and .35<sy<3.5
                and .65<sx/sy<1.55 and abs(transform[0,1])<.05*sx and abs(transform[1,0])<.05*sy)


def map_identity(ocr,image=None):
    headers=[text for box,text,score in ocr if float(score)>=.55
             and 950<=sum(p[0] for p in box)/4<=1920 and sum(p[1] for p in box)/4<170]
    labels={normalize(text) for _,text,score in ocr if float(score)>=.55}
    warehouse_map=(bool({'管理室','关闭'} & labels)
                   and {'活动报酬','第一仓储库','第二仓储库'}.issubset(labels))
    if chapter_id(''.join(headers)) or warehouse_map or matches_map_image(image):
        return '万圣节星球',TITLE
    return None


def banner_coordinate(ocr):
    points=[(sum(p[0] for p in box)/4,sum(p[1] for p in box)/4)
            for box,text,score in ocr if float(score)>=.55 and TITLE in normalize(text)
            and 900<=sum(p[0] for p in box)/4<=1850 and 150<=sum(p[1] for p in box)/4<=810]
    return (round(points[0][0]),round(points[0][1])+60) if len(points)==1 else None


class EventNavigator(OrdealNavigator):
    def __init__(self,engine):
        super().__init__(engine,HALLOWEEN_WAR)

    def enter(self,ocr):
        pos=banner_coordinate(ocr)
        if pos:
            self.engine.log(f'[活动入口] 已识别【{TITLE}】，点击活动横幅。')
            self.engine.ctrl.click(*pos,delay_after=2)
            return True
        return self.scan_entries(ocr,label='活动')
