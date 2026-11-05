"""战场外观数据与 Atlas 图片缓存，不参与计算或修改进场快照。"""
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests
from PIL import Image

from src.core.path_helper import get_root_dir
from src.strategy.interactive_simulation import valid_id


def atlas_url(value):
    if not isinstance(value, str):
        return ''
    url = urlsplit(value)
    if (url.scheme != 'https' or url.netloc != 'static.atlasacademy.io'
            or url.query or url.fragment or not url.path.endswith('.png')
            or not url.path.startswith(('/CN/', '/JP/', '/NA/'))
            or any(part in ('.', '..') for part in url.path.split('/'))):
        return ''
    return value


def asset(svt, kind, ascension=1, costume=None):
    assets = svt.get('extraAssets', {}).get(kind, {})
    if costume and assets.get('costume', {}).get(str(costume)):
        return atlas_url(assets['costume'][str(costume)])
    variants = assets.get('ascension', {})
    # 第四再临通常继续使用第三阶段的战斗外观。
    choices = sorted((int(k), v) for k, v in variants.items() if k.isdigit())
    eligible = [(k, v) for k, v in choices if k <= max(1, ascension)]
    return atlas_url((eligible or choices)[-1][1]) if choices else ''


def visual_metadata(engine, identity):
    path = engine.root / 'sessions' / (valid_id(identity) + '.json')
    session = json.loads(path.read_text(encoding='utf-8'))
    data, request = session['payload']['data'], session['payload']['request']
    servants = {s['id']: s for s in data['servants']}

    def appearance(svt, profile):
        ascension = profile.get('ascension', 1)
        costume = profile.get('battle_chara_id') or profile.get('costume_id')
        return {'class_name': svt.get('className', ''),
                'face': asset(svt, 'faces', ascension, costume) or atlas_url(svt.get('face')),
                'figure': asset(svt, 'charaFigure', ascension, costume),
                'command': asset(svt, 'commands', ascension, costume),
                'status': asset(svt, 'status', ascension, costume)}

    players = {}
    for row in request['team']:
        svt = servants.get(row['svt_id'], {})
        skills = {s['id']: s for s in svt.get('skills', [])}
        np_types = {}
        for np in svt.get('noblePhantasms', []):
            np_types.setdefault(np['name'], set()).add(str(np.get('card', '')))
        players[str(row['party_slot'])] = {**appearance(svt, row), 'lv': row['lv'],
            'np_types': {name: int(next(iter(types))) for name, types in np_types.items()
                         if len(types) == 1 and types <= {'1', '2', '3'}},
            'support': row.get('is_friend', False),
            'skills': {str(i): atlas_url(skills.get(sid, {}).get('icon'))
                       for i, sid in enumerate(row.get('skill_ids', []), 1)}}
    enemies = {}
    for stage in data['quest']['stages']:
        enemies[str(stage['wave'])] = {}
        for index, row in enumerate(stage['enemies'], 1):
            svt = {**servants.get(row['svt']['id'], {}), **row['svt']}
            enemies[str(stage['wave'])][str(index)] = {**appearance(svt, {}),
                'names': [name for name in (row.get('name'), svt.get('name'), svt.get('originalName')) if name]}
    mc = next((c for c in data.get('mystic_codes', []) if c['id'] == request.get('mystic_code_id')), {})
    return {'players': players, 'enemies': enemies, 'waves': len(data['quest']['stages']),
            'quest_name': session['metadata'].get('quest_name', ''),
            'master_skills': {str(i): atlas_url(s.get('icon')) for i, s in enumerate(mc.get('skills', []), 1)}}


def cached_image(url, root=None):
    url = atlas_url(url)
    if not url:
        raise ValueError('仅支持 Atlas 的公开 PNG 素材')
    folder = Path(root) if root else get_root_dir() / '.app_profile' / 'battle_assets'
    file = folder / (hashlib.sha256(url.encode()).hexdigest() + '.png')
    if file.is_file():
        return file.read_bytes()
    with requests.get(url, stream=True, timeout=(4, 8), allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError('Atlas 素材暂不可用')
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > 12 * 1024 * 1024:
                raise ValueError('素材超过大小上限')
    with Image.open(io.BytesIO(content)) as image:
        if image.format != 'PNG' or max(image.size) > 16384:
            raise ValueError('素材不是有效的 PNG 图片')
        image.verify()
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, suffix='.tmp', delete=False) as output:
        temporary = Path(output.name)
        output.write(content)
    try:
        os.replace(temporary, file)
    finally:
        temporary.unlink(missing_ok=True)
    return bytes(content)
