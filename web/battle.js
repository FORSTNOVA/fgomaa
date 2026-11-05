// 关卡模拟与作战页面；资源表单与自动首通使用同一模板及序列化逻辑。
let battleCatalog = [];
let battlePolling = false;

function resourceOptions(prefix) {
  function limit(suffix, max, label) {
    const name = prefix + '-' + suffix;
    if (!document.getElementById(name + '-enabled').checked) return null;
    const value = Number(document.getElementById(name).value);
    if (!Number.isSafeInteger(value) || value < 1 || value > max) throw new Error(label + (max === Number.MAX_SAFE_INTEGER ? '须为正整数' : '须为1至' + max + '的整数'));
    return value;
  }
  const refill = document.getElementById(prefix + '-refill-enabled').checked;
  return {max_quests:limit('limit', prefix === 'first-clear' ? Number.MAX_SAFE_INTEGER : 30, '最多完成数量'), ap_budget:limit('ap', 999, 'AP预算'),
          refill_apple:refill ? document.getElementById(prefix + '-apple').value : 'none',
          max_apples:refill ? limit('apples', 999, '果实使用上限') : 0};
}

function updateResourceControls(prefix) {
  const refill = document.getElementById(prefix + '-refill-enabled').checked;
  document.getElementById(prefix + '-apple').disabled = !refill;
  document.getElementById(prefix + '-apples-enabled').disabled = !refill;
  for (const suffix of ['limit', 'ap', 'apples']) {
    const name = prefix + '-' + suffix;
    const enabled = document.getElementById(name + '-enabled').checked;
    document.getElementById(name).disabled = !enabled || (suffix === 'apples' && !refill);
    document.getElementById(name + '-off').hidden = enabled || (suffix === 'apples' && !refill);
  }
}

function initBattleLab() {
  const template = document.getElementById('first-clear-controls').outerHTML
    .replaceAll('first-clear', 'battle').replaceAll('updateFirstClearControls()', "updateResourceControls('battle')")
    .replace('class="battle-controls"', 'class="first-clear-controls"').replaceAll('class="battle-control"', 'class="first-clear-control"')
    .replaceAll('首通', '作战').replace('<span>关</span>', '<span>场</span>').replace('不限关数', '不限场次');
  document.getElementById('battle-resources').innerHTML = template;
  // 只读模拟是每次打开软件的默认值，不从持久设置恢复出击授权。
  document.getElementById('battle-auto').checked = false;
  document.getElementById('battle-limit').value = '1';
  document.getElementById('battle-limit').max = '30';
  updateResourceControls('battle');
  updateBattleMode();
}

function battleActionText() {
  if (document.getElementById('battle-mode').value === 'interactive') return '进入模拟战斗';
  if (document.getElementById('battle-mode').value === 'manual') return '开始手动模拟';
  return document.getElementById('battle-auto').checked ? '模拟并自动战斗' : '开始模拟配队';
}

async function loadBattleDevices() {
  const selector = document.getElementById('battle-device');
  const previous = selector.value;
  const data = await fetch('/api/devices').then(r => r.json());
  selector.replaceChildren(new Option('请选择设备', ''));
  (data.devices || []).forEach(d => selector.add(new Option(`${d.serial} (${d.model})`, d.serial)));
  if ([...selector.options].some(o => o.value === previous)) selector.value = previous;
  else if (data.devices?.length === 1) selector.value = data.devices[0].serial;
}

function updateBattleMode() {
  const enabled = document.getElementById('battle-auto').checked;
  document.getElementById('battle-execution-settings').disabled = !enabled;
  document.getElementById('battle-start').textContent = battleActionText();
  if (currentTab === 'battle' && !isRunning) document.getElementById('main-btn-text').textContent = battleActionText();
  updateBattleTarget();
  if (enabled) loadBattleDevices().catch(error => { document.getElementById('battle-progress').textContent = error.message; });
}

async function loadBattleCatalog() {
  try {
    const data = await fetch('/api/battle/catalog').then(r => r.json());
    if (!data.success) throw new Error(data.message || '读取目录失败');
    battleCatalog = data.quests;
    filterBattleChoices();
    if (document.getElementById('battle-mode').value !== 'auto') await loadManualInventory(true);
    if (document.getElementById('battle-auto').checked) await loadBattleDevices();
    await pollBattleStatus();
  } catch (error) { document.getElementById('battle-progress').textContent = error.message; }
}

function questSearchKey(value) {
  return String(value).normalize('NFKC').toLowerCase().replace(/[\s\p{P}]+/gu, '');
}

function matchesQuestQuery(q, query) {
  return !query || [q.war, q.spot, q.name, q.game_id,
    q.recommend_lv ? `Lv${q.recommend_lv} 等级${q.recommend_lv}` : '']
    .some(value => questSearchKey(value).includes(query));
}

function questChoicePriority(q) {
  return q.kind === 'event' && q.navigation_supported ? 2 : Number(q.supported);
}

function filterBattleChoices() {
  const query = questSearchKey(document.getElementById('battle-search').value);
  const kind = document.getElementById('battle-kind').value;
  const selector = document.getElementById('battle-choice'), previous = selector.value;
  const rows = battleCatalog.filter(q => q.simulation_supported && (kind === 'all' || kind === q.kind)
    && matchesQuestQuery(q, query));
  rows.sort((a,b) => questChoicePriority(b)-questChoicePriority(a));
  selector.replaceChildren();
  rows.slice(0,300).forEach(q => selector.add(new Option(
    `${{free:'自由',daily:'日常',event:'活动'}[q.kind]} · ${q.war} / ${q.spot} / ${q.name}${q.recommend_lv ? ` · Lv${q.recommend_lv}` : ''} · 阶段${q.phase}${q.supported ? '' : '（仅模拟）'}`, q.quest_id)));
  if ([...selector.options].some(o => o.value === previous)) selector.value = previous;
  if (previous && previous !== selector.value && document.getElementById('battle-mode').value !== 'auto') manualChanged();
  updateBattleTarget();
}

function updateBattleTarget() {
  const row = battleCatalog.find(q => q.quest_id === document.getElementById('battle-choice').value);
  const label = document.getElementById('battle-target');
  if (!row) { label.textContent = '未找到可模拟的关卡；请导入账号或调整搜索条件。'; return; }
  label.textContent = `关卡 ID ${row.game_id}${row.recommend_lv ? ` · Lv${row.recommend_lv}` : ''} · 阶段 ${row.phase} · 标称 ${row.ap_per_run ?? '待读取'} AP` +
    (row.supported ? ' · 可自动导航战斗' : ' · 仅模拟：' + row.reason);
}

async function startBattleLab() {
  if (document.getElementById('battle-mode').value === 'interactive') return enterInteractiveBattle();
  if (document.getElementById('battle-mode').value === 'manual') return startManualSimulation();
  if (isRunning) { alert('已有任务正在运行，请先停止并等待结束'); return; }
  try {
    const quest_id = document.getElementById('battle-choice').value;
    const auto_battle = document.getElementById('battle-auto').checked;
    const row = battleCatalog.find(q => q.quest_id === quest_id);
    if (!row) throw new Error('请选择关卡');
    if (auto_battle && !row.supported) throw new Error('此关卡仅可模拟：' + row.reason);
    const body = {quest_id, auto_battle};
    if (auto_battle) {
      body.device_serial = document.getElementById('battle-device').value;
      if (!body.device_serial) throw new Error('请选择目标设备');
      body.options = resourceOptions('battle');
    }
    const data = await fetch('/api/battle/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r => r.json());
    if (!data.success) throw new Error(data.message);
    isRunning = true;
    document.getElementById('battle-progress').textContent = data.message;
    for (const id of ['battle-party', 'battle-simulation', 'battle-candidates']) document.getElementById(id).replaceChildren();
  } catch (error) { alert(error.message); }
}

function renderBattleReport(report) {
  const labels = {starting:'准备中',pending:'等待',simulating:'模拟配队',observing:'核对实机 AP',refilling:'恢复体力',navigating:'自动导航',assembling:'编队核对',equipping:'装备礼装',fighting:'战斗中',settling:'结算中',completed:'完成',preview_passed:'模拟通过',failed:'失败停止',cancelled:'已取消',ap_limit_reached:'达到资源限制，已停止'};
  const lines = [`${report.preview_only ? '仅模拟' : '模拟并自动战斗'}：${labels[report.status] || report.status}`,
    `确认完成 ${report.confirmed_clears || 0} 场 · 已提交出击估算消耗 ${report.estimated_ap_spent || 0} AP`];
  if (!report.preview_only && report.options) {
    lines.push(`场次上限 ${report.options.max_quests ?? '不限'} · AP预算 ${report.options.ap_budget ?? '不限'}`);
    lines.push(report.options.refill_apple === 'none' ? '体力恢复：关闭' : `果实已确认 ${report.apples_used || 0} 个 · 已提交 ${report.apples_committed || 0}/${report.options.max_apples ?? '不限'} 个`);
  }
  if (report.error) lines.push(report.error);
  if (report.artifacts_dir) lines.push('运行记录：' + report.artifacts_dir);
  document.getElementById('battle-progress').textContent = lines.join('\n');
  const row = [...(report.stations || [])].reverse().find(s => s.recommendation);
  if (!row) return;
  const r = row.recommendation, t = row.target;
  const party = [`${t.war} / ${t.spot} / ${t.name}（ID ${t.game_id}，阶段 ${t.phase}）`,
    `敌方：${r.enemy_class_cn} · ${r.waves.join('-')} · ${r.direction_name}`,
    `1号位：${r.slot1_dps}（${r.slot1_affinity}×克制）`, `2号位：${r.slot2_support}`,
    `3号位：${r.slot3_friend_support}（助战）`, `4号位：${r.slot4_backline_support}`, `御主礼装：${r.mystic_code}`];
  const ces = Object.keys(r.party_ce_details || {}).length ? r.party_ce_details : {1:r.slot1_ce_detail};
  for (const [slot, ce] of Object.entries(ces)) {
    if (ce?.name) party.push(`${slot}号位礼装：${ce.name} · Lv${ce.lv} · ${ce.is_mlb ? '满破' : '未满破'}`);
  }
  party.push('配队依据：' + r.direction_reason);
  document.getElementById('battle-party').textContent = party.join('\n');
  document.getElementById('battle-simulation').innerHTML = renderLaplace(r.laplace);
  const candidates = (r.candidate_dps_ranking || []).slice(0,6).map((c,i) =>
    `${i+1}. ${c.name} · ${c.class_name} · Lv${c.lv} 宝具${c.np_lv} · 克制${c.affinity}× · 礼装${c.ce_req}`);
  document.getElementById('battle-candidates').textContent = candidates.length ? '候选打手与礼装\n' + candidates.join('\n') : '';
}

async function pollBattleStatus() {
  if (currentTab !== 'battle' || document.getElementById('battle-mode').value !== 'auto' || battlePolling) return;
  battlePolling = true;
  try {
    const data = await fetch('/api/battle/status').then(r => r.json());
    if (data.report) renderBattleReport(data.report);
  } catch (_) {} finally { battlePolling = false; }
}
