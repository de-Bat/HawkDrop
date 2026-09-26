// Offline-first data layer.
//
// * The last server snapshot ("base") lives in IndexedDB, so the app opens
//   instantly and fully offline.
// * Every change goes into an outbox (also in IndexedDB) and is applied
//   optimistically on top of the base snapshot. The outbox is replayed when the
//   server is reachable; price entries carry a client id, so a replay never
//   records the same price twice.
// * iOS has no Background Sync, so we sync on launch, when the app comes back
//   to the foreground, when the network returns, and every few minutes.

const DB_NAME = 'hawkdrop';
const DB_VERSION = 1;

export class NetworkError extends Error {}
export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export const state = {
  base: null,        // last snapshot from the server
  snapshot: null,    // base + pending local changes (what the UI shows)
  outbox: [],
  failed: [],        // changes the server rejected
  online: navigator.onLine,
  serverReachable: null,
  syncing: false,
  lastSync: null,
  lastError: null,
  authError: false,
  token: null,
};

const bus = new EventTarget();
export const on = (type, fn) => bus.addEventListener(type, (e) => fn(e.detail));
const emit = (type = 'change', detail = null) => bus.dispatchEvent(new CustomEvent(type, { detail }));

// ---- IndexedDB (with an in-memory fallback, e.g. very old private tabs) --------

let dbPromise;
const memory = { kv: new Map(), outbox: new Map() };
let memorySeq = 1;

function idb() {
  if (!dbPromise) {
    dbPromise = new Promise((resolve) => {
      let req;
      try { req = indexedDB.open(DB_NAME, DB_VERSION); } catch { return resolve(null); }
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('kv')) db.createObjectStore('kv');
        if (!db.objectStoreNames.contains('outbox')) db.createObjectStore('outbox', { keyPath: 'seq', autoIncrement: true });
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    });
  }
  return dbPromise;
}

async function run(store, mode, fn) {
  const db = await idb();
  if (!db) {
    const r = fn(null, memory[store]);
    return r && typeof r === 'object' && 'result' in r ? r.result : r;
  }
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, mode);
    const req = fn(tx.objectStore(store));
    tx.oncomplete = () => resolve(req && 'result' in req ? req.result : undefined);
    tx.onerror = tx.onabort = () => reject(tx.error);
  });
}

const kvGet = (key) => run('kv', 'readonly', (s, m) => (s ? s.get(key) : { result: m.get(key) }));
const kvSet = (key, value) => run('kv', 'readwrite', (s, m) => (s ? s.put(value, key) : m.set(key, value)));
const outboxAll = () => run('outbox', 'readonly', (s, m) => (s ? s.getAll() : { result: [...m.values()] }));
const outboxPut = (op) => run('outbox', 'readwrite', (s, m) => {
  if (s) return s.put(op);
  if (!op.seq) op.seq = memorySeq++;
  m.set(op.seq, op);
  return { result: op.seq };
});
const outboxDelete = (seq) => run('outbox', 'readwrite', (s, m) => (s ? s.delete(seq) : m.delete(seq)));

// ---- HTTP ------------------------------------------------------------------------------

export async function api(method, path, body, { timeout = 20000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (state.token) headers['X-HawkDrop-Token'] = state.token;
    res = await fetch(path, {
      method, headers, cache: 'no-store', signal: ctrl.signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    throw new NetworkError(err.name === 'AbortError' ? 'server did not respond' : 'server unreachable');
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || res.statusText || `HTTP ${res.status}`);
  return data;
}

// ---- local changes -------------------------------------------------------------------------

export const uid = () => (crypto.randomUUID ? crypto.randomUUID()
  : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`);

const hostOf = (url) => { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; } };

function requestFor(op) {
  switch (op.type) {
    case 'create_item': return ['POST', 'api/items', op.body];
    case 'update_item': return ['PATCH', `api/items/${op.itemId}`, op.body];
    case 'delete_item': return ['DELETE', `api/items/${op.itemId}`];
    case 'add_offer': return ['POST', `api/items/${op.itemId}/offers`, op.body];
    case 'remove_offer': return ['DELETE', `api/items/${op.itemId}/offers/${op.offerId}`];
    case 'add_price': return ['POST', `api/items/${op.itemId}/prices`, { ...op.body, client_id: op.clientId }];
    case 'save_forwarder': return ['POST', 'api/forwarders/accounts', op.body];
    case 'remove_forwarder': return ['DELETE', `api/forwarders/accounts/${encodeURIComponent(op.body.forwarder)}/${encodeURIComponent(op.body.warehouse)}`];
    case 'set_rules': return ['POST', 'api/rules/manual', op.body];
    case 'decide_rule': return ['POST', `api/rules/changes/${op.body.id}/${op.body.decision}`];
    case 'notify_prefs': return ['PUT', 'api/notify/settings', op.body];
    case 'read_notifications': return ['POST', 'api/notifications/read', op.body];
    case 'save_settings': return ['PUT', 'api/settings', op.body];
    default: throw new Error(`unknown change ${op.type}`);
  }
}

export function describeOp(op) {
  const b = op.body || {};
  switch (op.type) {
    case 'create_item': return `Track “${b.name}”`;
    case 'update_item': return 'Edit item';
    case 'delete_item': return 'Remove item';
    case 'add_offer': return `Add store ${hostOf(b.url)}`;
    case 'remove_offer': return 'Remove store';
    case 'add_price': return `Price ${b.price} ${b.currency || ''} at ${hostOf(b.store)}`;
    case 'save_forwarder': return `Set up ${b.forwarder} ${b.warehouse}`;
    case 'remove_forwarder': return `Remove ${b.forwarder} ${b.warehouse}`;
    case 'set_rules': return `Change ${Object.keys(b.changes || {}).length} rule(s)`;
    case 'decide_rule': return `${b.decision === 'accept' ? 'Accept' : 'Reject'} rule change #${b.id}`;
    case 'notify_prefs': return 'Notification settings';
    case 'read_notifications': return 'Mark notifications read';
    case 'save_settings': return `Settings: ${Object.keys(b.changes || {}).map((k) => k.split('.').slice(-2).join(' ')).join(', ')}`;
    default: return op.type;
  }
}

function emptySnapshot() {
  return {
    generated_at: null, meta: null, items: [], events: [], stores: [], forwarders: { services: [], accounts: [] },
    rules: { values: [], pending: [], history: [] },
    notifications: { items: [], unread: 0, events: [], channels: [], subscriptions: {}, settings: {} },
    settings: null,
  };
}

function applyOp(snap, op) {
  const item = snap.items.find((i) => String(i.id) === String(op.itemId));
  const b = op.body || {};
  switch (op.type) {
    case 'create_item':
      if (!item) {
        snap.items.push({
          id: op.itemId, name: b.name, category: b.category || 'default', target_price: b.target_price ?? null,
          best: null, quotes: [], history: [], event_windows: [], pending: true, pending_prices: [],
          offers: (b.urls || []).map((u) => ({ id: null, store: hostOf(u), url: u, pending: true })),
          advice: { action: 'PENDING', reasons: [], candidates: [], active_events: [] },
        });
      }
      break;
    case 'update_item':
      if (item) {
        if (b.category) item.category = b.category;
        for (const k of ['target_price', 'weight_kg', 'dims']) if (k in b) item[k] = b[k] === '' ? null : b[k];
        if ('muted' in b) item.muted = b.muted;
        if ('weight_kg' in b && item.specs) item.specs.weight_source = b.weight_kg === '' ? null : 'manual';
        item.pending = true;
      }
      break;
    case 'delete_item':
      snap.items = snap.items.filter((i) => String(i.id) !== String(op.itemId));
      break;
    case 'add_offer':
      if (item) item.offers.push({ id: null, store: hostOf(b.url), url: b.url, shipping: b.shipping ?? null, pending: true });
      break;
    case 'remove_offer':
      if (item) {
        item.offers = item.offers.filter((o) => o.id !== op.offerId);
        item.quotes = item.quotes.filter((q) => q.offer_id !== op.offerId);
      }
      break;
    case 'add_price':
      if (item) (item.pending_prices ||= []).push({ ...b, clientId: op.clientId, at: op.at });
      break;
    case 'save_forwarder':
    case 'remove_forwarder': {
      const f = (snap.forwarders ||= { services: [], accounts: [] });
      f.accounts = f.accounts.filter((a) => !(a.forwarder === b.forwarder && a.warehouse === b.warehouse));
      if (op.type === 'save_forwarder') {
        const svc = f.services.find((x) => x.key === b.forwarder);
        const wh = svc && svc.warehouses.find((w) => w.code === b.warehouse);
        f.accounts.push({ ...b, forwarder_name: svc ? svc.name : b.forwarder, location: wh ? wh.location : '', pending: true });
      }
      break;
    }
    case 'set_rules': {
      const r = (snap.rules ||= { values: [], pending: [], history: [] });
      for (const [path, value] of Object.entries(b.changes || {})) {
        const row = r.values.find((v) => v.path === path);
        if (row && value !== null) Object.assign(row, { value, from: 'manual', pending: true });
        else if (!row && value !== null) r.values.push({ path, value, from: 'manual', pending: true });
      }
      break;
    }
    case 'decide_rule':
      if (snap.rules) snap.rules.pending = snap.rules.pending.filter((c) => c.id !== b.id);
      break;
    case 'notify_prefs': {
      const n = snap.notifications;
      if (n) {
        Object.assign(n.subscriptions, b.subscriptions || {});
        Object.assign(n.settings, b.settings || {});
      }
      break;
    }
    case 'read_notifications': {
      const n = snap.notifications;
      if (n) {
        const hit = (x) => !b.ids || b.ids.includes(x.id);
        n.unread = b.ids ? Math.max(0, n.unread - n.items.filter((x) => !x.read && hit(x)).length) : 0;
        for (const x of n.items) if (hit(x)) x.read = true;
      }
      break;
    }
    case 'save_settings': {
      const st = snap.settings;
      if (!st) break;
      for (const [path, value] of Object.entries(b.changes || {})) {
        const f = st.sections.flatMap((x) => x.fields).find((x) => x.path === path);
        if (f) {
          if (f.kind === 'secret') f.is_set = value != null;
          else f.value = value;
          f.source = value == null ? 'default' : 'app';
          continue;
        }
        const m = path.match(/^stores\.([^.]+(?:\.[^.]+)*)\.(\w+)$/);
        const store = m && st.stores.find((x) => x.key === m[1]);
        if (store) {
          if (value == null) { delete store.overrides[m[2]]; store.app = store.app.filter((k) => k !== m[2]); }
          else { store.overrides[m[2]] = value; if (!store.app.includes(m[2])) store.app.push(m[2]); }
          continue;
        }
        const src = path.match(/^rules\.sources\.([\w-]+)$/);
        if (src) {
          st.rule_sources = st.rule_sources.filter((x) => x.name !== src[1]);
          if (value) st.rule_sources.push({ name: src[1], ...value, from: 'app' });
        }
      }
      break;
    }
    default:
      break;
  }
}

function derive() {
  const snap = structuredClone(state.base || emptySnapshot());
  for (const item of snap.items) item.pending_prices = [];
  for (const op of state.outbox) applyOp(snap, op);
  state.snapshot = snap;
}

export async function mutate(op) {
  op.clientId ||= uid();
  op.at = new Date().toISOString();
  if (op.type === 'delete_item' && String(op.itemId).startsWith('tmp-')) {
    // never reached the server: just drop everything queued for it
    for (const o of state.outbox.filter((x) => x.itemId === op.itemId)) await outboxDelete(o.seq);
    state.outbox = state.outbox.filter((x) => x.itemId !== op.itemId);
  } else {
    op.seq = await outboxPut(op);
    state.outbox.push(op);
  }
  derive();
  emit();
  sync();
  return op;
}

export async function discardFailed() {
  state.failed = [];
  emit();
}

export async function discardPending(seq) {
  await outboxDelete(seq);
  state.outbox = state.outbox.filter((o) => o.seq !== seq);
  derive();
  emit();
}

async function remapItemId(from, to) {
  for (const op of state.outbox) {
    if (op.itemId === from) {
      op.itemId = to;
      await outboxPut(op);
    }
  }
  emit('idmap', { from, to });
}

// ---- sync --------------------------------------------------------------------------------------

let syncPromise = null;

export function sync() {
  if (syncPromise) return syncPromise;
  state.syncing = true;
  emit();
  syncPromise = (async () => {
    try {
      while (state.outbox.length) {
        const op = state.outbox[0];
        const [method, path, body] = requestFor(op);
        try {
          const res = await api(method, path, body);
          if (op.type === 'create_item' && res && res.id != null) await remapItemId(op.itemId, res.id);
        } catch (err) {
          // network / server trouble / bad token: keep the change and retry later
          if (err instanceof NetworkError || err.status >= 500 || err.status === 401 || err.status === 429) throw err;
          state.failed.push({ op, error: err.message }); // rejected (e.g. 400/404): drop it
          emit('failed', { op, error: err.message });
        }
        await outboxDelete(op.seq);
        state.outbox.shift();
      }
      const snap = await api('GET', 'api/snapshot', undefined, { timeout: 60000 });
      const seen = new Set(((state.base && state.base.notifications && state.base.notifications.items) || []).map((n) => n.id));
      const fresh = state.base ? ((snap.notifications && snap.notifications.items) || []).filter((n) => !n.read && !seen.has(n.id)) : [];
      if (fresh.length) emit('notifications', fresh);
      state.base = snap;
      state.lastSync = Date.now();
      state.lastError = null;
      state.serverReachable = true;
      state.authError = false;
      await kvSet('base', snap);
      await kvSet('lastSync', state.lastSync);
    } catch (err) {
      state.lastError = err.message;
      state.serverReachable = !(err instanceof NetworkError);
      state.authError = err.status === 401;
    } finally {
      state.syncing = false;
      syncPromise = null;
      derive();
      emit();
    }
  })();
  return syncPromise;
}

// Actions that need the server right now (scraping, demo data).
export async function online(method, path, body) {
  const res = await api(method, path, body, { timeout: 120000 });
  await sync();
  return res;
}

export async function setToken(token) {
  state.token = token || null;
  try { localStorage.setItem('hawkdrop.token', token || ''); } catch { /* storage may be unavailable */ }
  await kvSet('token', state.token);
  return sync();
}

export async function clearLocalData() {
  state.base = null;
  state.lastSync = null;
  await kvSet('base', null);
  await kvSet('lastSync', null);
  for (const op of state.outbox) await outboxDelete(op.seq);
  state.outbox = [];
  derive();
  emit();
}

export async function init() {
  const params = new URLSearchParams(location.search);
  let saved = null;
  try { saved = localStorage.getItem('hawkdrop.token'); } catch { /* ignore */ }
  state.token = params.get('token') || (await kvGet('token')) || saved || null;
  if (params.get('token')) await kvSet('token', state.token);

  state.base = (await kvGet('base')) || null;
  state.lastSync = (await kvGet('lastSync')) || null;
  state.outbox = ((await outboxAll()) || []).sort((a, b) => a.seq - b.seq);
  derive();
  emit();

  const maybeSync = () => { if (!state.syncing) sync(); };
  window.addEventListener('online', () => { state.online = true; emit(); maybeSync(); });
  window.addEventListener('offline', () => { state.online = false; emit(); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && (!state.lastSync || Date.now() - state.lastSync > 60000)) maybeSync();
  });
  setInterval(() => { if (document.visibilityState === 'visible') maybeSync(); }, 5 * 60000);

  // ask the browser not to evict our offline data (iOS 17+, Chrome, Firefox)
  if (navigator.storage && navigator.storage.persist) {
    navigator.storage.persisted().then((p) => p || navigator.storage.persist()).catch(() => {});
  }
  sync();
}
