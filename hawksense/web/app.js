// HawkSense web client. Plain ES modules, no build step.
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
  bell: '<path d="M6 16V11a6 6 0 1 1 12 0v5l1.5 2h-15zM10 20.5a2 2 0 0 0 4 0"/>',
  box: '<path d="M3.5 7.5 12 3l8.5 4.5v9L12 21l-8.5-4.5zM3.5 7.5 12 12l8.5-4.5M12 12v9"/>',
  warn: '<path d="M12 4 2.8 20h18.4zM12 10v4.5M12 17.2v.1"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
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

// a plain picker sheet: no form/submit, each row acts immediately when tapped
function openPicker(title, body) {
  const sheet = document.getElementById('sheet');
  sheet.innerHTML = str(html`
    <div class="sheet-backdrop" data-close></div>
    <div class="sheet-panel">
      <header class="sheet-head"><h2>${title}</h2><button type="button" class="icon-btn" data-close aria-label="Close">${icon('close')}</button></header>
      <div class="sheet-body">${body}</div>
    </div>`);
  sheet.hidden = false;
  document.body.classList.add('sheet-open');
  const panel = sheet.querySelector('.sheet-panel');
  const close = () => { sheet.hidden = true; sheet.innerHTML = ''; document.body.classList.remove('sheet-open'); };
  sheet.querySelectorAll('[data-close]').forEach((el) => { el.onclick = close; });
  return { panel, close };
}

// a failure shown in place: a short "failed" line whose full error is a tooltip and expands on tap
function failedNote(label, error) {
  const msg = String(error || 'unknown error');
  return html`<details class="fail-note" title="${msg}"><summary>${icon('warn')} ${label}</summary><p class="small">${msg}</p></details>`;
}

function checkFailures(it) {
  const bad = state.checkErrors && state.checkErrors[it.id];
  if (!bad || !bad.length) return '';
  return html`<section class="card"><h3>Last check</h3>${bad.map((b) => failedNote(`${b.store} failed`, b.error))}</section>`;
}

function shippingNote(r) {
  if (r.shipping == null) return ' · shipping unknown';
  if (r.shipping === 0) return ' · free shipping';
  return html` · +${money(r.shipping, r.currency)} shipping`;
}

// search across stores for a product and let the user pick the right listing;
// onPick(candidate) runs once, right before the sheet closes
async function productPickerSheet(query, onPick) {
  const { panel, close } = openPicker(`Search results · ${query}`, html`<p class="muted center">Searching stores for “${query}”…</p>`);
  const body = () => panel.querySelector('.sheet-body');
  let results;
  let errors = [];
  let suggestions = [];
  try {
    ({ results, errors = [], suggestions = [] } = await online('POST', 'api/search', { query }));
  } catch (e) {
    body().innerHTML = str(failedNote('Search failed', e.message));
    return;
  }
  if (!panel.isConnected) return; // closed while the search was in flight
  // stores that failed are noted, never fatal: the ones that answered still show
  // "did you mean": near-miss product names from the stores; tapping one re-runs the search with that phrase
  const didYouMean = suggestions.length
    ? html`<div class="did-you-mean"><span class="small muted">${results.length ? 'Not it? Did you mean' : 'Did you mean'}</span>
        <div class="chips">${suggestions.map((sg) => html`<button type="button" class="chip" data-suggest="${sg}">${sg}</button>`)}</div></div>`
    : '';
  const wireSuggestions = () => body().querySelectorAll('[data-suggest]').forEach((btn) => {
    btn.onclick = () => { close(); productPickerSheet(btn.dataset.suggest, onPick); };
  });
  const failed = errors.length
    ? html`<details class="fail-note quiet"><summary>${errors.length} store${errors.length > 1 ? 's' : ''} didn't respond</summary>${errors.map((x) => html`<p class="small"><b>${x.store}</b>: ${x.error}</p>`)}</details>`
    : '';
  if (!results.length) {
    // nothing came back: if that's because every store failed, say why up front rather than hiding it
    const reasons = [...new Set(errors.map((x) => x.error))];
    body().innerHTML = errors.length && !suggestions.length
      ? str(html`<p class="muted center">No results — the stores did not respond.</p>${reasons.map((r) => html`<p class="small muted center">${r}</p>`)}<p class="small muted center">You can still paste a product link instead.</p>`)
      : str(html`<p class="muted center">No exact matches for “${query}”. Try a shorter name, or paste a link instead.</p>${didYouMean}`);
    wireSuggestions();
    return;
  }
  const rows = results.map((r, i) => html`<li><button type="button" class="pick-row" data-i="${i}">
      ${r.image ? html`<img class="item-thumb" src="${r.image}" alt="" loading="lazy" referrerpolicy="no-referrer">` : html`<div class="item-thumb"></div>`}
      <div class="grow"><div class="pick-title">${r.title || r.store}</div>
        <div class="small muted">${r.store}${shippingNote(r)}${r.condition ? html` · <span class="chip tiny warn">${r.condition}</span>` : ''}</div></div>
      <div class="pick-price">${money(r.price, r.currency)}</div>
    </button></li>`);
  body().innerHTML = str(html`<ul class="plain pick-list">${rows}</ul>${didYouMean}${failed}`);
  wireSuggestions();
  body().querySelectorAll('.pick-row').forEach((btn) => {
    btn.onclick = () => { onPick(results[Number(btn.dataset.i)]); close(); };
  });
}

function pickedChip(c, { onRemove } = {}) {
  return html`<div class="card picked-chip">
    ${c.image ? html`<img class="item-thumb" src="${c.image}" alt="" loading="lazy" referrerpolicy="no-referrer">` : html`<div class="item-thumb"></div>`}
    <div class="grow"><b>${c.title || c.store}</b><div class="small muted">${c.store} · ${money(c.price, c.currency)}${shippingNote(c)}${c.condition ? html` · <span class="chip tiny warn">${c.condition}</span>` : ''}</div></div>
    ${onRemove ? html`<button type="button" class="icon-btn" data-action="${onRemove}" aria-label="Remove selection">${icon('close')}</button>` : ''}
  </div>`;
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
    <div><strong>Install HawkSense</strong><br><span class="small">Tap <b>Share</b> then <b>Add to Home Screen</b> to use it as an app, even offline.</span></div>
    <button class="icon-btn" type="button" data-action="dismiss-hint" aria-label="Dismiss">${icon('close')}</button>
  </div>`;
}

// ---- views ---------------------------------------------------------------------------------------

let itemSearch = '';
function itemSearchText(it) {
  const best = it.best;
  return [it.name, it.category, best ? best.store : '', ...(it.offers || []).map((o) => o.store)].join(' ').toLowerCase();
}

function filterItemCards() {
  const q = itemSearch.trim().toLowerCase();
  const cards = [...document.querySelectorAll('#view .item-card')];
  let shown = 0;
  for (const card of cards) {
    const match = !q || (card.dataset.search || '').includes(q);
    card.hidden = !match;
    if (match) shown += 1;
  }
  const empty = document.getElementById('search-empty');
  if (empty) empty.hidden = !q || shown > 0;
  const clear = document.getElementById('search-clear');
  if (clear) clear.hidden = !itemSearch;
}

function searchBar() {
  return html`<div class="search-bar">
    ${icon('search')}
    <input id="item-search" type="search" placeholder="Search your items…" autocomplete="off" value="${itemSearch}">
    <button type="button" class="icon-btn clear" id="search-clear" data-action="clear-search" aria-label="Clear search" ${itemSearch ? '' : 'hidden'}>${icon('close')}</button>
  </div>`;
}

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
          ? 'HawkSense could not reach its server yet. Once it syncs, everything keeps working offline.'
          : 'Add a product and a few store links. HawkSense compares delivered prices, including shipping and Israeli import tax, and tells you whether to buy now or wait for a sale.'}</p>
        <div class="row gap center">
          <a class="btn primary" href="#/add">${icon('plus')} Track an item</a>
          <button class="btn" type="button" data-action="demo">Load demo</button>
        </div>
      </section>`;
  }
  const q = itemSearch.trim().toLowerCase();
  const cards = items.map((it) => {
    const best = it.best;
    const pendingN = (it.pending_prices || []).length;
    const hay = itemSearchText(it);
    return html`<a class="card item-card" href="#/item/${it.id}" data-search="${hay}" ${q && !hay.includes(q) ? 'hidden' : ''}>
      ${it.image_url ? html`<img class="item-thumb" src="${it.image_url}" alt="" loading="lazy">` : ''}
      <div class="item-main">
        <div class="item-title">${it.name}</div>
        <div class="small muted">${it.category !== 'default' ? it.category : ''}${best ? html` · best at ${best.store}` : ''}${pendingN ? html` · <span class="pending-dot">${pendingN} pending</span>` : ''}</div>
        <div class="item-verdict">${verdict(it.advice, { compact: true })}</div>
      </div>
      <div class="item-side">
        <div class="price">${best ? (best.landed.hold ? html`<span class="warn-text small">set weight</span>` : money(best.landed.total)) : '–'}</div>
        <div class="spark-wrap">${raw(sparkline(it.history))}</div>
      </div>
    </a>`;
  });
  const asOf = snap.generated_at ? html`<p class="small muted center">Prices as of ${ago(snap.generated_at)}</p>` : '';
  return html`${installHint()}${items.length > 4 ? searchBar() : ''}<section class="stack">${cards}</section>
    <p class="muted center" id="search-empty" ${!q ? 'hidden' : ''}>No items match “${itemSearch}”.</p>${asOf}`;
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

function breakdown(l) {
  if (l.hold) return html`<p class="small warn-text">⚠ ${l.hold[0].toUpperCase() + l.hold.slice(1)}</p>`;
  const lines = l.lines && l.lines.length
    ? l.lines.map(([label, v]) => html`<tr><td>${label[0].toUpperCase() + label.slice(1)}</td><td>${money(v)}</td></tr>`)
    : [html`<tr><td>Shipping${l.shipping_known ? '' : ' (unknown)'}</td><td>${l.shipping_known ? money(l.shipping) : '?'}</td></tr>`,
      l.fees ? html`<tr><td>Clearance fee</td><td>${money(l.fees)}</td></tr>` : ''];
  return html`<table class="breakdown">
      <tr><td>Item</td><td>${money(l.item)}</td></tr>
      ${lines}
      ${l.sales_tax ? html`<tr><td>US sales tax</td><td>${money(l.sales_tax)}</td></tr>` : ''}
      ${l.duty ? html`<tr><td>Customs duty</td><td>${money(l.duty)}</td></tr>` : ''}
      ${l.vat ? html`<tr><td>Import VAT</td><td>${money(l.vat)}</td></tr>` : ''}
      <tr class="total"><td>Total</td><td>${money(l.total)}</td></tr>
    </table>
    ${l.notes.length ? html`<ul class="notes small muted">${l.notes.map((n) => html`<li>${n}</li>`)}</ul>` : ''}`;
}

function otherRoutes(q) {
  const others = (q.routes || []).filter((r) => r.route !== q.landed.route);
  if (!others.length) return '';
  return html`<details class="routes small" data-key="routes-${q.offer_id}"><summary>Other ways to get it (${others.length})</summary>
    <ul class="plain">${others.map((r) => html`<li><details data-key="route-${q.offer_id}-${r.route}">
      <summary class="row between"><span>${r.route === 'direct' ? 'Direct from the store' : r.route_label}${r.set_up === false ? html` <span class="chip tiny">not set up</span>` : ''}</span><b>${r.hold ? 'on hold' : money(r.total)}</b></summary>
      ${breakdown(r)}
      ${r.set_up === false && r.route !== 'direct' ? html`<div class="row gap small"><button type="button" class="link" data-action="use-forwarder" data-route="${r.route}">Set up this forwarder</button></div>` : ''}
    </details></li>`)}</ul>
  </details>`;
}

function quotesSection(it) {
  const cur = currency();
  const rows = it.quotes.map((q, i) => {
    const l = q.landed;
    return html`<li class="quote ${i === 0 && q.in_stock ? 'best' : ''} ${q.in_stock ? '' : 'oos'}">
      <details data-key="quote-${q.offer_id}">
        <summary>
          <div class="q-store">${flag(q.country)} ${q.store}${i === 0 && q.in_stock ? html` <span class="chip best-chip">best</span>` : ''}
            <div class="small muted">${money(q.price, q.currency)} · ${ago(q.seen)}${q.in_stock ? '' : ' · out of stock'}</div>
            ${l.route && l.route !== 'direct' ? html`<div class="small via">${l.route_label}</div>` : ''}</div>
          <div class="q-total">${l.hold ? 'on hold' : money(l.total)}<div class="small muted">${l.hold ? 'set the weight' : 'delivered'}</div></div>
        </summary>
        ${breakdown(l)}
        ${otherRoutes(q)}
        <div class="row gap small">
          ${/^https?:\/\//i.test(q.url || '') ? html`<a href="${q.url}" target="_blank" rel="noopener noreferrer">Open store page</a>` : ''}
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

const SPEC_LABEL = {
  verified: 'verified by several store pages', unverified: 'from one store page, not verified',
  majority: 'most store pages agree', conflict: 'store pages split evenly - nothing used',
  missing: 'not found on the store pages',
};

function specsCard(it) {
  const sp = it.specs || {};
  const manual = sp.weight_source === 'manual';
  const size = `${it.weight_kg != null ? `${it.weight_kg} kg` : 'weight unknown'}${it.dims ? ` · ${it.dims} cm` : ''}`;
  if (!sp.status && !manual && it.weight_kg == null) {
    return html`<section class="card specs"><div class="row between"><h3>${icon('box', 'icon inline')} Weight & size</h3>
      <button type="button" class="link small" data-action="edit-item">set</button></div>
      <p class="small muted">Read from the store pages on the next price check. Forwarders charge by weight.</p></section>`;
  }
  const obs = (sp.observations || []).map((o) => html`<li class="row between small"><span>${o.source}</span>
      <span class="muted">${o.weight_kg != null ? `${o.weight_kg} kg${o.weight_kind === 'package' ? ' boxed' : ''}` : 'no weight'}${o.dims ? ` · ${o.dims} cm` : ''}</span></li>`);
  return html`<section class="card specs ${sp.alert ? 'alert' : ''}">
    <div class="row between"><h3>${icon('box', 'icon inline')} Weight & size</h3><b>${size}</b></div>
    <p class="small ${sp.alert ? 'warn-text' : 'muted'}">${manual ? 'Set by you.' : (sp.alert ? '⚠ ' : '') + (SPEC_LABEL[sp.status] || '')}${sp.alert ? '. Forwarder prices are on hold until you set it.' : ''}</p>
    ${!manual && sp.messages && sp.messages.length ? html`<ul class="notes small muted">${sp.messages.map((m) => html`<li>${m}</li>`)}</ul>` : ''}
    ${sp.confirm && sp.confirm.length ? html`<div class="confirm small"><p class="warn-text">? Please confirm - forwarder prices use these values:</p>
      <ul class="notes">${sp.confirm.map((m) => html`<li>${m}</li>`)}</ul>
      <button type="button" class="btn small" data-action="specs-confirm">Looks right</button></div>` : ''}
    ${obs.length ? html`<details class="small"><summary>What each page says (${obs.length})</summary><ul class="plain">${obs}</ul></details>` : ''}
    <div class="row gap small">
      <button type="button" class="link" data-action="edit-item">${manual ? 'Change' : 'Set it yourself'}</button>
      <button type="button" class="link" data-action="specs-source" ${state.online ? '' : 'disabled'}>Cross-check with another page</button>
    </div>
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
      ${it.image_url ? html`<img class="item-head-thumb" src="${it.image_url}" alt="">` : ''}
      <div class="grow"><h1>${it.name}</h1><div class="small muted">${it.category}${it.target_price ? html` · target ${money(it.target_price)}` : ''}</div></div>
      <button class="icon-btn" type="button" data-action="edit-item" aria-label="Edit">${icon('edit')}</button>
    </div>
    <div class="actions">
      <button class="btn primary" type="button" data-action="log-price">${icon('tag')} Log price</button>
      <button class="btn" type="button" data-action="add-offer">${icon('store')} Add store</button>
      <button class="btn" type="button" data-action="check" ${!state.online || temp ? 'disabled' : ''} title="${state.online ? 'Fetch prices from the store pages' : 'Needs a connection'}">${icon('refresh')} Check</button>
    </div>
    ${adviceHero(it)}
    ${checkFailures(it)}
    <section class="card"><h3>Price history</h3>${raw(chart.html)}</section>
    ${salesAhead(it)}
    ${quotesSection(it)}
    ${specsCard(it)}`;
}

function viewInbox() {
  const n = state.snapshot.notifications || { items: [] };
  const rows = n.items.map((x) => html`<li class="card note-item ${x.read ? '' : 'unread'}">
      <div class="row between"><b>${x.title}</b><span class="small muted">${ago(x.ts)}</span></div>
      ${x.body ? html`<p class="small pre">${x.body}</p>` : ''}
      <div class="row gap small">
        ${x.item_id ? html`<a href="#/item/${x.item_id}">Open item</a>` : ''}
        ${Object.entries(x.deliveries || {}).map(([ch, st]) => html`<span class="chip ${st === 'sent' ? '' : 'warn'}" title="${st}">${ch}${st === 'sent' ? ' ✓' : ' !'}</span>`)}
      </div>
    </li>`);
  return html`<div class="row between"><h1 class="page-title">Notifications</h1>
      ${n.unread ? html`<button type="button" class="btn small" data-action="mark-all-read">Mark all read</button>` : ''}</div>
    ${rows.length ? html`<ul class="plain stack">${rows}</ul>` : html`<p class="muted">Nothing yet. Choose what to be told about in <a href="#/settings">Settings</a>.</p>`}`;
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

let addPicked = null;

function viewAdd() {
  return html`<h1 class="page-title">Track an item</h1>
    <form class="card form" id="add-form" novalidate>
      <label>Item name<input name="name" required placeholder="e.g. Sony WH-1000XM5" autocomplete="off"></label>
      <div class="row gap">
        <label class="grow">Category<select name="category">${categoryOptions('electronics')}</select></label>
        <label class="grow">Target price (${currency()})<input name="target_price" inputmode="decimal" placeholder="optional"></label>
      </div>
      <button type="button" class="btn wide" data-action="search-products" ${state.online ? '' : 'disabled'}
        title="${state.online ? '' : 'Needs a connection'}">${icon('search')} Search for this product</button>
      <div id="add-picked">${addPicked ? pickedChip(addPicked, { onRemove: 'add-picked-remove' }) : ''}</div>
      <details class="small"><summary>Or paste a product link yourself</summary>
        <label>Product links <span class="muted small">(one per line: KSP, Ivory, Amazon, AliExpress…)</span>
          <textarea name="urls" rows="3" placeholder="https://ksp.co.il/web/item/…&#10;https://www.amazon.com/dp/…"></textarea></label>
      </details>
      <label class="check"><input type="checkbox" name="search_all" checked> Search all stores for this item too</label>
      <label class="check"><input type="checkbox" name="check" checked> Fetch prices right away (when online)</label>
      <p class="form-error" hidden></p>
      <button class="btn primary wide" type="submit">Start tracking</button>
      <p class="small muted">Works offline too: the item is saved on this device and sent to the server when you're back online. Searching needs a connection.</p>
    </form>`;
}

function notifyCard() {
  const n = state.snapshot.notifications;
  if (!n || !n.events.length) return '';
  const chans = n.channels;
  const head = html`<tr><th></th>${chans.map((c) => html`<th title="${c.name}">${c.key === 'inbox' ? 'app' : c.key}</th>`)}</tr>`;
  const rows = n.events.map((e) => html`<tr><td title="${e.description}"><b>${e.title}</b><div class="small muted desc">${e.description}</div></td>
      ${chans.map((c) => html`<td><input type="checkbox" name="sub:${e.key}:${c.key}" aria-label="${e.title} by ${c.name}" ${(n.subscriptions[e.key] || []).includes(c.key) ? 'checked' : ''} ${c.configured ? '' : 'data-unconfigured'}></td>`)}</tr>`);
  const perm = 'Notification' in window ? Notification.permission : 'unsupported';
  return html`<form class="card form" id="notify-form">
    <h3>Notifications</h3>
    <p class="small muted">Choose which events reach you, and where. Set up the channels below.</p>
    <div class="chips">${chans.map((c) => html`<span class="chip ${c.configured ? 'on' : ''}">${c.name}${c.configured ? ' ✓' : ' – not set up'}${c.configured ? html` <button type="button" class="link small" data-action="test-channel" data-channel="${c.key}">test</button>` : ''}</span>`)}</div>
    <div class="table-scroll"><table class="grid">${head}${rows}</table></div>
    <div class="row gap">
      <label class="grow">Price drop (%)<input name="price_drop_pct" inputmode="decimal" value="${n.settings.price_drop_pct}"></label>
      <label class="grow">Sale warning (days)<input name="sale_soon_days" inputmode="numeric" value="${n.settings.sale_soon_days}"></label>
      <label class="grow">Failed checks<input name="check_failed_after" inputmode="numeric" value="${n.settings.check_failed_after}"></label>
    </div>
    <button class="btn" type="submit">Save notification settings</button>
    <p class="small muted">Alerts on this device: ${perm === 'granted' ? 'on ✓ (while the app is open)' : perm === 'unsupported' ? 'not supported by this browser' : html`<button type="button" class="link" data-action="device-alerts">turn on</button>`}</p>
  </form>`;
}

const RULE_FIELDS = [
  ['vat_rate', 'VAT on imports', '%'], ['vat_exempt_usd', 'VAT-free up to', '$'],
  ['duty_exempt_usd', 'Duty-free up to', '$'], ['clearance_fee', 'Courier clearance fee', ''],
];
const LAYER_LABEL = { builtin: 'built-in', fetched: 'updated', config: 'config', manual: 'yours' };

function rulesCard() {
  const r = state.snapshot.rules;
  const meta = state.snapshot.meta;
  if (!r || !meta) return '';
  const code = r.destination;
  const val = (path) => r.values.find((v) => v.path === path) || {};
  const shown = (row, unit) => (row.value == null ? '' : unit === '%' ? +(row.value * 100).toFixed(2) : row.value);
  const field = (path, label, unit) => {
    const row = val(path);
    return html`<label class="grow">${label}${unit === '%' ? ' (%)' : unit === '$' ? ' ($)' : ''} <span class="chip tiny">${LAYER_LABEL[row.from] || ''}</span>
      <input name="rule:${path}" data-unit="${unit}" inputmode="decimal" value="${shown(row, unit)}" data-orig="${shown(row, unit)}"></label>`;
  };
  const duty = r.values.filter((v) => v.path.startsWith(`destination.${code}.duty_rates.`));
  const fwdKeys = [...new Set(r.values.filter((v) => v.path.startsWith('forwarders.')).map((v) => v.path.split('.')[1]))];
  const services = (state.snapshot.forwarders && state.snapshot.forwarders.services) || [];
  const fwdBlocks = fwdKeys.map((k) => {
    const svc = services.find((x) => x.key === k);
    const whs = [...new Set(r.values.filter((v) => v.path.startsWith(`forwarders.${k}.warehouses.`)).map((v) => v.path.split('.')[3]))];
    return html`<details class="small"><summary>${svc ? svc.name : k}</summary>
      ${whs.map((w) => html`<div class="row gap">${field(`forwarders.${k}.warehouses.${w}.first`, `${w} first ${val(`forwarders.${k}.warehouses.${w}.first_kg`).value} kg`, '')}
        ${field(`forwarders.${k}.warehouses.${w}.additional`, `each ${val(`forwarders.${k}.warehouses.${w}.step_kg`).value} kg`, '')}</div>`)}
      <div class="row gap">${field(`forwarders.${k}.handling_fee`, 'Handling', '')}${field(`forwarders.${k}.tax_handling_fee`, 'Tax handling', '')}${field(`forwarders.${k}.service_fee_rate`, 'Service fee', '%')}</div>
    </details>`;
  });
  const pending = (r.pending || []).map((c) => html`<li class="row between"><div><b>${c.path}</b><div class="small muted">${c.old} → ${c.new} · ${c.source}${c.note ? ` · ${c.note}` : ''}</div></div>
      <div class="row gap"><button type="button" class="btn small" data-action="rule-decide" data-id="${c.id}" data-decision="accept">Accept</button>
      <button type="button" class="link small danger" data-action="rule-decide" data-id="${c.id}" data-decision="reject">Reject</button></div></li>`);
  const last = r.last_check;
  return html`<form class="card form" id="rules-form">
    <h3>Taxes & forwarder rates</h3>
    <p class="small muted">Delivered to ${meta.destination.name}. Checked for updates automatically${last ? ` (last ${ago(last.ts)})` : ''}; your changes here override them.</p>
    ${pending.length ? html`<div class="review"><h4>Changes to review</h4><ul class="plain list">${pending}</ul></div>` : ''}
    <div class="row gap">${RULE_FIELDS.slice(0, 2).map(([f, l, u]) => field(`destination.${code}.${f}`, l, u))}</div>
    <div class="row gap">${RULE_FIELDS.slice(2).map(([f, l, u]) => field(`destination.${code}.${f}`, l, u))}</div>
    <details class="small"><summary>Customs duty by category</summary><div class="row gap wrap">${duty.map((v) => field(v.path, v.path.split('.').pop(), '%'))}</div></details>
    <details class="small"><summary>Forwarder rates (in each service's currency)</summary>${fwdBlocks}</details>
    ${last && last.errors && last.errors.length ? html`<p class="small warn-text">Last check: ${last.errors.join('; ')}</p>` : ''}
    <div class="row gap wrap">
      <button class="btn" type="submit">Save rules</button>
      <button class="btn" type="button" data-action="rules-check" ${state.online ? '' : 'disabled'}>${icon('refresh')} Check for updates</button>
    </div>
    ${(r.history || []).length ? html`<details class="small"><summary>Recent changes</summary><ul class="plain">${r.history.slice(0, 12).map((c) => html`<li class="small">${ago(c.ts)} · ${c.path}: ${c.old ?? '–'} → ${c.new ?? '–'} <span class="muted">(${c.source}, ${c.status})</span></li>`)}</ul></details>` : ''}
  </form>`;
}

// ---- settings pages ---------------------------------------------------------------------------------

const SOURCE_LABEL = { app: 'set here', config: 'config.toml', default: 'default', environment: 'server env' };

function settingsSection(key) {
  const st = state.snapshot.settings;
  return st && st.sections.find((x) => x.key === key);
}

function fieldInput(f) {
  const name = `set:${f.path}`;
  const dis = f.locked ? 'disabled' : '';
  const badge = html`<span class="chip tiny" title="${f.locked ? `Set by ${f.env} on the server` : ''}">${f.locked ? `🔒 ${SOURCE_LABEL.environment}` : SOURCE_LABEL[f.source] || ''}</span>`;
  const help = f.help ? html`<span class="field-help">${f.help}</span>` : '';
  if (f.kind === 'bool') {
    return html`<label class="check"><input type="checkbox" name="${name}" data-kind="bool" data-orig="${f.value ? 'on' : ''}" ${f.value ? 'checked' : ''} ${dis}> ${f.label} ${badge}</label>`;
  }
  if (f.kind === 'choice') {
    return html`<label>${f.label} ${badge}<select name="${name}" data-kind="choice" data-orig="${f.value ?? ''}" ${dis}>
      ${f.choices.map(([v, l]) => html`<option value="${v}" ${v === f.value ? 'selected' : ''}>${l}</option>`)}</select>${help}</label>`;
  }
  if (f.kind === 'secret') {
    return html`<label>${f.label} ${badge}<input type="password" name="${name}" data-kind="secret" data-orig="" autocomplete="new-password"
        placeholder="${f.is_set ? 'saved ••••••  (type to replace)' : 'not set'}" ${dis}>
      ${f.is_set && !f.locked && f.source === 'app' ? html`<span class="field-help"><label class="check inline"><input type="checkbox" name="clear:${f.path}"> remove</label></span>` : help}</label>`;
  }
  const shown = f.value == null ? '' : f.kind === 'percent' ? +(f.value * 100).toFixed(4) : f.value;
  const type = f.kind === 'url' ? 'url' : f.kind === 'email' ? 'email' : 'text';
  const mode = f.kind === 'number' || f.kind === 'percent' ? 'decimal' : f.kind === 'email' ? 'email' : 'text';
  return html`<label>${f.label}${f.kind === 'percent' ? ' (%)' : ''} ${badge}
    <input type="${type}" name="${name}" data-kind="${f.kind}" value="${shown}" data-orig="${shown}" inputmode="${mode}"
      placeholder="${f.placeholder || ''}" autocomplete="off" autocapitalize="off" spellcheck="false" ${dis}>${help}</label>`;
}

function sectionForm(key, { test } = {}) {
  const sec = settingsSection(key);
  if (!sec) return html`<p class="muted small">Sync once to load the settings.</p>`;
  const state_ = sec.configured == null ? '' : sec.configured ? html`<span class="badge buy">set up</span>` : html`<span class="badge muted">not set up</span>`;
  return html`<form class="card form settings-form" data-section="${key}">
    <div class="row between"><h3>${sec.title}</h3>${state_}</div>
    <p class="small muted">${sec.description}</p>
    ${sec.fields.map(fieldInput)}
    <div class="row gap wrap">
      <button class="btn" type="submit">Save</button>
      ${test ? html`<button class="btn" type="button" data-action="test-channel" data-channel="${key}" ${sec.configured && state.online ? '' : 'disabled'}>Send a test</button>` : ''}
    </div>
  </form>`;
}

function collectSettings(form) {
  const changes = {};
  let secret = false;
  for (const el of form.querySelectorAll('[name^="set:"]')) {
    if (el.disabled) continue;
    const path = el.name.slice(4);
    const kind = el.dataset.kind;
    if (kind === 'bool') {
      if ((el.checked ? 'on' : '') !== el.dataset.orig) changes[path] = el.checked;
    } else if (kind === 'secret') {
      if (el.value) { changes[path] = el.value; secret = true; }
      const clear = form.querySelector(`[name="clear:${path}"]`);
      if (clear && clear.checked) { changes[path] = null; secret = true; }
    } else if (el.value.trim() !== String(el.dataset.orig)) {
      const v = el.value.trim();
      if (v === '') changes[path] = null;
      else if (kind === 'number') changes[path] = numOrNull(v);
      else if (kind === 'percent') changes[path] = numOrNull(v) / 100;
      else changes[path] = v;
    }
  }
  return { changes, secret };
}

// ---- "needs attention" indicators -----------------------------------------------------------

const dot = () => html`<i class="attn-dot" aria-hidden="true"></i>`;

function settingsIssues() {
  const out = { sections: {}, brokenChannels: [] };
  const pending = (state.snapshot.rules && state.snapshot.rules.pending) || [];
  if (pending.length) out.sections.rates = `${pending.length} rule change${pending.length > 1 ? 's' : ''} waiting for your review`;
  const n = state.snapshot.notifications;
  if (n && n.channels && n.events) {
    const broken = n.channels.filter((c) => c.key !== 'inbox' && !c.configured
      && n.events.some((e) => (n.subscriptions[e.key] || []).includes(c.key)));
    if (broken.length) {
      out.sections.notifications = `${broken.map((c) => c.name).join(', ')} needs setup to actually send alerts`;
      out.brokenChannels = broken.map((c) => c.key);
    }
  }
  return out;
}

const SETTINGS_PAGES = [
  ['notifications', 'Notifications', 'bell', () => {
    const n = state.snapshot.notifications;
    if (!n) return '';
    const broken = settingsIssues().sections.notifications;
    if (broken) return broken;
    const on = n.channels.filter((c) => c.configured && c.key !== 'inbox').map((c) => c.name);
    return on.length ? `${on.join(', ')} + app` : 'In the app only';
  }],
  ['forwarders', 'Package forwarders', 'box', () => {
    const a = (state.snapshot.forwarders && state.snapshot.forwarders.accounts) || [];
    return a.length ? a.map((x) => `${x.forwarder_name} ${x.warehouse}`).join(', ') : 'None set up';
  }],
  ['rates', 'Taxes & forwarder rates', 'tag', () => {
    const p = (state.snapshot.rules && state.snapshot.rules.pending) || [];
    return p.length ? `${p.length} change(s) to review` : (state.snapshot.meta ? `${state.snapshot.meta.destination.name} import rules` : '');
  }],
  ['checks', 'Automatic checks & rule updates', 'refresh', () => {
    const f = (settingsSection('checks') || { fields: [] }).fields.find((x) => x.path === 'schedule.check_every');
    return f && f.value ? `Prices every ${f.value} h` : 'Automatic price checks off';
  }],
  ['advice', 'Buy/wait advice', 'calendar', () => {
    const f = (settingsSection('advice') || { fields: [] }).fields.find((x) => x.path === 'advisor.max_wait_days');
    return f ? `Looks ${f.value} days ahead` : '';
  }],
  ['stores', 'Stores & shipping', 'store', () => 'Shipping costs and policies per store'],
  ['ebay', 'eBay', 'tag', () => ((settingsSection('ebay') || {}).configured ? 'API keys set up' : 'Reading public pages')],
  ['general', 'General & access', 'gear', () => (state.snapshot.meta ? `Deliver to ${state.snapshot.meta.destination.name}` : '')],
  ['device', 'Sync, device & data', 'share', () => `${state.outbox.length ? `${state.outbox.length} pending · ` : ''}synced ${ago(state.lastSync)}`],
];

function viewSettings() {
  const issues = settingsIssues();
  const rows = SETTINGS_PAGES.map(([key, title, ic, sub]) => html`<a class="settings-row ${issues.sections[key] ? 'needs-attention' : ''}" href="#/settings/${key}">
      ${icon(ic)}<div class="grow"><b>${title}${issues.sections[key] ? dot() : ''}</b><div class="small muted">${sub()}</div></div>${icon('back', 'icon flip')}</a>`);
  return html`<h1 class="page-title">Settings</h1>
    <div class="row between card slim"><span>Sync</span>${statusPill()}</div>
    <nav class="card settings-nav">${rows}</nav>
    <p class="small muted center">Settings saved here are stored on your HawkSense server and override config.toml.</p>`;
}

function storesPage() {
  const st = state.snapshot.settings;
  if (!st) return html`<p class="muted">Sync once to load the stores.</p>`;
  const sel = storesPage.selected || st.stores[0].key;
  const store = st.stores.find((x) => x.key === sel) || st.stores[0];
  const value = (k) => (k in store.overrides ? store.overrides[k] : store.values[k]);
  const fields = st.store_fields.map((f) => {
    const src = store.app.includes(f.key) ? 'set here' : k2src(store, f.key);
    const v = value(f.key);
    if (f.kind === 'bool') {
      const cur = v === true ? 'yes' : v === false ? 'no' : '';
      return html`<label>${f.label} <span class="chip tiny">${src}</span><select name="store:${f.key}" data-kind="bool3" data-orig="${cur}">
        <option value="" ${cur === '' ? 'selected' : ''}>unknown</option><option value="yes" ${cur === 'yes' ? 'selected' : ''}>yes</option><option value="no" ${cur === 'no' ? 'selected' : ''}>no</option></select></label>`;
    }
    return html`<label>${f.label} (${store.currency}) <span class="chip tiny">${src}</span>
      <input name="store:${f.key}" data-kind="number" inputmode="decimal" value="${v ?? ''}" data-orig="${v ?? ''}" placeholder="unknown">${f.help ? html`<span class="field-help">${f.help}</span>` : ''}</label>`;
  });
  return html`<form class="card form" id="store-form" data-store="${store.key}">
    <h3>Stores & shipping</h3>
    <p class="small muted">Correct a store's shipping policy. It changes every delivered price from that store.</p>
    <label>Store<select id="store-pick">${st.stores.map((x) => html`<option value="${x.key}" ${x.key === store.key ? 'selected' : ''}>${x.name}</option>`)}</select></label>
    ${fields}
    <div class="row gap wrap"><button class="btn" type="submit">Save</button>
      ${store.app.length ? html`<button class="btn" type="button" data-action="store-reset">Back to defaults</button>` : ''}</div>
  </form>`;
}
function k2src(store, key) { return key in store.overrides ? 'config.toml' : 'built-in'; }

const SOURCE_TARGETS = () => {
  const code = (state.snapshot.rules && state.snapshot.rules.destination) || 'IL';
  const fixed = [[`destination.${code}.vat_exempt_usd`, `${code}: VAT-free limit ($)`], [`destination.${code}.duty_exempt_usd`, `${code}: duty-free limit ($)`],
    [`destination.${code}.vat_rate`, `${code}: VAT rate (fraction, use scale 0.01 for %)`], [`destination.${code}.clearance_fee`, `${code}: courier clearance fee`]];
  const services = (state.snapshot.forwarders && state.snapshot.forwarders.services) || [];
  const tables = services.flatMap((f) => f.warehouses.map((w) => [`forwarders.${f.key}.warehouses.${w.code}`, `${f.name} ${w.code}: rate table`]));
  return [...fixed, ...tables];
};

function ruleSourcesCard() {
  const st = state.snapshot.settings;
  if (!st) return '';
  const rows = st.rule_sources.map((x) => html`<li class="row between"><div><b>${x.name}</b><div class="small muted">${x.target} · ${x.format === 'table' ? 'table' : 'regex'} · ${x.url}</div></div>
      ${x.from === 'app' ? html`<button type="button" class="link danger small" data-action="source-remove" data-name="${x.name}">remove</button>` : html`<span class="chip tiny">config.toml</span>`}</li>`);
  return html`<form class="card form" id="source-form">
    <h3>Rule sources</h3>
    <p class="small muted">Pages HawkSense reads on every rules check, e.g. an official customs page or a forwarder's price list. A regex reads one number (its first group); a table reads weight/price rows.</p>
    ${rows.length ? html`<ul class="plain list">${rows}</ul>` : ''}
    <details class="small" ${rows.length ? '' : 'open'}><summary>Add a source</summary>
      <label>Name<input name="name" placeholder="il_exemption" autocapitalize="off"></label>
      <label>Page address<input name="url" type="url" placeholder="https://…" autocapitalize="off"></label>
      <label>Sets<select name="target" id="source-target">${SOURCE_TARGETS().map(([v, l]) => html`<option value="${v}">${l}</option>`)}</select></label>
      <label>Read with<select name="format" id="source-format"><option value="">a regex</option><option value="table">a weight/price table</option></select></label>
      <label id="source-regex">Regex <span class="muted">(first group = the value)</span><input name="regex" placeholder="up to \\$\\s*(\\d+)" autocapitalize="off" spellcheck="false"></label>
      <label id="source-scale">Multiply by <span class="muted">(optional)</span><input name="scale" inputmode="decimal" placeholder="1"></label>
      <button class="btn" type="submit">Add source</button>
    </details>
  </form>`;
}

function settingsPage(key) {
  const back = html`<div class="item-head"><a class="icon-btn" href="#/settings" aria-label="Back">${icon('back')}</a>
    <h1 class="grow">${(SETTINGS_PAGES.find((p) => p[0] === key) || [0, 'Settings'])[1]}</h1></div>`;
  const meta = state.snapshot.meta;
  switch (key) {
    case 'notifications': {
      const broken = settingsIssues().brokenChannels;
      return html`${back}${notifyCard()}
        <h2 class="section-title">Channels</h2>
        ${['telegram', 'whatsapp', 'ntfy', 'email', 'webhook'].map((k) => html`<details class="card fold" data-key="channel-${k}">
          <summary><b>${(settingsSection(k) || { title: k }).title}</b> ${(settingsSection(k) || {}).configured ? html`<span class="badge buy">set up</span>` : html`<span class="badge muted">not set up</span>`}${broken.includes(k) ? dot() : ''}</summary>
          ${broken.includes(k) ? html`<p class="small warn-text">A price alert is subscribed to this channel, but it isn't set up yet - those alerts won't be delivered.</p>` : ''}
          ${sectionForm(k, { test: true })}</details>`)}`;
    }
    case 'forwarders': return html`${back}${forwardersCard()}`;
    case 'rates': return html`${back}${rulesCard()}`;
    case 'checks': return html`${back}${sectionForm('checks')}${ruleSourcesCard()}`;
    case 'advice': return html`${back}${sectionForm('advice')}`;
    case 'stores': return html`${back}${storesPage()}`;
    case 'ebay': return html`${back}${sectionForm('ebay')}`;
    case 'general':
      return html`${back}${sectionForm('general')}
        <form class="card form" id="token-form">
          <h3>Server access</h3>
          <label>Access token <span class="small muted">(only if the server was started with --token)</span>
            <input name="token" value="${state.token || ''}" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
          <button class="btn" type="submit">Save token</button>
        </form>`;
    case 'device': {
      const pending = state.outbox.map((op) => html`<li class="row between"><span>${describeOp(op)}<span class="small muted"> · ${ago(op.at)}</span></span>
          <button type="button" class="link danger small" data-action="discard" data-seq="${op.seq}">discard</button></li>`);
      const failed = state.failed.map((f) => html`<li><b>${describeOp(f.op)}</b><div class="small muted">${f.error}</div></li>`);
      return html`${back}
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
        <section class="card">
          <h3>Install on iPhone / iPad</h3>
          <ol class="small"><li>Open HawkSense in <b>Safari</b> (over HTTPS).</li><li>Tap <b>Share</b> ${icon('share', 'icon inline')}, then <b>Add to Home Screen</b>.</li><li>Open it once while online. After that it works offline.</li></ol>
          <p class="small muted">${isStandalone() ? 'Running as an installed app ✓' : 'Running in the browser.'} ${'serviceWorker' in navigator ? (navigator.serviceWorker.controller ? 'Offline mode active ✓' : 'Offline mode starts after a reload.') : 'This browser has no offline support (needs HTTPS).'}</p>
        </section>
        <section class="card">
          <h3>Data</h3>
          <div class="row gap wrap">
            <button class="btn" type="button" data-action="demo" ${state.online ? '' : 'disabled'}>Load demo item</button>
            <button class="btn danger" type="button" data-action="clear-local">Clear data on this device</button>
          </div>
          <p class="small muted">HawkSense ${meta ? meta.version : ''} · your items live on the server; this device keeps an offline copy.</p>
        </section>`;
    }
    default: return html`${back}<p class="muted">Unknown settings page.</p>`;
  }
}

// ---- router / rendering -------------------------------------------------------------------------

const ROUTES = [
  [/^#?\/?$/, () => viewList(), 'list', true],
  [/^#\/item\/([\w-]+)$/, (m) => viewItem(m[1]), 'list', true],
  [/^#\/add$/, () => viewAdd(), 'add', false],
  [/^#\/events$/, () => viewEvents(), 'events', true],
  [/^#\/settings$/, () => viewSettings(), 'settings', true],
  [/^#\/settings\/(\w+)$/, (m) => settingsPage(m[1]), 'settings', true],
  [/^#\/inbox$/, () => viewInbox(), 'inbox', true],
];

let lastHash = null;
function renderBell() {
  const unread = (state.snapshot.notifications && state.snapshot.notifications.unread) || 0;
  document.getElementById('bell').innerHTML = str(html`${icon('bell')}${unread ? html`<i class="count">${unread > 9 ? '9+' : unread}</i>` : ''}`);
}

function renderSettingsDot() {
  const dotEl = document.getElementById('settings-tab-dot');
  if (dotEl) dotEl.hidden = Object.keys(settingsIssues().sections).length === 0;
}

function render({ force = false } = {}) {
  document.getElementById('status').innerHTML = str(statusPill());
  renderBell();
  renderSettingsDot();
  const hash = location.hash || '#/';
  let route = ROUTES[0];
  let match = null;
  for (const r of ROUTES) { match = hash.match(r[0]); if (match) { route = r; break; } }
  const [, view, tab, live] = route;
  const main = document.getElementById('view');
  const sameRoute = hash === lastHash;
  if (sameRoute && !force && !live) return; // don't wipe a form the user is typing into
  if (sameRoute && !force && main.contains(document.activeElement) && document.activeElement.matches('input,textarea,select')) return;
  if (sameRoute && !force && main.querySelector('form[data-dirty]')) return; // keep unsaved edits
  const scroll = sameRoute ? window.scrollY : 0;
  const detailsKey = (d) => d.dataset.key || d.querySelector('summary')?.textContent;
  const openDetails = sameRoute ? [...main.querySelectorAll('details[open]')].map(detailsKey) : [];
  viewItem.after = null;
  main.innerHTML = str(view(match));
  if (viewItem.after) viewItem.after(main);
  main.querySelectorAll('details').forEach((d) => { if (openDetails.includes(detailsKey(d))) d.open = true; });
  document.querySelectorAll('.tabbar a').forEach((a) => a.classList.toggle('on', a.dataset.tab === tab));
  window.scrollTo(0, scroll);
  lastHash = hash;
  if (tab === 'inbox' && (state.snapshot.notifications || {}).unread && !render.marking) {
    render.marking = true;  // opening the inbox marks what you've seen as read
    setTimeout(async () => {
      try {
        const ids = location.hash === '#/inbox' ? state.snapshot.notifications.items.filter((x) => !x.read).map((x) => x.id) : [];
        if (ids.length) await mutate({ type: 'read_notifications', body: { ids } });
      } finally { render.marking = false; }
    }, 1500);
  }
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

function ebaySearchUrl(query, condition) {
  const p = new URLSearchParams({ _nkw: query, LH_BIN: '1', _sop: '15' });
  if (condition === 'new') p.set('LH_ItemCondition', '1000');
  if (condition === 'used') p.set('LH_ItemCondition', '3000');
  return `https://www.ebay.com/sch/i.html?${p}`;
}

function addOfferSheet(it) {
  const { close } = openSheet(`Add a store · ${it.name}`, html`
    <button type="button" class="btn wide" id="search-pick-listing" ${state.online ? '' : 'disabled'}>${icon('search')} Search & pick a listing</button>
    <p class="small muted center">or</p>
    <label>Product link<input name="url" type="url" placeholder="https://…" autocapitalize="off"></label>
    <p class="small muted center">or</p>
    <div class="row gap">
      <label class="grow">Search eBay <span class="small muted">(cheapest listing, re-checked every time)</span><input name="ebay" placeholder="${it.name}" autocomplete="off"></label>
      <label>Condition<select name="condition"><option value="new">new</option><option value="used">used</option><option value="any">any</option></select></label>
    </div>
    <div class="row gap">
      <label class="grow">Shipping to you <span class="small muted">(optional)</span><input name="shipping" inputmode="decimal" placeholder="store default"></label>
      <label>Currency<select name="shipping_currency"><option value="">price's</option>${['ILS', 'USD', 'EUR', 'GBP'].map((c) => html`<option>${c}</option>`)}</select></label>
    </div>
    <details class="small"><summary>Advanced</summary>
      <label>Shipping to a forwarder's warehouse <span class="muted">(store's domestic shipping)</span><input name="local_shipping" inputmode="decimal" placeholder="store default"></label>
      <label>Price regex <span class="muted">(first group = price)</span><input name="regex" autocapitalize="off" spellcheck="false"></label></details>`,
  async (v) => {
    let url = (v.url || '').trim();
    const query = (v.ebay || '').trim();
    if (url && query) throw new Error('Paste a link or search eBay, not both');
    if (query) url = ebaySearchUrl(query, v.condition);
    if (!/^https?:\/\//i.test(url)) throw new Error('Paste the full product link (https://…), search eBay, or use Search & pick above');
    await mutate({ type: 'add_offer', itemId: it.id, body: { url, shipping: numOrNull(v.shipping), shipping_currency: v.shipping_currency || null, local_shipping: numOrNull(v.local_shipping), regex: v.regex || null } });
    toast(query ? 'eBay search added' : 'Store added');
  }, { submitLabel: 'Add store', extra: html`<button class="btn" type="button" id="search-all-stores" ${state.online ? '' : 'disabled'}>Search all stores</button>` });
  const pickBtn = document.getElementById('search-pick-listing');
  if (pickBtn) pickBtn.onclick = () => {
    productPickerSheet(it.name, async (picked) => {
      try {
        await mutate({ type: 'add_offer', itemId: it.id, body: { url: picked.url } });
        toast('Store added');
      } catch (e) {
        toast(`Could not add store: ${e.message}`, { timeout: 7000 });
      }
    });
  };
  const searchBtn = document.getElementById('search-all-stores');
  if (searchBtn) searchBtn.onclick = async () => {
    searchBtn.disabled = true;
    toast('Searching stores for this item…', { timeout: 3000 });
    try {
      const before = it.offers.length;
      const res = await online('POST', `api/items/${it.id}/search`, { query: it.name });
      toast(`${res.offers.length - before} store(s) added`);
      close();
    } catch (e) {
      searchBtn.disabled = false;
      searchBtn.insertAdjacentHTML('afterend', str(failedNote('Search failed', e.message)));
    }
  };
}

function forwarderSheet(preset) {
  const services = (state.snapshot.forwarders && state.snapshot.forwarders.services) || [];
  if (!services.length) { toast('Sync once to load the forwarders'); return; }
  const whOptions = (svc) => svc.warehouses.map((w) => html`<option value="${w.code}">${w.code} · ${w.location}</option>`);
  const describe = (svc) => {
    const r = svc.warehouses.map((w) => `${w.code}: ${w.rate.first} ${w.rate.currency} first ${w.rate.first_kg} kg + ${w.rate.additional}/${w.rate.step_kg} kg${w.rate.min_price ? `, min ${w.rate.min_price}` : ''}`).join(' · ');
    const checked = (svc.verified || []).length ? ` Checked against the service's terms: ${svc.verified.join('; ')}.` : '';
    return `${svc.notes ? `${svc.notes} ` : ''}Rates: ${r}.${checked}`;
  };
  const { form } = openSheet(preset ? `Set up ${services.find((f) => f.key === preset.forwarder).name}` : 'Set up a package forwarder', html`
    <label>Service<select name="forwarder" id="fwd-service" ${preset ? 'disabled' : ''}>${services.map((f) => html`<option value="${f.key}" ${preset && f.key === preset.forwarder ? 'selected' : ''}>${f.name}</option>`)}</select></label>
    <p class="small muted" id="fwd-about"></p>
    <label>Warehouse<select name="warehouse" id="fwd-wh"></select></label>
    <label id="fwd-address">Your address there <span class="small muted">(exactly as the service shows it)</span>
      <textarea name="address" rows="3" placeholder="Your Name&#10;123 Warehouse Rd #IL12345&#10;City, ST 12345" autocapitalize="off"></textarea></label>
    <div class="row gap">
      <label class="grow" id="fwd-suite">Suite / customer no.<input name="suite" autocomplete="off" autocapitalize="off"></label>
      <label class="grow">Sales tax <span class="small muted">(%)</span><input name="sales_tax" inputmode="decimal" placeholder="from address"></label>
    </div>
    <p class="small muted">Have more than one address (e.g. US and UK)? Add each warehouse separately.</p>`,
  async (v) => {
    const forwarder = preset ? preset.forwarder : v.forwarder;
    const svc = services.find((f) => f.key === forwarder);
    const address = (v.address || '').trim();
    if (svc.needs_address && !address) throw new Error(`Enter the address ${svc.name} gave you`);
    const tax = numOrNull(v.sales_tax);
    await mutate({ type: 'save_forwarder', body: { forwarder, warehouse: v.warehouse, address, suite: (v.suite || '').trim(), sales_tax: tax == null ? null : tax / 100 } });
    toast(`${svc.name} ${v.warehouse} saved`);
  });
  if (preset) form.querySelector('#fwd-service').value = preset.forwarder;
  const sel = form.querySelector('#fwd-service');
  const update = () => {
    const svc = services.find((f) => f.key === sel.value);
    form.querySelector('#fwd-wh').innerHTML = str(whOptions(svc));
    if (preset && preset.warehouse) form.querySelector('#fwd-wh').value = preset.warehouse;
    form.querySelector('#fwd-about').textContent = describe(svc);
    form.querySelector('#fwd-address').hidden = !svc.needs_address;
    form.querySelector('#fwd-suite').hidden = !svc.needs_address;
  };
  sel.onchange = update;
  update();
}

function forwardersCard() {
  const f = state.snapshot.forwarders || { accounts: [] };
  const rows = (f.accounts || []).map((a) => html`<li class="row between"><div><b>${a.forwarder_name}</b> · ${a.warehouse}${a.location ? html` <span class="muted">(${a.location})</span>` : ''}${a.pending ? html` <span class="pending-dot">pending</span>` : ''}
      <div class="small muted pre">${a.address || 'no address needed'}${a.suite ? ` · suite ${a.suite}` : ''}</div>
      ${a.effective_sales_tax ? html`<div class="small muted">sales tax ${(a.effective_sales_tax * 100).toFixed(1)}% (${a.sales_tax_source})</div>` : ''}</div>
      <button type="button" class="link danger small" data-action="remove-forwarder" data-forwarder="${a.forwarder}" data-warehouse="${a.warehouse}">remove</button></li>`);
  return html`<section class="card">
    <h3>Package forwarders</h3>
    <p class="small muted">Buying from a store that won't ship to you, or ships expensively? Add your forwarder addresses (Dealtas, RedBox, Zipy, MyUS, Shipito…) and HawkSense prices every store through them too, with their shipping, fees, sales tax and import tax rules.</p>
    ${rows.length ? html`<ul class="plain list">${rows}</ul>` : ''}
    <button class="btn wide" type="button" data-action="add-forwarder">${icon('plus')} Add a forwarder address</button>
  </section>`;
}

function editItemSheet(it) {
  const { form, close } = openSheet(`Edit · ${it.name}`, html`
    <label>Category<select name="category">${categoryOptions(it.category)}</select></label>
    <label>Target price (${currency()})<input name="target_price" inputmode="decimal" value="${it.target_price ?? ''}" placeholder="none"></label>
    <div class="row gap">
      <label class="grow">Weight (kg)<input name="weight_kg" inputmode="decimal" value="${it.weight_kg ?? ''}" placeholder="for forwarders"></label>
      <label class="grow">Box size (cm)<input name="dims" value="${it.dims ?? ''}" placeholder="30x20x10" autocapitalize="off"></label>
    </div>
    <p class="small muted">Weight and size set what package forwarders charge for shipping. Leave them empty to read them from the store pages.</p>
    <label>Picture <span class="small muted">(read automatically from the store pages; paste one to override)</span>
      <input name="image_url" type="url" value="${it.image_url ?? ''}" placeholder="https://…" autocapitalize="off"></label>
    <label class="check"><input type="checkbox" name="notify" ${it.muted ? '' : 'checked'}> Notifications about this item</label>`,
  async (v) => {
    const dims = v.dims.trim();
    if (dims && !/^\d+(\.\d+)?\s*[x×*]\s*\d+(\.\d+)?\s*[x×*]\s*\d+(\.\d+)?$/i.test(dims)) throw new Error('Box size looks like 30x20x10');
    const image_url = v.image_url.trim();
    if (image_url && !/^https?:\/\//i.test(image_url)) throw new Error('Picture link looks like https://…');
    await mutate({ type: 'update_item', itemId: it.id, body: {
      category: v.category, target_price: v.target_price.trim() === '' ? '' : numOrNull(v.target_price),
      weight_kg: v.weight_kg.trim() === '' ? '' : numOrNull(v.weight_kg), dims, image_url, muted: v.notify !== 'on' } });
  }, { extra: html`<button class="btn danger" type="button" id="del-item">Stop tracking</button>` });
  form.querySelector('#del-item').onclick = async () => {
    if (!confirm(`Stop tracking “${it.name}” and delete its price history?`)) return;
    close();
    await mutate({ type: 'delete_item', itemId: it.id });
    location.hash = '#/';
  };
}

function specsSourceSheet(it) {
  openSheet(`Cross-check size · ${it.name}`, html`
    <label>Another page with the specs <span class="small muted">(manufacturer, another shop…)</span>
      <input name="url" type="url" required placeholder="https://…" autocapitalize="off"></label>
    <p class="small muted">HawkSense reads the weight and size there and compares them with your stores.</p>`,
  async (v) => {
    const url = (v.url || '').trim();
    if (!/^https?:\/\//i.test(url)) throw new Error('Paste the full link (https://…)');
    await online('POST', `api/items/${it.id}/specs`, { url });
    toast('Page checked');
  }, { submitLabel: 'Check page' });
}

async function checkPrices(it) {
  toast('Checking store pages…', { timeout: 2500 });
  try {
    const res = await online('POST', `api/items/${it.id}/check`);
    const ok = res.results.filter((r) => r.ok).length;
    const bad = res.results.filter((r) => !r.ok);
    state.checkErrors = { ...state.checkErrors, [it.id]: bad.map((b) => ({ store: b.store, error: b.error })) };
    toast(`${ok} price${ok === 1 ? '' : 's'} updated${bad.length ? `, ${bad.length} failed` : ''}`);
    render({ force: true });
  } catch (e) {
    state.checkErrors = { ...state.checkErrors, [it.id]: [{ store: 'Check', error: e.message }] };
    toast('Check failed');
    render({ force: true });
  }
}

async function onClick(ev) {
  const el = ev.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  const it = currentItem();
  switch (action) {
    case 'sync': sync(); break;
    case 'clear-search': {
      itemSearch = '';
      const input = document.getElementById('item-search');
      if (input) input.value = '';
      filterItemCards();
      break;
    }
    case 'search-products': {
      const form = document.getElementById('add-form');
      const name = form.querySelector('[name=name]').value.trim();
      if (!name) { toast('Type an item name first'); form.querySelector('[name=name]').focus(); break; }
      productPickerSheet(name, (picked) => {
        addPicked = picked;
        document.getElementById('add-picked').innerHTML = str(pickedChip(picked, { onRemove: 'add-picked-remove' }));
      });
      break;
    }
    case 'add-picked-remove':
      addPicked = null;
      document.getElementById('add-picked').innerHTML = '';
      break;
    case 'log-price': if (it) logPriceSheet(it); break;
    case 'add-offer': if (it) addOfferSheet(it); break;
    case 'edit-item': if (it) editItemSheet(it); break;
    case 'check': if (it) checkPrices(it); break;
    case 'add-forwarder': forwarderSheet(); break;
    case 'use-forwarder': {
      const [forwarder, warehouse] = (el.dataset.route || '').split(':');
      if (forwarder && warehouse) forwarderSheet({ forwarder, warehouse });
      break;
    }
    case 'specs-source': if (it) specsSourceSheet(it); break;
    case 'specs-confirm':
      if (it) {
        const body = {};
        if (it.weight_kg != null) body.weight_kg = it.weight_kg;
        if (it.dims) body.dims = it.dims;
        await mutate({ type: 'update_item', itemId: it.id, body });
        toast('Confirmed - these values are kept even if store pages change');
      }
      break;
    case 'mark-all-read': await mutate({ type: 'read_notifications', body: {} }); break;
    case 'test-channel':
      try { const r = await online('POST', 'api/notify/test', { channel: el.dataset.channel }); toast(`${el.dataset.channel}: ${r.result}`); } catch (e) { toast(`Test failed: ${e.message}`, { timeout: 7000 }); }
      break;
    case 'device-alerts':
      try { const p = await Notification.requestPermission(); toast(p === 'granted' ? 'Alerts on this device are on' : 'Alerts were not allowed'); } catch { toast('This browser does not support alerts'); }
      render({ force: true });
      break;
    case 'rules-check':
      toast('Checking for rule updates…', { timeout: 2500 });
      try {
        const r = await online('POST', 'api/rules/check');
        toast(`${r.applied} updated, ${r.pending} to review${r.errors.length ? `, ${r.errors.length} problem(s)` : ''}`, { timeout: 6000 });
      } catch (e) { toast(`Could not check: ${e.message}`); }
      break;
    case 'store-reset': {
      const f = document.getElementById('store-form');
      const store = state.snapshot.settings.stores.find((x) => x.key === f.dataset.store);
      await mutate({ type: 'save_settings', body: { changes: Object.fromEntries(store.app.map((k) => [`stores.${store.key}.${k}`, null])) } });
      render({ force: true });
      break;
    }
    case 'source-remove':
      if (confirm(`Remove the rule source “${el.dataset.name}”?`)) await mutate({ type: 'save_settings', body: { changes: { [`rules.sources.${el.dataset.name}`]: null } } });
      break;
    case 'rule-decide':
      await mutate({ type: 'decide_rule', body: { id: Number(el.dataset.id), decision: el.dataset.decision } });
      break;
    case 'remove-forwarder':
      if (confirm('Remove this forwarder address?')) await mutate({ type: 'remove_forwarder', body: { forwarder: el.dataset.forwarder, warehouse: el.dataset.warehouse } });
      break;
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
      if (addPicked) urls.push(addPicked.url);
      const bad = urls.find((u) => !/^https?:\/\//i.test(u));
      if (bad) throw new Error(`Not a link: ${bad}`);
      const existing = state.snapshot.items.find((i) => i.name.toLowerCase() === name.toLowerCase());
      const itemId = existing ? existing.id : `tmp-${uid()}`;
      await mutate({ type: 'create_item', itemId, body: { name, category: v.category, target_price: numOrNull(v.target_price), urls, check: v.check === 'on', search_all_stores: v.search_all === 'on' } });
      form.reset();
      addPicked = null;
      lastHash = null;
      location.hash = `#/item/${itemId}`;
      if (!state.online) toast('Saved on this device. It will sync when you are back online.');
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  } else if (form.id === 'notify-form') {
    ev.preventDefault();
    delete form.dataset.dirty;
    const n = state.snapshot.notifications;
    const fd = new FormData(form);
    const subscriptions = {};
    for (const e of n.events) subscriptions[e.key] = n.channels.map((c) => c.key).filter((c) => fd.get(`sub:${e.key}:${c}`) === 'on');
    try {
      const settings = {};
      for (const k of ['price_drop_pct', 'sale_soon_days', 'check_failed_after']) {
        const v = numOrNull(fd.get(k));
        if (!v || v > 100) throw new Error(`${k.replace(/_/g, ' ')}: enter a number between 1 and 100`);
        settings[k] = v;
      }
      await mutate({ type: 'notify_prefs', body: { subscriptions, settings } });
      const off = n.channels.filter((c) => !c.configured && n.events.some((e) => subscriptions[e.key].includes(c.key)));
      toast(off.length ? `Saved. Set up ${off.map((c) => c.name).join(', ')} under Channels to receive them.` : 'Notification settings saved', { timeout: 6000 });
    } catch (e) { toast(e.message); }
  } else if (form.id === 'rules-form') {
    ev.preventDefault();
    delete form.dataset.dirty;
    const changes = {};
    try {
      for (const input of form.querySelectorAll('input[name^="rule:"]')) {
        if (input.value.trim() === String(input.dataset.orig)) continue;
        const path = input.name.slice(5);
        const v = input.value.trim() === '' ? null : numOrNull(input.value);
        changes[path] = v == null ? null : input.dataset.unit === '%' ? v / 100 : v;
      }
    } catch (e) { toast(e.message); return; }
    if (!Object.keys(changes).length) { toast('Nothing changed'); return; }
    await mutate({ type: 'set_rules', body: { changes } });
    toast(`${Object.keys(changes).length} rule(s) saved`);
  } else if (form.classList.contains('settings-form')) {
    ev.preventDefault();
    try {
      const { changes, secret } = collectSettings(form);
      if (!Object.keys(changes).length) { toast('Nothing changed'); return; }
      if (secret && !state.online) throw new Error('Connect to your HawkSense server to save passwords and keys.');
      if (secret) {
        await online('PUT', 'api/settings', { changes }); // secrets never wait in the offline queue
      } else {
        await mutate({ type: 'save_settings', body: { changes } });
      }
      delete form.dataset.dirty;
      toast('Settings saved');
      render({ force: true });
    } catch (e) { toast(e.message, { timeout: 6000 }); }
  } else if (form.id === 'store-form') {
    ev.preventDefault();
    const changes = {};
    try {
      for (const el of form.querySelectorAll('[name^="store:"]')) {
        const v = el.value.trim();
        if (v === String(el.dataset.orig)) continue;
        const path = `stores.${form.dataset.store}.${el.name.slice(6)}`;
        changes[path] = el.dataset.kind === 'bool3' ? (v === '' ? null : v === 'yes') : (v === '' ? null : numOrNull(v));
      }
    } catch (e) { toast(e.message); return; }
    if (!Object.keys(changes).length) { toast('Nothing changed'); return; }
    await mutate({ type: 'save_settings', body: { changes } });
    delete form.dataset.dirty;
    toast('Store settings saved');
    render({ force: true });
  } else if (form.id === 'source-form') {
    ev.preventDefault();
    const v = Object.fromEntries(new FormData(form));
    const name = (v.name || '').trim();
    if (!/^[\w-]{1,40}$/.test(name)) { toast('Give the source a short name (letters, digits, - or _)'); return; }
    if (!/^https?:\/\//i.test((v.url || '').trim())) { toast('Enter the page address (https://…)'); return; }
    if (!v.format && !(v.regex || '').includes('(')) { toast('The regex needs a (group) around the value'); return; }
    const src = { url: v.url.trim(), target: v.target, format: v.format || null, regex: v.format ? null : v.regex, scale: v.scale ? Number(v.scale) : null };
    await mutate({ type: 'save_settings', body: { changes: { [`rules.sources.${name}`]: src } } });
    delete form.dataset.dirty;
    toast('Source added - it is read on the next rules check');
    render({ force: true });
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
    const offerUpdate = (worker) => toast('A new version of HawkSense is ready.', {
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
on('notifications', (fresh) => {
  toast(fresh.length === 1 ? `🔔 ${fresh[0].title}` : `🔔 ${fresh.length} new notifications`, { action: 'View', onAction: () => { location.hash = '#/inbox'; } });
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  navigator.serviceWorker?.ready.then((reg) => {
    for (const n of fresh.slice(0, 3)) reg.showNotification(n.title, { body: n.body, tag: `hawksense-${n.id}`, icon: 'icons/icon-192.png', data: { item: n.item_id } });
  }).catch(() => {});
});
on('failed', ({ op, error }) => toast(`Server rejected “${describeOp(op)}”: ${error}`, { timeout: 7000 }));
window.addEventListener('hashchange', () => render());
document.addEventListener('click', onClick);
document.addEventListener('submit', onSubmit);
document.addEventListener('input', (ev) => {
  const f = ev.target.closest('#view form'); if (f && f.id !== 'add-form') f.dataset.dirty = '1';
  if (ev.target.id === 'item-search') { itemSearch = ev.target.value; filterItemCards(); }
});
document.addEventListener('change', (ev) => {
  if (ev.target.id === 'store-pick') { storesPage.selected = ev.target.value; render({ force: true }); }
  if (ev.target.id === 'source-format') {
    const table = ev.target.value === 'table';
    document.getElementById('source-regex').hidden = table;
    document.getElementById('source-scale').hidden = table;
  }
});
setInterval(() => { document.getElementById('status').innerHTML = str(statusPill()); }, 30000);

if (state.snapshot === null) {
  state.snapshot = { items: [], events: [], stores: [], meta: null, forwarders: { services: [], accounts: [] }, rules: null, notifications: null };
}
render({ force: true });
init();
registerServiceWorker();

// a thumbnail that fails to load (dead link, hotlink block) becomes the empty placeholder
document.addEventListener('error', (e) => {
  const img = e.target;
  if (img instanceof HTMLImageElement && img.classList.contains('item-thumb')) {
    img.replaceWith(Object.assign(document.createElement('div'), { className: 'item-thumb' }));
  }
}, true);
