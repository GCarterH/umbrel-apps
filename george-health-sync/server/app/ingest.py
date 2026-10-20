"""Validate uploaded records and store them. Each function returns {accepted, duplicates, rejected}.

Duplicates are records whose uuid is already stored; they are skipped, so resending is always safe.
"""
import json, math
from . import db, timeutil as T
from .config import HOME_TZ

SLEEP = 'HKCategoryTypeIdentifierSleepAnalysis'
SLEEP_STAGES = {0: 'inBed', 1: 'asleepUnspecified', 2: 'awake', 3: 'asleepCore', 4: 'asleepDeep', 5: 'asleepREM'}


class Reject(Exception):
    pass


def _str(r, k, required=True):
    v = r.get(k)
    if v is None or v == '':
        if required:
            raise Reject(f'missing {k}')
        return None
    if not isinstance(v, str):
        raise Reject(f'{k} must be text')
    return v


def _num(r, k, required=False):
    v = r.get(k)
    if v is None:
        if required:
            raise Reject(f'missing {k}')
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise Reject(f'{k} must be a number')
    return float(v)


def _time(r, k):
    try:
        return T.norm(r.get(k))
    except (ValueError, TypeError):
        raise Reject(f'bad {k} time')


def _tz(r):
    tz = r.get('tz') or HOME_TZ
    if not isinstance(tz, str) or T.zone(tz) is None:
        raise Reject('unknown tz')
    return tz


def _json(v):
    if v is None:
        return None
    if not isinstance(v, dict):
        raise Reject('metadata must be an object')
    return json.dumps(v, separators=(',', ':'), ensure_ascii=False)


def _deleted(con, uuids):
    """uuid -> deleted_at for uuids the phone has already reported as deleted."""
    out, uuids = {}, list(uuids)
    for i in range(0, len(uuids), 500):
        chunk = uuids[i:i + 500]
        q = 'SELECT uuid, deleted_at FROM deletions WHERE uuid IN (%s)' % ','.join('?' * len(chunk))
        out.update((row[0], row[1]) for row in con.execute(q, chunk))
    return out


def _existing(con, table, uuids):
    """The uuids already stored in a table (one indexed lookup per chunk, not per record)."""
    if table == 'samples':
        return _existing_samples(con, uuids)
    out, uuids = set(), list(uuids)
    for i in range(0, len(uuids), 500):
        chunk = uuids[i:i + 500]
        out.update(r[0] for r in con.execute(f'SELECT uuid FROM {table} WHERE uuid IN (%s)' % ','.join('?' * len(chunk)), chunk))
    return out


def sample_rowid(con, uuid):
    """rowid of a stored sample, via sample_ids (checked against the row, in case two uuids share a key)."""
    hit = con.execute('SELECT rid FROM sample_ids WHERE k = ?', (db.uid_key(uuid),)).fetchone()
    if hit:
        row = con.execute('SELECT uuid FROM samples WHERE rowid = ?', (hit[0],)).fetchone()
        if row and row[0] == uuid:
            return hit[0]
    return None


def _existing_samples(con, uuids):
    keys = {}
    for u in uuids:
        keys.setdefault(db.uid_key(u), []).append(u)
    out, klist = set(), list(keys)
    for i in range(0, len(klist), 500):
        chunk = klist[i:i + 500]
        for k, rid in con.execute('SELECT k, rid FROM sample_ids WHERE k IN (%s)' % ','.join('?' * len(chunk)), chunk).fetchall():
            row = con.execute('SELECT uuid FROM samples WHERE rowid = ?', (rid,)).fetchone()
            if row and row[0] in keys[k]:
                out.add(row[0])
    return out


def _store(con, records, parse, table, cols, stat_type, time_col='start_utc', after=None):
    """Shared path: parse each record, insert the new ones in one go, count duplicates and rejections,
    and keep type_stats up to date."""
    duplicates = 0
    rejected, rows, seen = [], [], set()
    for r in records:
        uid = r.get('uuid') if isinstance(r, dict) else None
        try:
            if not isinstance(r, dict):
                raise Reject('record must be an object')
            row = parse(r)
        except Reject as e:
            rejected.append({'uuid': uid if isinstance(uid, str) else None, 'reason': str(e)})
            continue
        if row['uuid'] in seen:
            duplicates += 1
            continue
        seen.add(row['uuid'])
        rows.append((row, r))
    have = _existing(con, table, seen)
    new = [(row, r) for row, r in rows if row['uuid'] not in have]
    duplicates += len(rows) - len(new)
    if new:
        deleted = _deleted(con, [row['uuid'] for row, r in new])
        received = T.now()
        sql = f'INSERT OR IGNORE INTO {table} ({",".join(cols)}, received_at, deleted_at) VALUES ({",".join("?" * (len(cols) + 2))})'
        values = [[row[c] for c in cols] + [received, deleted.get(row['uuid'])] for row, r in new]
        if table == 'samples':   # one execute per row to get each rowid for sample_ids
            ids = []
            for row_values in values:
                ids.append((db.uid_key(row_values[0]), con.execute(sql, row_values).lastrowid))
            ids.sort()
            con.executemany('INSERT OR IGNORE INTO sample_ids (k, rid) VALUES (?, ?)', ids)
        else:
            con.executemany(sql, values)
        stats = {}
        for row, r in new:
            st = stats.setdefault(stat_type(row), [0, 0, ''])
            st[1 if row['uuid'] in deleted else 0] += 1
            st[2] = max(st[2], row[time_col])
            if after:
                after(con, row, r)
        for typ, (n, d, newest) in stats.items():
            db.bump(con, typ, n, d, newest)
        db.set_meta(con, 'last_upload', received)
    return len(new), duplicates, rejected


SAMPLE_COLS = ['uuid', 'type', 'start_utc', 'end_utc', 'tz', 'value', 'unit', 'label', 'source', 'device', 'metadata_json']
SLEEP_COLS = ['uuid', 'stage', 'start_utc', 'end_utc', 'tz', 'source', 'device', 'metadata_json']


def _sample(r):
    row = dict(uuid=_str(r, 'uuid'), type=_str(r, 'type'), start_utc=_time(r, 'start'), tz=_tz(r),
               value=_num(r, 'value'), unit=_str(r, 'unit', False), label=_str(r, 'label', False),
               source=_str(r, 'source', False), device=_str(r, 'device', False), metadata_json=_json(r.get('metadata')))
    row['end_utc'] = _time(r, 'end') if r.get('end') else row['start_utc']
    if row['end_utc'] < row['start_utc']:
        raise Reject('end is before start')
    return row


def _sleep(r):
    row = _sample(r)
    stage = row['label']
    if not stage and row['value'] is not None:
        stage = SLEEP_STAGES.get(int(row['value']))
    if not stage:
        raise Reject('sleep record has no label')
    row['stage'] = stage
    return row


def samples(con, records):
    """Quantity/category samples. Sleep goes to sleep_segments, everything else to samples."""
    sleep, other = [], []
    for r in records:
        (sleep if isinstance(r, dict) and r.get('type') == SLEEP else other).append(r)
    a1, d1, r1 = _store(con, other, _sample, 'samples', SAMPLE_COLS, lambda row: row['type'])
    a2, d2, r2 = _store(con, sleep, _sleep, 'sleep_segments', SLEEP_COLS, lambda row: db.SLEEP_TYPE)
    return dict(accepted=a1 + a2, duplicates=d1 + d2, rejected=r1 + r2)


WORKOUT_COLS = ['uuid', 'activity_type', 'activity_type_raw', 'start_utc', 'end_utc', 'tz', 'duration_s', 'energy_kcal',
                'distance_m', 'elevation_m', 'indoor', 'weather_json', 'source', 'device', 'metadata_json']


def _workout(r):
    row = dict(uuid=_str(r, 'uuid'), activity_type=_str(r, 'activity_type'), start_utc=_time(r, 'start'), end_utc=_time(r, 'end'),
               tz=_tz(r), duration_s=_num(r, 'duration_s'), energy_kcal=_num(r, 'energy_kcal'), distance_m=_num(r, 'distance_m'),
               elevation_m=_num(r, 'elevation_m'), source=_str(r, 'source', False), device=_str(r, 'device', False),
               metadata_json=_json(r.get('metadata')))
    raw = _num(r, 'activity_type_raw')
    row['activity_type_raw'] = int(raw) if raw is not None else None
    ind = r.get('indoor')
    if ind is not None and not isinstance(ind, bool):
        raise Reject('indoor must be true/false')
    row['indoor'] = None if ind is None else int(ind)
    w = r.get('weather')
    if w is not None and not isinstance(w, dict):
        raise Reject('weather must be an object')
    row['weather_json'] = json.dumps(w, separators=(',', ':')) if w else None
    if row['duration_s'] is None:
        row['duration_s'] = (T.from_db(row['end_utc']) - T.from_db(row['start_utc'])).total_seconds()
    hr = r.get('hr') or []
    if not isinstance(hr, list):
        raise Reject('hr must be a list')
    pts = []
    for p in hr:
        if not isinstance(p, dict):
            raise Reject('hr points must be objects')
        pts.append((row['uuid'], _time(p, 't'), _num(p, 'bpm', True)))
    row['_hr'] = pts
    return row


def _workout_hr(con, row, r):
    if row['_hr']:
        con.executemany('INSERT INTO workout_hr (workout_uuid, t_utc, bpm) VALUES (?,?,?)', row['_hr'])
        n = int(db.get_meta(con, 'workout_hr_points', 0)) + len(row['_hr'])
        db.set_meta(con, 'workout_hr_points', n)


def workouts(con, records):
    a, d, rj = _store(con, records, _workout, 'workouts', WORKOUT_COLS, lambda row: db.WORKOUT_TYPE, after=_workout_hr)
    return dict(accepted=a, duplicates=d, rejected=rj)


ECG_COLS = ['uuid', 'time_utc', 'tz', 'classification', 'avg_hr', 'symptoms', 'source', 'device']


def _ecg(r):
    return dict(uuid=_str(r, 'uuid'), time_utc=_time(r, 'time'), tz=_tz(r), classification=_str(r, 'classification', False),
                avg_hr=_num(r, 'avg_hr'), symptoms=_str(r, 'symptoms', False), source=_str(r, 'source', False),
                device=_str(r, 'device', False))


def ecgs(con, records):
    a, d, rj = _store(con, records, _ecg, 'ecgs', ECG_COLS, lambda row: db.ECG_TYPE, time_col='time_utc')
    return dict(accepted=a, duplicates=d, rejected=rj)


DELETABLE = (('samples', 'type'), ('sleep_segments', None), ('workouts', None), ('ecgs', None))
STAT_TYPE = {'sleep_segments': db.SLEEP_TYPE, 'workouts': db.WORKOUT_TYPE, 'ecgs': db.ECG_TYPE}


def deletions(con, records):
    """Mark records deleted (rows are kept). accepted = newly recorded deletions."""
    accepted = duplicates = 0
    rejected = []
    now = T.now()
    for r in records:
        uid = r.get('uuid') if isinstance(r, dict) else None
        if not isinstance(uid, str) or not uid:
            rejected.append({'uuid': None, 'reason': 'missing uuid'})
            continue
        typ = r.get('type') if isinstance(r.get('type'), str) else None
        cur = con.execute('INSERT OR IGNORE INTO deletions (uuid, type, deleted_at) VALUES (?,?,?)', (uid, typ, now))
        if cur.rowcount:
            accepted += 1
        else:
            duplicates += 1
        for table, type_col in DELETABLE:
            if table == 'samples':
                rid = sample_rowid(con, uid)
                hit = rid and con.execute('SELECT type FROM samples WHERE rowid = ? AND deleted_at IS NULL', (rid,)).fetchone()
                if hit:
                    con.execute('UPDATE samples SET deleted_at = ? WHERE rowid = ?', (now, rid))
                    db.bump(con, hit[0], -1, 1)
                continue
            hit = con.execute(f'SELECT {type_col or "NULL"} FROM {table} WHERE uuid = ? AND deleted_at IS NULL', (uid,)).fetchone()
            if hit:
                con.execute(f'UPDATE {table} SET deleted_at = ? WHERE uuid = ?', (now, uid))
                db.bump(con, hit[0] or STAT_TYPE[table], -1, 1)
    return dict(accepted=accepted, duplicates=duplicates, rejected=rejected)


def _deletion(r):
    if not isinstance(r.get('uuid'), str) or not r.get('uuid'):
        raise Reject('missing uuid')
    return r


PARSERS = {'samples': lambda r: (_sleep if r.get('type') == SLEEP else _sample)(r), 'workouts': _workout, 'ecgs': _ecg,
           'deletions': _deletion}
STORE = {'samples': samples, 'workouts': workouts, 'ecgs': ecgs, 'deletions': deletions}


def check(kind, records):
    """Validate an upload without touching the database: (number of good records, rejected list)."""
    good, rejected = 0, []
    for r in records:
        uid = r.get('uuid') if isinstance(r, dict) else None
        try:
            if not isinstance(r, dict):
                raise Reject('record must be an object')
            PARSERS[kind](r)
            good += 1
        except Reject as e:
            rejected.append({'uuid': uid if isinstance(uid, str) else None, 'reason': str(e)})
    return good, rejected
