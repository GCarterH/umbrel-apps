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
"""

# One writer at a time; SQLite readers carry on alongside it (WAL mode).
write_lock = threading.Lock()


def connect(path=None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.execute('PRAGMA foreign_keys=ON')
    return con


def init(path=None):
    con = connect(path)
    con.executescript(SCHEMA)
    con.commit()
    return con
