// Tiny SVG charts: a card sparkline and the item price-history chart.

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const DAY = 86400000;
const t = (iso) => Date.parse(`${iso}T00:00:00Z`);

export function sparkline(history, { width = 120, height = 36, days = 90 } = {}) {
  if (!history || history.length < 2) return '';
  const cutoff = t(history[history.length - 1][0]) - days * DAY;
  const pts = history.filter(([d]) => t(d) >= cutoff);
  if (pts.length < 2) return '';
  const ys = pts.map((p) => p[1]);
  const lo = Math.min(...ys);
  const hi = Math.max(...ys);
  const span = hi - lo || 1;
  const x0 = t(pts[0][0]);
  const xs = t(pts[pts.length - 1][0]) - x0 || 1;
  const coords = pts.map(([d, v]) => [((t(d) - x0) / xs) * (width - 4) + 2, height - 3 - ((v - lo) / span) * (height - 6)]);
  const line = coords.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join('');
  const [lx, ly] = coords[coords.length - 1];
  return `<svg class="spark" viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" aria-hidden="true">
    <path d="${line}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="2.6" fill="currentColor"/></svg>`;
}

/**
 * Price history chart. Returns {html, attach(el)}; attach() wires up the
 * touch/mouse crosshair after the HTML is in the DOM.
 */
export function historyChart(item, fmt, { days = 365 } = {}) {
  const all = item.history || [];
  if (all.length < 2) return { html: '<p class="muted small">Not enough history for a chart yet.</p>', attach() {} };
  const end = t(all[all.length - 1][0]);
  const pts = all.filter(([d]) => t(d) >= end - days * DAY);
  const W = 640;
  const H = 240;
  const pad = { l: 8, r: 8, t: 14, b: 22 };
  const ys = pts.map((p) => p[1]);
  const refs = [];
  if (item.target_price) refs.push({ v: item.target_price, cls: 'target', label: 'target' });
  const wait = item.advice && item.advice.wait;
  if (wait && wait.buy_below) refs.push({ v: wait.buy_below, cls: 'trigger', label: 'buy below' });
  let lo = Math.min(...ys, ...refs.map((r) => r.v));
  let hi = Math.max(...ys, ...refs.map((r) => r.v));
  const margin = (hi - lo) * 0.08 || hi * 0.05 || 1;
  lo -= margin;
  hi += margin;
  const x0 = t(pts[0][0]);
  const xspan = end - x0 || 1;
  const X = (ms) => pad.l + ((ms - x0) / xspan) * (W - pad.l - pad.r);
  const Y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  const bands = (item.event_windows || [])
    .filter((w) => t(w.end) >= x0 && t(w.start) <= end)
    .map((w) => {
      const a = X(Math.max(t(w.start), x0));
      const b = X(Math.min(t(w.end) + DAY, end));
      return `<rect class="band" x="${a.toFixed(1)}" y="${pad.t}" width="${Math.max(2, b - a).toFixed(1)}" height="${H - pad.t - pad.b}"><title>${esc(w.name)}</title></rect>`;
    }).join('');

  const line = pts.map(([d, v], i) => `${i ? 'L' : 'M'}${X(t(d)).toFixed(1)} ${Y(v).toFixed(1)}`).join('');
  const area = `${line}L${X(end).toFixed(1)} ${H - pad.b}L${X(x0).toFixed(1)} ${H - pad.b}Z`;
  const refLines = refs.map((r) => `<line class="ref ${r.cls}" x1="${pad.l}" x2="${W - pad.r}" y1="${Y(r.v).toFixed(1)}" y2="${Y(r.v).toFixed(1)}"/>
      <text class="ref-label ${r.cls}" x="${W - pad.r - 4}" y="${(Y(r.v) - 4).toFixed(1)}" text-anchor="end">${esc(r.label)} ${esc(fmt(r.v))}</text>`).join('');

  const month = (ms) => new Date(ms).toLocaleDateString(undefined, { month: 'short', year: '2-digit', timeZone: 'UTC' });
  const ticks = [];
  for (let i = 0; i <= 4; i += 1) {
    const ms = x0 + (xspan * i) / 4;
    ticks.push(`<text class="tick" x="${X(ms).toFixed(1)}" y="${H - 6}" text-anchor="${i === 0 ? 'start' : i === 4 ? 'end' : 'middle'}">${esc(month(ms))}</text>`);
  }
  const minV = Math.min(...ys);
  const maxV = Math.max(...ys);

  const html = `<div class="chart" data-chart>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Price history">
      ${bands}
      <path class="area" d="${area}"/>
      <path class="line" d="${line}"/>
      ${refLines}
      ${ticks.join('')}
      <line class="cursor" x1="0" x2="0" y1="${pad.t}" y2="${H - pad.b}" visibility="hidden"/>
      <circle class="dot" r="4" visibility="hidden"/>
    </svg>
    <div class="chart-tip" hidden></div>
    <div class="chart-legend small muted">
      <span>low ${esc(fmt(minV))}</span><span>high ${esc(fmt(maxV))}</span>
      ${bands ? '<span><i class="swatch band-swatch"></i>sales days</span>' : ''}
    </div>
  </div>`;

  function attach(root) {
    const box = root.querySelector('[data-chart]');
    if (!box) return;
    const svg = box.querySelector('svg');
    const cursor = svg.querySelector('.cursor');
    const dot = svg.querySelector('.dot');
    const tip = box.querySelector('.chart-tip');
    const coords = pts.map(([d, v]) => ({ x: X(t(d)), y: Y(v), d, v }));
    const move = (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * W;
      let best = coords[0];
      for (const c of coords) if (Math.abs(c.x - px) < Math.abs(best.x - px)) best = c;
      cursor.setAttribute('x1', best.x);
      cursor.setAttribute('x2', best.x);
      dot.setAttribute('cx', best.x);
      dot.setAttribute('cy', best.y);
      cursor.setAttribute('visibility', 'visible');
      dot.setAttribute('visibility', 'visible');
      const date = new Date(t(best.d)).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
      tip.textContent = `${date} · ${fmt(best.v)}`;
      tip.hidden = false;
      tip.style.left = `${Math.min(Math.max((best.x / W) * 100, 12), 88)}%`;
    };
    const leave = () => {
      cursor.setAttribute('visibility', 'hidden');
      dot.setAttribute('visibility', 'hidden');
      tip.hidden = true;
    };
    svg.addEventListener('pointermove', move);
    svg.addEventListener('pointerdown', move);
    svg.addEventListener('pointerleave', leave);
  }
  return { html, attach };
}
