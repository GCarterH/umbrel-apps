"""Medication doses (POST/GET/DELETE /api/v1/doses): injections such as tirzepatide, and any other medication, from
the Health Sync app's dose log. Each dose is kept whole (drug, amount, injection site, notes); deleting one on the
phone (DELETE /api/v1/doses/{id}) marks it deleted here, and the row is kept.
"""
import json
from . import timeutil as T
from .config import HOME_TZ
from .ingest import Reject

SITES = {'abdomen-left', 'abdomen-right', 'thigh-left', 'thigh-right', 'arm-left', 'arm-right'}
TEXT = ['drug', 'site', 'notes']
KNOWN = {'id', 'taken_at', 'time_zone', 'dose_mg'} | set(TEXT)
COLS = ['id', 'taken_at', 'time_zone', 'drug', 'dose_mg', 'site', 'notes', 'extra_json']
FIELDS = ['id', 'taken_at', 'local', 'time_zone', 'drug', 'dose_mg', 'site', 'notes', 'extra', 'received_at']


def _row(d):
    if not isinstance(d, dict):
        raise Reject('dose must be an object')
    did = d.get('id')
    if not isinstance(did, str) or not did:
        raise Reject('missing id')
    try:
        when = T.norm(d.get('taken_at'))
    except (ValueError, TypeError):
        raise Reject('bad taken_at time')
    tz = d.get('time_zone') or HOME_TZ
    if not isinstance(tz, str) or T.zone(tz) is None:
        raise Reject('unknown time_zone')
    row = dict(id=did, taken_at=when, time_zone=tz)
    for k in TEXT:
        v = d.get(k)
        row[k] = v if isinstance(v, str) else (None if v is None else str(v))
    if not row['drug']:
        raise Reject('missing drug')
    if row['site'] is not None and row['site'] not in SITES:
        raise Reject('site must be abdomen-left/right, thigh-left/right, arm-left/right or null')
    mg = d.get('dose_mg')
    if mg is not None and (isinstance(mg, bool) or not isinstance(mg, (int, float)) or mg != mg or mg < 0):
        raise Reject('dose_mg must be a number, 0 or more')
    row['dose_mg'] = float(mg) if mg is not None else None
    extra = {k: v for k, v in d.items() if k not in KNOWN}
    row['extra_json'] = json.dumps(extra, separators=(',', ':'), ensure_ascii=False) if extra else None
    return row


def store(con, items):
    """Insert new doses; an id already stored counts as a duplicate. Returns {accepted, duplicates, rejected}."""
    accepted = duplicates = 0
    rejected, now = [], T.now()
    sql = f'INSERT OR IGNORE INTO doses ({",".join(COLS)}, received_at) VALUES ({",".join("?" * (len(COLS) + 1))})'
    for d in items:
        try:
            row = _row(d)
        except Reject as e:
            rejected.append({'id': d.get('id') if isinstance(d, dict) and isinstance(d.get('id'), str) else None, 'reason': str(e)})
            continue
        if con.execute(sql, [row[c] for c in COLS] + [now]).rowcount:
            accepted += 1
        else:
            duplicates += 1
    return dict(accepted=accepted, duplicates=duplicates, rejected=rejected)


def delete(con, did):
    """Mark a dose deleted. True if it was there and not already deleted."""
    return con.execute('UPDATE doses SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL', (T.now(), did)).rowcount > 0


def as_dict(r):
    d = {k: r[k] for k in r.keys() if k not in ('extra_json', 'deleted_at')}
    d['extra'] = json.loads(r['extra_json']) if r['extra_json'] else None
    d['local'] = T.local(r['taken_at'], r['time_zone']).strftime('%Y-%m-%d %H:%M')
    return {k: d.get(k) for k in FIELDS}


def rows(con, date_from=None, date_to=None, newest_first=True):
    where, args = ['deleted_at IS NULL'], []
    if date_from:
        where.append('taken_at >= ?'); args.append(date_from + 'T00:00:00Z')
    if date_to:
        where.append('taken_at <= ?'); args.append(date_to + 'T23:59:59Z')
    q = f'SELECT * FROM doses WHERE {" AND ".join(where)} ORDER BY taken_at {"DESC" if newest_first else "ASC"}'
    return [as_dict(r) for r in con.execute(q, args)]
