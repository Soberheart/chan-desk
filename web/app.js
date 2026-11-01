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
    min_score: $('#sel-min').value, types: selTypes().join(','), boards: selBoards().join(',') });
  $('#sel-hint').textContent = '计算中…';
  try {
    const d = await api('/api/select?' + q);
    selRows = d.rows;
    if (!$('#sel-date').value) $('#sel-date').value = d.date;
    $('#sel-hint').textContent = `${d.date}（信号窗口 ${d.days ? d.days[0] : ''} ~ ${d.date}）共 ${selRows.length} 只。评分只用发现日及之前的数据。`;
    drawSelTable();
  } catch (e) { $('#sel-hint').textContent = '出错：' + e.message; }
}
function drawSelTable() {
  const cols = [['code', '代码', 'l'], ['name', '名称', 'l'], ['types', '买点', 'l'], ['score', '评分'], ['bsp_date', '买点日'],
    ['detect_date', '发现日'], ['close', '现价'], ['gain', '距买点'], ['c30', '区间套', 'l']];
  const k = selSort.k;
  const rows = [...selRows].sort((a, b) => ((a[k] ?? -1e9) > (b[k] ?? -1e9) ? 1 : -1) * (selSort.asc ? 1 : -1));
  $('#sel-table').innerHTML = `<thead><tr>${cols.map(c => `<th class="${c[2] || ''}" data-k="${c[0]}">${c[1]}${k === c[0] ? (selSort.asc ? ' ▲' : ' ▼') : ''}</th>`).join('')}</tr></thead>
  <tbody>${rows.map(r => `<tr data-code="${r.code}" class="${selCurrent === r.code ? 'sel' : ''}">
    <td class="l mono">${r.code}</td><td class="l">${esc(r.name)}</td><td class="l">${typesCN(r.types)}</td>
    <td><b>${num(r.score, 0)}</b></td><td>${r.bsp_date}</td><td>${r.detect_date}</td><td>${num(r.close)}</td><td>${pct(r.gain, 1)}</td>
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
const statTable = (rows, keyName) => `<table class="grid"><thead><tr><th class="l">${keyName}</th><th>样本</th>
  <th>5日均值</th><th>5日胜率</th><th>10日均值</th><th>10日胜率</th><th>10日超额</th><th>20日均值</th><th>20日胜率</th><th>20日超额</th><th>10日内最大回撤</th></tr></thead>
  <tbody>${rows.map(r => `<tr><td class="l">${keyName === '买点类型' ? (TYPE_CN[r.key] || r.key) : r.key}</td><td>${r.n}</td>
  <td>${pct(r.avg5)}</td><td>${pct(r.win5, 1)}</td><td>${pct(r.avg10)}</td><td>${pct(r.win10, 1)}</td><td>${pct(r.excess10)}</td>
  <td>${pct(r.avg20)}</td><td>${pct(r.win20, 1)}</td><td>${pct(r.excess20)}</td><td>${pct(r.mdd10)}</td></tr>`).join('')}</tbody></table>`;

$('#va-run').onclick = async () => {
  $('#va-result').innerHTML = '<div class="hint">计算中…</div>';
  try {
    const d = await api('/api/validate', { start: $('#va-start').value, end: $('#va-end').value, min_score: +$('#va-min').value,
      top_n: +$('#va-top').value, types: vaTypes(), boards: vaBoards(), entry: $('#va-entry').value });
    if (d.empty) { $('#va-result').innerHTML = '<div class="hint">这段时间没有符合条件的信号。</div>'; return; }
    const s = d.summary;
    const card = (h) => `<div class="card"><div class="k">${h}日后（共 ${s.n} 次）</div><div class="v">${pct(s['avg' + h])}</div>
      <div class="s">胜率 ${pct(s['win' + h], 1)} · 中位数 ${pct(s['med' + h])}<br>沪深300 ${pct(s['bench' + h])} · 超额 ${pct(s['excess' + h])}</div></div>`;
    $('#va-result').innerHTML = `<div class="cards">${card(5)}${card(10)}${card(20)}
      <div class="card"><div class="k">10日内平均最大回撤</div><div class="v">${pct(s.mdd10)}</div><div class="s">买入后10天内最低价相对买入价</div></div></div>
      <div class="cols"><div class="section"><h4>按评分分档（评分有效的话，高分档收益应更高）</h4>${statTable(d.by_bucket, '评分')}</div>
      <div class="section"><h4>按买点类型</h4>${statTable(d.by_type, '买点类型')}</div></div>
      <div class="section"><h4>每月表现（10日平均收益 vs 沪深300）</h4><div id="va-month" class="chart"></div></div>
      <div class="section"><h4>历史选股明细（最近 ${d.picks.length} 条，点击查看当时的缠论图）</h4><div class="tablewrap" style="max-height:420px">
      <table class="grid" id="va-picks"><thead><tr><th class="l">发现日</th><th class="l">代码</th><th class="l">名称</th><th class="l">买点</th><th>评分</th><th>5日</th><th>10日</th><th>20日</th><th>10日最大回撤</th><th>10日最大涨幅</th></tr></thead>
      <tbody>${d.picks.map(p => `<tr data-code="${p.code}" data-date="${p.date}" data-name="${esc(p.name)}"><td class="l">${p.date}</td><td class="l mono">${p.code}</td><td class="l">${esc(p.name)}</td><td class="l">${typesCN(p.types)}</td>
      <td>${num(p.score, 0)}</td><td>${pct(p.r5)}</td><td>${pct(p.r10)}</td><td>${pct(p.r20)}</td><td>${pct(p.mdd10)}</td><td>${pct(p.mfe10)}</td></tr>`).join('')}</tbody></table></div></div>`;
    const m = echarts.init($('#va-month'));
    m.setOption({ tooltip: { trigger: 'axis', valueFormatter: v => v == null ? '-' : (v * 100).toFixed(2) + '%' }, legend: { top: 0 },
      grid: { left: 50, right: 20, top: 30, bottom: 30 }, xAxis: { type: 'category', data: d.by_month.map(x => x.key) },
      yAxis: { type: 'value', axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' } },
      series: [{ name: '选股10日平均', type: 'bar', color: UP, data: d.by_month.map(x => ({ value: x.avg10, itemStyle: { color: x.avg10 >= 0 ? UP : DOWN } })) },
               { name: '沪深300 同期', type: 'line', color: '#2f5bea', data: d.by_month.map(x => x.bench10), symbol: 'none', lineStyle: { color: '#2f5bea' } }] });
    $('#va-picks').onclick = e => { const tr = e.target.closest('tr[data-code]'); if (tr) openModal(tr.dataset.code, tr.dataset.date, tr.dataset.name); };
  } catch (e) { $('#va-result').innerHTML = `<div class="down">${e.message}</div>`; }
};

// ------------------------------------------------------------ 弹窗：某天的缠论图
let modalChart = null;
async function openModal(code, date, name) {
  $('#modal').style.display = 'flex';
  $('#modal-title').textContent = `${name || ''} ${code} · 截至 ${date} 的缠论结构（只用到当天的数据）`;
  if (!modalChart) modalChart = chanChart($('#modal-chart'));
  modalChart.chart.resize();
  try {
    const d = await api(`/api/chart?code=${code}&date=${date}`);
    modalChart.render(d.chart.day, { markDate: date, zoomStart: 40, resetZoom: true });
    const sig = d.signals.filter(s => s.detect_date <= date).slice(0, 8);
    $('#modal-info').innerHTML = '近期信号：' + sig.map(s => `<span class="tag ${s.buy ? '' : 'sell'}">${s.detect_date} ${sigName(s.types, s.buy)}${s.score ? ' ' + num(s.score, 0) + '分' : ''}</span>`).join('');
  } catch (e) { $('#modal-info').textContent = e.message; }
}
$('#modal-close').onclick = () => $('#modal').style.display = 'none';
$('#modal').onclick = e => { if (e.target.id === 'modal') $('#modal').style.display = 'none'; };

// ------------------------------------------------------------ 策略回测
const btTypes = chips($('#bt-types'), TYPES, ['1', '2', '3a', '3b']);
const btBoards = chips($('#bt-boards'), BOARDS, ['sh60', 'sz00', 'sz30']);
$('#bt-run').onclick = async () => {
  $('#bt-result').innerHTML = '<div class="hint">回测中…</div>';
  try {
    const d = await api('/api/backtest', { start: $('#bt-start').value, end: $('#bt-end').value, min_score: +$('#bt-min').value,
      max_pos: +$('#bt-pos').value, stop_pct: +$('#bt-stop').value / 100, use_bsp_stop: $('#bt-bspstop').checked,
      take_profit: +$('#bt-tp').value / 100, max_hold: +$('#bt-hold').value, exit_on_sell: $('#bt-sell').checked,
      types: btTypes(), boards: btBoards() });
    const s = d.stats;
    const c = (k, v, sub = '') => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
    $('#bt-result').innerHTML = `<div class="cards">
      ${c('总收益', pct(s.total_return), '沪深300 ' + pct(s.bench_return))}${c('年化收益', pct(s.annual_return))}
      ${c('最大回撤', pct(s.max_drawdown))}${c('夏普比率', num(s.sharpe))}
      ${c('交易次数', s.trades, '平均持有 ' + num(s.avg_days, 1) + ' 天')}${c('胜率', pct(s.win_rate, 1), '盈亏比 ' + num(s.profit_factor))}
      ${c('平均每笔', pct(s.avg_ret), '盈利 ' + pct(s.avg_win) + ' / 亏损 ' + pct(s.avg_loss))}${c('持仓时间占比', pct(s.exposure, 0))}</div>
      <div class="section"><div id="bt-curve" class="chart tall" style="border:none"></div></div>
      <div class="cols"><div class="section"><h4>离场原因</h4><table class="grid"><thead><tr><th class="l">原因</th><th>次数</th><th>平均收益</th></tr></thead>
      <tbody>${(s.by_reason || []).map(r => `<tr><td class="l">${r.reason}</td><td>${r.count}</td><td>${pct(r.mean)}</td></tr>`).join('')}</tbody></table></div>
      <div class="section"><h4>期末持仓</h4><table class="grid"><thead><tr><th class="l">代码</th><th class="l">名称</th><th>买入日</th><th>收益</th></tr></thead>
      <tbody>${d.open_positions.map(p => `<tr><td class="l mono">${p.code}</td><td class="l">${esc(p.name)}</td><td>${p.buy_date}</td><td>${pct(p.ret)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted l">无</td></tr>'}</tbody></table></div></div>
      <div class="section"><h4>交易明细（${d.trades.length} 笔，点击查看买入时的缠论图）</h4><div class="tablewrap" style="max-height:420px"><table class="grid" id="bt-trades">
      <thead><tr><th class="l">代码</th><th class="l">名称</th><th class="l">买点</th><th>评分</th><th>买入日</th><th>买入价</th><th>卖出日</th><th>卖出价</th><th>持有</th><th class="l">原因</th><th>收益</th></tr></thead>
      <tbody>${d.trades.map(t => `<tr data-code="${t.code}" data-date="${t.buy_date}" data-name="${esc(t.name)}"><td class="l mono">${t.code}</td><td class="l">${esc(t.name)}</td><td class="l">${typesCN(t.types)}</td><td>${num(t.score, 0)}</td>
      <td>${t.buy_date}</td><td>${num(t.buy_price)}</td><td>${t.sell_date}</td><td>${num(t.sell_price)}</td><td>${t.days}</td><td class="l">${t.reason}</td><td>${pct(t.ret)}</td></tr>`).join('')}</tbody></table></div></div>`;
    const ch = echarts.init($('#bt-curve'));
    const cv = d.curve;
    ch.setOption({ animation: false, tooltip: { trigger: 'axis' }, legend: { top: 0 },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [{ left: 70, right: 20, top: 30, height: '58%' }, { left: 70, right: 20, top: '74%', height: '18%' }],
      xAxis: [{ type: 'category', data: cv.dates, axisLabel: { show: false } }, { type: 'category', gridIndex: 1, data: cv.dates }],
      yAxis: [{ scale: true, axisLabel: { formatter: v => (v / 10000).toFixed(0) + '万' } },
              { gridIndex: 1, axisLabel: { formatter: v => (v * 100).toFixed(0) + '%' }, max: 0 }],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }, { type: 'slider', xAxisIndex: [0, 1], height: 14, bottom: 0 }],
      series: [{ name: '策略净值', type: 'line', color: UP, data: cv.equity, symbol: 'none', lineStyle: { color: UP, width: 1.6 } },
               { name: '沪深300', type: 'line', color: '#2f5bea', data: cv.bench, symbol: 'none', lineStyle: { color: '#2f5bea', width: 1.2 } },
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
    ['行情更新于', m.time || '-'], ['信号计算于', b.time || '-']
  ].map(([k, v]) => `<div class="card"><div class="k">${k}</div><div class="v" style="font-size:15px">${v}</div></div>`).join('');
  const p = s.progress;
  if (p) {
    const ratio = p.total ? p.done / p.total : (p.running ? 0.02 : 1);
    $('#dt-bar').style.width = (ratio * 100).toFixed(1) + '%';
    $('#dt-ptext').textContent = `${p.running ? '运行中' : (p.ok ? '已完成' : '已结束')} · ${p.stage || ''} ${p.total ? `${p.done}/${p.total}` : ''} · 用时 ${Math.floor((p.elapsed || 0) / 60)} 分 ${(p.elapsed || 0) % 60} 秒 ${p.running ? '' : '· ' + (p.msg || '')}`;
  }
  try { $('#dt-log').textContent = (await api('/api/log')).join('\n'); } catch (_) {}
  $('#dt-update').disabled = $('#dt-signals').disabled = !!s.running;
  if (s.running && !dtTimer) dtTimer = setInterval(refreshData, 2000);
  if (!s.running && dtTimer) { clearInterval(dtTimer); dtTimer = null; }
}
async function startJob(mode) {
  try { await api('/api/update', { mode }); } catch (e) { alert(e.message); }
  refreshData();
}
$('#dt-update').onclick = () => startJob('update');
$('#dt-signals').onclick = () => startJob('signals');
pages.data = { show: refreshData };

// ------------------------------------------------------------ 初始化
(async function init() {
  const s = await refreshStatus();
  const last = (s && s.latest_day) || new Date().toISOString().slice(0, 10);
  const yearsAgo = (n) => { const d = new Date(last); d.setFullYear(d.getFullYear() - n); return d.toISOString().slice(0, 10); };
  $('#sel-date').value = last;
  $('#va-start').value = yearsAgo(2); $('#va-end').value = last;
  $('#bt-start').value = yearsAgo(3); $('#bt-end').value = last;
  $('#rp-start').value = yearsAgo(1);
  if (s && s.running) setInterval(refreshStatus, 5000);
  if (!s || !s.signals) {
    document.querySelector('#nav a[data-page=data]').click();
    $('#dt-log').textContent = '本地还没有数据。点击“更新行情 + 重算信号”开始首次下载（约 20–40 分钟）。';
  } else runSelect();
})();
