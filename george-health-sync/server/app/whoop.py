"""WHOOP strap days (POST/GET /api/v1/whoop/daily), sent by the Health Sync app: one row per local day woken on,
with the night's sleep, resting heart rate, HRV, breathing rate, the day's strain level and the plain sentence that
explains it, and any illness warning. A resend for the same day replaces the row (today's strain fills in during the
day). Plain figures only: there are no recovery or sleep scores, here or on the dashboard.
"""
import json, re
from . import timeutil as T
from .ingest import Reject

STRAIN_LEVELS = {'light', 'moderate', 'hard', 'veryHard'}
MINUTES = ['asleep_min', 'in_bed_min', 'deep_min', 'rem_min', 'light_min', 'nap_min']
REAL = MINUTES + ['hrv_rmssd_ms', 'hrv_sdnn_ms', 'breathing_rate', 'strain_load']
INT = ['resting_hr', 'high_hr_min', 'lift_sets']
KNOWN = {'day', 'time_zone', 'sleep_start', 'sleep_end', 'sleep_estimated', 'strain_level', 'strain_because', 'illness_warning',
         'illness_signs'} | set(REAL) | set(INT)
COLS = ['day', 'time_zone', 'sleep_start', 'sleep_end'] + MINUTES + ['sleep_estimated', 'resting_hr', 'hrv_rmssd_ms', 'hrv_sdnn_ms',
        'breathing_rate', 'strain_level', 'strain_because', 'strain_load', 'high_hr_min', 'lift_sets', 'illness_warning',
        'illness_signs_json', 'extra_json']
FIELDS = [c for c in COLS if not c.endswith('_json')] + ['illness_signs', 'extra', 'received_at', 'updated_at']


def _num(d, k, whole=False):
    v = d.get(k)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
        raise Reject(f'{k} must be a number, 0 or more')
    if whole and v != int(v):
        raise Reject(f'{k} must be a whole number')
    return int(v) if whole else float(v)


def _row(d):
    if not isinstance(d, dict):
        raise Reject('day must be an object')
    day = d.get('day')
    if not isinstance(day, str) or not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
        raise Reject('day must be YYYY-MM-DD')
    tz = d.get('time_zone')
    if not isinstance(tz, str) or T.zone(tz) is None:
        raise Reject('time_zone must be an IANA time zone')
    if not isinstance(d.get('illness_warning'), bool):
        raise Reject('illness_warning must be true or false')
    row = dict(day=day, time_zone=tz, illness_warning=int(d['illness_warning']))
    for k in ('sleep_start', 'sleep_end'):
        try:
            row[k] = T.norm(d[k]) if d.get(k) is not None else None
        except (ValueError, TypeError):
            raise Reject(f'bad {k} time')
    if row['sleep_start'] and row['sleep_end'] and row['sleep_end'] < row['sleep_start']:
        raise Reject('sleep_end is before sleep_start')
    for k in REAL:
        row[k] = _num(d, k)
    for k in INT:
        row[k] = _num(d, k, whole=True)
    est = d.get('sleep_estimated')
    if est is not None and not isinstance(est, bool):
        raise Reject('sleep_estimated must be true or false')
    row['sleep_estimated'] = None if est is None else int(est)
    lvl = d.get('strain_level')
    if lvl is not None and lvl not in STRAIN_LEVELS:
        raise Reject('strain_level must be light, moderate, hard or veryHard')
    row['strain_level'] = lvl
    why = d.get('strain_because')
    if why is not None and not isinstance(why, str):
        raise Reject('strain_because must be text')
    row['strain_because'] = why
    signs = d.get('illness_signs')
    if signs is not None and (not isinstance(signs, list) or not all(isinstance(x, str) for x in signs)):
        raise Reject('illness_signs must be a list of text')
    row['illness_signs_json'] = json.dumps(signs, ensure_ascii=False) if signs else None
    extra = {k: v for k, v in d.items() if k not in KNOWN}
    row['extra_json'] = json.dumps(extra, separators=(',', ':'), ensure_ascii=False) if extra else None
    return row


def store(con, items):
    """Insert or replace days (by day). Returns {accepted (new), updated, rejected: [{day, reason}]}."""
    accepted = updated = 0
    rejected, now = [], T.now()
    for d in items:
        try:
            row = _row(d)
        except Reject as e:
            rejected.append({'day': d.get('day') if isinstance(d, dict) and isinstance(d.get('day'), str) else None, 'reason': str(e)})
            continue
        old = con.execute('SELECT received_at FROM whoop_days WHERE day = ?', (row['day'],)).fetchone()
        con.execute(f'INSERT OR REPLACE INTO whoop_days ({",".join(COLS)}, received_at, updated_at) VALUES ({",".join("?" * (len(COLS) + 2))})',
                    [row[c] for c in COLS] + [old[0] if old else now, now])
        if old:
            updated += 1
        else:
            accepted += 1
    return dict(accepted=accepted, updated=updated, rejected=rejected)


def as_dict(r):
    d = {k: r[k] for k in r.keys()}
    for k in ('sleep_estimated', 'illness_warning'):
        d[k] = None if d[k] is None else bool(d[k])
    d['illness_signs'] = json.loads(r['illness_signs_json']) if r['illness_signs_json'] else []
    d['extra'] = json.loads(r['extra_json']) if r['extra_json'] else None
    return {k: d.get(k) for k in FIELDS}


def rows(con, date_from=None, date_to=None, newest_first=True):
    where, args = ['1 = 1'], []
    if date_from:
        where.append('day >= ?'); args.append(date_from)
    if date_to:
        where.append('day <= ?'); args.append(date_to)
    q = f'SELECT * FROM whoop_days WHERE {" AND ".join(where)} ORDER BY day {"DESC" if newest_first else "ASC"}'
    return [as_dict(r) for r in con.execute(q, args)]


def on_day(con, ds):
    r = con.execute('SELECT * FROM whoop_days WHERE day = ?', (ds,)).fetchone()
    return as_dict(r) if r else None
