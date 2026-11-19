"""Phone scan accuracy estimates, isolated from health samples and derived metrics."""
import datetime as dt
import json
import math
from uuid import UUID

from . import timeutil as T
from .ingest import Reject

KINDS = ('camera_vitals', 'pulse_mirror')
TOLERANCES = dict(heart_rate=5, hrv_sdnn=10, hrv_rmssd=10, respiratory_rate=2)


def _number(value, field, whole=False):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                              or not math.isfinite(value) or (whole and not isinstance(value, int))):
        raise Reject(f'{field} must be a finite {"integer" if whole else "number"} or null')


def _validate(item):
    if not isinstance(item, dict):
        raise Reject('item must be an object')
    try:
        UUID(item.get('id'))
    except (ValueError, TypeError, AttributeError):
        raise Reject('id must be a UUID string')
    if item.get('kind') not in KINDS:
        raise Reject('bad kind')
    try:
        start, end = (T.parse_utc(item.get(k)) for k in ('start', 'end'))
    except (ValueError, TypeError, OverflowError):
        raise Reject('bad start or end timestamp')
    if end < start:
        raise Reject('end must not precede start')
    for source, fields in (('phone', tuple(TOLERANCES)), ('strap', ('heart_rate', 'hrv_sdnn', 'hrv_rmssd', 'beats')),
                           ('watch', ('heart_rate', 'hrv_sdnn', 'respiratory_rate', 'samples'))):
        values = item.get(source)
        if values is None and source != 'phone':
            continue
        if not isinstance(values, dict):
            raise Reject(f'{source} must be an object' + (' or null' if source != 'phone' else ''))
        for field in fields:
            _number(values.get(field), f'{source}.{field}', field in ('beats', 'samples'))
    for field, value in (('confidence', item['phone'].get('confidence')), ('app_version', item.get('app_version'))):
        if value is not None and not isinstance(value, str):
            raise Reject(f'{field} must be a string or null')
    pairs = item.get('pairs', [])
    if not isinstance(pairs, list):
        raise Reject('pairs must be an array')
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get('t') is None:
            raise Reject('pair must be an object with t')
        for field in ('t', 'phone', 'strap'):
            _number(pair.get(field), f'pairs.{field}')
    return T.fmt(start), T.fmt(end)


def store(con, items):
    stored, rejected = 0, []
    for item in items:
        try:
            start, end = _validate(item)
            payload = json.dumps(item, separators=(',', ':'), allow_nan=False)
        except (Reject, ValueError, TypeError, OverflowError) as e:
            rejected.append(dict(id=item.get('id') if isinstance(item, dict) else None, reason=str(e)))
            continue
        con.execute('''INSERT INTO scan_comparisons (id, kind, start, end, received_at, item_json)
                       VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                       kind=excluded.kind, start=excluded.start, end=excluded.end,
                       received_at=excluded.received_at, item_json=excluded.item_json''',
                    (item['id'], item['kind'], start, end, T.now(), payload))
        stored += 1
    return dict(stored=stored, rejected=rejected)


def summary(items):
    """Per-scan paired errors; within_pct is a percentage (0–100), not a fraction."""
    errors = {}
    for item in items:
        for metric in TOLERANCES:
            phone = item['phone'].get(metric)
            if phone is None:
                continue
            for reference in ('strap', 'watch'):
                value = (item.get(reference) or {}).get(metric)
                if value is not None:
                    errors.setdefault(item['kind'], {}).setdefault(metric, {}).setdefault(reference, []).append(phone - value)
    return {kind: {metric: {ref: dict(n=len(es), mae=sum(abs(e) for e in es) / len(es),
                                    bias=sum(es) / len(es),
                                    within_pct=100 * sum(abs(e) <= TOLERANCES[metric] for e in es) / len(es))
                            for ref, es in refs.items()} for metric, refs in metrics.items()}
            for kind, metrics in errors.items()}


def readings(con, days=90):
    cutoff = T.fmt(dt.datetime.now(T.UTC) - dt.timedelta(days=days))
    items = [json.loads(r[0]) for r in con.execute(
        'SELECT item_json FROM scan_comparisons WHERE start >= ? ORDER BY start DESC, id DESC', (cutoff,))]
    return dict(items=items, summary=summary(items))
