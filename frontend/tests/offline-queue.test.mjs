// Run with: node --test frontend/tests/
// Exercises the offline stash without a browser by stubbing localStorage and navigator.
import test from 'node:test';
import assert from 'node:assert/strict';

const store = new Map();
globalThis.localStorage = {
  getItem: key => (store.has(key) ? store.get(key) : null),
  setItem: (key, value) => store.set(key, String(value)),
  removeItem: key => store.delete(key),
  clear: () => store.clear(),
};
Object.defineProperty(globalThis, 'navigator', {value: {onLine: false}, configurable: true});

const queue = await import('../src/offline-queue.js');

const counts = (overrides = {}) => ({
  point_id: 1, point_revision: 1, sample_day: '2026-11-10', slot: 'morning',
  total: 22, correct: 12, overflow: 1, ...overrides,
});

test('offline counts are stashed, not sent, and share one stable batch id', () => {
  store.clear();
  assert.equal(queue.queued('field').length, 0);
  queue.enqueue('field', counts(), 'QW-01 · 2026-11-10 · 早间');
  queue.enqueue('field', counts({slot: 'evening', total: 38, correct: 22, overflow: 3}), 'QW-01 · 2026-11-10 · 晚间');
  assert.equal(queue.queued('field').length, 2);
  const batch = localStorage.getItem('qiwu-offline-batch:field');
  queue.enqueue('field', counts({sample_day: '2026-11-11'}), 'QW-01 · 2026-11-11 · 早间');
  assert.equal(localStorage.getItem('qiwu-offline-batch:field'), batch);
});

test('sync sends the whole batch once and clears the stash', async () => {
  store.clear();
  queue.enqueue('field', counts(), 'QW-01 · 2026-11-10 · 早间');
  queue.enqueue('field', counts({slot: 'midday', total: 10, correct: 7, overflow: 0}), 'QW-01 · 2026-11-10 · 午间');
  const sent = [];
  const result = await queue.syncQueued('field', async payload => {
    sent.push(payload);
    return {summary: {booked: 1, pending: 1, returned: 0}};
  });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].records.length, 2);
  assert.deepEqual(Object.keys(sent[0].records[0]).sort(),
    ['captured_at', 'client_record_id', 'correct', 'overflow', 'point_id', 'point_revision', 'sample_day', 'slot', 'total']);
  assert.deepEqual(result, {synced: 2, summary: {booked: 1, pending: 1, returned: 0}});
  assert.equal(queue.queued('field').length, 0);
  assert.equal(localStorage.getItem('qiwu-offline-batch:field'), null);
});

test('an interrupted sync keeps the same records and batch for a retry', async () => {
  store.clear();
  queue.enqueue('field', counts(), 'QW-01 · 2026-11-10 · 早间');
  const batch = localStorage.getItem('qiwu-offline-batch:field');
  const firstIds = queue.queued('field').map(row => row.client_record_id);
  await assert.rejects(queue.syncQueued('field', async () => { throw new TypeError('Failed to fetch'); }));
  assert.equal(queue.queued('field').length, 1);
  const resent = [];
  await queue.syncQueued('field', async payload => { resent.push(payload); return {summary: {}}; });
  assert.equal(resent[0].client_batch_id, batch);
  assert.deepEqual(resent[0].records.map(row => row.client_record_id), firstIds);
});

test('observations captured during an in-flight sync stay queued', async () => {
  store.clear();
  queue.enqueue('field', counts(), 'QW-01 · 2026-11-10 · 早间');
  await queue.syncQueued('field', async () => {
    queue.enqueue('field', counts({sample_day: '2026-11-11'}), 'QW-01 · 2026-11-11 · 早间');
    return {summary: {}};
  });
  assert.equal(queue.queued('field').length, 1);
  assert.equal(queue.queued('field')[0].sample_day, '2026-11-11');
});

test('inspectors sharing a device keep separate stashes and batches', () => {
  store.clear();
  queue.enqueue('field', counts(), 'QW-01 · 2026-11-10 · 早间');
  queue.enqueue('ops', counts({point_id: 2}), 'QW-03 · 2026-11-10 · 早间');
  assert.equal(queue.queued('field').length, 1);
  assert.equal(queue.queued('ops').length, 1);
  assert.notEqual(localStorage.getItem('qiwu-offline-batch:field'), localStorage.getItem('qiwu-offline-batch:ops'));
  assert.deepEqual(queue.queued('desk'), []);
});
