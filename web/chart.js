/* 缠论图表：K线 + 笔 + 线段 + 中枢 + 买卖点 + MACD（ECharts） */
const UP = '#d93026', DOWN = '#13a05b';

function bspLabel(b, isSeg) {
  const t = b.types.split(',').join('/');
  return (isSeg ? '段' : '') + t + (b.buy ? '买' : '卖');
}

function chanChart(el) {
  const chart = echarts.init(el, null, { renderer: 'canvas' });
  window.addEventListener('resize', () => chart.resize());
  let keepZoom = null;
  chart.on('datazoom', () => {
    const o = chart.getOption();
    if (o.dataZoom && o.dataZoom[0]) keepZoom = { start: o.dataZoom[0].start, end: o.dataZoom[0].end };
  });

  function render(d, opts = {}) {
    if (!d || !d.bars || !d.bars.length) { chart.clear(); return; }
    const dates = d.bars.map(b => b[0]);
    const candle = d.bars.map(b => [b[1], b[2], b[3], b[4]]);
    const dx = i => dates[Math.max(0, Math.min(dates.length - 1, i))];
    const mkLines = (arr, color, width) => arr.map(s => ({
      coords: [[dx(s.x1), s.y1], [dx(s.x2), s.y2]],
      lineStyle: { color, width, type: s.sure ? 'solid' : 'dashed', opacity: s.sure ? 1 : .75 }
    }));
    const zsAreas = (d.zs || []).map(z => [
      { xAxis: dx(z.x1), yAxis: z.low,
        itemStyle: { color: z.sure ? 'rgba(47,91,234,0.08)' : 'rgba(199,119,0,0.07)',
                     borderColor: z.sure ? '#2f5bea' : '#c77700', borderWidth: 1, borderType: z.sure ? 'solid' : 'dashed' } },
      { xAxis: dx(z.x2), yAxis: z.high }
    ]);
    const lo = Math.min(...d.bars.map(b => b[3])), hi = Math.max(...d.bars.map(b => b[4]));
    const pad = (hi - lo) * 0.04;
    const bspPts = (arr, isSeg) => (arr || []).map(b => ({
      value: [dx(b.x), b.buy ? b.price - pad * (isSeg ? 2.2 : 0.9) : b.price + pad * (isSeg ? 2.2 : 0.9)],
      symbol: isSeg ? 'diamond' : 'triangle', symbolRotate: b.buy ? 0 : 180,
      symbolSize: isSeg ? 12 : 10,
      itemStyle: { color: b.sure ? (b.buy ? UP : DOWN) : '#fff', borderColor: b.buy ? UP : DOWN, borderWidth: 1.5 },
      label: { show: true, formatter: bspLabel(b, isSeg), position: b.buy ? 'bottom' : 'top',
               color: b.buy ? UP : DOWN, fontSize: 11, fontWeight: isSeg ? 'bold' : 'normal' },
      info: b
    }));
    const macd = d.bars.map(b => b[7]);
    const markLine = [];
    if (opts.markDate && dates.includes(opts.markDate)) markLine.push({ xAxis: opts.markDate });
    const zoom = opts.resetZoom || !keepZoom ? { start: opts.zoomStart ?? 0, end: 100 } : keepZoom;
    const option = {
      animation: false,
      title: opts.title ? { text: opts.title, left: 8, top: 4, textStyle: { fontSize: 13, fontWeight: 600 } } : undefined,
      legend: { top: 4, right: 60, data: ['笔', '线段', '买卖点', '段买卖点'], textStyle: { fontSize: 11 } },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, confine: true,
        formatter: ps => {
          const p = ps.find(x => x.seriesType === 'candlestick'); if (!p) return '';
          const b = d.bars[p.dataIndex];
          const chg = p.dataIndex > 0 ? (b[2] / d.bars[p.dataIndex - 1][2] - 1) * 100 : 0;
          let s = `<b>${b[0]}</b><br>开 ${b[1]}  收 ${b[2]}<br>低 ${b[3]}  高 ${b[4]}<br>涨跌 <span style="color:${chg >= 0 ? UP : DOWN}">${chg.toFixed(2)}%</span>`;
          ps.filter(x => x.seriesName.includes('买卖点') && x.data && x.data.info).forEach(x => {
            s += `<br>${bspLabel(x.data.info, x.seriesName === '段买卖点')} ${x.data.info.sure ? '（已确认）' : '（未确认）'}`;
          });
          return s;
        } },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      grid: [{ left: 56, right: 16, top: 30, height: opts.noMacd ? '78%' : '62%' },
             { left: 56, right: 16, top: '76%', height: '14%' }],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#c8cdd6' } }, axisLabel: { show: !!opts.noMacd } },
        { type: 'category', gridIndex: 1, data: dates, axisLabel: { fontSize: 11 }, show: !opts.noMacd }
      ],
      yAxis: [
        { scale: true, splitLine: { lineStyle: { color: '#f0f1f4' } }, axisLabel: { fontSize: 11 } },
        { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { fontSize: 10 }, splitLine: { show: false }, show: !opts.noMacd }
      ],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], ...zoom },
                 { type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 16, ...zoom }],
      series: [
        { name: 'K线', type: 'candlestick', data: candle,
          itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
          markArea: { silent: true, data: zsAreas },
          markLine: markLine.length ? { symbol: 'none', silent: true, data: markLine,
            lineStyle: { color: '#7c3aed', type: 'dashed' }, label: { formatter: opts.markLabel || '发现日', position: 'insideEndTop' } } : undefined },
        { name: '笔', type: 'lines', color: '#3b4a6b', coordinateSystem: 'cartesian2d', data: mkLines(d.bi || [], '#3b4a6b', 1.3), z: 3 },
        { name: '线段', type: 'lines', color: '#f08c00', coordinateSystem: 'cartesian2d', data: mkLines(d.seg || [], '#f08c00', 2.6), z: 4 },
        { name: '买卖点', type: 'scatter', color: UP, data: bspPts(d.bsp, false), z: 5 },
        { name: '段买卖点', type: 'scatter', color: '#7c3aed', data: bspPts(d.seg_bsp, true), z: 6 },
        { name: 'MACD', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: macd.map(v => ({ value: v, itemStyle: { color: v >= 0 ? UP : DOWN } })) },
        { name: 'DIF', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: d.bars.map(b => b[5]), symbol: 'none', lineStyle: { width: 1, color: '#2f5bea' } },
        { name: 'DEA', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: d.bars.map(b => b[6]), symbol: 'none', lineStyle: { width: 1, color: '#c77700' } }
      ]
    };
    chart.setOption(option, { notMerge: true });
  }
  return { chart, render, resetZoom() { keepZoom = null; } };
}
