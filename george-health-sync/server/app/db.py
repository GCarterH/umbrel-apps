"""SQLite schema (spec 5.3) and connection helper."""
import os, sqlite3, threading
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    uuid TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    tz TEXT NOT NULL,
    value REAL,
    unit TEXT,
    label TEXT,
    source TEXT,
    device TEXT,
    metadata_json TEXT,
    received_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS samples_type_start ON samples (type, start_utc);

CREATE TABLE IF NOT EXISTS sleep_segments (
    uuid TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    tz TEXT NOT NULL,
    source TEXT,
    device TEXT,
    metadata_json TEXT,
    received_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS sleep_start ON sleep_segments (start_utc);

CREATE TABLE IF NOT EXISTS workouts (
    uuid TEXT PRIMARY KEY,
    activity_type TEXT NOT NULL,
    activity_type_raw INTEGER,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    tz TEXT NOT NULL,
    duration_s REAL,
    energy_kcal REAL,
    distance_m REAL,
    elevation_m REAL,
    indoor INTEGER,
    weather_json TEXT,
    source TEXT,
    device TEXT,
    metadata_json TEXT,
    received_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS workouts_start ON workouts (start_utc);

CREATE TABLE IF NOT EXISTS workout_hr (
    workout_uuid TEXT NOT NULL,
    t_utc TEXT NOT NULL,
    bpm REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS workout_hr_uuid ON workout_hr (workout_uuid, t_utc);

CREATE TABLE IF NOT EXISTS ecgs (
    uuid TEXT PRIMARY KEY,
    time_utc TEXT NOT NULL,
    tz TEXT NOT NULL,
    classification TEXT,
    avg_hr REAL,
    symptoms TEXT,
    source TEXT,
    device TEXT,
    received_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS ecgs_time ON ecgs (time_utc);

-- Every deletion the phone reports, so a record that arrives after its deletion is still marked deleted.
CREATE TABLE IF NOT EXISTS deletions (
    uuid TEXT PRIMARY KEY,
    type TEXT,
    deleted_at TEXT NOT NULL
);

-- Running totals per type, so /status doesn't count millions of rows. Kept up to date by ingest.py.
CREATE TABLE IF NOT EXISTS type_stats (
    type TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0,      -- not deleted
    deleted INTEGER NOT NULL DEFAULT 0,
    newest TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Names /status uses for the tables that aren't per-type samples.
SLEEP_TYPE, WORKOUT_TYPE, ECG_TYPE = 'HKCategoryTypeIdentifierSleepAnalysis', 'HKWorkoutTypeIdentifier', 'HKDataTypeIdentifierElectrocardiogram'

# One writer at a time; SQLite readers carry on alongside it (WAL mode).
write_lock = threading.Lock()
_writer = {}
_local = threading.local()


def connect(path=None, cache_mb=8):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=60, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA temp_store=MEMORY')
    con.execute(f'PRAGMA cache_size=-{cache_mb * 1024}')
    return con


def writer():
    """The one long-lived connection for writes (use it while holding write_lock). Its cache stays warm."""
    if 'con' not in _writer:
        _writer['con'] = connect(cache_mb=64)
    return _writer['con']


def reader():
    """A long-lived read connection for the current thread."""
    con = getattr(_local, 'con', None)
    if con is None:
        con = _local.con = connect()
    return con


def bump(con, typ, count=0, deleted=0, newest=None):
    con.execute('INSERT INTO type_stats (type, count, deleted, newest) VALUES (?,?,?,?) ON CONFLICT(type) DO UPDATE SET '
                'count = count + excluded.count, deleted = deleted + excluded.deleted, '
                'newest = CASE WHEN excluded.newest > IFNULL(newest, \'\') THEN excluded.newest ELSE newest END',
                (typ, count, deleted, newest))


def set_meta(con, key, value):
    con.execute('INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))


def get_meta(con, key, default=None):
    r = con.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
    return r[0] if r else default


def build_stats(con):
    """Count everything once (databases made before type_stats existed). Takes a while on a big database."""
    con.execute('DELETE FROM type_stats')
    con.execute("INSERT INTO type_stats SELECT type, SUM(deleted_at IS NULL), SUM(deleted_at IS NOT NULL), MAX(start_utc) FROM samples GROUP BY type")
    for typ, table, col in ((SLEEP_TYPE, 'sleep_segments', 'start_utc'), (WORKOUT_TYPE, 'workouts', 'start_utc'), (ECG_TYPE, 'ecgs', 'time_utc')):
        con.execute(f"INSERT INTO type_stats SELECT t, c, d, m FROM (SELECT ? t, SUM(deleted_at IS NULL) c, SUM(deleted_at IS NOT NULL) d, "
                    f"MAX({col}) m, COUNT(*) n FROM {table}) WHERE n > 0", (typ,))
    set_meta(con, 'workout_hr_points', con.execute('SELECT COUNT(*) FROM workout_hr').fetchone()[0])
    last = max((con.execute(f'SELECT MAX(received_at) FROM {t}').fetchone()[0] or '' for t in ('samples', 'sleep_segments', 'workouts', 'ecgs')))
    if last:
        set_meta(con, 'last_upload', last)
    set_meta(con, 'stats_version', 1)


def init(path=None):
    con = connect(path)
    con.executescript(SCHEMA)
    if get_meta(con, 'stats_version') is None:
        with con:
            build_stats(con)
    con.commit()
    return con
