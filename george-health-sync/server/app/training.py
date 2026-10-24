"""Strength-training sessions with every set (POST/GET/DELETE /api/v1/training), from the Health Sync app's workout
tracker. The finished workout also reaches /workouts as an Apple Health workout (with heart rate); this keeps the
set-by-set detail. A session is upserted by id: sending it again replaces it and all its sets. Deleting its Apple
Health workout on the phone (a /deletions record with hk_workout_uuid) deletes the session too.

Exercise ids are free-exercise-db ids (github.com/yuhonas/free-exercise-db). Estimated one-rep max (for personal
records) is Epley's: weight × (1 + reps / 30), from working and failure sets only.
"""
import json
from . import timeutil as T
from .config import HOME_TZ
from .ingest import Reject

KINDS = {'warmup', 'working', 'drop', 'failure'}
PR_KINDS = ('working', 'failure')
SESSION_KNOWN = {'id', 'started', 'ended', 'time_zone', 'name', 'plan_day', 'hk_workout_uuid', 'notes', 'exercises'}
SESSION_COLS = ['id', 'started', 'ended', 'time_zone', 'name', 'plan_day', 'hk_workout_uuid', 'notes', 'extra_json']
SET_COLS = ['session_id', 'ex_order', 'exercise_id', 'exercise_name', 'set_order', 'kind', 'weight_kg', 'reps', 'rir', 'done_at']
FIELDS = ['id', 'started', 'ended', 'local', 'time_zone', 'name', 'plan_day', 'hk_workout_uuid', 'notes', 'exercises', 'extra',
          'received_at', 'updated_at']


def _text(d, k, required=False):
    v = d.get(k)
    if v is None or v == '':
        if required:
            raise Reject(f'missing {k}')
        return None
    if not isinstance(v, str):
        raise Reject(f'{k} must be text')
    return v


def _num(d, k, whole=False, required=False, top=None):
    v = d.get(k)
    if v is None:
        if required:
            raise Reject(f'missing {k}')
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v < 0 or (top is not None and v > top):
        raise Reject(f'{k} must be a number from 0' + (f' to {top}' if top is not None else ''))
    if whole and v != int(v):
        raise Reject(f'{k} must be a whole number')
    return int(v) if whole else float(v)


def _time(d, k, required=True):
    if d.get(k) is None and not required:
        return None
    try:
        return T.norm(d.get(k))
    except (ValueError, TypeError):
        raise Reject(f'bad {k} time')


def _parse(s):
    if not isinstance(s, dict):
        raise Reject('session must be an object')
    row = dict(id=_text(s, 'id', True), started=_time(s, 'started'), ended=_time(s, 'ended', False),
               name=_text(s, 'name'), plan_day=_text(s, 'plan_day'), hk_workout_uuid=_text(s, 'hk_workout_uuid'), notes=_text(s, 'notes'))
    if row['ended'] and row['ended'] < row['started']:
        raise Reject('ended is before started')
    tz = s.get('time_zone') or HOME_TZ
    if not isinstance(tz, str) or T.zone(tz) is None:
        raise Reject('unknown time_zone')
    row['time_zone'] = tz
    exs = s.get('exercises') or []
    if not isinstance(exs, list):
        raise Reject('exercises must be a list')
    sets = []
    for i, e in enumerate(exs):
        if not isinstance(e, dict):
            raise Reject('each exercise must be an object')
        eid, ename = _text(e, 'exercise_id'), _text(e, 'name')
        if not eid and not ename:
            raise Reject(f'exercise {i}: needs exercise_id or name')
        eo = _num(e, 'order', whole=True)
        ss = e.get('sets') or []
        if not isinstance(ss, list):
            raise Reject(f'exercise {i}: sets must be a list')
        for j, st in enumerate(ss):
            if not isinstance(st, dict):
                raise Reject(f'exercise {i} set {j}: must be an object')
            kind = st.get('kind') or 'working'
            if kind not in KINDS:
                raise Reject(f'exercise {i} set {j}: kind must be warmup, working, drop or failure')
            try:
                so = _num(st, 'order', whole=True)
                sets.append(dict(session_id=row['id'], ex_order=i if eo is None else eo, exercise_id=eid, exercise_name=ename,
                                 set_order=j if so is None else so, kind=kind, weight_kg=_num(st, 'weight_kg'),
                                 reps=_num(st, 'reps', whole=True, required=True), rir=_num(st, 'rir', top=10),
                                 done_at=_time(st, 'done_at', False)))
            except Reject as e2:
                raise Reject(f'exercise {i} set {j}: {e2}')
    extra = {k: v for k, v in s.items() if k not in SESSION_KNOWN}
    row['extra_json'] = json.dumps(extra, separators=(',', ':'), ensure_ascii=False) if extra else None
    return row, sets


def store(con, items):
    """Insert or replace sessions (by id). Returns {accepted (new), updated, rejected}."""
    accepted = updated = 0
    rejected, now = [], T.now()
    for s in items:
        try:
            row, sets = _parse(s)
        except Reject as e:
            rejected.append({'id': s.get('id') if isinstance(s, dict) and isinstance(s.get('id'), str) else None, 'reason': str(e)})
            continue
        old = con.execute('SELECT received_at FROM training_sessions WHERE id = ?', (row['id'],)).fetchone()
        con.execute('DELETE FROM training_sets WHERE session_id = ?', (row['id'],))
        con.execute('DELETE FROM training_sessions WHERE id = ?', (row['id'],))
        con.execute(f'INSERT INTO training_sessions ({",".join(SESSION_COLS)}, received_at, updated_at, deleted_at) '
                    f'VALUES ({",".join("?" * (len(SESSION_COLS) + 3))})',
                    [row[c] for c in SESSION_COLS] + [old[0] if old else now, now, None])
        con.executemany(f'INSERT INTO training_sets ({",".join(SET_COLS)}) VALUES ({",".join("?" * len(SET_COLS))})',
                        [[x[c] for c in SET_COLS] for x in sets])
        if old:
            updated += 1
        else:
            accepted += 1
    return dict(accepted=accepted, updated=updated, rejected=rejected)


def delete(con, sid):
    return con.execute('UPDATE training_sessions SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL', (T.now(), sid)).rowcount > 0


def _exercises(con, sid):
    out, cur = [], None
    for r in con.execute('SELECT * FROM training_sets WHERE session_id = ? ORDER BY ex_order, set_order', (sid,)):
        if cur is None or cur['order'] != r['ex_order']:
            cur = dict(exercise_id=r['exercise_id'], name=r['exercise_name'], order=r['ex_order'], sets=[])
            out.append(cur)
        cur['sets'].append(dict(order=r['set_order'], kind=r['kind'], weight_kg=r['weight_kg'], reps=r['reps'], rir=r['rir'],
                                done_at=r['done_at']))
    return out


def as_dict(con, r):
    d = {k: r[k] for k in r.keys() if k not in ('extra_json', 'deleted_at')}
    d['extra'] = json.loads(r['extra_json']) if r['extra_json'] else None
    d['local'] = T.local(r['started'], r['time_zone']).strftime('%Y-%m-%d %H:%M')
    d['exercises'] = _exercises(con, r['id'])
    return {k: d.get(k) for k in FIELDS}


def rows(con, date_from=None, date_to=None, newest_first=True):
    where, args = ['deleted_at IS NULL'], []
    if date_from:
        where.append('started >= ?'); args.append(date_from + 'T00:00:00Z')
    if date_to:
        where.append('started <= ?'); args.append(date_to + 'T23:59:59Z')
    q = f'SELECT * FROM training_sessions WHERE {" AND ".join(where)} ORDER BY started {"DESC" if newest_first else "ASC"}'
    return [as_dict(con, r) for r in con.execute(q, args)]


def e1rm(weight, reps):
    """Epley's estimated one-rep max, or None for a set with no weight or no reps."""
    return weight * (1 + reps / 30) if weight and reps else None


def week_summary(con, start_utc, end_utc):
    """Sessions, working sets, volume and personal records for sessions that started in [start_utc, end_utc).
    A record is a working or failure set whose estimated one-rep max beats every earlier session's for that exercise
    (an exercise done for the first time sets no record)."""
    sessions = con.execute('SELECT id, started FROM training_sessions WHERE deleted_at IS NULL AND started >= ? AND started < ? '
                           'ORDER BY started', (start_utc, end_utc)).fetchall()
    if not sessions:
        return None
    ids = [s['id'] for s in sessions]
    q = ','.join('?' * len(ids))
    sets = con.execute(f'SELECT * FROM training_sets WHERE session_id IN ({q})', ids).fetchall()
    work = [x for x in sets if x['kind'] in PR_KINDS]
    volume = sum((x['weight_kg'] or 0) * x['reps'] for x in work)
    prs, best_now = [], {}
    for x in work:
        key = x['exercise_id'] or x['exercise_name']
        v = e1rm(x['weight_kg'], x['reps'])
        if v and (key not in best_now or v > best_now[key][0]):
            best_now[key] = (v, x)
    for key, (v, x) in best_now.items():
        col = 'exercise_id' if x['exercise_id'] else 'exercise_name'
        before = con.execute(f'SELECT MAX(s.weight_kg * (1 + s.reps / 30.0)) FROM training_sets s JOIN training_sessions t ON t.id = s.session_id '
                             f'WHERE t.deleted_at IS NULL AND t.started < ? AND s.{col} = ? AND s.kind IN (?, ?) AND s.weight_kg > 0 AND s.reps > 0',
                             (start_utc, key) + PR_KINDS).fetchone()[0]
        if before is not None and v > before + 1e-9:
            prs.append(dict(exercise=x['exercise_name'] or x['exercise_id'], weight_kg=x['weight_kg'], reps=x['reps'],
                            e1rm=round(v, 1), previous_e1rm=round(before, 1)))
    prs.sort(key=lambda p: -(p['e1rm'] - p['previous_e1rm']))
    return dict(sessions=len(sessions), working_sets=len(work), volume_kg=round(volume), prs=prs)
