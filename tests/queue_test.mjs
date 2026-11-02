// Node test for the browser-side offline queue. Run from the repo root:
//   node tests/queue_test.mjs
// Uses a tiny in-memory stand-in for IndexedDB, so no browser or npm package
// is needed.

import assert from 'node:assert';
import { ChunkQueue } from '../static/queue.js';

/* --------------------------------------------------- fake IndexedDB API */
function makeFakeIndexedDB() {
  const stores = new Map(); // storeName -> Map(key -> record)

  return {
    open(_name, _version) {
      const request = { result: null, error: null, onsuccess: null, onerror: null, onupgradeneeded: null };
      queueMicrotask(() => {
        const db = {
          objectStoreNames: { contains: (name) => stores.has(name) },
          createObjectStore: (name) => stores.set(name, new Map()),
          transaction(storeName) {
            const data = stores.get(storeName) || new Map();
            stores.set(storeName, data);
            const pending = [];
            const tx = { oncomplete: null, onerror: null, onabort: null, error: null };
            tx.objectStore = () => ({
              put: (record) => pending.push(() => data.set(record.key, Object.assign({}, record))),
              delete: (key) => pending.push(() => data.delete(key)),
              getAll: () => {
                const result = { result: [...data.values()], onsuccess: null, onerror: null };
                queueMicrotask(() => result.onsuccess && result.onsuccess());
                return result;
              },
            });
            queueMicrotask(() => {
              for (const op of pending) op();
              if (tx.oncomplete) tx.oncomplete();
            });
            return tx;
          },
        };
        request.result = db;
        if (request.onupgradeneeded) request.onupgradeneeded();
        if (request.onsuccess) request.onsuccess();
      });
      return request;
    },
  };
}

/* ------------------------------------------------------------------ test */

const fakeIDB = makeFakeIndexedDB();
let calls = 0;
const delivered = [];

const fetchImpl = async (_url, options) => {
  calls += 1;
  if (calls <= 2) throw new TypeError('network down'); // simulate offline
  const form = options.body;
  delivered.push(form.get('chunk_index'));
  return { ok: true, status: 200 };
};

const makeQueue = () =>
  new ChunkQueue({ indexedDB: fakeIDB, fetchImpl, backoffBaseMs: 10, maxBackoffMs: 50 });

const queue = makeQueue();
await queue.add({
  shop_id: 'demo-shop',
  token: 'tok',
  session_id: 'sess-1',
  chunk_index: 0,
  start_timestamp: 0,
  level_db: -22.5,
  blob: new Blob(['fake-opus-bytes']),
});

let result = await queue.flush();
assert.strictEqual(result.failed, 1, 'first attempt fails while offline');
assert.strictEqual(await queue.count(), 1, 'chunk is still queued after a failure');

// A reload must not lose the chunk: a fresh instance reads the same store.
const reopened = makeQueue();
assert.strictEqual(await reopened.count(), 1, 'queued chunk survives a new page/queue instance');

result = await reopened.flush();
assert.strictEqual(result.failed, 1, 'second attempt still offline');
assert.strictEqual((await reopened.all())[0].attempts, 2, 'attempt counter is persisted');

// Backoff grows with attempts and is capped.
assert.strictEqual(reopened.backoffDelay(1), 10);
assert.strictEqual(reopened.backoffDelay(4), 50);

// Network is back: the same chunk is uploaded once and removed from the queue.
result = await reopened.flush();
assert.strictEqual(result.uploaded, 1, 'chunk uploads when the network returns');
assert.strictEqual(await reopened.count(), 0, 'queue is empty after a successful upload');
assert.deepStrictEqual(delivered, ['0'], 'each chunk is delivered exactly once');

// A refused upload (bad token) must keep the chunk too.
const rejecting = new ChunkQueue({
  indexedDB: fakeIDB,
  fetchImpl: async () => ({ ok: false, status: 403 }),
});
await rejecting.add({
  shop_id: 'demo-shop',
  token: 'bad',
  session_id: 'sess-2',
  chunk_index: 1,
  start_timestamp: 30000,
  level_db: -30,
  blob: new Blob(['x']),
});
result = await rejecting.flush();
assert.strictEqual(result.failed, 1, 'a 403 keeps the chunk queued');
assert.strictEqual(await rejecting.count(), 1, 'the refused chunk is still in storage');
assert.strictEqual((await rejecting.all())[0].session_id, 'sess-2');

console.log('ok - offline queue retry (persist, backoff, upload once, keep on 403)');
