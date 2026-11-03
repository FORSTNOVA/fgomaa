"""Atlas国服关卡全量目录：后台条件刷新、原子快照和精确ID绑定。"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
import uuid

from src.core.path_helper import get_root_dir, get_resource_path

EXPORT_URL = 'https://api.atlasacademy.io/export/CN/nice_war.json'
INFO_URL = 'https://api.atlasacademy.io/info'
REFRESH_SECONDS = 24 * 3600
MAX_BYTES = 96 * 1024 * 1024


def fetch_json(url, headers):
    request = urllib.request.Request(url, headers={'User-Agent':'FGO-MAA-QuestCatalog/1', 'Accept-Encoding':'gzip', **headers})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('关卡导出超过大小限制')
                if response.headers.get('Content-Encoding') == 'gzip':
                    import io
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                        raw = stream.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError('关卡解压后超过大小限制')
                return response.status, dict(response.headers), json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, dict(exc.headers), None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(.5 * (attempt + 1))


def normalize_export(wars):
    if not isinstance(wars, list) or not wars:
        raise ValueError('章节导出不是非空列表')
    entries = {}
    for war in wars:
        if not isinstance(war, dict) or type(war.get('id')) is not int or not isinstance(war.get('spots'), list):
            raise ValueError('章节数据格式无效')
        for spot in war['spots']:
            if type(spot.get('id')) is not int or not isinstance(spot.get('quests'), list):
                raise ValueError('地点数据格式无效')
            free_count = sum(q.get('type') == 'free' for q in spot['quests'])
            for quest in spot['quests']:
                phases = quest.get('phases')
                if (type(quest.get('id')) is not int or not isinstance(quest.get('name'), str)
                        or not isinstance(phases, list)
                        or any(type(p) is not int or p <= 0 for p in phases)):
                    raise ValueError('关卡ID、名称或阶段无效')
                qid = quest['id']
                key = f'atlas_cn_{qid}'
                kind = 'daily' if war['id'] == 1002 else quest.get('type', 'unknown')
                entry = {'id':qid, 'phase':max(phases, default=0), 'phases':phases,
                         'name':quest['name'], 'spot':spot.get('name',''),
                         'war':' '.join(war.get('longName', war.get('name','')).split()),
                         'war_id':war['id'], 'spot_id':spot['id'], 'kind':kind,
                         'quest_type':quest.get('type','unknown'), 'event_id':war.get('eventId',0),
                         'after_clear':quest.get('afterClear',''), 'consume_type':quest.get('consumeType',''),
                         'ap':quest.get('consume') if quest.get('consumeType') == 'ap' else None,
                         'opened_at':quest.get('openedAt',0), 'closed_at':quest.get('closedAt',0),
                         'flags':quest.get('flags',[]), 'release_conditions':quest.get('releaseConditions',[]),
                         'spot_free_quest_count':free_count,
                         'source':EXPORT_URL}
                if key in entries and entries[key] != entry:
                    raise ValueError(f'关卡{qid}的重复记录不一致，未替换旧目录')
                entries[key] = entry
    if not entries:
        raise ValueError('导出中没有关卡，未替换旧目录')
    return entries


class QuestCatalogSync:
    def __init__(self, root=None, fetch=None, clock=None):
        self.root = Path(root or get_root_dir()/'.app_profile/quest_catalog')
        self.fetch = fetch or fetch_json
        self.clock = clock or time.time
        self.lock = threading.Lock()
        self.running = False
        self.error = ''
        self.message = ''
        self._stamp = None
        self._snapshot = None
        self._retry_after = 0

    def snapshot(self):
        file = self.root/'current.json'
        stamp = file.stat().st_mtime_ns if file.exists() else None
        if self._snapshot is not None and stamp == self._stamp:
            return self._snapshot
        for candidate in (file, self.root/'previous.json'):
            try:
                data = json.loads(candidate.read_text(encoding='utf-8'))
                if data.get('schema') != 1 or not isinstance(data.get('entries'), dict) or not data['entries']:
                    continue
                self._snapshot, self._stamp = data, stamp
                return data
            except (OSError, ValueError):
                continue
        return {}

    def status(self):
        data = self.snapshot()
        stats = data.get('stats', {})
        return {'running':self.running, 'message':self.message, 'error':self.error,
                'last_checked':data.get('checked_at'), 'last_updated':data.get('synced_at'),
                'revision':data.get('revision',''), 'source':EXPORT_URL,
                'auto_refresh_hours':24, 'stats':stats, 'has_snapshot':bool(data)}

    def _publish(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        file = self.root/'current.json'
        temporary = self.root/f'{uuid.uuid4().hex}.tmp'
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',',':')), encoding='utf-8')
        if file.exists() and self._snapshot:
            backup = self.root/'previous.tmp'
            # Preserve the last validated snapshot even if the current file was damaged.
            backup.write_text(json.dumps(self._snapshot, ensure_ascii=False, separators=(',',':')), encoding='utf-8')
            backup.replace(self.root/'previous.json')
        temporary.replace(file)
        self._stamp = None
        self._snapshot = None

    def due(self):
        data = self.snapshot()
        return self.clock() >= self._retry_after and (not data or self.clock()-data.get('checked_at',0) >= REFRESH_SECONDS)

    def start(self, *, force=False, log=None):
        with self.lock:
            if self.running or (not force and not self.due()):
                return False
            self.running = True
            self.error = ''
            self.message = '正在同步Atlas国服全量关卡目录'
        def worker():
            self._refresh(force, log)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def refresh(self, *, force=False, log=None):
        """CLI和验证使用的同步入口；网络工作从不持有状态锁。"""
        with self.lock:
            if self.running:
                return False
            self.running = True
        return self._refresh(force, log)

    def _refresh(self, force, log):
        old = self.snapshot()
        try:
            status, _, info = self.fetch(INFO_URL, {})
            if status != 200 or not isinstance(info, dict) or not isinstance(info.get('CN'), dict) or not info['CN'].get('hash'):
                raise ValueError('无法核对Atlas国服数据版本')
            headers = {}
            if old and not force:
                if old.get('etag'):
                    headers['If-None-Match'] = old['etag']
                if old.get('last_modified'):
                    headers['If-Modified-Since'] = old['last_modified']
            status, response_headers, wars = self.fetch(EXPORT_URL, headers)
            now = self.clock()
            revision = str(info['CN']['hash']) + ':' + str(info.get('JP',{}).get('hash',''))
            if status == 304:
                if not old:
                    raise ValueError('服务器返回未修改，但本地没有完整目录')
                data = {**old, 'checked_at':now, 'revision':revision}
                if revision != old.get('revision'):
                    data['enemy_refresh_after'] = now
                self.message = '关卡目录已是最新（服务器返回304）'
            elif status == 200:
                entries = normalize_export(wars)
                digest = hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                data = {'schema':1, 'region':'CN', 'source':EXPORT_URL, 'checked_at':now, 'synced_at':now,
                        'revision':revision, 'sha256':digest, 'entries':entries,
                        'etag':next((v for k,v in response_headers.items() if k.lower()=='etag'),''),
                        'last_modified':next((v for k,v in response_headers.items() if k.lower()=='last-modified'),''),
                        'enemy_refresh_after':now if force or revision != old.get('revision') else old.get('enemy_refresh_after',now),
                        'stats':{'wars':len(wars), 'quests':len(entries),
                                 'free':sum(e['kind']=='free' for e in entries.values()),
                                 'daily':sum(e['kind']=='daily' for e in entries.values())}}
                self.message = f"全量目录同步完成：{len(wars)}个章节，{len(entries)}个关卡"
            else:
                raise ValueError(f'目录响应状态无效：{status}')
            self._publish(data)
            self.error = ''
            self._retry_after = 0
            if log:
                log('[关卡库] '+self.message)
            return True
        except Exception as exc:
            self.error = str(exc)
            self.message = '刷新失败，保留上次完整目录' if old else '同步失败，继续使用内置关卡目录'
            self._retry_after = self.clock()+600
            if log:
                log('[关卡库] '+self.message+'：'+str(exc))
            return False
        finally:
            with self.lock:
                self.running = False


_default = None
_default_lock = threading.Lock()


def catalog_sync():
    global _default
    with _default_lock:
        if _default is None:
            _default = QuestCatalogSync()
    return _default


def quest_bindings():
    """内置别名保持不变；新增关卡使用稳定的atlas_cn_游戏ID键。"""
    base = json.loads(get_resource_path('data/laplace/quests.json').read_text(encoding='utf-8'))
    entries = catalog_sync().snapshot().get('entries', {})
    result = dict(entries)
    for key, entry in base.items():
        remote_key = f"atlas_cn_{entry['id']}"
        remote = entries.get(remote_key)
        merged = {**remote, **entry} if remote else dict(entry)
        if remote:
            # Names and costs refresh from CN; the alias's exact bound phase stays stable.
            merged.update({k:remote[k] for k in ('name','spot','war')})
            result.pop(remote_key, None)
        result[key] = merged
    return result


def quest_binding(key):
    entries = quest_bindings()
    if key in entries:
        return entries[key]
    # 明确指定首次攻略阶段；不改变目录默认的周回阶段或队列通关要求。
    import re
    match = re.fullmatch(r'(.+)@phase=([1-9]\d*)', key)
    if match is None:
        return None
    entry = entries.get(match[1])
    phase = int(match[2])
    if entry is None or phase not in entry.get('phases', [entry.get('phase')]):
        return None
    return {**entry, 'phase':phase}


def quest_profile(key):
    entry = quest_binding(key)
    if entry is None:
        raise ValueError('没有准确关卡ID，禁止近似配队')
    from collections import Counter
    from src.strategy.laplace import PublicData
    data = PublicData()
    quest = data.get('CN','quest',entry['id'],entry['phase'])
    if not quest.get('stages') or any(not s.get('enemies') for s in quest['stages']):
        quest = data.get('JP','quest',entry['id'],entry['phase'])
    if not quest.get('stages') or any(not s.get('enemies') for s in quest['stages']):
        raise ValueError('目标关卡缺少完整敌方配置，无法配队')
    counts = Counter(e['svt']['className'] for s in quest['stages'] for e in s['enemies'])
    return {'id':key, 'name':entry['name'], 'spot':entry['spot'], 'war':entry['war'],
            'quest_name':entry['spot']+' - '+entry['name'], 'enemy_classes':dict(counts),
            'enemy_class':next(iter(counts)).capitalize() if len(counts)==1 else 'Mixed',
            'enemy_class_cn':' / '.join(f'{name} {count}只' for name,count in counts.items()),
            'waves':[len(s['enemies']) for s in quest['stages']]}


if __name__ == '__main__':
    import argparse
    cli = argparse.ArgumentParser(description='同步或刷新Atlas国服全量关卡目录')
    cli.add_argument('--force',action='store_true')
    args = cli.parse_args()
    service = catalog_sync()
    ok = service.refresh(force=args.force, log=print)
    print(json.dumps(service.status(), ensure_ascii=False, indent=2))
    raise SystemExit(0 if ok else 1)
