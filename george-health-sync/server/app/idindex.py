"""In-memory index of every record id the server holds, for POST /api/v1/have.

The phone asks "which of these uuids do you already have?" before uploading, so a re-sync doesn't re-send millions
of records. To answer 50,000 uuids in well under a second on the Umbrel's hard disk, nothing here touches the disk:

- stored: a sorted array of 8-byte keys (db.uid_key, the same key as sample_ids) for every stored sample, sleep
  segment, workout and ECG, loaded once at startup (about 8 bytes per record, 37 MB for 4.6 million), plus a set of
  keys written since then (merged into the array now and then);
- queued: keys of every upload in the spool that isn't written yet.

Keys are added only after the write commits, so a record is never reported as held before it really is. A record
that was rejected as invalid also counts as held, so the phone doesn't keep re-sending it. Two different uuids
sharing a 64-bit key is about a one-in-a-million chance across 5 million records (see ingest._existing_samples).
"""
import array, bisect, heapq, logging, threading, time

from . import db

log = logging.getLogger('healthsync')
_lock = threading.Lock()
_stored = array.array('q')   # sorted
_recent = set()
_queued = {}                 # key -> number of queued uploads holding it
_ready = threading.Event()
MERGE_AT = 300000


def ready():
    return _ready.is_set()


def _merged(arr, more):
    """Sorted array arr plus the keys in more, as a new sorted array without repeats (no big Python set)."""
    out, prev = array.array('q'), None
    for k in heapq.merge(arr, sorted(more)):
        if k != prev:
            out.append(k)
            prev = k
    return out


def _load():
    global _stored
    t = time.monotonic()
    con = db.connect(cache_mb=16)
    keys = array.array('q')
    for (k,) in con.execute('SELECT k FROM sample_ids ORDER BY k'):
        keys.append(k)
    extra = []
    for table in ('sleep_segments', 'workouts', 'ecgs'):
        extra.extend(db.uid_key(u) for (u,) in con.execute(f'SELECT uuid FROM {table}'))
    con.close()
    keys = _merged(keys, extra)
    with _lock:
        snapshot = set(_recent)
    keys = _merged(keys, snapshot)
    with _lock:
        _stored = keys
        _recent.difference_update(snapshot)
    _ready.set()
    log.info('record ids loaded for /have: %d in %.1fs', len(_stored), time.monotonic() - t)


def start(prepare=None):
    def run():
        if prepare:
            try:
                prepare()
            except Exception:
                log.exception('indexing queued uploads for /have failed')
        while True:
            try:
                _load()
                return
            except Exception:
                log.exception('loading record ids for /have failed; retrying in 30s')
                time.sleep(30)
    threading.Thread(target=run, name='id-index', daemon=True).start()


def _merge():
    """Fold _recent into the sorted array. The merge runs outside the lock; only the swap holds it."""
    global _stored
    with _lock:
        snapshot, arr = set(_recent), _stored
    merged = _merged(arr, snapshot)
    with _lock:
        _stored = merged
        _recent.difference_update(snapshot)


def add_stored(uuids):
    """Call after a write commits, with the uuids of every record in it."""
    with _lock:
        _recent.update(db.uid_key(u) for u in uuids if isinstance(u, str))
        big = len(_recent) >= MERGE_AT and _ready.is_set()
    if big:
        _merge()


def add_queued(keys):
    with _lock:
        for k in keys:
            _queued[k] = _queued.get(k, 0) + 1


def remove_queued(keys):
    with _lock:
        for k in keys:
            n = _queued.get(k, 0) - 1
            if n > 0:
                _queued[k] = n
            else:
                _queued.pop(k, None)


def keys_of(records):
    return [db.uid_key(r['uuid']) for r in records if isinstance(r, dict) and isinstance(r.get('uuid'), str) and r['uuid']]


def missing(uuids):
    """The uuids that are neither stored nor queued."""
    out = []
    with _lock:
        arr, n = _stored, len(_stored)
        for u in uuids:
            k = db.uid_key(u)
            if k in _recent or k in _queued:
                continue
            i = bisect.bisect_left(arr, k)
            if i < n and arr[i] == k:
                continue
            out.append(u)
    return out
