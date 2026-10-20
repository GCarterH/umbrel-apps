"""Saved daily summary: one row per local date, with the same columns as /daily (daily.py).

Working out a day from raw records means reading tens of thousands of samples, which is slow on the Umbrel's hard
disk once there are millions. So every write marks the dates it touches (daily_dirty), and a background thread
recomputes just those days into daily_cache. The dashboard reads daily_cache; /daily reads it too, and works out any
dates still marked live, so its answers are always current.

Bump VERSION whenever daily.py changes how a day is calculated: the next start rebuilds the whole cache.
"""
import datetime as dt, json, logging, threading, time

from . import daily as D, db

log = logging.getLogger('healthsync')
VERSION = '1'
CHUNK_DAYS = 31
_wake = threading.Event()


def mark(con, utc_times):
    """Mark the days around these UTC times for recalculation (call inside the write transaction).
    The day before and after are included: a record's local date can differ from its UTC date, and a night's
    sleep counts on the morning after."""
    dates = set()
    for t in utc_times:
        if t:
            d = dt.date(int(t[0:4]), int(t[5:7]), int(t[8:10]))
            dates.update((d - dt.timedelta(days=1), d, d + dt.timedelta(days=1)))
    if dates:
        gen = time.time_ns()
        con.executemany('INSERT INTO daily_dirty (date, gen) VALUES (?, ?) ON CONFLICT(date) DO UPDATE SET gen = excluded.gen',
                        [(d.isoformat(), gen) for d in sorted(dates)])
        _wake.set()


def _ranges(dates):
    """Sorted ISO dates -> [(first, last)] runs of consecutive days, each at most CHUNK_DAYS long."""
    out = []
    for s in dates:
        d = dt.date.fromisoformat(s)
        if out and (d - out[-1][1]).days == 1 and (d - out[-1][0]).days < CHUNK_DAYS:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a.isoformat(), b.isoformat()) for a, b in out]


def ready(con):
    return db.get_meta(con, 'daily_cache_version') == VERSION


def pending(con):
    return con.execute('SELECT COUNT(*) FROM daily_dirty').fetchone()[0]


def _start_full_build():
    """Mark every day from the first record to today (first start of 0.2.0, or after VERSION changes)."""
    con = db.writer()
    with db.write_lock:
        firsts = []
        types = [r[0] for r in con.execute('SELECT type FROM type_stats')]
        for typ in types:
            if typ in (db.SLEEP_TYPE, db.WORKOUT_TYPE, db.ECG_TYPE):
                continue
            r = con.execute('SELECT MIN(start_utc) FROM samples WHERE type = ?', (typ,)).fetchone()
            if r[0]:
                firsts.append(r[0])
        for table in ('sleep_segments', 'workouts'):
            r = con.execute(f'SELECT MIN(start_utc) FROM {table}').fetchone()
            if r[0]:
                firsts.append(r[0])
        with con:
            con.execute('DELETE FROM daily_cache')
            if firsts:
                d, end = dt.date.fromisoformat(min(firsts)[:10]) - dt.timedelta(days=1), dt.date.today() + dt.timedelta(days=1)
                days = []
                while d <= end:
                    days.append((d.isoformat(),))
                    d += dt.timedelta(days=1)
                con.executemany('INSERT OR IGNORE INTO daily_dirty (date, gen) VALUES (?, 0)', days)
            db.set_meta(con, 'daily_cache_version', VERSION)
    log.info('daily summary: building for all days (%d marked); the dashboard fills in as it goes', pending(con))


def refresh_some(max_days=400):
    """Recalculate up to max_days marked days. Returns how many were done."""
    rcon = db.reader()
    marks = rcon.execute('SELECT date, gen FROM daily_dirty ORDER BY date LIMIT ?', (max_days,)).fetchall()
    if not marks:
        return 0
    dates, gens = [m[0] for m in marks], {m[0]: m[1] for m in marks}
    t = time.monotonic()
    wcon = db.writer()
    for a, b in _ranges(dates):
        rows = D.daily(rcon, a, b)
        with db.write_lock:
            with wcon:
                wcon.execute('DELETE FROM daily_cache WHERE date BETWEEN ? AND ?', (a, b))
                wcon.executemany('INSERT OR REPLACE INTO daily_cache (date, row_json) VALUES (?, ?)',
                                 [(r['Date'], json.dumps(r, separators=(',', ':'))) for r in rows])
                # Unmark only days nobody marked again while we were calculating.
                wcon.executemany('DELETE FROM daily_dirty WHERE date = ? AND gen = ?',
                                 [(d, gens[d]) for d in dates if a <= d <= b])
    left = pending(rcon)
    log.info('daily summary: recalculated %d days in %.1fs; %d days still to do', len(dates), time.monotonic() - t, left)
    return len(dates)


def _run():
    try:
        if not ready(db.reader()):
            _start_full_build()
    except Exception:
        log.exception('daily summary: could not start the build; will retry')
    while True:
        try:
            if refresh_some():
                continue
        except Exception:
            log.exception('daily summary: recalculation failed; retrying in 30s')
            time.sleep(30)
            continue
        _wake.wait(30)
        _wake.clear()
        time.sleep(2)   # let a burst of uploads finish marking before recalculating


def start():
    threading.Thread(target=_run, name='daily-summary', daemon=True).start()


def rows(con, date_from, date_to):
    """Daily rows for date_from..date_to: saved days from the cache, any days still marked worked out now."""
    if not ready(con):
        return D.daily(con, date_from, date_to)
    # One read transaction, so the saved days and the marked days come from the same moment: otherwise a batch saved
    # and unmarked between the two reads would be in neither list.
    con.execute('BEGIN')
    try:
        out = {r[0]: json.loads(r[1]) for r in con.execute(
            'SELECT date, row_json FROM daily_cache WHERE date BETWEEN ? AND ?', (date_from, date_to))}
        dirty = [r[0] for r in con.execute('SELECT date FROM daily_dirty WHERE date BETWEEN ? AND ? ORDER BY date', (date_from, date_to))]
    finally:
        con.execute('COMMIT')
    for a, b in _ranges(dirty):
        for d in list(out):
            if a <= d <= b:
                del out[d]
        for r in D.daily(con, a, b):
            out[r['Date']] = r
    return [out[d] for d in sorted(out)]
