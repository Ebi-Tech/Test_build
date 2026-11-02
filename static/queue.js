/**
 * Durable chunk upload queue.
 *
 * Every chunk is written to IndexedDB *before* it is uploaded, so a failed or
 * interrupted upload never loses audio. Failed chunks stay queued and are
 * retried with exponential backoff (driven by app.js when the network is back).
 *
 * Kept dependency-free so it can be unit tested in Node with a fake IndexedDB.
 */

const DB_NAME = 'beyond-the-sale';
const DB_VERSION = 1;
const STORE = 'chunks';

export class ChunkQueue {
  constructor({ indexedDB, fetchImpl, endpoint = '/upload', backoffBaseMs = 1000, maxBackoffMs = 30000 } = {}) {
    this.idb = indexedDB ?? globalThis.indexedDB;
    this.fetch = fetchImpl ?? globalThis.fetch?.bind(globalThis);
    this.endpoint = endpoint;
    this.backoffBaseMs = backoffBaseMs;
    this.maxBackoffMs = maxBackoffMs;
    this._db = null;
    this._flushing = false;
    this.onChange = () => {};
  }

  async open() {
    if (this._db) return this._db;
    this._db = await new Promise((resolve, reject) => {
      const request = this.idb.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: 'key' });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return this._db;
  }

  /** Enqueue one chunk record: {session_id, chunk_index, start_timestamp, level_db, blob, shop_id, token}. */
  async add(item) {
    const db = await this.open();
    const record = {
      attempts: 0,
      queued_at: Date.now(),
      ...item,
      key: `${item.session_id}:${item.chunk_index}`,
    };
    await this._put(record);
    this.onChange(await this.count());
    return record;
  }

  async _put(record) {
    const db = await this.open();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(record);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  }

  async all() {
    const db = await this.open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly');
      const request = tx.objectStore(STORE).getAll();
      request.onsuccess = () => resolve(request.result || []);
      request.onerror = () => reject(request.error);
    });
  }

  async count() {
    return (await this.all()).length;
  }

  async remove(key) {
    const db = await this.open();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).delete(key);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  /** One upload attempt per pending chunk, oldest first. */
  async flush() {
    if (this._flushing) return { uploaded: 0, failed: 0, pending: await this.count(), busy: true };
    this._flushing = true;
    let uploaded = 0;
    let failed = 0;
    try {
      const items = (await this.all()).sort((a, b) => a.queued_at - b.queued_at || a.chunk_index - b.chunk_index);
      for (const item of items) {
        const ok = await this._upload(item);
        if (ok) {
          await this.remove(item.key);
          uploaded += 1;
        } else {
          item.attempts = (item.attempts || 0) + 1;
          item.last_attempt_at = Date.now();
          await this._put(item);
          failed += 1;
        }
      }
    } finally {
      this._flushing = false;
    }
    const pending = await this.count();
    this.onChange(pending);
    return { uploaded, failed, pending, busy: false };
  }

  async _upload(item) {
    try {
      const form = new FormData();
      form.append('shop_id', item.shop_id);
      form.append('token', item.token);
      form.append('session_id', item.session_id);
      form.append('chunk_index', String(item.chunk_index));
      form.append('start_timestamp', String(item.start_timestamp));
      if (item.level_db != null) form.append('level_db', String(item.level_db));
      form.append('audio', item.blob, `chunk-${item.chunk_index}.webm`);
      const response = await this.fetch(this.endpoint, { method: 'POST', body: form });
      // A duplicate is a success: the server already has this chunk.
      return response.ok;
    } catch (error) {
      return false;
    }
  }

  /** Delay before the next retry for a chunk that has failed `attempts` times. */
  backoffDelay(attempts) {
    const exponent = Math.max(0, attempts - 1);
    return Math.min(this.maxBackoffMs, this.backoffBaseMs * Math.pow(2, exponent));
  }
}
