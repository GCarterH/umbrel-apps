"""Upload spool: an upload is checked, written to a file in data/spool and synced to disk, then answered.
One background thread moves spooled uploads into SQLite in arrival order, several per transaction.

On a hard disk a batch insert can take seconds (random index lookups for the duplicate check, and the occasional
WAL checkpoint), so the phone no longer waits for it. If the server stops, spooled uploads are written on the
next start. Spool files are named <time>-<seq>.<kind>.<gz|json>, so sorting them gives arrival order.
"""
import itertools, json, logging, os, shutil, threading, time, zlib

from . import config, db, ingest

log = logging.getLogger('healthsync')
DIR = os.path.join(config.DATA_DIR, 'spool')
FAILED = os.path.join(DIR, 'failed')
# Uploads are only refused (503 + Retry-After) when spooling one would leave less than this much free disk space.
# The spool is durable, so the phone can keep sending at full speed and the writer catches up afterwards.
MIN_FREE = int(os.environ.get('HEALTHSYNC_MIN_FREE_MB', '1024')) * 1024 * 1024
GROUP = 50000          # records per transaction: bigger transactions write each changed index page once
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
    if shutil.disk_usage(DIR).free - len(raw) < MIN_FREE:
        raise BadBody(503, 'the Umbrel is nearly out of disk space; uploads will resume once earlier ones are written')
    name = f'{time.time_ns():020d}-{next(_seq):06d}.{kind}.{len(recs)}.{"gz" if gz else "json"}'
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
    if _wal_size() > WAL_PAUSE and not _caught_up.is_set():
        # Let the checkpointer catch up; uploads queue meanwhile (503 once the queue is full). After a complete
        # checkpoint the next commit starts the WAL from the beginning, so it stays around WAL_PAUSE at most.
        _ckpt_wake.set()
        _caught_up.wait(5)
        return True
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
        _caught_up.clear()   # new WAL pages since the last checkpoint
        if _wal_size() > WAL_CHECKPOINT:
            _ckpt_wake.set()
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
    log.info('wrote %s: %d records from %d uploads (%d new, %d duplicates, %d rejected) in %.2fs = %.0f records/s '
                '[insert %.2fs, commit %.2fs]; %d records still queued',
                kind, n, len(group), acc, dup, rej, t2 - t0, n / max(t2 - t0, 1e-6), t1 - t0, t2 - t1, left)
    return True


WAL_CHECKPOINT = 128 * 1024 * 1024   # start a checkpoint once the WAL is this big (or when uploads stop)
WAL_PAUSE = 1024 * 1024 * 1024       # writer waits above this, so the WAL can't grow without limit
_last_write = [0.0]
_caught_up = threading.Event()       # set when a checkpoint has copied the whole WAL and nothing was written since
_ckpt_wake = threading.Event()


def _wal_size():
    wal = config.DB_PATH + '-wal'
    return os.path.getsize(wal) if os.path.exists(wal) else 0


def _checkpointer():
    """Copy the WAL into the database in the background, on its own connection, alongside the writer
    (SQLite allows that in WAL mode). Commits stay fast appends; each changed page is written back once per round."""
    con = db.connect()
    while True:
        _ckpt_wake.wait(5)
        _ckpt_wake.clear()
        try:
            size = _wal_size()
            idle = time.monotonic() - _last_write[0] > 20 and not _files()
            if size < WAL_CHECKPOINT and not (idle and size):
                continue
            t = time.monotonic()
            busy, frames, done = con.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            took = time.monotonic() - t
            if frames == done:
                _caught_up.set()
                if not idle:
                    continue   # all copied; the writer's next commit starts the WAL from the beginning
            if idle and frames == done:
                with db.write_lock:   # nothing is writing: reset the WAL file to zero length
                    con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            log.info('checkpoint: %d of %d WAL pages (%.0f MB) copied into the database in %.1fs%s',
                     done, frames, size / 1e6, took, ', WAL emptied' if idle and frames == done else '')
        except Exception:
            log.exception('checkpoint failed; will try again')


def _run():
    while True:
        try:
            if _write_group():
                _last_write[0] = time.monotonic()
                continue
        except Exception:
            log.exception('writing spooled uploads failed; retrying in 10s')
            time.sleep(10)
            continue
        _wake.wait(5)
        _wake.clear()


def start():
    os.makedirs(FAILED, exist_ok=True)
    for f in os.listdir(DIR):
        if f.endswith('.tmp'):
            os.remove(os.path.join(DIR, f))   # never synced, so never answered with 200
    leftover = _files()
    for name in leftover:
        parts = name.split('.')
        if len(parts) == 4 and parts[2].isdigit():   # record count is in the name, so startup doesn't read the files
            _queued[name] = int(parts[2])
        else:   # spooled by 0.1.5 or 0.1.6
            try:
                with open(os.path.join(DIR, name), 'rb') as f:
                    _queued[name] = len(decode(f.read(), name.endswith('.gz')))
            except (OSError, BadBody):
                _queued[name] = 0
    if leftover:
        log.info('%d spooled uploads from before the restart will be written now', len(leftover))
    threading.Thread(target=_run, name='spool-writer', daemon=True).start()
    threading.Thread(target=_checkpointer, name='checkpointer', daemon=True).start()
