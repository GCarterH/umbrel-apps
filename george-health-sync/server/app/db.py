"""SQLite schema (spec 5.3) and connection helper."""
import hashlib, logging, os, sqlite3, threading, time
from . import config

SAMPLES_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    uuid TEXT NOT NULL,
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
)"""
SAMPLE_COLUMNS = 'uuid, type, start_utc, end_utc, tz, value, unit, label, source, device, metadata_json, received_at, deleted_at'

SCHEMA = SAMPLES_TABLE.format(name='samples') + """;
CREATE INDEX IF NOT EXISTS samples_type_start ON samples (type, start_utc);

-- uuid lookups for samples go through this small table: an 8-byte key per record (a hash of the uuid) and the
-- sample's rowid, about 20 bytes per record. The text index on uuid it replaces took about 51; on the Umbrel it
-- stopped fitting in memory at about 2.5 million records, after which every insert cost a random disk read
-- (about 140 records/s).
CREATE TABLE IF NOT EXISTS sample_ids (k INTEGER PRIMARY KEY, rid INTEGER NOT NULL);
"""

SCHEMA += """
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

-- Scale weigh-ins from the Health Sync app's Weigh In (POST /api/v1/weighins): every measurement the scale gives,
-- including ones Apple Health has no field for. Percentages are 0-100. Any field not listed is kept in extra_json.
CREATE TABLE IF NOT EXISTS weighins (
    id TEXT PRIMARY KEY,
    measured_at TEXT NOT NULL,
    time_zone TEXT NOT NULL,
    weight_kg REAL,
    impedance_ohms INTEGER,
    bmi REAL,
    body_fat_pct REAL,
    lean_mass_kg REAL,
    muscle_mass_kg REAL,
    body_water_pct REAL,
    bone_mass_kg REAL,
    visceral_fat REAL,
    bmr_kcal INTEGER,
    protein_pct REAL,
    profile_json TEXT,
    scale_model TEXT,
    formula TEXT,
    source TEXT,
    app_version TEXT,
    extra_json TEXT,
    received_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS weighins_time ON weighins (measured_at);

-- Food log entries from the Health Sync app (POST /api/v1/food, food.py). id is the uuid of the entry's
-- DietaryEnergyConsumed sample, so deleting that sample on the phone deletes the entry. Any field not listed is kept
-- in extra_json.
CREATE TABLE IF NOT EXISTS food_log (
    id TEXT PRIMARY KEY,
    logged_at TEXT NOT NULL,
    time_zone TEXT NOT NULL,
    meal TEXT,
    name TEXT NOT NULL,
    brand TEXT,
    barcode TEXT,
    source TEXT,
    serving_g REAL,
    kcal REAL NOT NULL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    sat_fat_g REAL,
    sugar_g REAL,
    fibre_g REAL,
    salt_g REAL,
    extra_json TEXT,
    received_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS food_log_time ON food_log (logged_at);

-- Medication doses from the Health Sync app's dose log (POST /api/v1/doses, doses.py), e.g. tirzepatide injections.
-- id is the phone's UUID. DELETE /api/v1/doses/{id} sets deleted_at. Any field not listed is kept in extra_json.
CREATE TABLE IF NOT EXISTS doses (
    id TEXT PRIMARY KEY,
    taken_at TEXT NOT NULL,
    time_zone TEXT NOT NULL,
    drug TEXT NOT NULL,
    dose_mg REAL,
    site TEXT,
    notes TEXT,
    extra_json TEXT,
    received_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS doses_time ON doses (taken_at);

-- Strength-training sessions from the Health Sync app's workout tracker (POST /api/v1/training, training.py), with
-- every set in training_sets. A session is replaced whole when sent again; deleted_at marks a deleted one.
CREATE TABLE IF NOT EXISTS training_sessions (
    id TEXT PRIMARY KEY,
    started TEXT NOT NULL,
    ended TEXT,
    time_zone TEXT NOT NULL,
    name TEXT,
    plan_day TEXT,
    hk_workout_uuid TEXT,
    notes TEXT,
    extra_json TEXT,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS training_sessions_time ON training_sessions (started);
CREATE INDEX IF NOT EXISTS training_sessions_hk ON training_sessions (hk_workout_uuid);
CREATE TABLE IF NOT EXISTS training_sets (
    session_id TEXT NOT NULL,
    ex_order INTEGER NOT NULL,
    exercise_id TEXT,
    exercise_name TEXT,
    set_order INTEGER NOT NULL,
    kind TEXT NOT NULL,
    weight_kg REAL,
    reps INTEGER NOT NULL,
    rir REAL,
    done_at TEXT
);
CREATE INDEX IF NOT EXISTS training_sets_session ON training_sets (session_id, ex_order, set_order);
CREATE INDEX IF NOT EXISTS training_sets_exercise ON training_sets (exercise_id);

-- Reports and results kept with the health data (documents.py), e.g. the pharmacogenomic report. Never expire.
-- The file itself is in data/documents/<file_sha256><ext>.
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    kind TEXT,
    title TEXT,
    issued_on TEXT,
    provider TEXT,
    notes TEXT,
    structured_json TEXT,
    file_name TEXT,
    content_type TEXT,
    file_size INTEGER,
    file_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

-- Saved daily summary for the dashboard and /daily (dailycache.py): one row per local date, as JSON.
CREATE TABLE IF NOT EXISTS daily_cache (date TEXT PRIMARY KEY, row_json TEXT NOT NULL);
-- Days to recalculate. gen is when a day was last marked: a day is only unmarked once the numbers calculated after
-- that are saved, so it is never missing from /daily in between.
CREATE TABLE IF NOT EXISTS daily_dirty (date TEXT PRIMARY KEY, gen INTEGER NOT NULL DEFAULT 0);
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
        con = connect(cache_mb=256)   # holds sample_ids (about 20 bytes per record) with room to spare up to ~12 million records
        # No checkpoints on commit: on a hard disk they take 20-30 s. spool.py checkpoints in its own thread instead.
        con.execute('PRAGMA wal_autocheckpoint=0')
        con.execute('PRAGMA journal_size_limit=134217728')   # when the WAL restarts, trim the file back to 128 MB
        _writer['con'] = con
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


def add_meta(con, key, n):
    con.execute('INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + excluded.value',
                (key, int(n)))


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


def uid_key(uuid):
    """8-byte signed key for a sample uuid (the first 8 bytes of its BLAKE2b hash)."""
    return int.from_bytes(hashlib.blake2b(uuid.encode(), digest_size=8).digest(), 'big', signed=True)


def needs_migration(con):
    """True for databases from before 0.1.10, whose samples table has a text index on uuid."""
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_autoindex_samples_1'").fetchone() is not None


def migrate(path=None):
    """One-off rebuild of samples without the text uuid index, plus sample_ids. One transaction: if it's interrupted,
    the old table is untouched and it runs again on the next start. Rowids are kept."""
    log = logging.getLogger('healthsync')
    con = connect(path, cache_mb=160)
    con.isolation_level = None
    if not needs_migration(con):
        return
    con.create_function('uid_key', 1, uid_key, deterministic=True)
    t = time.monotonic()
    n = con.execute('SELECT MAX(rowid) FROM samples').fetchone()[0] or 0
    log.info('one-off database upgrade: rebuilding the samples table (about %d records); uploads keep queuing meanwhile', n)
    con.execute('BEGIN IMMEDIATE')
    try:
        con.execute('DROP TABLE IF EXISTS samples_new')
        con.execute(SAMPLES_TABLE.format(name='samples_new'))
        con.execute(f'INSERT INTO samples_new (rowid, {SAMPLE_COLUMNS}) SELECT rowid, {SAMPLE_COLUMNS} FROM samples ORDER BY rowid')
        t1 = time.monotonic()
        con.execute('DELETE FROM sample_ids')
        con.execute('INSERT OR IGNORE INTO sample_ids (k, rid) SELECT uid_key(uuid), rowid FROM samples_new ORDER BY 1')
        t2 = time.monotonic()
        con.execute('DROP TABLE samples')
        con.execute('ALTER TABLE samples_new RENAME TO samples')
        con.execute('CREATE INDEX samples_type_start ON samples (type, start_utc)')
        con.execute('COMMIT')
    except Exception:
        con.execute('ROLLBACK')
        raise
    finally:
        con.close()
    log.info('database upgrade done in %.0fs (copy %.0fs, ids %.0fs, index %.0fs)', time.monotonic() - t, t1 - t, t2 - t1,
             time.monotonic() - t2)


def init(path=None):
    con = connect(path)
    con.executescript(SCHEMA)
    if get_meta(con, 'stats_version') is None:
        with con:
            build_stats(con)
    con.commit()
    return con
