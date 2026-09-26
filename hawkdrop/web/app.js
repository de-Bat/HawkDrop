// HawkDrop web client. Plain ES modules, no build step.
import { historyChart, sparkline } from './charts.js';
import {
  clearLocalData, describeOp, discardFailed, discardPending, init, mutate, on, online, setToken, state, sync, uid,
} from './store.js';

// ---- tiny templating (auto-escaping) -------------------------------------------------

class Raw { constructor(s) { this.s = s; } }
const raw = (s) => new Raw(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const str = (v) => (v == null || v === false ? '' : v instanceof Raw ? v.s : Array.isArray(v) ? v.map(str).join('') : esc(v));
const html = (strings, ...vals) => raw(strings.reduce((out, s, i) => out + str(vals[i - 1]) + s));

// ---- formatting -------------------------------------------------------------------------

const currency = () => (state.snapshot && state.snapshot.meta && state.snapshot.meta.destination.currency) || 'ILS';
function money(v, cur = currency()) {
  if (v == null || Number.isNaN(v)) return '–';
  const digits = Math.abs(v) < 100 && v % 1 !== 0 ? 2 : 0;
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: cur, maximumFractionDigits: digits, minimumFractionDigits: digits }).format(v);
  } catch {
    return `${v.toFixed(digits)} ${cur}`;
  }
}
const pct = (v) => `${Math.round(v * 100)}%`;
const day = (iso) => new Date(`${iso.slice(0, 10)}T00:00:00Z`);
const fmtDate = (iso, opts = { day: 'numeric', month: 'short' }) => day(iso).toLocaleDateString(undefined, { ...opts, timeZone: 'UTC' });
const today = () => new Date().toISOString().slice(0, 10);
const daysUntil = (iso) => Math.round((day(iso) - day(today())) / 86400000);
function ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, (Date.now() - (typeof ts === 'number' ? ts : Date.parse(ts))) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
const FLAGS = { IL: '🇮🇱', US: '🇺🇸', UK: '🇬🇧', DE: '🇩🇪', CN: '🇨🇳', FR: '🇫🇷', JP: '🇯🇵', EU: '🇪🇺' };
const flag = (c) => FLAGS[c] || '🌐';

// ---- icons ---------------------------------------------------------------------------------

const ICONS = {
  list: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  gear: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2.8v2.4M12 18.8v2.4M4.2 7.5l2 1.2M17.8 15.3l2 1.2M4.2 16.5l2-1.2M17.8 8.7l2-1.2"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>',
  share: '<path d="M12 3v12M7.5 7.5 12 3l4.5 4.5M5 12v7a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 19v-7"/>',
  tag: '<path d="M3 12V4.5A1.5 1.5 0 0 1 4.5 3H12l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
  store: '<path d="M4 9.5 5.5 4h13L20 9.5M4 9.5h16v10.5H4zM9 20v-5h6v5"/>',
  edit: '<path d="M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
};
const icon = (name, cls = 'icon') => raw(`<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`);

// ---- toast / sheet -----------------------------------------------------------------------

function toast(message, { action, onAction, timeout = 4000 } = {}) {
  const box = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast';
  el.innerHTML = str(html`<span>${message}</span>${action ? html`<button type="button">${action}</button>` : ''}`);
  if (action) el.querySelector('button').onclick = () => { el.remove(); onAction(); };
  box.appendChild(el);
  if (timeout) setTimeout(() => el.remove(), timeout);
}

function openSheet(title, body, onSubmit, { submitLabel = 'Save', extra } = {}) {
  const sheet = document.getElementById('sheet');
  sheet.innerHTML = str(html`
    <div class="sheet-backdrop" data-close></div>
    <form class="sheet-panel" novalidate>
      <header class="sheet-head"><h2>${title}</h2><button type="button" class="icon-btn" data-close aria-label="Close">${icon('close')}</button></header>
      <div class="sheet-body">${body}</div>
      <p class="form-error" hidden></p>
      <footer class="sheet-foot">${extra || ''}<button class="btn primary" type="submit">${submitLabel}</button></footer>
    </form>`);
  sheet.hidden = false;
  document.body.classList.add('sheet-open');
  const form = sheet.querySelector('form');
  const close = () => { sheet.hidden = true; sheet.innerHTML = ''; document.body.classList.remove('sheet-open'); };
  sheet.querySelectorAll('[data-close]').forEach((el) => { el.onclick = close; });
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const err = form.querySelector('.form-error');
    try {
      await onSubmit(Object.fromEntries(new FormData(form)), form);
      close();
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  };
  setTimeout(() => { const first = form.querySelector('input:not([type=hidden]),select'); if (first && !('ontouchstart' in window)) first.focus(); }, 50);
  return { form, close };
}

const numOrNull = (v) => {
  if (v == null || String(v).trim() === '') return null;
  const n = Number(String(v).replace(/,/g, ''));
  if (!Number.isFinite(n) || n < 0) throw new Error('Please enter a valid number');
  return n;
};

// ---- shared bits ---------------------------------------------------------------------------

function statusPill() {
  const pending = state.outbox.length;
  let cls = 'ok';
  let text = `Synced ${ago(state.lastSync)}`;
  if (state.syncing) { cls = 'busy'; text = 'Syncing…'; }
  else if (!state.online) { cls = 'off'; text = pending ? `Offline · ${pending} pending` : 'Offline'; }
  else if (state.authError) { cls = 'warn'; text = 'Access token needed'; }
  else if (state.serverReachable === false) { cls = 'off'; text = pending ? `Server unreachable · ${pending} pending` : 'Server unreachable'; }
  else if (state.lastError) { cls = 'warn'; text = 'Sync problem'; }
  else if (pending) { cls = 'busy'; text = `${pending} pending`; }
  else if (!state.lastSync) { cls = 'off'; text = 'Not synced yet'; }
  return html`<button class="pill ${cls}" type="button" data-action="sync" title="Sync now"><i></i>${text}</button>`;
}

function verdict(adv, { compact = false } = {}) {
  if (!adv || adv.action === 'PENDING') return html`<span class="badge muted">Waiting for sync</span>`;
  if (adv.action === 'NO_DATA') return html`<span class="badge muted">No prices yet</span>`;
  if (adv.action === 'BUY_NOW') return html`<span class="badge buy">Buy now · ${pct(adv.confidence)}</span>`;
  const w = adv.wait;
  return html`<span class="badge wait">Wait${compact ? '' : ` for ${w.label}`} · ${fmtDate(w.date)} · ${pct(adv.confidence)}</span>`;
}

const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const isStandalone = () => window.navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
function installHint() {
  let dismissed = false;
  try { dismissed = localStorage.getItem('hawkdrop.installHint') === 'no'; } catch { /* ignore */ }
  if (!isIOS() || isStandalone() || dismissed) return '';
  return html`<div class="card hint" data-hint>
    <div>${icon('share')}</div>
    <div><strong>Install HawkDrop</strong><br><span class="small">Tap <b>Share</b> then <b>Add to Home Screen</b> to use it as an app, even offline.</span></div>
    <button class="icon-btn" type="button" data-action="dismiss-hint" aria-label="Dismiss">${icon('close')}</button>
  </div>`;
}

// ---- views ---------------------------------------------------------------------------------------

function viewList() {
  const snap = state.snapshot;
  const items = snap.items;
  if (!items.length) {
    const neverSynced = !state.base;
    return html`${installHint()}
      <section class="empty">
        <div class="empty-art">${icon('tag', 'icon big')}</div>
        <h2>${neverSynced && !state.serverReachable ? 'Connect once to get started' : 'Track your first item'}</h2>
        <p class="muted">${neverSynced && state.serverReachable === false
          ? 'HawkDrop could not reach its server yet. Once it syncs, everything keeps working offline.'
          : 'Add a product and a few store links. HawkDrop compares delivered prices, including shipping and Israeli import tax, and tells you whether to buy now or wait for a sale.'}</p>
        <div class="row gap center">
          <a class="btn primary" href="#/add">${icon('plus')} Track an item</a>
          <button class="btn" type="button" data-action="demo">Load demo</button>
        </div>
      </section>`;
  }
  const cards = items.map((it) => {
    const best = it.best;
    const pendingN = (it.pending_prices || []).length;
    return html`<a class="card item-card" href="#/item/${it.id}">
      <div class="item-main">
        <div class="item-title">${it.name}</div>
        <div class="small muted">${it.category !== 'default' ? it.category : ''}${best ? html` · best at ${best.store}` : ''}${pendingN ? html` · <span class="pending-dot">${pendingN} pending</span>` : ''}</div>
        <div class="item-verdict">${verdict(it.advice, { compact: true })}</div>
      </div>
      <div class="item-side">
        <div class="price">${best ? money(best.landed.total) : '–'}</div>
        <div class="spark-wrap">${raw(sparkline(it.history))}</div>
      </div>
    </a>`;
  });
  const asOf = snap.generated_at ? html`<p class="small muted center">Prices as of ${ago(snap.generated_at)}</p>` : '';
  return html`${installHint()}<section class="stack">${cards}</section>${asOf}`;
}

function adviceHero(it) {
  const a = it.advice;
  const pendingN = (it.pending_prices || []).length;
  const pendingNote = pendingN ? html`<p class="note">${pendingN} new price${pendingN > 1 ? 's' : ''} will be included in the advice after the next sync.</p>` : '';
  if (!a || a.action === 'PENDING') {
    return html`<section class="card hero muted-hero"><h2>Advice pending</h2><p class="muted">This item has not reached the server yet. Advice appears after the next sync.</p>${pendingNote}</section>`;
  }
  if (a.action === 'NO_DATA') {
    return html`<section class="card hero muted-hero"><h2>No prices yet</h2><p class="muted">Log a price, or tap <b>Check prices</b> to fetch them from the store pages.</p>${pendingNote}</section>`;
  }
  const buy = a.action === 'BUY_NOW';
  const w = a.wait;
  const conf = html`<div class="conf"><div class="conf-bar"><i style="width:${Math.round(a.confidence * 100)}%"></i></div>
      <span>${pct(a.confidence)} confidence (${a.confidence_label})</span></div>`;
  const body = buy
    ? html`<div class="hero-price">${money(a.current)}</div>
           <div class="muted small">best delivered price now${it.best ? html` at ${it.best.store}` : ''}</div>`
    : html`<div class="hero-price">~${money(a.expected)}</div>
           <div class="muted small">expected if you wait (80% range ${money(a.low)} – ${money(a.high)}) · now ${money(a.current)}</div>
           <div class="trigger">Buy as soon as the delivered price drops below <b>${money(w.buy_below)}</b></div>`;
  return html`<section class="card hero ${buy ? 'buy' : 'wait'}">
    <div class="hero-label">${buy ? 'Buy now' : html`Wait for ${w.label}`}</div>
    ${buy ? '' : html`<div class="small">${w.estimated ? 'around ' : ''}${fmtDate(w.date, { weekday: 'short', day: 'numeric', month: 'short' })} · in ${w.days} days</div>`}
    ${body}${conf}
    <details class="why"><summary>Why?</summary><ul>${a.reasons.map((r) => html`<li>${r}</li>`)}</ul></details>
    ${pendingNote}
  </section>`;
}

function salesAhead(it) {
  const cands = (it.advice && it.advice.candidates) || [];
  if (!cands.length) return '';
  const rows = cands.map((c) => html`<li class="sale-row ${it.advice.wait && it.advice.wait.label === c.label ? 'chosen' : ''}">
    <div><div class="sale-name">${c.label}</div>
      <div class="small muted">${c.estimated ? '~' : ''}${fmtDate(c.date)} · in ${c.days} d${c.participation != null ? html` · ${pct(c.participation)} likely on sale, ~${pct(c.depth)} off` : ''}${c.observed ? html` · dropped ${c.hits}/${c.observed} times before` : ''}</div></div>
    <div class="sale-nums"><div><b>${pct(c.p_hit_by_then)}</b><span class="small muted"> chance</span></div>
      <div class="small muted">buy &lt; ${money(c.buy_below)}</div></div>
  </li>`);
  return html`<section class="card"><h3>Sales ahead</h3><p class="small muted">Chance that watching until this sale gets you a price below the trigger.</p><ul class="plain">${rows}</ul></section>`;
}

function storeLabel(it, ref) {
  const offer = it.offers.find((o) => o.url === ref || o.store_key === ref);
  if (offer && offer.store) return offer.store;
  const known = (state.snapshot.stores || []).find((s) => s.key === ref);
  if (known) return known.name;
  return ref.replace(/^https?:\/\/(www\.)?/, '').split('/')[0];
}

function quotesSection(it) {
  const cur = currency();
  const rows = it.quotes.map((q, i) => {
    const l = q.landed;
    return html`<li class="quote ${i === 0 && q.in_stock ? 'best' : ''} ${q.in_stock ? '' : 'oos'}">
      <details>
        <summary>
          <div class="q-store">${flag(q.country)} ${q.store}${i === 0 && q.in_stock ? html` <span class="chip best-chip">best</span>` : ''}
            <div class="small muted">${money(q.price, q.currency)}${q.currency !== cur ? '' : ''} · ${ago(q.seen)}${q.in_stock ? '' : ' · out of stock'}</div></div>
          <div class="q-total">${money(l.total)}<div class="small muted">delivered</div></div>
        </summary>
        <table class="breakdown">
          <tr><td>Item</td><td>${money(l.item)}</td></tr>
          <tr><td>Shipping${l.shipping_known ? '' : ' (unknown)'}</td><td>${l.shipping_known ? money(l.shipping) : '?'}</td></tr>
          ${l.duty ? html`<tr><td>Customs duty</td><td>${money(l.duty)}</td></tr>` : ''}
          ${l.vat ? html`<tr><td>Import VAT</td><td>${money(l.vat)}</td></tr>` : ''}
          ${l.fees ? html`<tr><td>Clearance fee</td><td>${money(l.fees)}</td></tr>` : ''}
          <tr class="total"><td>Total</td><td>${money(l.total)}</td></tr>
        </table>
        ${l.notes.length ? html`<ul class="notes small muted">${l.notes.map((n) => html`<li>${n}</li>`)}</ul>` : ''}
        <div class="row gap small">
          ${q.url ? html`<a href="${q.url}" target="_blank" rel="noopener">Open store page</a>` : ''}
          <button type="button" class="link danger" data-action="remove-offer" data-offer="${q.offer_id}">Stop tracking this store</button>
        </div>
      </details>
    </li>`;
  });
  const unpriced = it.offers.filter((o) => !o.has_prices).map((o) => html`<li class="quote unpriced">
      <div class="q-store">${o.store}<div class="small muted">${o.pending ? 'waiting to sync' : 'no price yet'}</div></div>
      ${o.id ? html`<button type="button" class="link danger small" data-action="remove-offer" data-offer="${o.id}">remove</button>` : ''}
    </li>`);
  const pend = (it.pending_prices || []).map((p) => html`<li class="quote pending">
      <div class="q-store">${storeLabel(it, p.store)}<div class="small muted">logged ${ago(p.at)} · waiting to sync</div></div>
      <div class="q-total">${money(Number(p.price), p.currency || cur)}<div class="small muted">shelf price</div></div>
    </li>`);
  return html`<section class="card"><div class="row between"><h3>Stores</h3><span class="small muted">delivered to ${state.snapshot.meta ? state.snapshot.meta.destination.name : 'you'}</span></div>
    <ul class="plain quotes">${pend}${rows}${unpriced}</ul>
    ${!rows.length && !unpriced.length && !pend.length ? html`<p class="muted small">No stores yet.</p>` : ''}
  </section>`;
}

function viewItem(id) {
  const it = state.snapshot.items.find((i) => String(i.id) === String(id));
  if (!it) {
    return html`<section class="empty"><h2>Item not found</h2><p class="muted">It may have been removed.</p><a class="btn" href="#/">Back to items</a></section>`;
  }
  const chart = historyChart(it, (v) => money(v));
  viewItem.after = (root) => chart.attach(root);
  const temp = String(it.id).startsWith('tmp-');
  return html`
    <div class="item-head">
      <a class="icon-btn" href="#/" aria-label="Back">${icon('back')}</a>
      <div class="grow"><h1>${it.name}</h1><div class="small muted">${it.category}${it.target_price ? html` · target ${money(it.target_price)}` : ''}</div></div>
      <button class="icon-btn" type="button" data-action="edit-item" aria-label="Edit">${icon('edit')}</button>
    </div>
    <div class="actions">
      <button class="btn primary" type="button" data-action="log-price">${icon('tag')} Log price</button>
      <button class="btn" type="button" data-action="add-offer">${icon('store')} Add store</button>
      <button class="btn" type="button" data-action="check" ${!state.online || temp ? 'disabled' : ''} title="${state.online ? 'Fetch prices from the store pages' : 'Needs a connection'}">${icon('refresh')} Check</button>
    </div>
    ${adviceHero(it)}
    <section class="card"><h3>Price history</h3>${raw(chart.html)}</section>
    ${salesAhead(it)}
    ${quotesSection(it)}`;
}

let eventFilter = 'all';
function viewEvents() {
  const evs = state.snapshot.events || [];
  const filters = [['all', 'All'], ['IL', 'Israel'], ['AMAZON', 'Amazon'], ['CN', 'China'], ['US', 'US']];
  const shown = evs.filter((e) => eventFilter === 'all' || e.regions.includes(eventFilter));
  const cards = shown.map((e) => {
    const d = daysUntil(e.start);
    return html`<li class="card event ${e.active || (d <= 0 && daysUntil(e.end) >= 0) ? 'active' : ''}">
      <div class="event-date"><b>${day(e.start).getUTCDate()}</b><span>${fmtDate(e.start, { month: 'short' })}</span></div>
      <div class="grow"><div class="sale-name">${e.name}</div>
        <div class="small muted">${fmtDate(e.start)} – ${fmtDate(e.end)}${e.estimated ? ' · date estimated' : ''}</div>
        <div class="chips">${e.regions.map((r) => html`<span class="chip">${r}</span>`)}</div></div>
      <div class="countdown">${d <= 0 && daysUntil(e.end) >= 0 ? html`<span class="badge buy">on now</span>` : html`<b>${d}</b><span class="small muted">days</span>`}</div>
    </li>`;
  });
  return html`<h1 class="page-title">Sales days</h1>
    <div class="chips filters">${filters.map(([k, label]) => html`<button type="button" class="chip ${eventFilter === k ? 'on' : ''}" data-action="event-filter" data-filter="${k}">${label}</button>`)}</div>
    ${cards.length ? html`<ul class="plain stack">${cards}</ul>` : html`<p class="muted">${state.base ? 'No events.' : 'Sync once to load the sales calendar.'}</p>`}`;
}

function categoryOptions(selected) {
  const cats = (state.snapshot.meta && state.snapshot.meta.categories) || ['electronics', 'computers', 'phones', 'appliances', 'clothing', 'shoes', 'toys', 'default'];
  return cats.map((c) => html`<option value="${c}" ${c === selected ? 'selected' : ''}>${c === 'default' ? 'other' : c}</option>`);
}

function viewAdd() {
  return html`<h1 class="page-title">Track an item</h1>
    <form class="card form" id="add-form" novalidate>
      <label>Item name<input name="name" required placeholder="e.g. Sony WH-1000XM5" autocomplete="off"></label>
      <div class="row gap">
        <label class="grow">Category<select name="category">${categoryOptions('electronics')}</select></label>
        <label class="grow">Target price (${currency()})<input name="target_price" inputmode="decimal" placeholder="optional"></label>
      </div>
      <label>Product links <span class="muted small">(one per line: KSP, Ivory, Amazon, AliExpress…)</span>
        <textarea name="urls" rows="4" placeholder="https://ksp.co.il/web/item/…&#10;https://www.amazon.com/dp/…"></textarea></label>
      <label class="check"><input type="checkbox" name="check" checked> Fetch prices right away (when online)</label>
      <p class="form-error" hidden></p>
      <button class="btn primary wide" type="submit">Start tracking</button>
      <p class="small muted">Works offline too: the item is saved on this device and sent to the server when you're back online.</p>
    </form>`;
}

function viewSettings() {
  const meta = state.snapshot.meta;
  const d = meta && meta.destination;
  const pending = state.outbox.map((op) => html`<li class="row between"><span>${describeOp(op)}<span class="small muted"> · ${ago(op.at)}</span></span>
      <button type="button" class="link danger small" data-action="discard" data-seq="${op.seq}">discard</button></li>`);
  const failed = state.failed.map((f) => html`<li><b>${describeOp(f.op)}</b><div class="small muted">${f.error}</div></li>`);
  return html`<h1 class="page-title">Settings</h1>
    <section class="card">
      <h3>Sync</h3>
      <div class="row between"><span>Status</span>${statusPill()}</div>
      <div class="row between small"><span class="muted">Last synced</span><span>${ago(state.lastSync)}</span></div>
      ${state.lastError ? html`<div class="row between small"><span class="muted">Last error</span><span>${state.lastError}</span></div>` : ''}
      <button class="btn wide" type="button" data-action="sync">${icon('refresh')} Sync now</button>
    </section>
    <section class="card">
      <h3>Pending changes <span class="muted small">(${state.outbox.length})</span></h3>
      ${pending.length ? html`<ul class="plain list">${pending}</ul>` : html`<p class="small muted">Everything is synced.</p>`}
      ${failed.length ? html`<h4>Rejected by the server</h4><ul class="plain list">${failed}</ul><button class="link small" type="button" data-action="clear-failed">clear</button>` : ''}
    </section>
    <form class="card form" id="token-form">
      <h3>Server access</h3>
      <label>Access token <span class="small muted">(only if the server was started with --token)</span>
        <input name="token" value="${state.token || ''}" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
      <button class="btn" type="submit">Save token</button>
    </form>
    ${d ? html`<section class="card"><h3>Taxes & destination</h3>
      <table class="breakdown">
        <tr><td>Destination</td><td>${d.name} (${d.currency})</td></tr>
        <tr><td>VAT on imports</td><td>${pct(d.vat_rate)}</td></tr>
        <tr><td>VAT-free up to</td><td>$${d.vat_exempt_usd}</td></tr>
        <tr><td>Duty-free up to</td><td>$${d.duty_exempt_usd}</td></tr>
        <tr><td>Exchange rates</td><td>${meta.fx_source}</td></tr>
      </table><p class="small muted">Change these in the server's config.toml.</p></section>` : ''}
    <section class="card">
      <h3>Install on iPhone / iPad</h3>
      <ol class="small"><li>Open HawkDrop in <b>Safari</b> (over HTTPS).</li><li>Tap <b>Share</b> ${icon('share', 'icon inline')}, then <b>Add to Home Screen</b>.</li><li>Open it once while online. After that it works offline.</li></ol>
      <p class="small muted">${isStandalone() ? 'Running as an installed app ✓' : 'Running in the browser.'} ${'serviceWorker' in navigator ? (navigator.serviceWorker.controller ? 'Offline mode active ✓' : 'Offline mode starts after a reload.') : 'This browser has no offline support (needs HTTPS).'}</p>
    </section>
    <section class="card">
      <h3>Data</h3>
      <div class="row gap wrap">
        <button class="btn" type="button" data-action="demo" ${state.online ? '' : 'disabled'}>Load demo item</button>
        <button class="btn danger" type="button" data-action="clear-local">Clear data on this device</button>
      </div>
      <p class="small muted">HawkDrop ${meta ? meta.version : ''} · your items live on the server; this device keeps an offline copy.</p>
    </section>`;
}

// ---- router / rendering -------------------------------------------------------------------------

const ROUTES = [
  [/^#?\/?$/, () => viewList(), 'list', true],
  [/^#\/item\/([\w-]+)$/, (m) => viewItem(m[1]), 'list', true],
  [/^#\/add$/, () => viewAdd(), 'add', false],
  [/^#\/events$/, () => viewEvents(), 'events', true],
  [/^#\/settings$/, () => viewSettings(), 'settings', true],
];

let lastHash = null;
function render({ force = false } = {}) {
  document.getElementById('status').innerHTML = str(statusPill());
  const hash = location.hash || '#/';
  let route = ROUTES[0];
  let match = null;
  for (const r of ROUTES) { match = hash.match(r[0]); if (match) { route = r; break; } }
  const [, view, tab, live] = route;
  const main = document.getElementById('view');
  const sameRoute = hash === lastHash;
  if (sameRoute && !force && !live) return; // don't wipe a form the user is typing into
  if (sameRoute && !force && main.contains(document.activeElement) && document.activeElement.matches('input,textarea,select')) return;
  const scroll = sameRoute ? window.scrollY : 0;
  const openDetails = sameRoute ? [...main.querySelectorAll('details[open]')].map((d) => d.querySelector('summary')?.textContent) : [];
  viewItem.after = null;
  main.innerHTML = str(view(match));
  if (viewItem.after) viewItem.after(main);
  main.querySelectorAll('details').forEach((d) => { if (openDetails.includes(d.querySelector('summary')?.textContent)) d.open = true; });
  document.querySelectorAll('.tabbar a').forEach((a) => a.classList.toggle('on', a.dataset.tab === tab));
  window.scrollTo(0, scroll);
  lastHash = hash;
}

// ---- actions ---------------------------------------------------------------------------------------

function currentItem() {
  const m = (location.hash || '').match(/^#\/item\/([\w-]+)$/);
  return m && state.snapshot.items.find((i) => String(i.id) === m[1]);
}

function logPriceSheet(it) {
  const stores = state.snapshot.stores || [];
  const own = it.offers.filter((o) => o.url || o.store_key);
  const ownKeys = new Set(own.map((o) => o.store_key));
  const opt = (value, label, cur) => html`<option value="${value}" data-cur="${cur || ''}">${label}</option>`;
  const storeCur = (key) => (stores.find((s) => s.key === key) || {}).currency || '';
  const body = html`
    <label>Store<select name="store" id="price-store">
      ${own.length ? html`<optgroup label="This item's stores">${own.map((o) => opt(o.url || o.store_key, o.store, storeCur(o.store_key)))}</optgroup>` : ''}
      <optgroup label="Other stores">${stores.filter((s) => !ownKeys.has(s.key)).map((s) => opt(s.key, `${flag(s.country)} ${s.name}`, s.currency))}</optgroup>
      <option value="__url">Another shop (paste link)…</option>
    </select></label>
    <label id="price-url" hidden>Product link<input name="url" type="url" placeholder="https://…" autocapitalize="off"></label>
    <div class="row gap">
      <label class="grow">Price<input name="price" inputmode="decimal" required placeholder="0"></label>
      <label>Currency<select name="currency" id="price-cur">${['ILS', 'USD', 'EUR', 'GBP', 'CNY'].map((c) => html`<option>${c}</option>`)}</select></label>
    </div>
    <div class="row gap">
      <label class="grow">Shipping <span class="small muted">(optional)</span><input name="shipping" inputmode="decimal" placeholder="store default"></label>
      <label class="grow">Date<input name="date" type="date" value="${today()}" max="${today()}"></label>
    </div>
    <label class="check"><input type="checkbox" name="in_stock" checked> In stock</label>`;
  const { form } = openSheet(`Log a price · ${it.name}`, body, async (v) => {
    const store = v.store === '__url' ? (v.url || '').trim() : v.store;
    if (!store) throw new Error('Choose a store or paste a product link');
    const price = numOrNull(v.price);
    if (!price) throw new Error('Enter the price');
    await mutate({ type: 'add_price', itemId: it.id, body: { store, price, currency: v.currency, shipping: numOrNull(v.shipping), in_stock: v.in_stock === 'on', date: v.date || today() } });
    toast(state.online ? 'Price saved' : 'Price saved on this device. It will sync when you are back online.');
  }, { submitLabel: 'Save price' });
  const sel = form.querySelector('#price-store');
  const cur = form.querySelector('#price-cur');
  const sync = () => {
    form.querySelector('#price-url').hidden = sel.value !== '__url';
    const c = sel.selectedOptions[0] && sel.selectedOptions[0].dataset.cur;
    if (c) cur.value = c;
  };
  sel.onchange = sync;
  sync();
}

function addOfferSheet(it) {
  openSheet(`Add a store · ${it.name}`, html`
    <label>Product link<input name="url" type="url" required placeholder="https://…" autocapitalize="off"></label>
    <div class="row gap">
      <label class="grow">Shipping to you <span class="small muted">(optional)</span><input name="shipping" inputmode="decimal" placeholder="store default"></label>
      <label>Currency<select name="shipping_currency"><option value="">price's</option>${['ILS', 'USD', 'EUR', 'GBP'].map((c) => html`<option>${c}</option>`)}</select></label>
    </div>
    <details class="small"><summary>Advanced</summary>
      <label>Price regex <span class="muted">(first group = price)</span><input name="regex" autocapitalize="off" spellcheck="false"></label></details>`,
  async (v) => {
    const url = (v.url || '').trim();
    if (!/^https?:\/\//i.test(url)) throw new Error('Paste the full product link (https://…)');
    await mutate({ type: 'add_offer', itemId: it.id, body: { url, shipping: numOrNull(v.shipping), shipping_currency: v.shipping_currency || null, regex: v.regex || null } });
    toast('Store added');
  }, { submitLabel: 'Add store' });
}

function editItemSheet(it) {
  const { form, close } = openSheet(`Edit · ${it.name}`, html`
    <label>Category<select name="category">${categoryOptions(it.category)}</select></label>
    <label>Target price (${currency()})<input name="target_price" inputmode="decimal" value="${it.target_price ?? ''}" placeholder="none"></label>`,
  async (v) => {
    await mutate({ type: 'update_item', itemId: it.id, body: { category: v.category, target_price: v.target_price.trim() === '' ? '' : numOrNull(v.target_price) } });
  }, { extra: html`<button class="btn danger" type="button" id="del-item">Stop tracking</button>` });
  form.querySelector('#del-item').onclick = async () => {
    if (!confirm(`Stop tracking “${it.name}” and delete its price history?`)) return;
    close();
    await mutate({ type: 'delete_item', itemId: it.id });
    location.hash = '#/';
  };
}

async function checkPrices(it) {
  toast('Checking store pages…', { timeout: 2500 });
  try {
    const res = await online('POST', `api/items/${it.id}/check`);
    const ok = res.results.filter((r) => r.ok).length;
    const bad = res.results.filter((r) => !r.ok);
    toast(`${ok} price${ok === 1 ? '' : 's'} updated${bad.length ? `, ${bad.length} failed (${bad.map((b) => b.store).join(', ')}). Log those by hand.` : ''}`, { timeout: 7000 });
  } catch (e) {
    toast(`Could not check prices: ${e.message}`);
  }
}

async function onClick(ev) {
  const el = ev.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  const it = currentItem();
  switch (action) {
    case 'sync': sync(); break;
    case 'log-price': if (it) logPriceSheet(it); break;
    case 'add-offer': if (it) addOfferSheet(it); break;
    case 'edit-item': if (it) editItemSheet(it); break;
    case 'check': if (it) checkPrices(it); break;
    case 'remove-offer':
      if (it && confirm('Stop tracking this store for this item?')) await mutate({ type: 'remove_offer', itemId: it.id, offerId: Number(el.dataset.offer) });
      break;
    case 'demo':
      try { const item = await online('POST', 'api/demo'); location.hash = `#/item/${item.id}`; } catch (e) { toast(`Demo needs the server: ${e.message}`); }
      break;
    case 'event-filter': eventFilter = el.dataset.filter; render({ force: true }); break;
    case 'dismiss-hint': try { localStorage.setItem('hawkdrop.installHint', 'no'); } catch { /* ignore */ } render({ force: true }); break;
    case 'discard': await discardPending(Number(el.dataset.seq)); break;
    case 'clear-failed': await discardFailed(); break;
    case 'clear-local':
      if (confirm('Remove the offline copy and any unsynced changes from this device? Items on the server are not affected.')) { await clearLocalData(); sync(); }
      break;
    default: break;
  }
}

async function onSubmit(ev) {
  const form = ev.target;
  if (form.id === 'add-form') {
    ev.preventDefault();
    const v = Object.fromEntries(new FormData(form));
    const err = form.querySelector('.form-error');
    try {
      const name = v.name.trim();
      if (!name) throw new Error('Give the item a name');
      const urls = v.urls.split(/\s+/).map((u) => u.trim()).filter(Boolean);
      const bad = urls.find((u) => !/^https?:\/\//i.test(u));
      if (bad) throw new Error(`Not a link: ${bad}`);
      const existing = state.snapshot.items.find((i) => i.name.toLowerCase() === name.toLowerCase());
      const itemId = existing ? existing.id : `tmp-${uid()}`;
      await mutate({ type: 'create_item', itemId, body: { name, category: v.category, target_price: numOrNull(v.target_price), urls, check: v.check === 'on' } });
      form.reset();
      lastHash = null;
      location.hash = `#/item/${itemId}`;
      if (!state.online) toast('Saved on this device. It will sync when you are back online.');
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  } else if (form.id === 'token-form') {
    ev.preventDefault();
    await setToken(new FormData(form).get('token').trim());
    toast(state.authError ? 'Token rejected by the server' : 'Token saved');
    render({ force: true });
  }
}

// ---- service worker -------------------------------------------------------------------------------

function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('./sw.js', { scope: './' }).then((reg) => {
    const offerUpdate = (worker) => toast('A new version of HawkDrop is ready.', {
      action: 'Reload', timeout: 0, onAction: () => worker.postMessage({ type: 'SKIP_WAITING' }),
    });
    if (reg.waiting && navigator.serviceWorker.controller) offerUpdate(reg.waiting);
    reg.addEventListener('updatefound', () => {
      const worker = reg.installing;
      worker.addEventListener('statechange', () => {
        if (worker.state === 'installed' && navigator.serviceWorker.controller) offerUpdate(worker);
      });
    });
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') reg.update().catch(() => {}); });
  }).catch((err) => console.warn('service worker registration failed', err));
  let reloading = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (reloading || !window.__hadController) return;
    reloading = true;
    location.reload();
  });
  window.__hadController = !!navigator.serviceWorker.controller;
}

// ---- boot ----------------------------------------------------------------------------------------------

on('change', () => render());
on('idmap', ({ from, to }) => {
  if (location.hash === `#/item/${from}`) { lastHash = null; location.replace(`#/item/${to}`); }
});
on('failed', ({ op, error }) => toast(`Server rejected “${describeOp(op)}”: ${error}`, { timeout: 7000 }));
window.addEventListener('hashchange', () => render());
document.addEventListener('click', onClick);
document.addEventListener('submit', onSubmit);
setInterval(() => { document.getElementById('status').innerHTML = str(statusPill()); }, 30000);

if (state.snapshot === null) state.snapshot = { items: [], events: [], stores: [], meta: null };
render({ force: true });
init();
registerServiceWorker();
