"""One day in detail, for the dashboard's day view: the day's summary row (the same numbers as /daily), every measure
the Watch tracks daily (METRICS) with its shape across the day, steps hour by hour, heart rate through the day, the
night's sleep stages, workouts with their heart rate, and blood pressure readings.

Times are given as minutes after the day's local midnight, in the zone most of the day's records were in (as /daily
does), so the night before shows as negative minutes. Steps per hour use the same Watch-first rule as the day's total.
"""
import datetime as dt
from collections import Counter, defaultdict
from . import body, daily as D, food, timeutil as T, whoop
from .config import HOME_TZ


HK = D.HK
# The measures the Watch (and iPhone) track every day, as the day view shows them: type -> (name, group, how, unit,
# factor, decimals). how: 'sum' adds the day up (Watch first, like /daily), 'avg' averages its readings (with the low
# and high), 'stood' counts the hours with a stand. One-off records (ECGs, food, mindfulness, events) are left out.
METRICS = {
    HK + 'StepCount': ('Steps', 'Activity', 'sum', '', 1, 0),
    HK + 'DistanceWalkingRunning': ('Walking + running distance', 'Activity', 'sum', 'km', 0.001, 2),
    HK + 'DistanceCycling': ('Cycling distance', 'Activity', 'sum', 'km', 0.001, 2),
    HK + 'ActiveEnergyBurned': ('Active energy', 'Activity', 'sum', 'kcal', 1, 0),
    HK + 'BasalEnergyBurned': ('Resting energy', 'Activity', 'sum', 'kcal', 1, 0),
    HK + 'AppleExerciseTime': ('Exercise', 'Activity', 'sum', 'min', 1, 0),
    'HKCategoryTypeIdentifierAppleStandHour': ('Stand hours', 'Activity', 'stood', 'hours', 1, 0),
    HK + 'AppleStandTime': ('Stand time', 'Activity', 'sum', 'min', 1, 0),
    HK + 'FlightsClimbed': ('Flights climbed', 'Activity', 'sum', '', 1, 0),
    HK + 'TimeInDaylight': ('Time in daylight', 'Activity', 'sum', 'min', 1, 0),
    HK + 'PhysicalEffort': ('Physical effort', 'Activity', 'avg', 'METs', 1, 1),
    HK + 'HeartRate': ('Heart rate', 'Heart', 'avg', 'bpm', 1, 0),
    HK + 'RestingHeartRate': ('Resting heart rate', 'Heart', 'avg', 'bpm', 1, 0),
    HK + 'WalkingHeartRateAverage': ('Walking heart rate', 'Heart', 'avg', 'bpm', 1, 0),
    HK + 'HeartRateVariabilitySDNN': ('Heart rate variability', 'Heart', 'avg', 'ms', 1, 0),
    HK + 'OxygenSaturation': ('Blood oxygen', 'Heart', 'avg', '%', 1, 1),
    HK + 'HeartRateRecoveryOneMinute': ('Cardio recovery', 'Heart', 'avg', 'bpm', 1, 0),
    HK + 'VO2Max': ('VO₂ max', 'Heart', 'avg', 'ml/kg/min', 1, 1),
    HK + 'RespiratoryRate': ('Respiratory rate', 'Overnight', 'avg', 'breaths/min', 1, 1),
    HK + 'AppleSleepingWristTemperature': ('Wrist temperature', 'Overnight', 'avg', '°C', 1, 2),
    HK + 'AppleSleepingBreathingDisturbances': ('Breathing disturbances', 'Overnight', 'avg', '', 1, 1),
    HK + 'WalkingSpeed': ('Walking speed', 'Walking', 'avg', 'km/h', 3.6, 1),
    HK + 'WalkingStepLength': ('Step length', 'Walking', 'avg', 'cm', 1, 0),
    HK + 'WalkingDoubleSupportPercentage': ('Double support time', 'Walking', 'avg', '%', 1, 1),
    HK + 'WalkingAsymmetryPercentage': ('Walking asymmetry', 'Walking', 'avg', '%', 1, 1),
    HK + 'StairAscentSpeed': ('Stair speed up', 'Walking', 'avg', 'm/s', 1, 2),
    HK + 'StairDescentSpeed': ('Stair speed down', 'Walking', 'avg', 'm/s', 1, 2),
    HK + 'EnvironmentalAudioExposure': ('Environmental sound', 'Noise', 'avg', 'dB', 1, 0),
    HK + 'HeadphoneAudioExposure': ('Headphone audio', 'Noise', 'avg', 'dB', 1, 0),
}
GROUPS = ['Activity', 'Heart', 'Overnight', 'Walking', 'Noise']
BY_MORNING = {HK + 'AppleSleepingWristTemperature', HK + 'AppleSleepingBreathingDisturbances'}


def _metrics(recs, at):
    """One entry per measure recorded that day, in METRICS order: the day's value, and its shape across the day
    (hourly totals for sums, a point a minute for readings, the hours for stand hours)."""
    out = []
    for t, (name, group, how, unit, f, dp) in METRICS.items():
        rs = recs.get(t)
        if not rs:
            continue
        real = [r for r in rs if not D.imported(r['uuid'])]
        rs = real or rs
        rnd = (lambda v: round(v * f, dp)) if dp else (lambda v: round(v * f))
        m = dict(key=t, name=name, group=group, how=how, unit=unit, decimals=dp)
        if how == 'sum':
            hours = defaultdict(float)
            total = D.dedup_sum(rs, into=(hours, lambda r: int(at(r['start_utc']) // 60)))
            m.update(value=rnd(total), hourly=[[h, round(v * f, max(dp, 1))] for h, v in sorted(hours.items()) if 0 <= h < 24 and v > 0])
        elif how == 'stood':
            stood = sorted({int(at(r['start_utc']) // 60) for r in rs if r.get('label') == 'stood'})
            m.update(value=len(stood), hourly=[[h, 1] for h in stood if 0 <= h < 24])
        else:
            per = defaultdict(list)
            for r in rs:
                per[int(at(r['start_utc']))].append(r['value'])
            vs = [r['value'] for r in rs]
            m.update(value=rnd(sum(vs) / len(vs)), low=rnd(min(vs)), high=rnd(max(vs)), count=len(vs),
                     points=[[k, rnd(sum(v) / len(v))] for k, v in sorted(per.items())])
        out.append(m)
    return out


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
    types = list(dict.fromkeys([hr_t, step_t] + bp_t + list(METRICS)))
    for r in D._rows(con, 'SELECT uuid, type, start_utc, end_utc, tz, value, label, source, device FROM samples WHERE type IN (%s) '
                          'AND start_utc >= ? AND start_utc < ? AND deleted_at IS NULL AND value IS NOT NULL' % ','.join('?' * len(types)),
                     types + [lo, hi]):
        D._prep(r)
        if r['type'] in BY_MORNING:   # one value a night: belongs to the morning you woke up, like sleep
            r['_date'] = T.local(r['end_utc'], r['tz']).date().isoformat()
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
    return dict(date=ds, tz=tz_name, place=D.place(tz_name), row=row, metrics=_metrics(recs, at), groups=GROUPS,
                steps_hourly=steps_hourly, heart=heart, bp=bp,
                sleep=sleep, workouts=workouts, weighins=weighins, food=food.on_day(con, ds), whoop=whoop.on_day(con, ds), first_day=first[0], last_day=first[1])
