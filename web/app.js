/* 缠论复盘选股台 前端 */
const $ = s => document.querySelector(s);
const TYPES = [['1', '一买'], ['1p', '盘整一买'], ['2', '二买'], ['2s', '类二买'], ['3a', '三买a'], ['3b', '三买b']];
const BOARDS = [['sh60', '沪主板'], ['sz00', '深主板'], ['sz30', '创业板'], ['sh68', '科创板']];
const TYPE_CN = Object.fromEntries(TYPES);

async function api(url, body) {
  const opt = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
  if (!j.ok) throw new Error(j.error || '请求失败');
  return j.data;
}
const pct = (v, d = 2) => v == null ? '<span class="muted">-</span>' :
  `<span class="${v > 0 ? 'up' : v < 0 ? 'down' : ''}">${(v * 100).toFixed(d)}%</span>`;
const num = (v, d = 2) => v == null ? '-' : Number(v).toFixed(d);
const typesCN = t => (t || '').split(',').map(x => TYPE_CN[x] || x).join('+');
const sigName = (t, buy) => buy ? typesCN(t) : typesCN(t).replace(/买/g, '卖');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function chips(el, items, onDefault = []) {
  el.innerHTML = items.map(([k, v]) => `<span class="chip ${onDefault.includes(k) ? 'on' : ''}" data-k="${k}">${v}</span>`).join('');
  el.onclick = e => { const c = e.target.closest('.chip'); if (c) c.classList.toggle('on'); };
  return () => [...el.querySelectorAll('.chip.on')].map(c => c.dataset.k);
}
function toast(msg) { $('#statusbar').textContent = msg; }
const MODES = [['龙头', '龙头'], ['补涨', '补涨'], ['切换', '切换']];
const PHASE_COLOR = { '低位试错': '#7d8aa0', '主升': '#f0524a', '高位震荡': '#e0a030', '主跌': '#26b36e' };
const phaseTag = p => p ? `<span class="ptag" style="background:${PHASE_COLOR[p] || '#999'}">${p}</span>` : '<span class="muted">-</span>';
function phaseBanner(el, e, small) {
  if (!e || !e.phase) { el.style.display = 'none'; return; }
  el.style.display = '';
  el.style.borderLeftColor = PHASE_COLOR[e.phase];
  el.innerHTML = `<span class="ph" style="color:${PHASE_COLOR[e.phase]}">${e.phase}</span>
    <span class="meta">水温 <b>${num(e.temp, 0)}</b> · 建议仓位上限 <b>${e.cap == null ? '-' : Math.round(e.cap * 100) + '%'}</b> · 推荐模式 ${(e.recommend || []).map(m => `<span class="tag">${m}</span>`).join('') || '-'} · ${e.date}</span>
    ${small ? '' : (e.transition ? `<div class="meta">自 ${e.transition.date} 起进入${e.transition.phase}：${esc(e.transition.note)}</div>` : '')}
    <div class="adv${small ? ' muted' : ''}" style="${small ? 'font-size:12px;margin-top:2px' : ''}">${esc(e.advice)}</div>`;
}

// ------------------------------------------------------------ 导航
const pages = {};
document.querySelectorAll('#nav a').forEach(a => a.onclick = () => {
  document.querySelectorAll('#nav a').forEach(x => x.classList.toggle('active', x === a));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === 'page-' + a.dataset.page));
  (pages[a.dataset.page] || {}).show?.();
  window.dispatchEvent(new Event('resize'));
});

let STATUS = null;
async function refreshStatus() {
  try {
    STATUS = await api('/api/status');
    const s = STATUS;
    let t = `最新交易日 ${s.latest_day || '无数据'} · ${s.stocks} 只股票 · ${s.signals} 条信号`;
    if (s.running && s.progress) t = `后台任务：${s.progress.stage} ${s.progress.total ? `${s.progress.done}/${s.progress.total}` : ''}`;
    if (s.emotion && s.emotion.phase && !s.running) t += ` · 情绪：${s.emotion.phase}`;
    if (s.mock) t += ' · 模拟数据';
    toast(t);
  } catch (e) { toast('服务未连接：' + e.message); }
  return STATUS;
}

// ------------------------------------------------------------ 选股台
const selTypes = chips($('#sel-types'), TYPES, ['1', '2', '2s', '3a', '3b']);
const selBoards = chips($('#sel-boards'), BOARDS, ['sh60', 'sz00', 'sz30']);
const selChart = chanChart($('#sel-chart'));
let selChart30 = null;
let selRows = [], selSort = { k: 'score', asc: false }, selCurrent = null;

async function runSelect() {
  const q = new URLSearchParams({ date: $('#sel-date').value, window: $('#sel-window').value,
    min_score: $('#sel-min').value, types: selTypes().join(','), boards: selBoards().join(','), gate: $('#sel-gate').checked ? '1' : '0' });
  $('#sel-hint').textContent = '计算中…';
  try {
    const d = await api('/api/select?' + q);
    selRows = d.rows;
    if (!$('#sel-date').value) $('#sel-date').value = d.date;
    $('#sel-hint').textContent = `${d.date}（信号窗口 ${d.days ? d.days[0] : ''} ~ ${d.date}）共 ${selRows.length} 只${d.blocked ? `，水温闸门拦掉 ${d.blocked} 只` : ''}。评分只用发现日及之前的数据。`;
    phaseBanner($('#sel-emo'), d.emotion, true);
    drawSelTable();
  } catch (e) { $('#sel-hint').textContent = '出错：' + e.message; }
}
function drawSelTable() {
  const cols = [['code', '代码', 'l'], ['name', '名称', 'l'], ['types', '买点', 'l'], ['score', '评分'], ['theme', '题材/情绪', 'l'],
    ['bsp_date', '买点日'], ['detect_date', '发现日'], ['close', '现价'], ['gain', '距买点'], ['c30', '区间套', 'l']];
  const k = selSort.k;
  const rows = [...selRows].sort((a, b) => ((a[k] ?? -1e9) > (b[k] ?? -1e9) ? 1 : -1) * (selSort.asc ? 1 : -1));
  $('#sel-table').innerHTML = `<thead><tr>${cols.map(c => `<th class="${c[2] || ''}" data-k="${c[0]}">${c[1]}${k === c[0] ? (selSort.asc ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr></thead>
  <tbody>${rows.map(r => `<tr data-code="${r.code}" class="${selCurrent === r.code ? 'sel' : ''}">
    <td class="l mono">${r.code}</td><td class="l">${esc(r.name)}</td><td class="l">${typesCN(r.types)}</td>
    <td><b>${num(r.score, 0)}</b></td>
    <td class="l">${phaseTag(r.phase)} ${esc(r.theme || '')} ${r.theme_new ? '<span class="tag new">新题材</span>' : ''}${r.theme_old ? '<span class="tag old">老题材</span>' : ''}${r.gate_ok === false ? `<span class="tag warn">${r.gate_reason}</span>` : ''}</td>
    <td>${r.bsp_date}</td><td>${r.detect_date}</td><td>${num(r.close)}</td><td>${pct(r.gain, 1)}</td>
    <td class="l">${r.c30 == null ? '' : r.c30 === 'err' ? '<span class="muted">失败</span>' : r.c30 ? '<span class="tag">30分钟买点</span>' : '<span class="muted">无</span>'}</td></tr>`).join('')}</tbody>`;
}
$('#sel-table').onclick = e => {
  const th = e.target.closest('th');
  if (th) { const k = th.dataset.k; selSort = { k, asc: selSort.k === k ? !selSort.asc : false }; drawSelTable(); return; }
  const tr = e.target.closest('tr[data-code]');
  if (tr) showSelStock(tr.dataset.code);
};
async function showSelStock(code) {
  selCurrent = code; drawSelTable();
  const r = selRows.find(x => x.code === code);
  const date = $('#sel-date').value;
  const det = r.detail || {};
  const maxes = { '类型': 35, '力度': 25, '结构': 15, '及时': 10, '确认': 10, '位置': 15 };
  $('#sel-detail').innerHTML = `<h3>${esc(r.name)} <span class="mono muted">${r.code}</span> <span class="tag">${typesCN(r.types)}</span> 评分 <b>${num(r.score, 0)}</b></h3>
    <div class="muted">买点 ${r.bsp_date} @ ${num(r.bsp_price)} · 发现于 ${r.detect_date} · 现价 ${num(r.close)}（距买点 ${pct(r.gain, 1)}）· ${r.board}</div>
    <div class="muted">发现日情绪：${phaseTag(r.phase)} ${r.theme ? '题材 ' + esc(r.theme) : '无热门题材'}${r.hyped ? ` · 近40日最高 ${r.hyped} 板` : ''}${r.gate_ok === false ? ` · <span class="tag warn">闸门：${r.gate_reason}</span>` : ''}</div>
    <div class="bars">${Object.entries(det).map(([k, v]) => `<span>${k}</span><div class="bar"><i style="width:${Math.min(100, v / (maxes[k] || 25) * 100)}%"></i></div><span>${v}/${maxes[k] || ''}</span>`).join('')}</div>
    <div class="muted" style="font-size:12px">特征：${Object.entries(r.features || {}).map(([k, v]) => `${k}=${v}`).join('，') || '无'}</div>
    <div style="margin-top:6px"><button id="sel-one30">30分钟区间套确认</button> <button id="sel-replay">在复盘里回放</button> <span id="sel-30msg" class="muted"></span></div>`;
  $('#sel-one30').onclick = () => confirm30(code, true);
  $('#sel-replay').onclick = () => { $('#rp-code').value = code; const d = new Date(r.bsp_date); d.setDate(d.getDate() - 60);
    $('#rp-start').value = d.toISOString().slice(0, 10); document.querySelector('#nav a[data-page=replay]').click(); startReplay(); };
  $('#sel-chart30').style.display = 'none';
  try {
    const d = await api(`/api/chart?code=${code}&date=${date}`);
    selChart.resetZoom();
    selChart.render(d.chart.day, { title: `${r.name} 日线（截至 ${d.date}）`, markDate: r.detect_date, zoomStart: 35, resetZoom: true });
  } catch (e) { $('#sel-detail').insertAdjacentHTML('beforeend', `<div class="down">${e.message}</div>`); }
}
async function confirm30(code, show) {
  const r = selRows.find(x => x.code === code);
  if (show) $('#sel-30msg').textContent = '下载30分钟数据并计算…';
  try {
    const d = await api(`/api/confirm30?code=${code}&date=${$('#sel-date').value}`);
    r.c30 = d.confirmed;
    if (show) {
      $('#sel-30msg').innerHTML = d.confirmed ? `最近3天30分钟级别出现买点：${d.hits.map(h => `${h.time} ${typesCN(h.types)}`).join('；')}` : '最近3天30分钟级别没有买点';
      $('#sel-chart30').style.display = '';
      if (!selChart30) selChart30 = chanChart($('#sel-chart30'));
      selChart30.render(d.chart, { title: '30分钟', zoomStart: 40, resetZoom: true });
    }
  } catch (e) { r.c30 = 'err'; if (show) $('#sel-30msg').textContent = e.message; }
  drawSelTable();
}
$('#sel-run').onclick = runSelect;
$('#sel-confirm').onclick = async () => {
  const btn = $('#sel-confirm'); btn.disabled = true;
  const top = [...selRows].sort((a, b) => b.score - a.score).slice(0, 20);
  for (let i = 0; i < top.length; i++) { btn.textContent = `确认中 ${i + 1}/${top.length}`; await confirm30(top[i].code, false); }
  btn.textContent = '前20只做区间套确认'; btn.disabled = false;
};
pages.select = { show() { if (!selRows.length) runSelect(); } };

// ------------------------------------------------------------ 水温·情绪周期
let emChart = null, EM = null;
function phaseBands(dates, phases) {
  const out = [];
  let s = 0;
  for (let i = 1; i <= dates.length; i++) {
    if (i === dates.length || phases[i] !== phases[s]) {
      if (phases[s]) out.push([{ xAxis: dates[s], itemStyle: { color: PHASE_COLOR[phases[s]], opacity: phases[s] === '低位试错' ? 0.05 : 0.16 } }, { xAxis: dates[i - 1] }]);
      s = i;
    }
  }
  return out;
}
async function runEmotion(date, withSeries = true) {
  const q = new URLSearchParams({ date: date || $('#em-date').value || '', series: withSeries ? '1' : '0' });
  const yrs = +$('#em-range').value;
  if (withSeries && yrs) { const d = new Date(date || $('#em-date').value || Date.now()); d.setFullYear(d.getFullYear() - yrs); q.set('start', d.toISOString().slice(0, 10)); }
  $('#em-empty').textContent = '加载中…';
  try {
    const d = await api('/api/emotion?' + q);
    $('#em-empty').textContent = '';
    if (withSeries) { EM = d.series; drawEmotionChart(); }
    drawEmotionDay(d.detail);
  } catch (e) { $('#em-empty').textContent = e.message; }
}
function drawEmotionChart() {
  if (!emChart) {
    emChart = echarts.init($('#em-chart'), THEME);
    window.addEventListener('resize', () => emChart.resize());
    let hover = null;
    emChart.on('updateAxisPointer', e => { const a = (e.axesInfo || [])[0]; if (a) hover = a.value; });
    emChart.getZr().on('click', e => {
      if (!EM || hover == null || !emChart.containPixel({ gridIndex: [0, 1] }, [e.offsetX, e.offsetY])) return;
      const x = EM[hover];
      if (x) { $('#em-date').value = x.date; runEmotion(x.date, false); }
    });
  }
  const dates = EM.map(x => x.date);
  emChart.setOption({
    animation: false, legend: { top: 0 }, axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: { trigger: 'axis', formatter: ps => { const x = EM[ps[0].dataIndex];
      return `<b>${x.date}</b> ${x.phase}<br>水温 ${num(x.temp, 0)} · 最高 ${x.height} 板<br>涨停 ${x.zt} · 跌停 ${x.dt} · 炸板率 ${x.zb_rate == null ? '-' : (x.zb_rate * 100).toFixed(0) + '%'}<br>昨日涨停溢价 ${x.prev_zt_ret == null ? '-' : (x.prev_zt_ret * 100).toFixed(2) + '%'}${x.note ? '<br>' + esc(x.note) : ''}`; } },
    grid: [{ left: 50, right: 50, top: 30, height: '50%' }, { left: 50, right: 50, top: '66%', height: '24%' }],
    xAxis: [{ type: 'category', data: dates, axisLabel: { show: false } }, { type: 'category', gridIndex: 1, data: dates }],
    yAxis: [{ min: 0, max: 100, name: '水温', splitLine: { lineStyle: { color: '#202834' } } },
            { gridIndex: 1, name: '家数', splitLine: { show: false } }, { gridIndex: 1, name: '板数', position: 'right', splitLine: { show: false } }],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }, { type: 'slider', xAxisIndex: [0, 1], height: 14, bottom: 0 }],
    series: [
      { name: '水温', type: 'line', data: EM.map(x => x.temp), symbol: 'none', lineStyle: { color: '#e3e7ee', width: 1.4 }, color: '#e3e7ee',
        markArea: { silent: true, data: phaseBands(dates, EM.map(x => x.phase)) } },
      { name: '涨停', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: EM.map(x => x.zt), color: UP, barMaxWidth: 4 },
      { name: '跌停', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: EM.map(x => -x.dt), color: DOWN, barMaxWidth: 4, barGap: '-100%' },
      { name: '最高板', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: EM.map(x => x.height), symbol: 'none', step: 'end', lineStyle: { color: '#6f95ff', width: 1 }, color: '#6f95ff' },
    ] }, true);
}
function drawEmotionDay(D) {
  const m = D.metrics;
  phaseBanner($('#em-banner'), { ...m, cap: D.cap, recommend: D.recommend, advice: D.advice, transition: D.transition }, false);
  const P = v => v == null ? '-' : (v * 100).toFixed(1) + '%';
  const card = (k, v, s = '') => `<div class="card"><div class="k">${k}</div><div class="v" style="font-size:17px">${v}</div><div class="s">${s}</div></div>`;
  $('#em-cards').innerHTML = [
    card('涨停 / 跌停', `<span class="up">${m.zt}</span> / <span class="down">${m.dt}</span>`, `炸板 ${m.zb} · 炸板率 ${P(m.zb_rate)}`),
    card('空间高度', `${m.height} 板`, `连板股 ${m.lb} 只`),
    card('晋级率', `${P(m.promo1)} / ${P(m.promo_lb)}`, '1进2 / 连板晋级'),
    card('昨日涨停今日', pct(m.prev_zt_ret), `昨日连板今日 ${pct(m.prev_lb_ret)}`),
    card('大面 / A杀', `${m.damian} / ${m.kill}`, '昨涨停今跌≥5% / 高位股跌≥7%'),
    card('高位股 / 中位股', `${pct(m.hp_ret)} / ${pct(m.mid_ret)}`, '当日平均涨跌'),
    card('上涨家数占比', P(m.up_ratio), `成交额 ${num(m.amount, 0)} 亿`),
  ].join('');
  // 选股
  const cols = D.recommend || [];
  $('#em-picks').innerHTML = ['龙头', '补涨', '切换'].map(mode => {
    const rows = D.picks[mode] || [];
    const rec = cols.includes(mode);
    return `<div><h5 class="${rec ? 'rec' : ''}">${rec ? '★ ' : ''}${mode}（${rows.length}）</h5>
      ${rows.length ? `<table class="grid em-pick"><thead><tr><th class="l">名称</th><th class="l">题材</th><th>板</th><th>评分</th><th class="l">适配</th><th>次日开盘后3日</th></tr></thead><tbody>
      ${rows.map(r => `<tr data-code="${r.code}" data-name="${esc(r.name)}" title="${esc(Object.entries(r.detail).map(([k, v]) => k + '：' + v).join('，'))}">
        <td class="l">${esc(r.name)} <span class="mono muted">${r.code.slice(3)}</span></td><td class="l">${esc(r.theme || '-')}</td><td>${r.board}</td><td><b>${num(r.score, 0)}</b></td>
        <td class="l">${r.fit ? '<span class="tag">适配</span>' : '<span class="tag gray">阶段不符</span>'}${r.buyable === false ? '<span class="tag warn">一字</span>' : ''}</td><td>${pct(r.oret3)}</td></tr>`).join('')}</tbody></table>`
      : '<div class="muted">今天没有符合条件的股票</div>'}</div>`;
  }).join('');
  document.querySelectorAll('.em-pick').forEach(t => t.onclick = e => { const tr = e.target.closest('tr[data-code]'); if (tr) openModal(tr.dataset.code, D.date, tr.dataset.name); });
  // 连板梯队
  const byB = {};
  D.ladder.forEach(x => (byB[x.board] = byB[x.board] || []).push(x));
  $('#em-ladder').innerHTML = Object.keys(byB).sort((a, b) => b - a).map(b => `<div class="ladder-row"><b>${b}板</b><div>${byB[b].map(x =>
    `<span class="stk" data-code="${x.code}" data-name="${esc(x.name)}">${esc(x.name)}<span class="muted">${x.theme ? '·' + esc(x.theme) : ''}</span></span>`).join('')}</div></div>`).join('')
    || '<div class="muted">今天没有连板股</div>';
  $('#em-ladder').onclick = e => { const s = e.target.closest('.stk'); if (s) openModal(s.dataset.code, D.date, s.dataset.name); };
  // 题材
  $('#em-tsrc').textContent = D.theme_source ? `来源：${D.theme_source}${D.theme_note ? '（' + D.theme_note + '）' : ''}` : '';
  $('#em-themes').innerHTML = `<thead><tr><th class="l">题材</th><th>今日涨停</th><th>5日涨停</th><th class="l">龙头</th><th class="l">标记</th></tr></thead><tbody>${D.themes.map(t => `<tr>
    <td class="l">${esc(t.theme)}</td><td>${t.zt}</td><td>${t.zt5}</td><td class="l">${t.leader ? esc(t.leader_name) + ` <span class="muted">${t.leader_board}板</span>` : '-'}</td>
    <td class="l">${D.mainline.includes(t.theme) ? '<span class="tag main">主线</span>' : ''}${t.old ? '<span class="tag old">老题材</span>' : ''}${t.new ? '<span class="tag new">新题材</span>' : ''}</td></tr>`).join('')
    || '<tr><td colspan="5" class="muted l">近5日没有题材出现涨停</td></tr>'}</tbody>`;
}
$('#em-run').onclick = () => runEmotion();
$('#em-range').onchange = () => runEmotion();
pages.emotion = { show() { if (!EM) runEmotion(); else emChart && emChart.resize(); } };

// ------------------------------------------------------------ 连板天梯
const LD = { data: null, theme: null, kind: 'zt' };
const TCOLORS = ['#ff6b5f', '#ff8f3f', '#f2b134', '#e05db5', '#b57bff', '#5b8cff', '#36b3e0', '#2fc4a0', '#8bc34a', '#f06292', '#a1887f', '#7986cb'];
function themeColor(t) {
  if (!t) return '#5c6778';
  let h = 0; for (const ch of t) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return TCOLORS[h % TCOLORS.length];
}
const probColor = p => p == null ? '#6b7585' : p >= 0.5 ? '#ff5a4f' : p >= 0.35 ? '#ff9f43' : p >= 0.2 ? '#d7dce4' : '#6b7585';
const probTxt = p => p == null ? '--' : Math.round(p * 100) + '%';
const sealKey = r => r.tags.first ? (r.tags.first === '09:25' ? '09:25' : r.tags.first) : '99:99';
function whyHtml(why, max = 6) {
  return (why || []).slice(0, max).map(([k, v, c]) => `<b class="${c >= 0 ? 'pos' : 'neg'}">${c >= 0 ? '▲' : '▼'}</b> ${esc(k)} ${esc(v)}`).join('　');
}
async function runLadder(date) {
  $('#ld-msg').textContent = '加载中…';
  try {
    const d = await api('/api/ladder?' + new URLSearchParams({ date: date || $('#ld-date').value || '', kind: LD.kind }));
    LD.data = d; $('#ld-date').value = d.date; $('#ld-msg').textContent = '';
    if (LD.theme && !d.rows.some(r => r.theme === LD.theme)) LD.theme = null;
    drawLadder();
  } catch (e) { $('#ld-msg').textContent = e.message; $('#ld-board').innerHTML = ''; $('#ld-focus').innerHTML = ''; }
}
function ldSort(rows) {
  const by = $('#ld-sort').value, heat = {};
  rows.forEach(r => heat[r.theme] = (heat[r.theme] || 0) + 1);
  const f = {
    prob: (a, b) => (b.prob ?? -1) - (a.prob ?? -1),
    seal: (a, b) => sealKey(a).localeCompare(sealKey(b)),
    turn: (a, b) => (b.tags.turn ?? -1) - (a.tags.turn ?? -1),
    theme: (a, b) => (heat[b.theme] || 0) - (heat[a.theme] || 0) || String(a.theme).localeCompare(String(b.theme)) || (b.prob ?? -1) - (a.prob ?? -1),
  }[by];
  return rows.slice().sort(f);
}
function ldCard(r, fail) {
  const c = themeColor(r.theme), t = r.tags || {};
  let res = '';
  if (fail) res = `<span class="t no">断板 ${pct(r.pct, 1)}</span>`;
  else if (r.nxt_zt === 1) res = '<span class="t ok">次日晋级</span>';
  else if (r.nxt_zt === 0) res = `<span class="t no">次日 ${pct(r.nxt_pct, 1)}</span>`;
  const seal = LD.kind === 'dt' ? '' : (t.first ? `首封 ${t.first}` : '首封 --');
  return `<div class="ld-card${fail ? ' fail' : ''}" style="--c:${c}" data-code="${r.code}" data-name="${esc(r.name)}">
    <div class="r1"><span class="nm">${esc(r.name)}</span>${LD.kind === 'zt' ? `<span class="pp" style="color:${probColor(r.prob)}">${probTxt(r.prob)}</span>` : ''}</div>
    <div class="r2"><span class="tt">${esc(r.theme || '无热门题材')}</span><span class="seal">${seal}</span></div>
    <div class="r3">${t.yizi ? '<span class="t yz">一字</span>' : ''}${t.tzi ? '<span class="t tz">T字</span>' : ''}${t.cy ? '<span class="t cy">创</span>' : ''}${t.opens ? `<span class="t op">开板${t.opens}次</span>` : ''}${res}${t.nm ? `<span class="t nmz">${t.nm}</span>` : ''}</div>
    ${LD.kind === 'zt' ? `<div class="pb"><i style="width:${Math.round((r.prob || 0) * 100)}%;background:${probColor(r.prob)}"></i></div>` : ''}
  </div>`;
}
function drawLadder() {
  const d = LD.data, zt = LD.kind === 'zt';
  // 题材筛选
  const cnt = {};
  d.rows.forEach(r => { if (r.theme) cnt[r.theme] = (cnt[r.theme] || 0) + 1; });
  const ths = Object.entries(cnt).sort((a, b) => b[1] - a[1]);
  const chip = ([t, n]) => `<span class="ld-chip${LD.theme === t ? ' on' : ''}" style="--c:${themeColor(t)}" data-t="${esc(t)}">${esc(t)} ${n}</span>`;
  $('#ld-themes').innerHTML = `<span class="ld-chip${LD.theme ? '' : ' on'}" style="--c:#5b8cff" data-t="">全部题材</span>${ths.slice(0, 6).map(chip).join('')}
    ${ths.length > 6 ? `<details class="ld-more"><summary>更多 (${ths.length - 6}) ▾</summary><div class="pop">${ths.slice(6).map(chip).join('')}</div></details>` : ''}`;
  const e = d.emotion;
  $('#ld-emo').innerHTML = e ? `${phaseTag(e.phase)} 水温 ${num(e.temp, 0)}` : '';
  const rows = d.rows.filter(r => !LD.theme || r.theme === LD.theme);
  const fails = $('#ld-failed').checked && zt ? d.failed.filter(r => !LD.theme || r.theme === LD.theme) : [];
  // 二进三 重点面板
  if (zt) {
    const two = rows.filter(r => r.board === 2).sort((a, b) => (b.prob ?? -1) - (a.prob ?? -1));
    const ev = (d.eval || {})['二板'] || {};
    const base = ev.base, t1 = ev.top1 || {};
    $('#ld-focus').innerHTML = `<div class="ld-panel"><h4>二进三候选<span class="sub">今天的二板，按明天晋级三板的概率排序</span></h4>
      ${two.length ? `<table class="ld-rank"><thead><tr><th>#</th><th>名称</th><th>题材</th><th>晋级概率</th><th>首封</th><th>开板</th><th>换手</th><th>主要理由</th>${two.some(r => r.nxt_zt != null) ? '<th>次日</th>' : ''}</tr></thead><tbody>
      ${two.map((r, i) => `<tr data-code="${r.code}" data-name="${esc(r.name)}"><td>${i + 1}</td><td>${esc(r.name)}</td><td style="color:${themeColor(r.theme)}">${esc(r.theme || '-')}</td>
        <td><span class="ld-pbar"><i style="width:${Math.round((r.prob || 0) * 100)}%;background:${probColor(r.prob)}"></i></span><b style="color:${probColor(r.prob)}">${probTxt(r.prob)}</b></td>
        <td>${r.tags.first || '--'}</td><td>${r.tags.opens ?? '--'}</td><td>${r.tags.turn == null ? '--' : r.tags.turn.toFixed(1) + '%'}</td><td class="why">${whyHtml(r.why, 4)}</td>
        ${r.nxt_zt != null ? `<td>${r.nxt_zt ? '<span class="t ok">晋级</span>' : `<span class="t no">${pct(r.nxt_pct, 1)}</span>`}</td>` : (two.some(x => x.nxt_zt != null) ? '<td></td>' : '')}</tr>`).join('')}</tbody></table>`
      : '<div class="ld-msg">今天没有二板股</div>'}</div>
      <div class="ld-panel ld-stat"><h4>二进三模型 · 样本外</h4>
      ${ev.n ? `历史二板 <b>${ev.n}</b> 次，平均晋级率 <b>${pct(base, 1)}</b><br>
        区分度 AUC <b>${num(ev.auc, 2)}</b>（0.5 = 瞎猜）<br>
        每天只挑概率最高的一只：晋级率 <b>${pct(t1.rate, 1)}</b>（${t1.days || 0} 天）<br>
        前三名合计晋级率 <b>${pct((ev.top3 || {}).rate, 1)}</b><br>
        ${ev.use_intra ? '已使用分时封板特征' : `<span style="color:#ff9f43">未使用分时封板特征（覆盖率 ${pct(ev.intra_cover, 0)}），可在「数据」页补全</span>`}`
        : '历史样本不足，暂时没有样本外结果'}
      <div style="margin-top:6px"><button class="ld-btn" onclick="openLadderModel()">看校准、打板收益和权重</button></div></div>`;
  } else $('#ld-focus').innerHTML = '';
  // 天梯
  const levels = {};
  rows.forEach(r => (levels[r.board] = levels[r.board] || []).push(r));
  fails.forEach(r => (levels[r.board] = levels[r.board] || []).push({ ...r, _fail: true }));
  const ks = Object.keys(levels).map(Number).sort((a, b) => b - a);
  const top = ks[0];
  $('#ld-board').innerHTML = ks.map(b => {
    const list = levels[b], act = list.filter(x => !x._fail), fl = list.filter(x => x._fail);
    const pr = d.promo[b];
    const lbl = zt ? (b === 1 ? '首板' : `${b} 板`) : (b === 1 ? '首次跌停' : `${b} 连跌停`);
    return `<div class="ld-level${zt && b === 2 ? ' focus' : ''}"><div class="ld-lv"><div class="h${b === top && zt ? ' top' : ''}">${lbl}</div><div class="n">${act.length} 只${fl.length ? ` · 淘汰 ${fl.length}` : ''}</div>
      ${zt && pr && b > 1 ? `<div class="pr">今日 ${b - 1}进${b}：${act.length}/${pr.from}（${pct(act.length / pr.from, 0)}）</div>` : ''}
      ${zt && b === 2 ? '<div class="fx">二进三重点</div>' : ''}</div>
      <div class="ld-cards">${ldSort(act).map(r => ldCard(r)).join('')}${fl.map(r => ldCard(r, true)).join('')}</div></div>`;
  }).join('') || `<div class="ld-msg" style="padding:20px">${zt ? '今天没有涨停股' : '今天没有跌停股'}</div>`;
}
window.openLadderModel = async () => {
  $('#ld-model').style.display = 'flex';
  const ev = await api('/api/ladder/eval');
  $('#ld-model-body').innerHTML = ['二板', '首板', '三板+'].filter(k => ev[k]).map(k => {
    const v = ev[k];
    if (!v.calib) return `<div class="seg"><h4>${k}</h4><div class="muted">样本外数据不足</div></div>`;
    return `<div class="seg"><h4>${k === '二板' ? '二进三（二板模型）' : k}<span class="muted" style="font-weight:400;font-size:12px">　样本外 ${v.n} 次 · 平均晋级率 ${pct(v.base, 1)} · AUC ${num(v.auc, 2)} · 每天最高概率一只晋级率 ${pct(v.top1.rate, 1)}${v.use_intra ? ' · 含分时特征' : ''}</span></h4>
      <div class="cols"><div><div id="ld-cal-${k}" class="chart" style="height:220px;border:none"></div><div class="muted" style="font-size:12px">按预测概率从低到高分 10 组：柱子是实际晋级率，线是平均预测概率。两者贴近说明概率可信。</div></div>
      <div><table class="grid"><thead><tr><th class="l">预测概率</th><th>次数</th><th>实际晋级率</th><th>打板次数</th><th>打板次日开盘卖</th><th>打板胜率</th></tr></thead><tbody>
      ${v.dab.map(x => `<tr><td class="l">${pct(x.p_lo, 0)} ~ ${pct(x.p_hi, 0)}</td><td>${x.n}</td><td>${pct(x.rate, 1)}</td><td>${x.trades}</td><td>${pct(x.dab)}</td><td>${pct(x.dab_win, 0)}</td></tr>`).join('')}</tbody></table>
      <div class="muted" style="font-size:12px;margin-top:4px">打板 = 次日盘中摸到涨停价就以涨停价买入（一字板买不到不算），第三天开盘卖出，含手续费和滑点；摸板后炸板的亏损也算在内。</div>
      <h4 style="margin-top:10px">权重（标准化后，正数 = 提高晋级概率）</h4>
      <div>${(v.coef || []).slice(0, 14).map(c => `<span class="tag${c.w >= 0 ? '' : ' sell'}">${c.label} ${c.w > 0 ? '+' : ''}${c.w.toFixed(2)}</span>`).join(' ')}</div></div></div></div>`;
  }).join('') || '<div class="muted">还没有模型结果</div>';
  ['二板', '首板', '三板+'].forEach(k => {
    const el = document.getElementById('ld-cal-' + k), v = ev[k];
    if (!el || !v.calib) return;
    echarts.init(el, THEME).setOption({ grid: { left: 40, right: 10, top: 10, bottom: 24 }, tooltip: { trigger: 'axis', valueFormatter: x => (x * 100).toFixed(1) + '%' },
      xAxis: { type: 'category', data: v.calib.map((_, i) => i + 1) }, yAxis: { type: 'value', max: 1, axisLabel: { formatter: x => x * 100 + '%' } },
      series: [{ name: '实际晋级率', type: 'bar', data: v.calib.map(c => c.rate), color: UP }, { name: '平均预测概率', type: 'line', data: v.calib.map(c => c.p), color: '#6f95ff' }] });
  });
};
$('#ld-model-btn').onclick = () => openLadderModel();
$('#ld-model-close').onclick = () => $('#ld-model').style.display = 'none';
$('#ld-model').onclick = e => { if (e.target.id === 'ld-model') $('#ld-model').style.display = 'none'; };
$('#ld-kind').onclick = e => { const b = e.target.closest('button'); if (!b) return; LD.kind = b.dataset.k; document.querySelectorAll('#ld-kind button').forEach(x => x.classList.toggle('on', x === b)); runLadder(); };
$('#ld-sort').onchange = () => LD.data && drawLadder();
$('#ld-failed').onchange = () => LD.data && drawLadder();
$('#ld-date').onchange = () => runLadder();
$('#ld-prev').onclick = () => LD.data && LD.data.prev && runLadder(LD.data.prev);
$('#ld-next').onclick = () => LD.data && LD.data.next && runLadder(LD.data.next);
$('#ld-themes').onclick = e => { const c = e.target.closest('.ld-chip'); if (!c) return; LD.theme = c.dataset.t || null; drawLadder(); };
function ldFind(code) { return LD.data && (LD.data.rows.find(r => r.code === code) || LD.data.failed.find(r => r.code === code)); }
$('#page-ladder').addEventListener('click', e => {
  const el = e.target.closest('.ld-card, .ld-rank tr[data-code]');
  if (!el) return;
  const r = ldFind(el.dataset.code);
  const extra = r && r.why && r.why.length ? `<div style="margin-bottom:6px">晋级概率 <b>${probTxt(r.prob)}</b>：${whyHtml(r.why)}</div>` : '';
  openModal(el.dataset.code, LD.data.date, el.dataset.name, extra);
});
$('#page-ladder').addEventListener('mousemove', e => {
  const el = e.target.closest('.ld-card'), tip = $('#ld-tip');
  if (!el || LD.kind !== 'zt') { tip.style.display = 'none'; return; }
  const r = ldFind(el.dataset.code);
  if (!r) { tip.style.display = 'none'; return; }
  tip.innerHTML = `<div class="hd">${esc(r.name)} ${r.code} · 晋级概率 ${probTxt(r.prob)}</div>
    ${r.why && r.why.length ? `<table>${r.why.map(([k, v, c]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td><td class="${c >= 0 ? 'pos' : 'neg'}">${c >= 0 ? '▲ 加分' : '▼ 减分'}</td></tr>`).join('')}</table>` : '<div class="muted">没有模型结果（历史样本不足）</div>'}
    <div style="color:#6b7585;margin-top:4px">${r.tags && r.tags.last ? `最后回封 ${r.tags.last} · ` : ''}点击看缠论结构</div>`;
  tip.style.display = 'block';
  const x = Math.min(e.clientX + 14, window.innerWidth - 360), y = Math.min(e.clientY + 14, window.innerHeight - tip.offsetHeight - 10);
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
$('#page-ladder').addEventListener('mouseleave', () => $('#ld-tip').style.display = 'none');
pages.ladder = { show() { if (!LD.data) runLadder(); } };

// ------------------------------------------------------------ 复盘回放
const rpChart = chanChart($('#rp-chart'));
let rpChart30 = null, RP = null, rpTimer = null, rpBusy = false;
let lastBsp = null;

$('#rp-code').addEventListener('input', async e => {
  const q = e.target.value.trim(); if (q.length < 2) return;
  try { const d = await api('/api/search?q=' + encodeURIComponent(q));
    $('#rp-list').innerHTML = d.map(x => `<option value="${x.code}">${esc(x.name)}</option>`).join(''); } catch (_) {}
});
async function resolveCode(v) {
  v = v.trim();
  if (/^(sh|sz)\.\d{6}$/.test(v)) return v;
  const d = await api('/api/search?q=' + encodeURIComponent(v.replace(/^(sh|sz)\.?/i, '')));
  if (!d.length) throw new Error('找不到股票：' + v);
  return d[0].code;
}
async function startReplay() {
  stopPlay();
  try {
    const code = await resolveCode($('#rp-code').value);
    $('#rp-code').value = code;
    $('#rp-log').innerHTML = '<div class="muted">准备数据…</div>';
    RP = await api('/api/replay/start', { code, start: $('#rp-start').value, multi: $('#rp-multi').checked });
    RP.code = code;
    $('#rp-slider').max = RP.dates.length; $('#rp-slider').min = 1;
    $('#rp-log').innerHTML = RP.note ? `<div class="down">${RP.note}</div>` : '';
    lastBsp = null; rpChart.resetZoom();
    $('#rp-chart30').style.display = RP.levels.includes('30m') ? '' : 'none';
    if (RP.levels.includes('30m') && !rpChart30) rpChart30 = chanChart($('#rp-chart30'));
    window.dispatchEvent(new Event('resize'));
    await rpGoto(RP.pos);
  } catch (e) { $('#rp-log').innerHTML = `<div class="down">${e.message}</div>`; }
}
function bspKey(b) { return `${b.time}|${b.buy ? 'B' : 'S'}`; }
function diffBsp(date, bsps) {
  const cur = new Map(bsps.map(b => [bspKey(b), b]));
  const lines = [];
  if (lastBsp) {
    for (const [k, b] of cur) {
      const o = lastBsp.get(k);
      if (!o) lines.push(`<span class="${b.buy ? 'up' : 'down'}">新出现</span> ${bspLabel(b)}（${b.time}${b.sure ? '' : '，未确认'}）`);
      else if (o.types !== b.types) lines.push(`类型变化 ${bspLabel(o)} → ${bspLabel(b)}（${b.time}）`);
      else if (!o.sure && b.sure) lines.push(`已确认 ${bspLabel(b)}（${b.time}）`);
    }
    for (const [k, b] of lastBsp) if (!cur.has(k)) lines.push(`<span class="muted">消失</span> ${bspLabel(b)}（${b.time}）`);
  }
  lastBsp = cur;
  if (lines.length) $('#rp-log').insertAdjacentHTML('afterbegin', `<div><b>${date}</b> ${lines.join('；')}</div>`);
}
async function rpGoto(pos) {
  if (!RP || rpBusy) return;
  pos = Math.max(1, Math.min(RP.dates.length, pos));
  rpBusy = true;
  try {
    const d = await api('/api/replay/goto', { token: RP.token, pos });
    if (pos < RP.pos) lastBsp = null;
    RP.pos = d.pos;
    $('#rp-slider').value = d.pos; $('#rp-date').textContent = `${d.date}（${d.pos}/${RP.dates.length}）`;
    rpChart.render(d.chart.day, { title: `${RP.code} 日线` });
    if (d.chart['30m'] && rpChart30) rpChart30.render(d.chart['30m'], { title: '30分钟' });
    diffBsp(d.date, d.chart.day.bsp || []);
  } catch (e) {
    $('#rp-log').insertAdjacentHTML('afterbegin', `<div class="down">${e.message}</div>`); stopPlay();
  } finally { rpBusy = false; }
}
function stopPlay() { clearInterval(rpTimer); rpTimer = null; $('#rp-play').textContent = '▶ 播放'; }
$('#rp-go').onclick = startReplay;
$('#rp-next').onclick = () => rpGoto(RP.pos + 1);
$('#rp-back').onclick = () => rpGoto(RP.pos - 1);
$('#rp-slider').onchange = e => rpGoto(+e.target.value);
$('#rp-play').onclick = () => {
  if (rpTimer) return stopPlay();
  $('#rp-play').textContent = '❚❚ 暂停';
  rpTimer = setInterval(() => { if (!RP || RP.pos >= RP.dates.length) return stopPlay(); if (!rpBusy) rpGoto(RP.pos + 1); }, +$('#rp-speed').value);
};
$('#rp-speed').onchange = () => { if (rpTimer) { stopPlay(); $('#rp-play').click(); } };
document.addEventListener('keydown', e => {
  if (!$('#page-replay').classList.contains('active') || e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight') rpGoto(RP.pos + 1);
  if (e.key === 'ArrowLeft') rpGoto(RP.pos - 1);
  if (e.key === ' ') { e.preventDefault(); $('#rp-play').click(); }
});

// ------------------------------------------------------------ 历史验证
const vaTypes = chips($('#va-types'), TYPES, ['1', '2', '2s', '3a', '3b']);
const vaBoards = chips($('#va-boards'), BOARDS, ['sh60', 'sz00', 'sz30']);
const vaModes = chips($('#va-modes'), MODES, ['龙头', '补涨', '切换']);
$('#va-source').onchange = () => {
  const emo = $('#va-source').value === 'emotion';
  document.querySelectorAll('.va-chan').forEach(x => x.style.display = emo ? 'none' : '');
  document.querySelectorAll('.va-emo').forEach(x => x.style.display = emo ? '' : 'none');
  $('#va-min').value = emo ? 0 : 60;
};
const statTable = (rows, keyName, H, label = r => r.label || r.key) => `<table class="grid"><thead><tr><th class="l">${keyName}</th><th>样本</th>
  ${H.map(h => `<th>${h}日均值</th><th>${h}日胜率</th>`).join('')}<th>${H[1]}日超额</th><th>${H[H.length - 1]}日超额</th><th>最大回撤</th></tr></thead>
  <tbody>${rows.map(r => `<tr><td class="l">${label(r)}</td><td>${r.n}</td>
  ${H.map(h => `<td>${pct(r['avg' + h])}</td><td>${pct(r['win' + h], 1)}</td>`).join('')}
  <td>${pct(r['excess' + H[1]])}</td><td>${pct(r['excess' + H[H.length - 1]])}</td><td>${pct(r.mdd)}</td></tr>`).join('')}</tbody></table>`;

$('#va-run').onclick = async () => {
  $('#va-result').innerHTML = '<div class="hint">计算中…</div>';
  try {
    const emo = $('#va-source').value === 'emotion';
    const d = await api('/api/validate', { start: $('#va-start').value, end: $('#va-end').value, min_score: +$('#va-min').value,
      top_n: +$('#va-top').value, types: vaTypes(), boards: vaBoards(), entry: $('#va-entry').value,
      source: emo ? 'emotion' : 'chan', gate: $('#va-gate').checked, modes: vaModes(), fit_only: $('#va-fit').checked });
    if (d.empty) { $('#va-result').innerHTML = '<div class="hint">这段时间没有符合条件的信号。</div>'; return; }
    const s = d.summary, H = d.horizons, hm = H[1];
    const card = (h) => `<div class="card"><div class="k">${h}日后（共 ${s.n} 次）</div><div class="v">${pct(s['avg' + h])}</div>
      <div class="s">胜率 ${pct(s['win' + h], 1)} · 中位数 ${pct(s['med' + h])}<br>沪深300 ${pct(s['bench' + h])} · 超额 ${pct(s['excess' + h])}</div></div>`;
    const note = emo ? (d.unbuyable ? `另有 ${d.unbuyable} 次次日一字涨停买不进，未计入。` : '')
      : (d.blocked ? `水温闸门拦掉 ${d.blocked} 次信号。` : '');
    const pickHead = emo ? '<th class="l">日期</th><th class="l">代码</th><th class="l">名称</th><th class="l">模式</th><th class="l">题材</th><th>板</th><th>评分</th><th class="l">阶段</th><th>1日</th><th>3日</th><th>5日</th><th>10日</th><th>5日最大回撤</th>'
      : '<th class="l">发现日</th><th class="l">代码</th><th class="l">名称</th><th class="l">买点</th><th>评分</th><th class="l">阶段</th><th class="l">题材</th><th>5日</th><th>10日</th><th>20日</th><th>10日最大回撤</th><th>10日最大涨幅</th>';
    const pickRow = p => emo ? `<td class="l">${p.date}</td><td class="l mono">${p.code}</td><td class="l">${esc(p.name)}</td><td class="l">${p.mode}</td><td class="l">${esc(p.theme || '')}</td><td>${p.board}</td><td>${num(p.score, 0)}</td><td class="l">${phaseTag(p.phase)}</td><td>${pct(p.r1)}</td><td>${pct(p.r3)}</td><td>${pct(p.r5)}</td><td>${pct(p.r10)}</td><td>${pct(p.mdd5)}</td>`
      : `<td class="l">${p.date}</td><td class="l mono">${p.code}</td><td class="l">${esc(p.name)}</td><td class="l">${typesCN(p.types)}</td><td>${num(p.score, 0)}</td><td class="l">${phaseTag(p.phase)}</td><td class="l">${esc(p.theme || '')}</td><td>${pct(p.r5)}</td><td>${pct(p.r10)}</td><td>${pct(p.r20)}</td><td>${pct(p.mdd10)}</td><td>${pct(p.mfe10)}</td>`;
    $('#va-result').innerHTML = `${note ? `<div class="hint">${note}</div>` : ''}<div class="cards">${H.map(card).join('')}
      <div class="card"><div class="k">平均最大回撤</div><div class="v">${pct(s.mdd)}</div><div class="s">买入后${emo ? 5 : 10}天内最低价相对买入价</div></div></div>
      <div class="section"><h4>按情绪阶段（检验水温：阶段判断有用的话，同样的信号在不同阶段表现应明显不同）</h4>${statTable(d.by_phase, '情绪阶段', H, r => phaseTag(r.key === '无情绪数据' ? null : r.key) + (r.key === '无情绪数据' ? ' 无情绪数据' : ''))}</div>
      <div class="cols"><div class="section"><h4>按评分分档（评分有效的话，高分档收益应更高）</h4>${statTable(d.by_bucket, '评分', H)}</div>
      <div class="section"><h4>${emo ? '按模式' : '按买点类型'}</h4>${statTable(d.by_type, emo ? '模式' : '买点类型', H)}</div></div>
      <div class="section"><h4>每月表现（${hm}日平均收益 vs 沪深300）</h4><div id="va-month" class="chart"></div></div>
      <div class="section"><h4>历史选股明细（最近 ${d.picks.length} 条，点击查看当时的缠论图）</h4><div class="tablewrap" style="max-height:420px">
      <table class="grid" id="va-picks"><thead><tr>${pickHead}</tr></thead>
      <tbody>${d.picks.map(p => `<tr data-code="${p.code}" data-date="${p.date}" data-name="${esc(p.name)}">${pickRow(p)}</tr>`).join('')}</tbody></table></div></div>`;
    const m = echarts.init($('#va-month'), THEME);
    m.setOption({ tooltip: { trigger: 'axis', valueFormatter: v => v == null ? '-' : (v * 100).toFixed(2) + '%' }, legend: { top: 0 },
      grid: { left: 50, right: 20, top: 30, bottom: 30 }, xAxis: { type: 'category', data: d.by_month.map(x => x.key) },
      yAxis: { type: 'value', axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' } },
      series: [{ name: `选股${hm}日平均`, type: 'bar', color: UP, data: d.by_month.map(x => ({ value: x['avg' + hm], itemStyle: { color: x['avg' + hm] >= 0 ? UP : DOWN } })) },
               { name: '沪深300 同期', type: 'line', color: '#6f95ff', data: d.by_month.map(x => x['bench' + hm]), symbol: 'none', lineStyle: { color: '#6f95ff' } }] });
    $('#va-picks').onclick = e => { const tr = e.target.closest('tr[data-code]'); if (tr) openModal(tr.dataset.code, tr.dataset.date, tr.dataset.name); };
  } catch (e) { $('#va-result').innerHTML = `<div class="down">${e.message}</div>`; }
};

// ------------------------------------------------------------ 弹窗：某天的缠论图
let modalChart = null;
async function openModal(code, date, name, extra = '') {
  $('#modal').style.display = 'flex';
  $('#modal-title').textContent = `${name || ''} ${code} · 截至 ${date} 的缠论结构（只用到当天的数据）`;
  if (!modalChart) modalChart = chanChart($('#modal-chart'));
  modalChart.chart.resize();
  try {
    const d = await api(`/api/chart?code=${code}&date=${date}`);
    modalChart.render(d.chart.day, { markDate: date, zoomStart: 40, resetZoom: true });
    const sig = d.signals.filter(s => s.detect_date <= date).slice(0, 8);
    $('#modal-info').innerHTML = extra + '近期信号：' + sig.map(s => `<span class="tag ${s.buy ? '' : 'sell'}">${s.detect_date} ${sigName(s.types, s.buy)}${s.score ? ' ' + num(s.score, 0) + '分' : ''}</span>`).join('');
  } catch (e) { $('#modal-info').textContent = e.message; }
}
$('#modal-close').onclick = () => $('#modal').style.display = 'none';
$('#modal').onclick = e => { if (e.target.id === 'modal') $('#modal').style.display = 'none'; };

// ------------------------------------------------------------ 策略回测
const btTypes = chips($('#bt-types'), TYPES, ['1', '2', '3a', '3b']);
const btBoards = chips($('#bt-boards'), BOARDS, ['sh60', 'sz00', 'sz30']);
const btModes = chips($('#bt-modes'), MODES, ['龙头', '补涨', '切换']);
$('#bt-strategy').onchange = () => {
  const st = $('#bt-strategy').value;
  document.querySelectorAll('.bt-emo').forEach(x => x.style.display = st === 'chan' ? 'none' : '');
  if (st !== 'chan') $('#bt-cap').checked = true;
};
$('#bt-run').onclick = async () => {
  $('#bt-result').innerHTML = '<div class="hint">回测中…</div>';
  try {
    const d = await api('/api/backtest', { start: $('#bt-start').value, end: $('#bt-end').value, min_score: +$('#bt-min').value,
      max_pos: +$('#bt-pos').value, stop_pct: +$('#bt-stop').value / 100, use_bsp_stop: $('#bt-bspstop').checked,
      take_profit: +$('#bt-tp').value / 100, max_hold: +$('#bt-hold').value, exit_on_sell: $('#bt-sell').checked,
      types: btTypes(), boards: btBoards(), strategy: $('#bt-strategy').value, gate: $('#bt-gate').checked,
      phase_cap: $('#bt-cap').checked, modes: btModes(), emo_min_score: +$('#bt-emin').value });
    const s = d.stats;
    const c = (k, v, sub = '') => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
    $('#bt-result').innerHTML = `<div class="cards">
      ${c('总收益', pct(s.total_return), '沪深300 ' + pct(s.bench_return))}${c('年化收益', pct(s.annual_return))}
      ${c('最大回撤', pct(s.max_drawdown))}${c('夏普比率', num(s.sharpe))}
      ${c('交易次数', s.trades, '平均持有 ' + num(s.avg_days, 1) + ' 天')}${c('胜率', pct(s.win_rate, 1), '盈亏比 ' + num(s.profit_factor))}
      ${c('平均每笔', pct(s.avg_ret), '盈利 ' + pct(s.avg_win) + ' / 亏损 ' + pct(s.avg_loss))}${c('持仓时间占比', pct(s.exposure, 0), s.gate_blocked ? `闸门拦掉 ${s.gate_blocked} 次信号` : '')}</div>
      <div class="section"><div id="bt-curve" class="chart tall" style="border:none"></div></div>
      <div class="cols"><div class="section"><h4>按模式</h4><table class="grid"><thead><tr><th class="l">模式</th><th>次数</th><th>平均收益</th><th>胜率</th></tr></thead>
      <tbody>${(s.by_mode || []).map(r => `<tr><td class="l">${r.mode}</td><td>${r.count}</td><td>${pct(r.mean)}</td><td>${pct(r.win, 1)}</td></tr>`).join('')}</tbody></table></div>
      <div class="section"><h4>离场原因</h4><table class="grid"><thead><tr><th class="l">原因</th><th>次数</th><th>平均收益</th></tr></thead>
      <tbody>${(s.by_reason || []).map(r => `<tr><td class="l">${r.reason}</td><td>${r.count}</td><td>${pct(r.mean)}</td></tr>`).join('')}</tbody></table></div>
      </div><div class="section"><h4>期末持仓</h4><table class="grid"><thead><tr><th class="l">代码</th><th class="l">名称</th><th class="l">模式</th><th>买入日</th><th>收益</th></tr></thead>
      <tbody>${d.open_positions.map(p => `<tr><td class="l mono">${p.code}</td><td class="l">${esc(p.name)}</td><td class="l">${p.mode}</td><td>${p.buy_date}</td><td>${pct(p.ret)}</td></tr>`).join('') || '<tr><td colspan="5" class="muted l">无</td></tr>'}</tbody></table></div>
      <div class="section"><h4>交易明细（${d.trades.length} 笔，点击查看买入时的缠论图）</h4><div class="tablewrap" style="max-height:420px"><table class="grid" id="bt-trades">
      <thead><tr><th class="l">代码</th><th class="l">名称</th><th class="l">模式/买点</th><th class="l">题材</th><th>评分</th><th>买入日</th><th>买入价</th><th>卖出日</th><th>卖出价</th><th>持有</th><th class="l">原因</th><th>收益</th></tr></thead>
      <tbody>${d.trades.map(t => `<tr data-code="${t.code}" data-date="${t.buy_date}" data-name="${esc(t.name)}"><td class="l mono">${t.code}</td><td class="l">${esc(t.name)}</td><td class="l">${t.mode === '缠论' ? typesCN(t.types) : t.mode}</td><td class="l">${esc(t.theme || '')}</td><td>${num(t.score, 0)}</td>
      <td>${t.buy_date}</td><td>${num(t.buy_price)}</td><td>${t.sell_date}</td><td>${num(t.sell_price)}</td><td>${t.days}</td><td class="l">${t.reason}</td><td>${pct(t.ret)}</td></tr>`).join('')}</tbody></table></div></div>`;
    const ch = echarts.init($('#bt-curve'), THEME);
    const cv = d.curve;
    ch.setOption({ animation: false, tooltip: { trigger: 'axis' }, legend: { top: 0 },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [{ left: 70, right: 20, top: 30, height: '58%' }, { left: 70, right: 20, top: '74%', height: '18%' }],
      xAxis: [{ type: 'category', data: cv.dates, axisLabel: { show: false } }, { type: 'category', gridIndex: 1, data: cv.dates }],
      yAxis: [{ scale: true, axisLabel: { formatter: v => (v / 10000).toFixed(0) + '万' } },
              { gridIndex: 1, axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' }, max: 0 }],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }, { type: 'slider', xAxisIndex: [0, 1], height: 14, bottom: 0 }],
      series: [{ name: '策略净值', type: 'line', color: UP, data: cv.equity, symbol: 'none', lineStyle: { color: UP, width: 1.6 },
                 markArea: { silent: true, data: phaseBands(cv.dates, cv.phase || []) } },
               { name: '沪深300', type: 'line', color: '#6f95ff', data: cv.bench, symbol: 'none', lineStyle: { color: '#6f95ff', width: 1.2 } },
               { name: '回撤', type: 'line', color: DOWN, xAxisIndex: 1, yAxisIndex: 1, data: cv.drawdown, symbol: 'none', areaStyle: { color: 'rgba(19,160,91,.25)' }, lineStyle: { width: 0 } }] });
    $('#bt-trades').onclick = e => { const tr = e.target.closest('tr[data-code]'); if (tr) openModal(tr.dataset.code, prevDay(tr.dataset.date), tr.dataset.name); };
  } catch (e) { $('#bt-result').innerHTML = `<div class="down">${e.message}</div>`; }
};
function prevDay(d) { const x = new Date(d); x.setDate(x.getDate() - 1); return x.toISOString().slice(0, 10); }

// ------------------------------------------------------------ 数据
let dtTimer = null;
async function refreshData() {
  const s = await refreshStatus(); if (!s) return;
  const m = s.market || {}, b = s.build || {};
  $('#dt-cards').innerHTML = [
    ['最新交易日', s.latest_day || '无'], ['股票数', s.stocks], ['信号条数', s.signals],
    ['信号区间', s.signal_range && s.signal_range[0] ? `${s.signal_range[0]} ~ ${s.signal_range[1]}` : '-'],
    ['行情更新于', m.time || '-'], ['信号计算于', b.time || '-'],
    ['情绪周期计算于', (s.emotion || {}).time || '-'],
    ['题材来源', s.themes ? `${s.themes.source || '-'}（${s.themes.themes || 0} 个）` : '-']
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v" style="font-size:15px">${v}</div></div>`).join('');
  const p = s.progress;
  if (p) {
    const ratio = p.total ? p.done / p.total : (p.running ? 0.02 : 1);
    $('#dt-bar').style.width = (ratio * 100).toFixed(1) + '%';
    $('#dt-ptext').textContent = `${p.running ? '运行中' : (p.ok ? '已完成' : '已结束')} · ${p.stage || ''} ${p.total ? `${p.done}/${p.total}` : ''} · 用时 ${Math.floor((p.elapsed || 0) / 60)} 分 ${(p.elapsed || 0) % 60} 秒 ${p.running ? '' : '· ' + (p.msg || '')}`;
  }
  try { $('#dt-log').textContent = (await api('/api/log')).join('\n'); } catch (_) {}
  $('#dt-update').disabled = $('#dt-signals').disabled = $('#dt-emotion').disabled = $('#dt-themes').disabled = $('#dt-seals').disabled = !!s.running;
  if (s.running && !dtTimer) dtTimer = setInterval(refreshData, 2000);
  if (!s.running && dtTimer) { clearInterval(dtTimer); dtTimer = null; }
}
async function startJob(mode) {
  try { await api('/api/update', { mode }); } catch (e) { alert(e.message); }
  refreshData();
}
$('#dt-update').onclick = () => startJob('update');
$('#dt-signals').onclick = () => startJob('signals');
$('#dt-emotion').onclick = () => startJob('emotion');
$('#dt-themes').onclick = () => startJob('themes');
$('#dt-seals').onclick = () => startJob('seals');
pages.data = { show: refreshData };

// ------------------------------------------------------------ 初始化
(async function init() {
  const s = await refreshStatus();
  const last = (s && s.latest_day) || new Date().toISOString().slice(0, 10);
  const yearsAgo = (n) => { const d = new Date(last); d.setFullYear(d.getFullYear() - n); return d.toISOString().slice(0, 10); };
  $('#sel-date').value = last;
  $('#em-date').value = last;
  $('#va-start').value = yearsAgo(2); $('#va-end').value = last;
  $('#bt-start').value = yearsAgo(3); $('#bt-end').value = last;
  $('#rp-start').value = yearsAgo(1);
  if (s && s.running) setInterval(refreshStatus, 5000);
  if (!s || !s.signals) {
    document.querySelector('#nav a[data-page=data]').click();
    $('#dt-log').textContent = '本地还没有数据。点击“更新行情 + 重算信号”开始首次下载（约 20–40 分钟）。';
  } else runSelect();
})();
