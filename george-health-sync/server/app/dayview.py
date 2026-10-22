"""One day in detail, for the dashboard's day view: the day's summary row (the same numbers as /daily), steps hour by
hour, heart rate through the day, the night's sleep stages, workouts with their heart rate, and blood pressure
readings.

Times are given as minutes after the day's local midnight, in the zone most of the day's records were in (as /daily
does), so the night before shows as negative minutes. Steps per hour use the same Watch-first rule as the day's total.
"""
import datetime as dt
from collections import Counter, defaultdict
from . import body, daily as D, timeutil as T
from .config import HOME_TZ


def _minutes(t, zone, midnight):
    """Minutes from local midnight of the day to UTC datetime t."""
    return round((t.astimezone(zone) - midnight).total_seconds() / 60, 1)


def day(con, ds):
    d0 = dt.date.fromisoformat(ds)
    lo = (d0 - dt.timedelta(days=2)).isoformat() + 'T00:00:00Z'
    hi = (d0 + dt.timedelta(days=3)).isoformat() + 'T00:00:00Z'
    rows = D.daily(con, ds, ds)
    row = rows[0] if rows else None

    # Records on this local date (each by its own zone), and the zone most of them were in.
    hr_t, step_t = D.HEART_RATE, D.CUM['Steps']
    bp_t = list(D.BP.values())
    recs = defaultdict(list)
    zones = Counter()
    for r in D._rows(con, 'SELECT uuid, type, start_utc, end_utc, tz, value, source, device FROM samples WHERE type IN (?,?,?,?) '
                          'AND start_utc >= ? AND start_utc < ? AND deleted_at IS NULL AND value IS NOT NULL',
                     [hr_t, step_t] + bp_t + [lo, hi]):
        D._prep(r)
        if r['_date'] == ds:
            recs[r['type']].append(r)
            if not D.imported(r['uuid']):
                zones[r['tz']] += 1
    tz_name = zones.most_common(1)[0][0] if zones else HOME_TZ
    zone = T.zone(tz_name) or T.UTC
    midnight = dt.datetime(d0.year, d0.month, d0.day, tzinfo=zone)
    at = lambda s: _minutes(T.from_db(s), zone, midnight)

    # Steps each hour: the same Watch-first, fill-the-gaps rule as the day's total, applied hour by hour.
    steps = recs[step_t]
    real = [r for r in steps if not D.imported(r['uuid'])]
    steps = real or steps
    by_hour = defaultdict(float)
    D.dedup_sum(steps, into=(by_hour, lambda r: int(at(r['start_utc']) // 60)))
    steps_hourly = [[h, round(v)] for h, v in sorted(by_hour.items()) if 0 <= h < 24 and round(v)]

    # Heart rate: one point a minute (the average of that minute's readings).
    per_min = defaultdict(list)
    for r in recs[hr_t]:
        per_min[int(at(r['start_utc']))].append(r['value'])
    heart = [[m, round(sum(v) / len(v))] for m, v in sorted(per_min.items())]

    # Blood pressure: every pair taken that day.
    dia = {r['start_utc']: r for r in recs[D.BP['Diastolic BP']]}
    bp = [dict(t=at(r['start_utc']), time=T.local(r['start_utc'], tz_name).strftime('%H:%M'), systolic=round(r['value']),
               diastolic=round(dia[r['start_utc']]['value']))
          for r in sorted(recs[D.BP['Systolic BP']], key=lambda r: r['start_utc']) if r['start_utc'] in dia]

    # The night you woke up from that morning, stage by stage.
    sleep = None
    c = D.main_nights(con, lo, hi).get(ds)
    if c:
        segs = sorted(c['segs'], key=lambda g: (g['start_utc'], g['end_utc']))
        sleep = dict(source=c['source'], tz=c['tz'], place=D.place(c['tz']), start=at(T.fmt(c['start'])), end=at(T.fmt(c['end'])),
                     asleep_h=round(c['asleep'] / 3600, 2), awakenings=c['awakenings'],
                     stages={k: round(v / 60) for k, v in c['totals'].items()},
                     segments=[[g['stage'], at(g['start_utc']), at(g['end_utc'])] for g in segs])

    # Workouts that started on this day, with their heart rate.
    workouts = []
    for w in D.day_workouts(con, lo, hi):
        if w['_date'] != ds:
            continue
        x = con.execute('SELECT energy_kcal, distance_m, indoor, source FROM workouts WHERE uuid = ?', (w['uuid'],)).fetchone()
        h = con.execute('SELECT AVG(bpm), MAX(bpm), COUNT(*) FROM workout_hr WHERE workout_uuid = ?', (w['uuid'],)).fetchone()
        workouts.append(dict(type=w['activity_type'], kind=D.wtype(w['activity_type']), start=at(w['start_utc']), end=at(w['end_utc']),
                             time=T.local(w['start_utc'], tz_name).strftime('%H:%M'), minutes=round((w['duration_s'] or 0) / 60),
                             kcal=round(x['energy_kcal']) if x and x['energy_kcal'] else None,
                             km=round(x['distance_m'] / 1000, 2) if x and x['distance_m'] else None,
                             avg_hr=round(h[0]) if h and h[2] else None, max_hr=round(h[1]) if h and h[2] else None,
                             source=x['source'] if x else None))
    workouts.sort(key=lambda w: w['start'])

    weighins = [dict(w, time=w['local'][11:16]) for w in body.readings(con, (d0 - dt.timedelta(days=1)).isoformat(),
                                                                        (d0 + dt.timedelta(days=1)).isoformat())
                if w['local'][:10] == ds]

    first = con.execute('SELECT MIN(date), MAX(date) FROM daily_cache').fetchone()   # the date picker's limits
    return dict(date=ds, tz=tz_name, place=D.place(tz_name), row=row, steps_hourly=steps_hourly, heart=heart, bp=bp,
                sleep=sleep, workouts=workouts, weighins=weighins, first_day=first[0], last_day=first[1])
