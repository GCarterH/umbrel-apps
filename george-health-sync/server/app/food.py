"""Food log (POST/GET /api/v1/food): each entry from the Health Sync app's food log, kept whole (meal, name, brand,
barcode, where the numbers came from, serving and macros). The app also writes each entry to Apple Health as a food
correlation of Dietary samples, which arrive through /samples as usual and feed the daily totals. An entry's id
is the uuid of its DietaryEnergyConsumed sample, so when the phone reports that sample deleted (Undo or delete), the
entry is deleted here too (ingest.deletions), as weigh-ins are through their BodyMass sample.
"""
import datetime as dt, json
from . import db, timeutil as T
from .config import HOME_TZ
from .ingest import Reject

MEALS = {'breakfast', 'lunch', 'dinner', 'snack'}
SOURCES = {'openfoodfacts', 'cofid', 'photo', 'label', 'quick'}
REAL = ['serving_g', 'kcal', 'protein_g', 'carbs_g', 'fat_g', 'sat_fat_g', 'sugar_g', 'fibre_g', 'salt_g']
TEXT = ['meal', 'name', 'brand', 'barcode', 'source']
KNOWN = {'id', 'logged_at', 'time_zone'} | set(REAL) | set(TEXT)
COLS = ['id', 'logged_at', 'time_zone'] + TEXT + REAL + ['extra_json']
FIELDS = ['id', 'logged_at', 'local', 'time_zone'] + TEXT + REAL + ['extra', 'received_at']


def _number(f, k):
    v = f.get(k)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0:
        raise Reject(f'{k} must be a number, 0 or more')
    return float(v)


def _row(f):
    if not isinstance(f, dict):
        raise Reject('food entry must be an object')
    fid = f.get('id')
    if not isinstance(fid, str) or not fid:
        raise Reject('missing id')
    try:
        when = T.norm(f.get('logged_at'))
    except (ValueError, TypeError):
        raise Reject('bad logged_at time')
    tz = f.get('time_zone') or HOME_TZ
    if not isinstance(tz, str) or T.zone(tz) is None:
        raise Reject('unknown time_zone')
    row = dict(id=fid, logged_at=when, time_zone=tz)
    for k in TEXT:
        v = f.get(k)
        row[k] = v if isinstance(v, str) else (None if v is None else str(v))
    if not row['name']:
        raise Reject('missing name')
    if row['meal'] is not None and row['meal'] not in MEALS:
        raise Reject('meal must be breakfast, lunch, dinner or snack')
    if row['source'] is not None and row['source'] not in SOURCES:
        raise Reject('source must be openfoodfacts, cofid, photo, label or quick')
    for k in REAL:
        row[k] = _number(f, k)
    if row['kcal'] is None:
        raise Reject('missing kcal')
    extra = {k: v for k, v in f.items() if k not in KNOWN}
    row['extra_json'] = json.dumps(extra, separators=(',', ':'), ensure_ascii=False) if extra else None
    return row


def store(con, items):
    """Insert new entries; an id already stored counts as a duplicate. Returns {accepted, duplicates, rejected}.
    An entry deleted on the phone before it got here stays deleted (its id is in deletions)."""
    accepted = duplicates = 0
    rejected, now = [], T.now()
    sql = f'INSERT OR IGNORE INTO food_log ({",".join(COLS)}, received_at, deleted_at) VALUES ({",".join("?" * (len(COLS) + 2))})'
    for f in items:
        try:
            row = _row(f)
        except Reject as e:
            rejected.append({'id': f.get('id') if isinstance(f, dict) and isinstance(f.get('id'), str) else None, 'reason': str(e)})
            continue
        gone = con.execute('SELECT deleted_at FROM deletions WHERE uuid = ?', (row['id'],)).fetchone()
        if con.execute(sql, [row[c] for c in COLS] + [now, gone[0] if gone else None]).rowcount:
            accepted += 1
        else:
            duplicates += 1
    return dict(accepted=accepted, duplicates=duplicates, rejected=rejected)


def as_dict(r):
    d = {k: r[k] for k in r.keys() if k not in ('extra_json', 'deleted_at')}
    d['extra'] = json.loads(r['extra_json']) if r['extra_json'] else None
    d['local'] = T.local(r['logged_at'], r['time_zone']).strftime('%Y-%m-%d %H:%M')
    return {k: d.get(k) for k in FIELDS}


def rows(con, date_from=None, date_to=None, newest_first=True):
    where, args = ['deleted_at IS NULL'], []
    if date_from:
        where.append('logged_at >= ?'); args.append(date_from + 'T00:00:00Z')
    if date_to:
        where.append('logged_at <= ?'); args.append(date_to + 'T23:59:59Z')
    q = f'SELECT * FROM food_log WHERE {" AND ".join(where)} ORDER BY logged_at {"DESC" if newest_first else "ASC"}'
    return [as_dict(r) for r in con.execute(q, args)]


def on_day(con, ds):
    """The entries logged on local date ds (by each entry's own zone), in time order."""
    return [f for f in rows(con, _shift(ds, -1), _shift(ds, 1), newest_first=False) if f['local'][:10] == ds]


def _shift(ds, n):
    return (dt.date.fromisoformat(ds) + dt.timedelta(days=n)).isoformat()


# The current daily target, worked out on the phone (PUT /api/v1/food/target). Kept in meta as JSON.
TARGET_KEYS = ['kcal', 'protein_g', 'carbs_g', 'fat_g', 'fibre_g']


def target(con):
    v = db.get_meta(con, 'food_target')
    return json.loads(v) if v else None


def set_target(con, body):
    if not isinstance(body, dict):
        raise Reject('expected a JSON object')
    t = {}
    for k in TARGET_KEYS:
        t[k] = _number(body, k)
    if t['kcal'] is None:
        raise Reject('missing kcal')
    extra = {k: v for k, v in body.items() if k not in TARGET_KEYS}
    if extra:
        t['extra'] = extra
    t['updated_at'] = T.now()
    db.set_meta(con, 'food_target', json.dumps(t, separators=(',', ':'), ensure_ascii=False))
    return t
