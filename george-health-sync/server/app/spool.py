"""Upload spool: an upload is checked, written to a file in data/spool and synced to disk, then answered.
One background thread moves spooled uploads into SQLite in arrival order, several per transaction.

On a hard disk a batch insert can take seconds (random index lookups for the duplicate check, and the occasional
WAL checkpoint), so the phone no longer waits for it. If the server stops, spooled uploads are written on the
next start. Spool files are named <time>-<seq>.<kind>.<gz|json>, so sorting them gives arrival order.
"""
import itertools, json, logging, os, threading, time, zlib

from . import config, db, ingest

log = logging.getLogger('healthsync')
DIR = os.path.join(config.DATA_DIR, 'spool')
FAILED = os.path.join(DIR, 'failed')
MAX_QUEUED = int(os.environ.get('HEALTHSYNC_MAX_QUEUED', '100000'))   # records; above this, uploads get 503 + Retry-After
GROUP = 20000          # records per transaction
_seq = itertools.count()
_lock = threading.Lock()
_queued = {}           # spool file name -> record count
_results = {}          # spool file name -> result, for ?wait=1
_done = threading.Condition(_lock)
_wake = threading.Event()


class BadBody(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def decode(raw, gz):
    """Upload body -> list of records."""
    if gz:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = d.decompress(raw, config.MAX_BODY)
        except zlib.error:
            raise BadBody(400, 'body is not valid gzip')
        if d.unconsumed_tail:
            raise BadBody(413, 'body too large')
    if len(raw) > config.MAX_BODY:
        raise BadBody(413, 'body too large')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BadBody(400, 'body is not valid JSON')
    recs = body.get('records') if isinstance(body, dict) else body
    if not isinstance(recs, list):
        raise BadBody(400, 'expected {"records": [...]}')
    return recs


def queued():
    with _lock:
        return len(_queued), sum(_queued.values())


def put(kind, raw, enc):
    """Check an upload and spool it. Returns (spool name, records, response)."""
    t0 = time.monotonic()
    if enc not in ('', 'identity', 'gzip'):
        raise BadBody(415, f'unsupported Content-Encoding {enc}')
    gz = enc == 'gzip' or raw[:2] == b'\x1f\x8b'
    recs = decode(raw, gz)
    good, rejected = ingest.check(kind, recs)
    t1 = time.monotonic()
    if not good:
        return None, recs, dict(accepted=0, duplicates=0, rejected=rejected, queued=False), (t1 - t0, 0.0)
    with _lock:
        if sum(_queued.values()) + len(recs) > MAX_QUEUED:
            raise BadBody(503, 'server is busy writing earlier uploads; try again shortly')
    name = f'{time.time_ns():020d}-{next(_seq):06d}.{kind}.{"gz" if gz else "json"}'
    path = os.path.join(DIR, name)
    with open(path + '.tmp', 'wb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(path + '.tmp', path)
    dfd = os.open(DIR, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    with _lock:
        _queued[name] = len(recs)
    _wake.set()
    # accepted = records taken on; whether each was new or a duplicate is only known once written (see ?wait=1).
    return name, recs, dict(accepted=good, duplicates=0, rejected=rejected, queued=True), (t1 - t0, time.monotonic() - t1)


def wait(name, timeout=300):
    """Block until a spooled upload is in the database; returns its real {accepted, duplicates, rejected}."""
    end = time.monotonic() + timeout
    with _done:
        while name not in _results:
            left = end - time.monotonic()
            if left <= 0:
                return None
            _done.wait(left)
        return _results.pop(name)


def _files():
    return sorted(f for f in os.listdir(DIR) if f.endswith(('.gz', '.json')))


def _write_group():
    """Write the next run of spooled uploads of one kind in one transaction. Returns False if there was nothing."""
    files = _files()
    if not files:
        return False
    group, n, kind = [], 0, files[0].split('.')[1]
    for name in files:
        if name.split('.')[1] != kind or (group and n >= GROUP):
            break
        try:
            with open(os.path.join(DIR, name), 'rb') as f:
                recs = decode(f.read(), name.endswith('.gz'))
        except (OSError, BadBody) as e:
            log.error('spooled upload %s is unreadable (%s); moved to spool/failed', name, e)
            os.replace(os.path.join(DIR, name), os.path.join(FAILED, name))
            with _lock:
                _queued.pop(name, None)
            continue
        group.append((name, recs))
        n += len(recs)
    if not group:
        return True
    t0 = time.monotonic()
    results = {}
    with db.write_lock:
        con = db.writer()
        try:
            for name, recs in group:   # one call per upload so each gets its own counts
                results[name] = ingest.STORE[kind](con, recs)
            t1 = time.monotonic()
            con.commit()
        except Exception:
            con.rollback()
            raise
        t2 = time.monotonic()
        wal = config.DB_PATH + '-wal'
        if os.path.exists(wal) and os.path.getsize(wal) > 256 * 1024 * 1024:   # don't let the WAL grow without limit
            con.execute('PRAGMA wal_checkpoint(PASSIVE)')
    for name, _ in group:
        os.remove(os.path.join(DIR, name))
    acc = sum(r['accepted'] for r in results.values())
    dup = sum(r['duplicates'] for r in results.values())
    rej = sum(len(r['rejected']) for r in results.values())
    with _done:
        for name, _ in group:
            _queued.pop(name, None)
            _results[name] = results[name]
        if len(_results) > 1000:   # nobody waited for these
            for k in sorted(_results)[:-1000]:
                _results.pop(k)
        _done.notify_all()
        left = sum(_queued.values())
    log.info('wrote %s: %d records from %d uploads (%d new, %d duplicates, %d rejected) in %.2fs '
                '[insert %.2fs, commit %.2fs]; %d records still queued',
                kind, n, len(group), acc, dup, rej, t2 - t0, t1 - t0, t2 - t1, left)
    return True


def _idle_checkpoint():
    with db.write_lock:
        t = time.monotonic()
        busy, wal_pages, moved = db.writer().execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
    if wal_pages:
        log.info('checkpoint while idle: %d of %d WAL pages moved into the database in %.2fs', moved, wal_pages, time.monotonic() - t)


def _warm():
    """Read the uuid indexes once so the duplicate check doesn't seek around the disk for every record."""
    t = time.monotonic()
    con = db.writer()
    with db.write_lock:
        for table in ('samples', 'sleep_segments', 'workouts', 'ecgs'):
            con.execute(f'SELECT COUNT(*) FROM {table} INDEXED BY sqlite_autoindex_{table}_1').fetchone()
    log.info('uuid indexes read into memory in %.1fs', time.monotonic() - t)


def _run():
    try:
        _warm()
    except Exception:
        log.exception('index warm-up failed (not fatal)')
    idle_since = None
    while True:
        try:
            if _write_group():
                idle_since = None
                continue
        except Exception:
            log.exception('writing spooled uploads failed; retrying in 10s')
            time.sleep(10)
            continue
        if idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > 30:
            try:
                _idle_checkpoint()
            except Exception:
                log.exception('idle checkpoint failed')
            idle_since = float('inf')
        _wake.wait(5)
        _wake.clear()


def start():
    os.makedirs(FAILED, exist_ok=True)
    for f in os.listdir(DIR):
        if f.endswith('.tmp'):
            os.remove(os.path.join(DIR, f))   # never synced, so never answered with 200
    leftover = _files()
    for name in leftover:
        try:
            with open(os.path.join(DIR, name), 'rb') as f:
                _queued[name] = len(decode(f.read(), name.endswith('.gz')))
        except (OSError, BadBody):
            _queued[name] = 0
    if leftover:
        log.info('%d spooled uploads from before the restart will be written now', len(leftover))
    threading.Thread(target=_run, name='spool-writer', daemon=True).start()
