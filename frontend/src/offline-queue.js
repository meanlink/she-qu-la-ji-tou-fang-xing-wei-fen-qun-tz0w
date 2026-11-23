const queueKey = owner => `qiwu-offline-queue:${owner}`;
const batchKey = owner => `qiwu-offline-batch:${owner}`;
const pointsKey = 'qiwu-points-cache';

const read = (key, fallback) => {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
};

const uid = prefix => `${prefix}-${crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;

export const isOffline = () => navigator.onLine === false;

export const capturedAt = () => new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', dateStyle: 'short', timeStyle: 'medium',
}).format(new Date()).replace(' ', 'T');

// Records are owned by the signed-in inspector so a shared device cannot repost another's counts.
export const queued = owner => owner ? read(queueKey(owner), []) : [];
export const hasQueued = owner => queued(owner).length > 0;

// Last known point list, so a reload while offline can still offer the inspector's points.
export const cachedPoints = () => read(pointsKey, []);
export const rememberPoints = points => {
  if (points?.length) localStorage.setItem(pointsKey, JSON.stringify(points));
};

function batchId(owner, create = false) {
  let id = localStorage.getItem(batchKey(owner));
  if (!id && create) {
    id = uid('batch');
    localStorage.setItem(batchKey(owner), id);
  }
  return id;
}

export function enqueue(owner, record, label) {
  const rows = queued(owner);
  rows.push({...record, client_record_id: uid('window'), label, captured_at: capturedAt()});
  localStorage.setItem(queueKey(owner), JSON.stringify(rows));
  batchId(owner, true);
  return rows.length;
}

export async function syncQueued(owner, send) {
  const rows = queued(owner);
  if (!rows.length) return {synced: 0, summary: null};
  const result = await send({
    client_batch_id: batchId(owner, true),
    records: rows.map(({label, ...record}) => record),
  });
  // Remove only what was sent: observations captured while the request was in flight stay queued.
  const sent = new Set(rows.map(row => row.client_record_id));
  const remaining = queued(owner).filter(row => !sent.has(row.client_record_id));
  if (remaining.length) localStorage.setItem(queueKey(owner), JSON.stringify(remaining));
  else {
    localStorage.removeItem(queueKey(owner));
    localStorage.removeItem(batchKey(owner));
  }
  return {synced: rows.length, summary: result.summary};
}
