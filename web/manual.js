// 手动方案与自动推荐分开存储；任何编辑都会使当前结果失效。
let manualInventory = null, manualLoadedId = null, manualSaved = [];
let manualParty = Array.from({length:6},(_,i)=>({source:i===2?'support':'owned',svt_id:0,ce_id:null}));
let manualTurns = Array.from({length:3},()=>({actions:[{kind:'np',actor:1,enemy:1}]}));
let manualRevision = 0, manualRequest = null, manualStarting = false, manualStopRequested = false;
let manualSupportMetadata = new Map(), manualSupportLoading = 0, manualPolling = false;

function manualElement(tag,text='',className='') {
  const el=document.createElement(tag); el.textContent=text; if(className)el.className=className; return el;
}
function manualButton(text,action) {
  const el=manualElement('button',text,'btn'); el.onclick=action; return el;
}
function manualSelect(rows,value,change,label) {
  const el=manualElement('select','','select-control'); el.setAttribute('aria-label',label);
  rows.forEach(([id,text])=>el.add(new Option(text,String(id)))); el.value=String(value??'');
  el.onchange=()=>change(el.value); return el;
}
function manualNumber(value,min,max,change,label) {
  const el=document.createElement('input'); el.type='number'; el.value=value; el.min=min; el.max=max;
  el.className='manual-small'; el.setAttribute('aria-label',label); el.oninput=()=>change(Number(el.value)); return el;
}
function manualLabel(text,el) { const label=manualElement('label',text+' '); label.append(el); return label; }
function manualSlotOptions() { return Array.from({length:6},(_,i)=>[i+1,`${i+1}号位`]); }
function defaultManualSupport() { return {lv:90,np_lv:1,skills:[10,10,10],appends:[0,0,0,0,0],atk_fou:0,hp_fou:0,ascension:4,skill_ids:[],np_id:null}; }

const manualMashForms = {
  ortinax: {label:'四星 · 奥特瑙斯（改建型）', face:800150, maxLevel:80},
  paladin: {label:'五星 · 圣骑士', face:800190, maxLevel:120}
};

async function changeBattlePlanningMode() {
  const mode=document.getElementById('battle-mode').value,manual=mode!=='auto',interactive=mode==='interactive';
  document.getElementById('manual-panel').hidden=!manual;
  document.getElementById('battle-auto-card').hidden=manual;
  document.getElementById('battle-result-card').hidden=manual;
  document.getElementById('manual-turn-card').hidden=interactive;
  document.getElementById('manual-run-card').hidden=interactive;
  document.getElementById('interactive-panel').hidden=!interactive;
  if(manualRequest && mode!=='manual')await stopManualSimulation();
  if(!interactive && interactiveBusy){interactiveRevision++;await stopInteractiveCalculation();}
  if(manual) {
    document.getElementById('battle-auto').checked=false; updateBattleMode();
    await loadManualInventory(); renderManualRoster(); renderManualTurns(); await refreshManualPlans();
    if(interactive)await initInteractiveBattle();
  } else if(manualRequest) await stopManualSimulation();
  if(!isRunning)document.getElementById('main-btn-text').textContent=battleActionText();
}

function manualChanged() {
  manualRevision++;
  document.getElementById('manual-result').textContent='配置已修改，旧模拟结果已失效。请重新开始模拟。';
  if(manualRequest && !manualStopRequested) stopManualSimulation();
  if(document.getElementById('battle-mode').value==='interactive')queueMicrotask(interactiveConfigurationChanged);
}

async function loadManualInventory(force=false) {
  if(manualInventory && !force)return;
  try {
    const data=await fetch('/api/manual/inventory').then(r=>r.json());
    if(!data.success)throw new Error(data.message);
    const inventoryChanged=manualInventory && JSON.stringify(manualInventory)!==JSON.stringify(data);
    manualInventory=data;
    const mc=document.getElementById('manual-mystic-code'),previous=mc.value;
    mc.replaceChildren(new Option('不使用御主技能',''));
    data.mystic_codes.forEach(c=>mc.add(new Option(`${c.name} · Lv${c.lv}`,String(c.id))));
    if([...mc.options].some(o=>o.value===previous))mc.value=previous;
    else if(previous){mc.add(new Option('账号缺少等级记录：'+previous,previous));mc.value=previous;}
    syncManualMysticLevel();
    if(inventoryChanged)manualChanged();
    renderManualRoster(); await refreshManualPlans();
  } catch(error){document.getElementById('manual-result').textContent=error.message;}
}

function changeManualSupportSlot() {
  const index=Number(document.getElementById('manual-support-slot').value)-1;
  const previous=manualParty.findIndex(slot=>slot.source==='support');
  [manualParty[previous],manualParty[index]]=[manualParty[index],manualParty[previous]];
  manualChanged();renderManualRoster();
}

function manualFaceUrl(identity) {
  if (!Number.isSafeInteger(identity) || identity < 1) return '';
  return 'https://static.atlasacademy.io/CN/Faces/f_' + identity + '0.png';
}

function manualThumb(url, className, alt = '') {
  const thumb = battleImage(url, className, alt);
  const image = thumb.querySelector('img');
  if (image) image.loading = 'lazy';
  return thumb;
}

function manualDialogHeader(dialog, title) {
  const header = manualElement('div', '', 'fgo-dialog-header');
  const close = manualElement('button', '✕', 'fgo-dialog-close');
  close.type = 'button';
  close.setAttribute('aria-label', '关闭');
  close.onclick = () => dialog.close();
  header.append(manualElement('h3', title, 'fgo-dialog-title'), close);
  return header;
}

function formatManualNpEntry(np) {
  if (!np) return null;
  const colorMap = { Arts: '蓝卡', Buster: '红卡', Quick: '绿卡' };
  const cardType = np.card_type || 'Unknown';
  const colorCn = colorMap[cardType] || '';
  const target = np.target || '';
  const rawName = String(np.name || '').trim();

  const prefixRegex = /^(?:(?:蓝卡|红卡|绿卡|Arts|Buster|Quick)\s*[·\s/:-]?\s*)?(?:(?:单体|全体|辅助)\s*[·\s/:-]?\s*)?/i;
  let cleanName = rawName;
  const m = rawName.match(prefixRegex);
  if (m && m[0] && m[0].trim().length > 0) {
    const remainder = rawName.slice(m[0].length).replace(/^[·\s/:-]+/, '').trim();
    if (remainder.length > 0) cleanName = remainder;
  }

  const tags = [];
  if (colorCn) tags.push(colorCn);
  else if (cardType && cardType !== 'Unknown') tags.push(cardType);
  if (target) tags.push(target);

  const prefix = tags.length ? tags.join(' · ') : '';
  const optionText = prefix ? `${prefix} · ${cleanName}` : cleanName;

  return {
    id: np.id,
    cardType,
    colorCn,
    target,
    cleanName,
    rawName,
    optionText,
    badgeClass: cardType.toLowerCase()
  };
}

function openManualPicker(index, isCE) {
  const slot = manualParty[index];
  const support = slot.source === 'support';
  const dialog = document.getElementById(isCE ? 'manual-ce-dialog' : 'manual-servant-dialog');
  const field = isCE ? 'ce_id' : 'svt_id';
  const name = isCE ? '概念礼装' : '从者';
  const rows = support ? (isCE ? manualInventory.support_ces : manualInventory.support_servants)
    : (isCE ? manualInventory.craft_essences : manualInventory.servants);
  const identity = row => support ? row.id : row[field];
  const selected = slot[field];
  const assigned = new Set(support || isCE ? [] : manualParty
    .filter(other => other !== slot && other.source === 'owned').map(other => other.svt_id));
  let page = 0;

  function choose(id) {
    dialog.close();
    if (id === selected || manualParty[index] !== slot) return;
    slot[field] = id;
    if (isCE) {
      delete slot.owned_ce;
      if (support) slot.support_ce = id ? {lv:1, is_mlb:false} : null;
    } else {
      delete slot.owned_stats;
      delete slot.mash_form;
      if (support) slot.support = defaultManualSupport();
    }
    manualChanged();
    renderManualRoster();
    if (support && !isCE && id) loadManualSupport(slot, true);
  }

  const body = manualElement('div', '', 'fgo-dialog-body');
  const searchBar = manualElement('div', '', 'fgo-dialog-search-bar');
  const search = document.createElement('input');
  search.type = 'search';
  search.placeholder = `搜索${name}名称或 ID`;
  search.setAttribute('aria-label', search.placeholder);
  searchBar.append(search);
  const stars = new Set();
  const classes = new Set();
  const standardClasses = ['saber', 'archer', 'lancer', 'rider', 'caster', 'assassin', 'berserker'];
  const filters = manualElement('div', '', 'fgo-filter-panel');
  if (!isCE) {
    for (const [title, choices, selectedValues] of [
      ['星级', [5, 4, 3, 2, 1, 0].map(value => [value, `${value}星`]), stars],
      ['职阶', [...standardClasses.map((value, i) => [value, ['剑阶', '弓阶', '枪阶', '骑阶', '术阶', '杀阶', '狂阶'][i]]), ['extra', '额外']], classes]
    ]) {
      const group = manualElement('fieldset', '', 'fgo-filter-group');
      const chipContainer = manualElement('div', '', 'fgo-filter-chips');
      group.append(manualElement('legend', title, 'fgo-filter-legend'));
      for (const [value, label] of choices) {
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = String(value);
        checkbox.onchange = () => {
          if (checkbox.checked) selectedValues.add(value);
          else selectedValues.delete(value);
          page = 0;
          renderChoices();
        };
        const control = manualElement('label', '', 'fgo-filter-chip');

        control.append(checkbox, manualElement('span', label, 'fgo-chip-label'));
        chipContainer.append(control);
      }
      group.append(chipContainer);
      filters.append(group);
    }
    const filterToolbar = manualElement('div', '', 'fgo-filter-toolbar');
    filterToolbar.append(manualButton('重置筛选', () => {
      stars.clear(); classes.clear();
      filters.querySelectorAll('input').forEach(input => { input.checked = false; });
      page = 0;
      renderChoices();
    }), manualElement('span', '同组可多选，未勾选表示全部', 'fgo-picker-desc'));
    filters.append(filterToolbar);
  }
  const count = manualElement('div', '', 'fgo-dialog-count');
  count.setAttribute('aria-live', 'polite');
  const grid = manualElement('div', '', 'fgo-picker-grid');
  if (!isCE) grid.style.minHeight = '180px';
  const pagination = manualElement('div', '', 'fgo-dialog-pagination');
  const previous = manualButton('上一页', () => { page--; renderChoices(); });
  const next = manualButton('下一页', () => { page++; renderChoices(); });
  const pageNumber = manualElement('span');
  pagination.append(previous, pageNumber, next);
  if (selected) {
    const clear = manualElement('button', isCE ? '卸下礼装' : '移出从者', 'btn-unequip');
    clear.type = 'button';
    clear.onclick = () => choose(isCE ? null : 0);
    body.append(clear);
  }
  body.append(searchBar);
  if (!isCE) body.append(filters);
  body.append(count, grid, pagination);
  dialog.replaceChildren(manualDialogHeader(dialog, `${index+1}号位 · 选择${name}${support ? '（助战）' : ''}`), body);

  function renderChoices() {
    const query = search.value.normalize('NFKC').trim().toLowerCase();
    const matches = rows.filter(row => {
      const textMatches = `${identity(row)} ${row.name}`.normalize('NFKC').toLowerCase().includes(query);
      if (isCE) return textMatches;
      const rarities = row.rarities ?? (row.rarity == null ? [] : [row.rarity]);
      const classGroup = standardClasses.includes(row.class_name) ? row.class_name : (row.class_name ? 'extra' : null);
      return textMatches && (!stars.size || rarities.some(value => stars.has(value)))
        && (!classes.size || classes.has(classGroup));
    });
    const pageSize = 24;
    const total = Math.max(1, Math.ceil(matches.length / pageSize));
    count.textContent = `找到 ${matches.length} 项${name}`;
    pageNumber.textContent = `第 ${page+1} / ${total} 页`;
    previous.disabled = page === 0;
    next.disabled = page + 1 >= total;
    grid.replaceChildren();
    if (!matches.length) grid.append(manualElement('span', '没有符合搜索条件的结果'));
    for (const row of matches.slice(page*pageSize, (page+1)*pageSize)) {
      const id = identity(row);
      const active = id === selected;
      const button = manualElement('button', '', 'fgo-picker-item' + (active ? ' selected' : ''));
      button.type = 'button';
      button.disabled = assigned.has(id);
      button.setAttribute('aria-pressed', String(active));
      button.onclick = () => choose(id);
      const label = manualElement('span', row.name, 'fgo-picker-name');
      label.title = row.name;
      const detail = support ? `ID ${id}` : `Lv.${row.lv} · ${isCE ? (row.is_mlb ? '满破' : '未满破') : `宝具${row.np_lv}`}`;
      button.append(manualThumb(manualFaceUrl(id), 'fgo-picker-thumb'), label,
        manualElement('span', detail, 'fgo-picker-desc'));
      if (active || button.disabled) button.append(manualElement('span', active ? '已选' : '其他槽位', 'fgo-picker-badge'));
      grid.append(button);
    }
    grid.scrollTop = 0;
  }
  const onSearch = () => { page = 0; renderChoices(); };
  search.addEventListener('input', event => { if (!event.isComposing) onSearch(); });
  search.addEventListener('compositionend', onSearch);
  renderChoices();
  dialog.showModal();
  search.focus();
}

const manualStatSections = [
  ['基础属性', [['lv','等级',1,120], ['np_lv','宝具等级',1,5], ['ascension','灵基再临',0,4],
    ['atk_fou','攻击芙芙',0,2000], ['hp_fou','生命芙芙',0,2000]]],
  ['主动技能', Array.from({length:3}, (_, i) => [`skill_lv${i+1}`, `技能 ${i+1}`, 1, 10])],
  ['附加技能', Array.from({length:5}, (_, i) => [`append_skill_lv${i+1}`, `附加技能 ${i+1}`, 0, 10])]
];

async function manualServantMetadata(identity) {
  if (manualSupportMetadata.has(identity)) return manualSupportMetadata.get(identity);
  const meta = await fetch('/api/manual/support?svt_id=' + identity).then(response => response.json());
  if (!meta.success) throw new Error(meta.message);
  manualSupportMetadata.set(identity, meta);
  return meta;
}

function openManualEditorDialog(index) {
  const slot = manualParty[index];
  if (!slot.svt_id) return;
  const support = slot.source === 'support';
  const mashForm = !support && slot.svt_id === 800100 ? manualMashForms[slot.mash_form] : null;
  const dialog = document.getElementById('manual-editor-dialog');
  const servant = support ? manualInventory.support_servants.find(row => row.id === slot.svt_id)
    : manualInventory.servants.find(row => row.svt_id === slot.svt_id);
  const ownedCE = manualInventory.craft_essences.find(row => row.ce_id === slot.ce_id);
  const stats = support ? slot.support || defaultManualSupport() : slot.owned_stats || {};
  const ce = support ? slot.support_ce || {lv:1,is_mlb:false} : slot.owned_ce || ownedCE;
  const body = manualElement('div', '', 'fgo-dialog-body');
  const form = manualElement('form');
  form.append(manualElement('p', support ? '助战使用此处填写的练度。' : '默认使用账号练度。修改项随模拟方案保存，账号数值照常保留。', 'fgo-editor-note'));
  const original = {};

  function section(title) {
    const container = manualElement('section', '', 'fgo-editor-section');
    const fields = manualElement('div', '', 'fgo-editor-fields');
    container.append(manualElement('h4', title, 'fgo-editor-section-title'), fields);
    form.append(container);
    return fields;
  }

  function field(label, input) {
    const wrapper = manualElement('label', '', 'fgo-editor-field');
    wrapper.append(manualElement('span', label), input);
    return wrapper;
  }

  function numberInput(key, label, min, max, value) {
    const input = document.createElement('input');
    input.type = 'number';
    input.name = key;
    input.min = min;
    input.max = max;
    input.step = 1;
    input.required = true;
    input.value = value;
    return field(label, input);
  }

  for (const [title, fields] of manualStatSections) {
    const group = section(title);
    for (const [key, label, min, max] of fields) {
      const fallback = key === 'ascension' ? 4 : (key.endsWith('_fou') ? servant?.fou ?? 0 : min);
      original[key] = servant?.[key] ?? fallback;
      let value = stats[key] ?? original[key];
      if (support && key.startsWith('skill_lv')) value = stats.skills[Number(key.at(-1))-1];
      if (support && key.startsWith('append_skill_lv')) value = stats.appends[Number(key.at(-1))-1];
      group.append(numberInput(key, label, min, max, value));
    }
  }

  const versions = section('强化版本');
  const versionNotice = manualElement('p', '正在读取技能与宝具版本…', 'manual-row-text');
  versionNotice.setAttribute('role','status');
  versions.append(versionNotice);
  let versionsReady = false;
  const identity = slot.svt_id;
  const addVersions = (key, label, rows, value, imported) => {
    const options = rows.map(row => [row.id, `${row.name} · 版本${row.priority}`]);
    if (value && !rows.some(row => row.id === value)) options.push([value, `已保存版本 ID ${value}`]);
    const importedRow = rows.find(row => row.id === imported);
    options.unshift(['', support ? '按助战配置解析' : (importedRow ? `账号默认 · ${importedRow.name}` : '账号默认')]);
    const select = manualSelect(options, value ?? '', () => {}, label);
    select.name = key;
    versions.append(field(label, select));
  };
  if (mashForm) {
    versionNotice.textContent = `技能与宝具按${mashForm.label}自动选择，原有版本设置会保留。`;
  } else {
    manualServantMetadata(identity).then(meta => {
      if (!dialog.open || !dialog.contains(versions) || manualParty[index] !== slot || slot.svt_id !== identity) return;
      versions.replaceChildren();
      meta.skills.forEach((rows,i) => addVersions(`version${i}`,`技能 ${i+1}`,rows,stats.skill_ids?.[i],servant?.skill_ids?.[i]));
      addVersions('np_id','宝具',meta.nps,stats.np_id,servant?.np_id);
      versionsReady = true;
    }).catch(() => { versionNotice.textContent = '暂时无法读取强化版本，已选版本会保留。'; });
  }

  if (slot.ce_id && ce) {
    const group = section('概念礼装');
    group.append(numberInput('ce_lv','等级',1,100,ce.lv));
    const mlb = document.createElement('input');
    mlb.type = 'checkbox';
    mlb.name = 'ce_mlb';
    mlb.checked = ce.is_mlb;
    group.append(field('最大解放（满破）',mlb));
  }

  const footer = manualElement('div', '', 'fgo-dialog-footer');
  const reset = manualButton(support ? '恢复助战默认' : '恢复账号默认', () => {
    if (support) {
      slot.support = defaultManualSupport();
      slot.support_ce = slot.ce_id ? {lv:1,is_mlb:false} : null;
    } else {
      delete slot.owned_stats;
      delete slot.owned_ce;
    }
    dialog.close();
    manualChanged();
    renderManualRoster();
    if (support) loadManualSupport(slot, true);
  });
  const cancel = manualButton('取消', () => dialog.close());
  reset.type = cancel.type = 'button';
  const save = manualElement('button', '应用练度', 'btn btn-primary');
  save.type = 'submit';
  footer.append(reset, cancel, save);
  form.append(footer);
  form.onsubmit = event => {
    event.preventDefault();
    if (!form.reportValidity() || manualParty[index] !== slot || slot.svt_id !== identity) return;
    const inputs = new FormData(form);
    const values = Object.fromEntries(Object.keys(original).map(key => [key, Number(inputs.get(key))]));
    const skillIds = versionsReady ? [0,1,2].map(i => Number(inputs.get(`version${i}`)) || null) : stats.skill_ids;
    const npId = versionsReady ? Number(inputs.get('np_id')) || null : stats.np_id;
    const ceValues = inputs.has('ce_lv') ? {lv:Number(inputs.get('ce_lv')), is_mlb:inputs.has('ce_mlb')} : null;
    if (support) {
      const {lv,np_lv,ascension,atk_fou,hp_fou} = values;
      slot.support = {lv,np_lv,ascension,atk_fou,hp_fou,
        skills:[1,2,3].map(i=>values[`skill_lv${i}`]),
        appends:[1,2,3,4,5].map(i=>values[`append_skill_lv${i}`]),
        skill_ids:skillIds?.map(id=>id||0) ?? [], np_id:npId ?? null};
      slot.support_ce = ceValues;
    } else {
      const custom = Object.fromEntries(Object.entries(values).filter(([key,value])=>value !== original[key]));
      if (skillIds?.some(Boolean)) custom.skill_ids = skillIds;
      if (npId) custom.np_id = npId;
      slot.owned_stats = Object.keys(custom).length ? custom : null;
      slot.owned_ce = ceValues && (ceValues.lv !== ownedCE?.lv || ceValues.is_mlb !== ownedCE?.is_mlb) ? ceValues : null;
    }
    dialog.close();
    manualChanged();
    renderManualRoster();
  };
  body.append(form);
  dialog.replaceChildren(manualDialogHeader(dialog, `${index+1}号位 · ${servant?.name || '从者'}练度`), body);
  dialog.showModal();
}

function renderManualRoster() {
  if (!manualInventory) return;
  const container = document.getElementById('manual-roster');
  if (!container) return;
  container.replaceChildren();

  const sections = [
    { title: '前排 · 首发主力 (Starting)', indices: [0, 1, 2] },
    { title: '后排 · 替补阵容 (Sub)', indices: [3, 4, 5] }
  ];

  sections.forEach(sec => {
    const secBox = manualElement('div', '', 'fgo-roster-section');
    secBox.append(manualElement('div', sec.title, 'fgo-roster-section-header'));
    const groupGrid = manualElement('div', '', 'fgo-roster-group');

    sec.indices.forEach(index => {
      const slot = manualParty[index];
      if (!slot) return;
      const isSupport = slot.source === 'support';
      const isOwnedMash = !isSupport && slot.svt_id === 800100;
      const mashForm = isOwnedMash ? manualMashForms[slot.mash_form] : null;
      const slotCard = manualElement('div', '', 'fgo-roster-slot' + (isSupport ? ' is-support' : ''));

      // 槽位头部
      const header = manualElement('div', '', 'fgo-slot-header');
      const idxWrap = manualElement('div', '', 'fgo-slot-index-wrap');
      idxWrap.append(manualElement('span', (index + 1) + '号位', 'fgo-slot-num'));
      idxWrap.append(manualElement('span', index < 3 ? '前排' : '后排', 'fgo-slot-pos'));

      if (isSupport) {
        idxWrap.append(manualElement('span', '助战', 'fgo-badge fgo-badge-support'));
      }

      const hasCustom = !isSupport && (Boolean(slot.owned_stats && Object.keys(slot.owned_stats).length > 0) || Boolean(slot.owned_ce));
      if (hasCustom) {
        const badge = manualElement('span', '已修改练度', 'fgo-badge fgo-badge-custom');
        badge.title = '当前槽位已自定义模拟练度';
        idxWrap.append(badge);
      }
      header.append(idxWrap);

      if (slot.svt_id) {
        const trainBtn = manualElement('button', '练度', 'fgo-slot-train-btn');
        trainBtn.type = 'button';
        trainBtn.setAttribute('aria-label', (index + 1) + '号位从者练度');
        trainBtn.onclick = () => openManualEditorDialog(index);
        header.append(trainBtn);
      }
      slotCard.append(header);

      // 从者大肖像按钮
      const owned = isSupport ? null : (manualInventory.servants || []).find(s => s.svt_id === slot.svt_id);
      const servantBtn = manualElement('button', '', 'fgo-portrait-card' + (slot.svt_id ? '' : ' empty'));
      servantBtn.type = 'button';
      servantBtn.setAttribute('aria-label', (index + 1) + '号位从者：' + (slot.svt_id ? (owned?.name || ('ID: ' + slot.svt_id)) : '未编入'));
      servantBtn.onclick = () => openManualPicker(index, false);

      if (slot.svt_id) {
        const svtName = isSupport
          ? ((manualInventory.support_servants || []).find(s => s.id === slot.svt_id)?.name || ('助战 ID ' + slot.svt_id))
          : (owned?.name || ('从者 ID ' + slot.svt_id));
        servantBtn.append(manualThumb(manualFaceUrl(mashForm?.face ?? slot.svt_id), 'fgo-portrait-thumb', svtName));

        const metaBox = manualElement('div', '', 'fgo-portrait-meta');
        const nameRow = manualElement('div', svtName, 'fgo-portrait-name');
        nameRow.title = svtName;

        let lvText = '', skillText = '';
        if (!isSupport) {
          const lv = Math.min(slot.owned_stats?.lv ?? owned?.lv ?? 1, mashForm?.maxLevel ?? Infinity);
          const np = slot.owned_stats?.np_lv ?? owned?.np_lv ?? 1;
          lvText = 'Lv.' + lv + ' · 宝具' + np;
          const s1 = slot.owned_stats?.skill_lv1 ?? owned?.skill_lv1 ?? 1;
          const s2 = slot.owned_stats?.skill_lv2 ?? owned?.skill_lv2 ?? 1;
          const s3 = slot.owned_stats?.skill_lv3 ?? owned?.skill_lv3 ?? 1;
          const fou = slot.owned_stats?.atk_fou ?? owned?.atk_fou ?? owned?.fou ?? 0;
          skillText = '技能 ' + s1 + '/' + s2 + '/' + s3 + ' · 芙芙+' + fou;
        } else {
          slot.support ??= defaultManualSupport();
          lvText = 'Lv.' + slot.support.lv + ' · 宝具' + slot.support.np_lv;
          const sks = slot.support.skills || [10, 10, 10];
          skillText = '技能 ' + sks.join('/') + ' · 自定义助战';
        }

        metaBox.append(nameRow, manualElement('div', lvText, 'fgo-portrait-stats'), manualElement('div', skillText, 'fgo-portrait-sub'));
        servantBtn.append(metaBox);
      } else {
        servantBtn.append(manualThumb('', 'fgo-portrait-thumb missing', '未编入'));
        const metaBox = manualElement('div', '', 'fgo-portrait-meta');
        metaBox.append(
          manualElement('div', '＋ 点击选择从者', 'fgo-portrait-name'),
          manualElement('div', '未编入角色', 'fgo-portrait-stats'),
          manualElement('div', isSupport ? '自定义助战位' : '自有从者位', 'fgo-portrait-sub')
        );
        servantBtn.append(metaBox);
      }
      slotCard.append(servantBtn);

      if (slot.svt_id) {
        const npContainer = manualElement('div', '', 'fgo-np-selector-box');
        const npHeader = manualElement('div', '', 'fgo-np-selector-header');
        npHeader.append(
          manualElement('span', '宝具形态', 'fgo-np-selector-title'),
          manualElement('span', isSupport ? '助战指定' : '形态分支', 'fgo-np-selector-tag')
        );
        npContainer.append(npHeader);
        slotCard.append(npContainer);


        if (mashForm) {
          npContainer.append(manualElement('div', `跟随${mashForm.label}（自动绑定专属宝具）`, 'fgo-np-mash-note'));
        } else {
          const status = manualElement('div', '正在读取宝具资料…', 'fgo-np-loading');
          npContainer.append(status);
          const identity = slot.svt_id;
          manualServantMetadata(identity).then(meta => {
            if (!npContainer.isConnected || manualParty[index] !== slot || slot.svt_id !== identity) return;
            const stats = isSupport ? slot.support : slot.owned_stats;
            const defaultLabel = isSupport ? '自动选择' : '跟随账号';
            const npEntries = (meta.nps || []).map(formatManualNpEntry);
            const options = [['', defaultLabel], ...npEntries.map(entry => [entry.id, entry.optionText])];
            const preview = manualElement('div', '', 'fgo-np-preview');
            function renderPreview(currentId) {
              const entry = npEntries.find(item => item.id === currentId);
              const detail = manualElement('div', '', 'fgo-np-detail');
              let badge, name, description;
              if (!currentId) {
                badge = manualElement('span', '默认', 'fgo-np-badge default');
                name = isSupport ? '进入战斗时按助战配置选择' : '进入战斗时按账号配置选择';
                description = '可在上方指定宝具形态';
              } else if (entry) {
                badge = manualElement('span', entry.colorCn || '未知', 'fgo-np-badge ' + entry.badgeClass);
                name = entry.cleanName;
                description = [entry.target, entry.cardType].filter(Boolean).join(' · ');
              } else {
                badge = manualElement('span', '已保存', 'fgo-np-badge unknown');
                name = `已保存宝具 ID ${currentId}`;
                description = '暂未找到对应的宝具资料';
              }
              const nameElement = manualElement('div', name, 'fgo-np-full-name');
              nameElement.title = name;
              detail.append(nameElement, manualElement('div', description, 'fgo-np-meta-text'));
              preview.replaceChildren(badge, detail);
            }
            if (stats?.np_id && !meta.nps.some(np => np.id === stats.np_id)) {
              options.push([stats.np_id, `已保存宝具 ID ${stats.np_id}`]);
            }
            const select = manualSelect(options, stats?.np_id, value => {
              if (manualParty[index] !== slot || slot.svt_id !== identity) return;
              const chosenId = value ? Number(value) : null;
              if (isSupport) slot.support = {...slot.support, np_id: chosenId};
              else {
                const updated = {...slot.owned_stats};
                if (chosenId) updated.np_id = chosenId;
                else delete updated.np_id;
                slot.owned_stats = Object.keys(updated).length ? updated : null;
              }
              renderPreview(chosenId);
              manualChanged();
            }, `${index+1}号位宝具形态`);
            select.className = 'select-control fgo-np-select';
            renderPreview(stats?.np_id);
            status.replaceWith(select, preview);
          }).catch(() => {
            if (!npContainer.isConnected) return;
            status.className = 'fgo-np-error';
            status.textContent = '宝具资料读取失败，请重新打开编队';
          });
        }
      }

      if (isOwnedMash && isInteractiveMode()) {
        const id = `manual-mash-form-${index}`;
        const label = manualElement('label', '', 'fgo-editor-field');
        const options = [['','跟随账号'], ...Object.entries(manualMashForms).map(([key,form]) => [key,form.label])];
        const select = manualSelect(options, slot.mash_form, value => {
          slot.mash_form = value || null;
          manualChanged();
          renderManualRoster();
          document.getElementById(id)?.focus();
        }, `${index+1}号位玛修灵基`);
        select.id = id;
        label.append(manualElement('span','玛修灵基'), select);
        slotCard.append(label);
      }

      // 概念礼装肖像按钮
      const ownedCe = (!isSupport && slot.ce_id) ? (manualInventory.craft_essences || []).find(c => c.ce_id === slot.ce_id) : null;
      const ceBtn = manualElement('button', '', 'fgo-portrait-card' + (slot.ce_id ? '' : ' empty'));
      ceBtn.type = 'button';
      ceBtn.setAttribute('aria-label', (index + 1) + '号位概念礼装：' + (slot.ce_id ? (ownedCe?.name || ('ID: ' + slot.ce_id)) : '未装备'));
      ceBtn.onclick = () => openManualPicker(index, true);

      if (slot.ce_id) {
        const ceName = isSupport
          ? ((manualInventory.support_ces || []).find(c => c.id === slot.ce_id)?.name || ('助战礼装 ID ' + slot.ce_id))
          : (ownedCe?.name || ('礼装 ID ' + slot.ce_id));
        ceBtn.append(manualThumb(manualFaceUrl(slot.ce_id), 'fgo-ce-thumb', ceName));

        const ceMeta = manualElement('div', '', 'fgo-portrait-meta');
        const ceNameRow = manualElement('div', ceName, 'fgo-portrait-name');
        ceNameRow.title = ceName;

        let ceStatsText = '';
        if (!isSupport) {
          const ceLv = slot.owned_ce?.lv ?? ownedCe?.lv ?? 1;
          const isMlb = slot.owned_ce?.is_mlb ?? ownedCe?.is_mlb ?? false;
          ceStatsText = 'Lv.' + ceLv + ' · ' + (isMlb ? '满破' : '未满破');
        } else {
          slot.support_ce ??= { lv: 1, is_mlb: false };
          ceStatsText = 'Lv.' + slot.support_ce.lv + ' · ' + (slot.support_ce.is_mlb ? '满破' : '未满破');
        }
        ceMeta.append(ceNameRow, manualElement('div', ceStatsText, 'fgo-portrait-stats'));
        ceBtn.append(ceMeta);
      } else {
        ceBtn.append(manualThumb('', 'fgo-ce-thumb missing', '空礼装'));
        const ceMeta = manualElement('div', '', 'fgo-portrait-meta');
        ceMeta.append(
          manualElement('div', '空礼装 (点击装备)', 'fgo-portrait-name'),
          manualElement('div', '未装备概念礼装', 'fgo-portrait-stats')
        );
        ceBtn.append(ceMeta);
      }
      slotCard.append(ceBtn);

      groupGrid.append(slotCard);
    });

    secBox.append(groupGrid);
    container.append(secBox);
  });
}

async function loadManualSupport(slot, resetLevel = false) {
  const identity = slot?.svt_id;
  if (!identity) return;
  const stats = slot.support;
  manualSupportLoading++;
  try {
    const meta = await manualServantMetadata(identity);
    if (!stats || !manualParty.includes(slot) || slot.svt_id !== identity || slot.support !== stats) return;
    if (resetLevel) stats.lv = meta.lv_max;
    if (!stats.skill_ids.length) stats.skill_ids = meta.skills.map(rows => rows[0]?.id || 0);
    if (!stats.np_id && resetLevel) stats.np_id = meta.nps[0]?.id || null;
    manualChanged();
    renderManualRoster();
  } catch (error) {
    document.getElementById('manual-result').textContent = '读取助战资料失败：' + error.message;
  } finally {
    manualSupportLoading--;
  }
}

function syncManualMysticLevel(reset=false) {
  const input=document.getElementById('manual-mystic-level');
  if(!input)return;
  const id=Number(document.getElementById('manual-mystic-code').value);
  const imported=manualInventory?.mystic_codes.find(c=>c.id===id);
  input.disabled=!imported;
  input.placeholder=imported?`账号 Lv${imported.lv}`:'';
  if(reset||!imported)input.value='';
}

function changeManualMysticCode() {
  syncManualMysticLevel(true);
  manualChanged();
  loadManualMysticCode();
}

function manualMysticLevel() {
  const input=document.getElementById('manual-mystic-level');
  if(!input || input.disabled || input.value==='')return null;
  if(!input.checkValidity())throw new Error('御主礼装等级须为 1 至 10 的整数');
  return Number(input.value);
}

async function loadManualMysticCode() {
  const id=document.getElementById('manual-mystic-code').value,el=document.getElementById('manual-mystic-detail');
  el.textContent='';if(!id)return;
  try {
    const data=await fetch('/api/manual/mystic-code?id='+id).then(r=>r.json());
    if(document.getElementById('manual-mystic-code').value!==id)return;
    if(!data.success)throw new Error(data.message);
    el.textContent=`${data.name} · 模拟 Lv${manualMysticLevel()??data.lv}\n`+data.skills.map(s=>`技能${s.num}：${s.name}`).join('；');
  }catch(error){el.textContent=error.message;}
}

function newManualAction(kind) {
  if(kind==='skill')return {kind,actor:1,skill:1,target:null,enemy:1};
  if(kind==='master')return {kind,skill:1,target:null,swap:null,enemy:1};
  if(kind==='facecards')return {kind,enemy:1};
  return {kind:'np',actor:1,enemy:1};
}
function addManualTurn() {
  if(manualTurns.length>=30){alert('最多30回合');return;}
  manualTurns.push({actions:[newManualAction('np')]});manualChanged();renderManualTurns();
}
function renderManualTurns() {
  const container=document.getElementById('manual-turns');container.replaceChildren();
  manualTurns.forEach((turn,ti)=>{
    const block=manualElement('div','','manual-slot'),header=manualElement('div','','manual-toolbar');
    header.append(manualElement('b',`第${ti+1}回合`),manualButton('移除回合',()=>{if(manualTurns.length>1){manualTurns.splice(ti,1);manualChanged();renderManualTurns();}}));block.append(header);
    turn.actions.forEach((action,ai)=>{
      const row=manualElement('div','','manual-action');row.append(manualElement('span',String(ai+1)));
      row.append(manualSelect([['skill','从者技能'],['master','御主技能／换人'],['np','宝具'],['facecards','任意三卡普攻']],action.kind,kind=>{turn.actions[ai]=newManualAction(kind);manualChanged();renderManualTurns();},`回合${ti+1}行动${ai+1}类型`));
      if(action.kind==='skill'||action.kind==='np')row.append(manualSelect(manualSlotOptions(),action.actor,v=>{action.actor=Number(v);manualChanged();},`回合${ti+1}行动${ai+1}角色`));
      if(action.kind==='skill'||action.kind==='master') {
        row.append(manualSelect([[1,'技能1'],[2,'技能2'],[3,'技能3']],action.skill,v=>{action.skill=Number(v);manualChanged();},`回合${ti+1}行动${ai+1}技能`));
        row.append(manualSelect([['','目标：前排首位'],...manualSlotOptions()],action.target??'',v=>{action.target=v?Number(v):null;manualChanged();},`回合${ti+1}行动${ai+1}目标`));
      }
      if(action.kind==='master') {
        const swap=document.createElement('input');swap.type='checkbox';swap.checked=!!action.swap;swap.setAttribute('aria-label',`回合${ti+1}行动${ai+1}换人`);
        swap.onchange=()=>{action.swap=swap.checked?[2,4]:null;manualChanged();renderManualTurns();};row.append(manualLabel('换人',swap));
        if(action.swap)action.swap.forEach((value,i)=>row.append(manualSelect(manualSlotOptions(),value,v=>{action.swap[i]=Number(v);manualChanged();},`回合${ti+1}行动${ai+1}${i?'换上':'换下'}`)));
      }
      row.append(manualLabel('敌方',manualNumber(action.enemy??1,1,6,v=>{action.enemy=v;manualChanged();},`回合${ti+1}行动${ai+1}敌方目标`)));
      for(const [text,move] of [['↑',-1],['↓',1]])row.append(manualButton(text,()=>{
        const next=ai+move;if(next>=0&&next<turn.actions.length){[turn.actions[ai],turn.actions[next]]=[turn.actions[next],turn.actions[ai]];manualChanged();renderManualTurns();}
      }));
      row.append(manualButton('移除',()=>{turn.actions.splice(ai,1);manualChanged();renderManualTurns();}));block.append(row);
    });
    block.append(manualButton('＋添加行动',()=>{if(turn.actions.length<40){turn.actions.push(newManualAction('skill'));manualChanged();renderManualTurns();}}));container.append(block);
  });
}

function manualSlotPayload(slot) {
  return {source:slot.source,svt_id:slot.svt_id||0,ce_id:slot.ce_id||null,
    support:slot.source==='support'?structuredClone(slot.support||defaultManualSupport()):null,
    support_ce:slot.source==='support'&&slot.ce_id?structuredClone(slot.support_ce??null):null,
    owned_stats:slot.source==='owned'?structuredClone(slot.owned_stats??null):null,
    owned_ce:slot.source==='owned'&&slot.ce_id?structuredClone(slot.owned_ce??null):null,
    mash_form:slot.source==='owned'&&slot.svt_id===800100?(slot.mash_form??null):null};
}

function currentManualPlan() {
  if(manualSupportLoading)throw new Error('请等待助战资料读取完成');
  if(manualParty.some(s=>!s.svt_id))throw new Error('请为全部六个槽位选择角色');
  return {quest_id:document.getElementById('battle-choice').value,
    party:manualParty.map(manualSlotPayload),
    mystic_code_id:Number(document.getElementById('manual-mystic-code').value)||null,
    mystic_code_lv:manualMysticLevel(),turns:structuredClone(manualTurns)};
}

async function startManualSimulation() {
  if(isRunning||manualStarting||manualRequest){alert('请先中断当前任务并等待结束');return;}
  try {
    const plan=currentManualPlan(),revision=manualRevision;manualStarting=true;
    const data=await fetch('/api/manual/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan,client_revision:revision})}).then(r=>r.json());
    if(!data.success)throw new Error(data.message);
    manualRequest=data.request_id;manualStopRequested=false;isRunning=true;
    if(revision!==manualRevision)await stopManualSimulation();
    else document.getElementById('manual-result').textContent='正在按六人编队与指定顺序模拟，可随时中断或修改礼装。';
  }catch(error){document.getElementById('manual-result').textContent=error.message;}
  finally{manualStarting=false;}
}

async function stopManualSimulation() {
  if(!manualRequest)return;
  manualStopRequested=true;
  try {
    const data=await fetch('/api/manual/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:manualRequest})}).then(r=>r.json());
    if(!data.success)throw new Error(data.message);
  }catch(error){manualStopRequested=false;document.getElementById('manual-result').textContent='中断请求未确认：'+error.message;}
}

async function pollManualStatus() {
  if(!manualRequest||manualPolling)return;
  manualPolling=true;
  try {
    const data=await fetch('/api/manual/status').then(r=>r.json()),report=data.report;
    if(!report||report.request_id!==manualRequest)return;
    if(report.status==='running')return;
    manualRequest=null;manualStopRequested=false;
    if(report.client_revision!==manualRevision)return;
    if(report.status!=='completed') {document.getElementById('manual-result').textContent=report.status==='cancelled'?'模拟已中断，可更换配置后重新计算。':report.error;return;}
    const r=report.result,lines=[`手动模拟${r.passed?'通过':'未清场／停止'}：${r.message}`,`关卡 ${r.quest_id} · 阶段${r.phase} · ${r.enemy_region}敌方样本`];
    r.team.forEach(s=>lines.push(`${s.party_slot}号位 ${s.name}${s.is_friend?'（自定义助战）':''} · Lv${s.lv} 宝具${s.np_lv} · 技能${[s.skill_lv1,s.skill_lv2,s.skill_lv3].join('/')} · ${s.ce?`${s.ce.name} Lv${s.ce.lv} ${s.ce.is_mlb?'满破':'未满破'}`:'空礼装'}`));
    lines.push(r.mystic_code?`御主礼装：${r.mystic_code.name} Lv${r.mystic_code.lv}`:'未使用御主礼装技能');
    for(const step of r.steps) {
      lines.push(`\nT${step.turn} · Wave ${step.wave} → ${step.wave_after??step.wave}`);
      if(step.np_before)lines.push('选卡前NP：'+step.np_before.map(s=>`${s.slot}号位 ${s.np.toFixed(2)}%`).join('；'));
      for(const d of step.damage||[])lines.push(`${d.actor_slot??''}号位 → ${d.target}：伤害${Number(d.damage).toLocaleString()} / HP${Number(d.hp).toLocaleString()}`);
      lines.push('回合后：'+(step.after||[]).map(s=>`${s.slot}号位 NP${s.np.toFixed(2)}%`).join('；'));
    }
    if(r.failure)lines.push(`停止位置：T${r.failure.turn} 第${r.failure.action}个行动，${r.failure.message}`);
    lines.push('\n计算条件：',...r.assumptions);
    if(report.save_error)lines.push('运行记录保存失败：'+report.save_error);
    document.getElementById('manual-result').textContent=lines.join('\n');
  }catch(error){document.getElementById('manual-result').textContent=error.message;}
  finally{manualPolling=false;}
}

async function refreshManualPlans() {
  const data=await fetch('/api/manual/plans').then(r=>r.json());if(!data.success)throw new Error(data.message);
  manualSaved=data.plans;const select=document.getElementById('manual-saved-plans'),previous=select.value;
  select.replaceChildren(new Option('请选择已保存方案',''));manualSaved.forEach(p=>select.add(new Option(p.name,p.id)));
  select.value=manualLoadedId||previous;
}
async function saveManualPlan(asNew=false) {
  try {
    const body={name:document.getElementById('manual-plan-name').value,plan:currentManualPlan()};
    if(!asNew&&manualLoadedId)body.id=manualLoadedId;
    const data=await fetch('/api/manual/plans/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
    if(!data.success)throw new Error(data.message);manualLoadedId=data.saved.id;await refreshManualPlans();
    document.getElementById('manual-result').textContent='已保存队伍、角色与礼装配置、助战练度及全部行动顺序：'+data.saved.name;
  }catch(error){document.getElementById('manual-result').textContent=error.message;}
}
async function loadSavedManualPlan() {
  try {
    const saved=manualSaved.find(p=>p.id===document.getElementById('manual-saved-plans').value);if(!saved)throw new Error('请选择已保存方案');
    manualChanged();const plan=structuredClone(saved.plan);manualLoadedId=saved.id;manualParty=plan.party;manualTurns=plan.turns;
    document.getElementById('manual-plan-name').value=saved.name;
    document.getElementById('manual-support-slot').value=String(manualParty.findIndex(s=>s.source==='support')+1);
    const selector=document.getElementById('battle-choice');
    if(![...selector.options].some(o=>o.value===plan.quest_id)) {
      const q=battleCatalog.find(q=>q.quest_id===plan.quest_id);selector.add(new Option(q?`${q.war} / ${q.spot} / ${q.name}`:`已保存关卡 ${plan.quest_id}`,plan.quest_id));
    }
    selector.value=plan.quest_id;updateBattleTarget();
    const code=document.getElementById('manual-mystic-code');code.value=plan.mystic_code_id?String(plan.mystic_code_id):'';
    if(plan.mystic_code_id&&code.value===''){code.add(new Option('账号缺少等级记录：'+plan.mystic_code_id,String(plan.mystic_code_id)));code.value=String(plan.mystic_code_id);}
    const level=document.getElementById('manual-mystic-level');if(level)level.value=plan.mystic_code_lv??'';
    syncManualMysticLevel();
    renderManualRoster();renderManualTurns();await loadManualMysticCode();
    await loadManualSupport(manualParty.find(s=>s.source==='support'));
    document.getElementById('manual-result').textContent='方案已载入，请重新模拟当前账号与配置。';
  }catch(error){document.getElementById('manual-result').textContent=error.message;}
}
