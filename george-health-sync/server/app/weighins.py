"""Scale weigh-ins (POST/GET /api/v1/weighins): the full set of measurements from the Health Sync app's Weigh In,
kept apart from Apple Health's samples so nothing is lost where HealthKit has no field. The app also writes weight,
BMI, body fat and lean mass to Apple Health, which arrive through /samples as usual; body.py merges the two.
"""
import json
from . import timeutil as T
from .config import HOME_TZ
from .ingest import Reject

REAL = ['weight_kg', 'bmi', 'body_fat_pct', 'lean_mass_kg', 'muscle_mass_kg', 'body_water_pct', 'bone_mass_kg', 'visceral_fat', 'protein_pct']
INT = ['impedance_ohms', 'bmr_kcal']
TEXT = ['scale_model', 'formula', 'source', 'app_version']
KNOWN = {'id', 'measured_at', 'time_zone', 'profile'} | set(REAL) | set(INT) | set(TEXT)
COLS = ['id', 'measured_at', 'time_zone'] + REAL + INT + ['profile_json'] + TEXT + ['extra_json']
FIELDS = ['id', 'measured_at', 'local', 'time_zone'] + REAL + INT + ['profile'] + TEXT + ['extra', 'received_at']


def _number(w, k, whole):
    v = w.get(k)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise Reject(f'{k} must be a number')
    return int(round(v)) if whole else float(v)


def _row(w):
    if not isinstance(w, dict):
        raise Reject('weigh-in must be an object')
    wid = w.get('id')
    if not isinstance(wid, str) or not wid:
        raise Reject('missing id')
    try:
        when = T.norm(w.get('measured_at'))
    except (ValueError, TypeError):
        raise Reject('bad measured_at time')
    tz = w.get('time_zone') or HOME_TZ
    if not isinstance(tz, str) or T.zone(tz) is None:
        raise Reject('unknown time_zone')
    row = dict(id=wid, measured_at=when, time_zone=tz)
    for k in REAL:
        row[k] = _number(w, k, False)
    for k in INT:
        row[k] = _number(w, k, True)
    if row['weight_kg'] is None:
        raise Reject('missing weight_kg')
    prof = w.get('profile')
    if prof is not None and not isinstance(prof, dict):
        raise Reject('profile must be an object')
    row['profile_json'] = json.dumps(prof, separators=(',', ':'), ensure_ascii=False) if prof else None
    for k in TEXT:
        v = w.get(k)
        row[k] = v if isinstance(v, str) else (None if v is None else str(v))
    extra = {k: v for k, v in w.items() if k not in KNOWN}
    row['extra_json'] = json.dumps(extra, separators=(',', ':'), ensure_ascii=False) if extra else None
    return row


def store(con, items):
    """Insert new weigh-ins; an id already stored counts as a duplicate. Returns {accepted, duplicates, rejected}."""
    accepted = duplicates = 0
    rejected, now = [], T.now()
    sql = f'INSERT OR IGNORE INTO weighins ({",".join(COLS)}, received_at) VALUES ({",".join("?" * (len(COLS) + 1))})'
    for w in items:
        try:
            row = _row(w)
        except Reject as e:
            rejected.append({'id': w.get('id') if isinstance(w, dict) and isinstance(w.get('id'), str) else None, 'reason': str(e)})
            continue
        if con.execute(sql, [row[c] for c in COLS] + [now]).rowcount:
            accepted += 1
        else:
            duplicates += 1
    return dict(accepted=accepted, duplicates=duplicates, rejected=rejected)


def as_dict(r):
    d = {k: r[k] for k in r.keys() if k not in ('profile_json', 'extra_json', 'deleted_at')}
    d['profile'] = json.loads(r['profile_json']) if r['profile_json'] else None
    d['extra'] = json.loads(r['extra_json']) if r['extra_json'] else None
    d['local'] = T.local(r['measured_at'], r['time_zone']).strftime('%Y-%m-%d %H:%M')
    return {k: d.get(k) for k in FIELDS}


def rows(con, date_from=None, date_to=None, newest_first=True):
    where, args = ['deleted_at IS NULL'], []
    if date_from:
        where.append('measured_at >= ?'); args.append(date_from + 'T00:00:00Z')
    if date_to:
        where.append('measured_at <= ?'); args.append(date_to + 'T23:59:59Z')
    q = f'SELECT * FROM weighins WHERE {" AND ".join(where)} ORDER BY measured_at {"DESC" if newest_first else "ASC"}'
    return [as_dict(r) for r in con.execute(q, args)]
