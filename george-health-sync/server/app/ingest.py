"""Validate uploaded records and store them. Each function returns {accepted, duplicates, rejected}.

Duplicates are records whose uuid is already stored; they are skipped, so resending is always safe.
"""
import json, math
from . import timeutil as T
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


def _store(con, records, parse, table, cols, after=None):
    """Shared loop: parse each record, insert new ones, count duplicates and rejections."""
    accepted = duplicates = 0
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
    deleted = _deleted(con, seen)
    received = T.now()
    sql = f'INSERT OR IGNORE INTO {table} ({",".join(cols)}, received_at, deleted_at) VALUES ({",".join("?" * (len(cols) + 2))})'
    for row, r in rows:
        cur = con.execute(sql, [row[c] for c in cols] + [received, deleted.get(row['uuid'])])
        if cur.rowcount:
            accepted += 1
            if after:
                after(con, row, r)
        else:
            duplicates += 1
    return accepted, duplicates, rejected


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


def samples(con, records):
    """Quantity/category samples. Sleep goes to sleep_segments, everything else to samples."""
    sleep, other = [], []
    for r in records:
        (sleep if isinstance(r, dict) and r.get('type') == SLEEP else other).append(r)
    a1, d1, r1 = _store(con, other, _sample, 'samples', SAMPLE_COLS)

    def parse_sleep(r):
        row = _sample(r)
        stage = row['label']
        if not stage and row['value'] is not None:
            stage = SLEEP_STAGES.get(int(row['value']))
        if not stage:
            raise Reject('sleep record has no label')
        row['stage'] = stage
        return row
    a2, d2, r2 = _store(con, sleep, parse_sleep, 'sleep_segments', SLEEP_COLS)
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


def workouts(con, records):
    a, d, rj = _store(con, records, _workout, 'workouts', WORKOUT_COLS, after=_workout_hr)
    return dict(accepted=a, duplicates=d, rejected=rj)


ECG_COLS = ['uuid', 'time_utc', 'tz', 'classification', 'avg_hr', 'symptoms', 'source', 'device']


def _ecg(r):
    return dict(uuid=_str(r, 'uuid'), time_utc=_time(r, 'time'), tz=_tz(r), classification=_str(r, 'classification', False),
                avg_hr=_num(r, 'avg_hr'), symptoms=_str(r, 'symptoms', False), source=_str(r, 'source', False),
                device=_str(r, 'device', False))


def ecgs(con, records):
    a, d, rj = _store(con, records, _ecg, 'ecgs', ECG_COLS)
    return dict(accepted=a, duplicates=d, rejected=rj)


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
        for table in ('samples', 'sleep_segments', 'workouts', 'ecgs'):
            con.execute(f'UPDATE {table} SET deleted_at = ? WHERE uuid = ? AND deleted_at IS NULL', (now, uid))
    return dict(accepted=accepted, duplicates=duplicates, rejected=rejected)
