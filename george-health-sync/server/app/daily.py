"""One row per day with the same columns as the Daily tab of Health Data.xlsx (tools/update_spreadsheet.py).

Rules, matching the spreadsheet:
- Each record is put on a day using its own time zone (so a trip to New York is counted in New York time).
- Steps, distance, energy, exercise and flights are summed. When the Watch and iPhone both counted the same
  minutes, the Watch wins and the iPhone only fills the gaps (like the Health app), so nothing is counted twice.
  Exact repeats from one source (an app that re-adds data) count once; overlapping sleep stages from one source too.
- Resting HR, walking HR, VO2 max and blood oxygen are the average of that day's readings; weight, body fat and
  lean mass are the day's latest reading. Blood pressure is the day's first systolic/diastolic pair.
- Sleep is listed on the morning you woke up. Only nights with at least 3 hours asleep count; if several apps
  tracked the same night, the session that started latest is used (as the spreadsheet does).
- Records loaded from the old Health Export Kit files (uuid starting 'hek:') are only used for a day (or night,
  or workout) the phone hasn't sent its own data for.
"""
import bisect, datetime as dt, json
from collections import Counter, defaultdict
from . import timeutil as T
from .config import HOME_TZ

HK = 'HKQuantityTypeIdentifier'
CUM = {'Steps': HK + 'StepCount', 'Distance (km)': HK + 'DistanceWalkingRunning', 'Exercise min': HK + 'AppleExerciseTime',
       'Active kcal': HK + 'ActiveEnergyBurned', 'Flights climbed': HK + 'FlightsClimbed'}
AVG = {'Resting HR': HK + 'RestingHeartRate', 'Walking HR': HK + 'WalkingHeartRateAverage', 'VO2 max': HK + 'VO2Max',
       'Blood oxygen %': HK + 'OxygenSaturation'}
LATEST = {'Weight (kg)': HK + 'BodyMass', 'Body fat %': HK + 'BodyFatPercentage', 'Lean mass (kg)': HK + 'LeanBodyMass'}
# Other daily readings that make a day appear in the Daily tab, as they do in the spreadsheet.
PRESENCE = [HK + 'HeartRateRecoveryOneMinute', HK + 'AppleSleepingBreathingDisturbances', HK + 'BodyTemperature',
            HK + 'BodyMassIndex', HK + 'WaistCircumference', HK + 'Height']
# Blood pressure: the day's first reading (usually the morning one). A reading is a systolic and a diastolic
# sample with the same start time.
BP = {'Systolic BP': HK + 'BloodPressureSystolic', 'Diastolic BP': HK + 'BloodPressureDiastolic'}
HEART_RATE, HRV = HK + 'HeartRate', HK + 'HeartRateVariabilitySDNN'
# Night summaries from Health Export Kit files: awakenings and sleep HR/HRV, which need raw data the exports don't have.
IMPORTED_NIGHT = 'HEKSleepSession'

ASLEEP = ('asleepCore', 'asleepDeep', 'asleepREM', 'asleepUnspecified')
SESSION_GAP = dt.timedelta(minutes=30)   # a longer break with no sleep data starts a new session (matches Health Export Kit)
MIN_NIGHT = 3 * 3600

STRENGTH = {'traditionalStrengthTraining', 'functionalStrengthTraining', 'coreTraining', 'highIntensityIntervalTraining',
            'crossTraining', 'Workout'}   # 'Workout' is Health Export Kit's name for a generic workout

COLUMNS = ['Date', 'Week starting', 'Month', 'Year', 'Where', 'Steps', 'Distance (km)', 'Exercise min', 'Active kcal',
           'Flights climbed', 'Resting HR', 'Walking HR', 'VO2 max', 'Blood oxygen %', 'Sleep (h)', 'Bedtime (hrs)', 'Bedtime',
           'Deep %', 'REM %', 'Awakenings', 'Sleep HR', 'Sleep HRV (ms)', 'Weight (kg)', 'Body fat %', 'Lean mass (kg)',
           'Workouts', 'Walking min', 'Cycling min', 'Running min', 'Strength min', 'Other workout min',
           'Systolic BP', 'Diastolic BP', 'BP time']   # not in the Daily tab yet


def imported(uuid):
    return uuid.startswith('hek:')


def priority(source, device):
    """Lower wins when sources overlap: Apple Watch, then iPhone, then anything else."""
    s, d = source or '', device or ''
    if d.startswith('Watch') or 'Watch' in s:
        return 0
    if d.startswith('iPhone') or 'iPhone' in s:
        return 1
    return 2


def wtype(t):
    if t in ('walking', 'hiking'):
        return 'walk'
    if t == 'cycling':
        return 'cycle'
    if t == 'running':
        return 'run'
    if t in STRENGTH:
        return 'strength'
    return 'other'


def place(tz):
    return tz.split('/')[-1].replace('_', ' ')


# ---------------------------------------------------------------- helpers
def _overlap(covered, s, e):
    """Seconds of [s, e] already covered by the merged interval list."""
    i = max(bisect.bisect_right(covered, [s, float('inf')]) - 1, 0)
    tot = 0.0
    while i < len(covered) and covered[i][0] < e:
        a, b = covered[i]
        tot += max(0.0, min(b, e) - max(a, s))
        i += 1
    return tot


def _add(covered, s, e):
    """Add [s, e] to a merged interval list in place."""
    i = bisect.bisect_left(covered, [s, s])
    if i and covered[i - 1][1] >= s:
        i -= 1
    j = i
    while j < len(covered) and covered[j][0] <= e:
        s, e = min(s, covered[j][0]), max(e, covered[j][1])
        j += 1
    covered[i:j] = [[s, e]]


def _inside(covered, t):
    i = bisect.bisect_right(covered, [t, float('inf')]) - 1
    return i >= 0 and covered[i][0] <= t <= covered[i][1]


# A higher-priority source counts as covering this long either side of each of its samples. Checked against the
# Health app's own totals for Jul-Sep 2020 (89 days): with 5 minutes, 73 days' steps match exactly and all are within
# 2.3%; with no margin the iPhone was over-counted by about 7%.
COVER_MARGIN = 300


def dedup_sum(rows, into=None):
    """Sum a day's cumulative samples like the Health app: the Watch wins where both counted, and the iPhone
    (then other apps) only fills the time the Watch didn't cover, pro rata. Within one source, samples are summed
    as the Health app does, except exact repeats (same start, end and value), which count once.
    into: optional (dict, key function); each record's share is also added to dict[key(record)] (e.g. by hour)."""
    groups = defaultdict(list)
    for r in rows:
        groups[(priority(r['source'], r['device']), r['source'] or '')].append(r)
    covered, total = [], 0.0
    for key in sorted(groups):
        seen, mine = set(), []
        for r in groups[key]:
            s, e = r['_s'], r['_e']
            if (s, e, r['value']) in seen:
                continue
            seen.add((s, e, r['value']))
            if e <= s:   # an instant
                part = 0.0 if _inside(covered, s) else r['value']
            else:
                part = r['value'] * (1 - min(1.0, _overlap(covered, s, e) / (e - s)))
            total += part
            if into:
                into[0][into[1](r)] += part
            mine.append((s, e))
        for s, e in mine:
            _add(covered, s - COVER_MARGIN, e + COVER_MARGIN)
    return total


def _rows(con, sql, args):
    return [dict(r) for r in con.execute(sql, args)]


def _prep(r, start='start_utc', end='end_utc'):
    r['_s'] = T.from_db(r[start]).timestamp()
    r['_e'] = T.from_db(r[end]).timestamp() if end else r['_s']
    r['_date'] = T.local(r[start], r['tz']).date().isoformat()
    return r


# ---------------------------------------------------------------- sleep
def sleep_sessions(segs):
    """Group segments into sessions per source (imported and phone data kept apart)."""
    groups = defaultdict(list)
    for g in segs:
        if g['stage'] in ('inBed', 'unknown'):
            continue
        groups[(imported(g['uuid']), g['source'] or '', g['device'] or '')].append(g)
    out = []
    for (imp, source, device), lst in groups.items():
        lst.sort(key=lambda g: (g['start_utc'], g['end_utc']))
        cur = None
        for g in lst:
            s, e = T.from_db(g['start_utc']), T.from_db(g['end_utc'])
            if cur is None or s - cur['end'] > SESSION_GAP:
                cur = dict(imported=imp, source=source, device=device, start=s, end=e, segs=[], tz=g['tz'])
                out.append(cur)
            cur['segs'].append(g)
            if e > cur['end']:
                cur['end'], cur['tz'] = e, g['tz']
    for c in out:
        # Overlapping segments from the same source (a re-added or rewritten night) count once; the earlier one wins.
        totals, covered = defaultdict(float), []
        awakenings, prev = 0, None
        for g in c['segs']:
            s, e = T.from_db(g['start_utc']).timestamp(), T.from_db(g['end_utc']).timestamp()
            new = (e - s) - _overlap(covered, s, e)
            if new <= 0:
                continue
            _add(covered, s, e)
            totals[g['stage']] += new
            if g['stage'] == 'awake' and prev != 'awake':
                awakenings += 1
            prev = g['stage']
        c['totals'] = totals
        c['asleep'] = sum(totals[k] for k in ASLEEP)
        c['awakenings'] = awakenings
        c['date'] = c['end'].astimezone(T.zone(c['tz']) or T.UTC).date().isoformat()
        c['priority'] = priority(c['source'], c['device'])
    return out


def _avg_between(con, typ, s, e):
    r = con.execute('SELECT AVG(value), COUNT(*) FROM samples WHERE type = ? AND start_utc >= ? AND start_utc <= ? AND deleted_at IS NULL',
                    (typ, T.fmt(s), T.fmt(e))).fetchone()
    return r[0] if r[1] else None


def main_nights(con, lo, hi):
    """wake date -> the main sleep session for that morning."""
    segs = _rows(con, 'SELECT uuid, stage, start_utc, end_utc, tz, source, device FROM sleep_segments '
                      'WHERE start_utc >= ? AND start_utc < ? AND deleted_at IS NULL', (lo, hi))
    sess = sleep_sessions(segs)
    phone = [c for c in sess if not c['imported']]
    sess = [c for c in sess if not c['imported'] or not any(p['start'] < c['end'] and c['start'] < p['end'] for p in phone)]
    best = {}
    for c in sess:
        if c['asleep'] < MIN_NIGHT:
            continue
        # Same rule as the spreadsheet: when several apps tracked the night, the latest-starting session wins.
        k = c['start']
        if c['date'] not in best or k > best[c['date']][0]:
            best[c['date']] = (k, c)
    nights = {d: c for d, (k, c) in best.items()}
    summaries = _rows(con, 'SELECT start_utc, end_utc, source, metadata_json FROM samples WHERE type = ? AND start_utc >= ? AND start_utc < ? '
                           'AND deleted_at IS NULL', (IMPORTED_NIGHT, lo, hi))
    by_source = defaultdict(list)
    for m in summaries:
        by_source[m['source']].append((T.from_db(m['start_utc']), T.from_db(m['end_utc']), m))
    for c in nights.values():
        c['hr'] = c['hrv'] = None
        if c['imported']:
            for ms, me, m in by_source[c['source']]:
                if ms < c['end'] and c['start'] < me:
                    md = json.loads(m['metadata_json'] or '{}')
                    c['hr'], c['hrv'] = md.get('heartRateAvg'), md.get('hrvAvg')
                    if md.get('awakenings') is not None:
                        c['awakenings'] = md['awakenings']
                    break
        else:
            hr, hrv = _avg_between(con, HEART_RATE, c['start'], c['end']), _avg_between(con, HRV, c['start'], c['end'])
            c['hr'] = round(hr) if hr is not None else None
            c['hrv'] = round(hrv, 1) if hrv is not None else None
    return nights


def bed_hours(c):
    """Sleep start as hours after the previous midnight in local time (e.g. 25.0 = 1am)."""
    t = c['start'].astimezone(T.zone(c['tz']) or T.UTC)
    h = t.hour + t.minute / 60
    return round(h if h >= 12 else h + 24, 3)


def _pct(c, k):
    tot = sum(c['totals'].get(x, 0) for x in ('asleepCore', 'asleepDeep', 'asleepREM'))
    return round(100 * c['totals'].get(k, 0) / tot, 1) if tot else None


# ---------------------------------------------------------------- workouts
def day_workouts(con, lo, hi):
    ws = [_prep(w) for w in _rows(con, 'SELECT uuid, activity_type, start_utc, end_utc, tz, duration_s FROM workouts '
                                       'WHERE start_utc >= ? AND start_utc < ? AND deleted_at IS NULL', (lo, hi))]
    phone = [w for w in ws if not imported(w['uuid'])]
    ws = [w for w in ws if not imported(w['uuid']) or not any(abs(p['_s'] - w['_s']) < 300 for p in phone)]
    return ws


# ---------------------------------------------------------------- daily
def daily(con, date_from, date_to):
    """Rows for local dates date_from..date_to (inclusive, 'YYYY-MM-DD')."""
    d0, d1 = dt.date.fromisoformat(date_from), dt.date.fromisoformat(date_to)
    lo = (d0 - dt.timedelta(days=2)).isoformat() + 'T00:00:00Z'
    hi = (d1 + dt.timedelta(days=3)).isoformat() + 'T00:00:00Z'
    in_range = lambda ds: date_from <= ds <= date_to

    types = list(CUM.values()) + list(AVG.values()) + list(LATEST.values()) + PRESENCE + list(BP.values())
    q = 'SELECT uuid, type, start_utc, end_utc, tz, value, source, device FROM samples WHERE type IN (%s) ' \
        'AND start_utc >= ? AND start_utc < ? AND deleted_at IS NULL AND value IS NOT NULL' % ','.join('?' * len(types))
    by = defaultdict(lambda: defaultdict(list))   # type -> date -> rows
    zones = defaultdict(Counter)                  # date -> tz of the phone's own records that day
    for r in _rows(con, q, types + [lo, hi]):
        _prep(r)
        if not in_range(r['_date']):
            continue
        by[r['type']][r['_date']].append(r)
        if not imported(r['uuid']):
            zones[r['_date']][r['tz']] += 1

    def pick(rows):   # phone data beats imported data for the same day
        real = [r for r in rows if not imported(r['uuid'])]
        return real or rows

    nights = {d: c for d, c in main_nights(con, lo, hi).items() if in_range(d)}
    wk = defaultdict(lambda: defaultdict(float))
    for w in day_workouts(con, lo, hi):
        if in_range(w['_date']):
            wk[w['_date']][wtype(w['activity_type'])] += (w['duration_s'] or 0) / 60
            wk[w['_date']]['n'] += 1
            zones[w['_date']][w['tz']] += 5
    for d, c in nights.items():
        zones[d][c['tz']] += 50

    steps_days = {d for d, rows in by[CUM['Steps']].items() if dedup_sum(pick(rows))}
    dates = set(steps_days) | set(nights) | set(wk)
    for t in list(AVG.values()) + list(LATEST.values()) + PRESENCE + list(BP.values()):
        dates |= set(by[t])

    out = []
    for ds in sorted(dates):
        day = dt.date.fromisoformat(ds)
        row = dict.fromkeys(COLUMNS)
        row.update({'Date': ds, 'Week starting': (day - dt.timedelta(days=day.weekday())).isoformat(), 'Month': ds[:7], 'Year': ds[:4]})
        tz = zones[ds].most_common(1)[0][0] if zones[ds] else HOME_TZ
        row['Where'] = place(tz) if tz != HOME_TZ else None
        if ds in steps_days:
            sums = {k: dedup_sum(pick(by[t][ds])) if by[t].get(ds) else None for k, t in CUM.items()}
            fmt = {'Steps': lambda v: round(v) or None, 'Distance (km)': lambda v: round(v / 1000, 2), 'Exercise min': round,
                   'Active kcal': lambda v: round(v, 1), 'Flights climbed': round}
            for k, v in sums.items():
                row[k] = fmt[k](v) if v is not None else None
        for k, t in AVG.items():
            v = [r['value'] for r in pick(by[t].get(ds, []))]
            row[k] = round(sum(v) / len(v), 1) if v else None
        for k, t in LATEST.items():
            v = pick(by[t].get(ds, []))
            row[k] = round(max(v, key=lambda r: r['start_utc'])['value'], 2) if v else None
        sys_, dia = by[BP['Systolic BP']].get(ds, []), {r['start_utc']: r for r in by[BP['Diastolic BP']].get(ds, [])}
        pairs = sorted(((r['start_utc'], r, dia[r['start_utc']]) for r in sys_ if r['start_utc'] in dia), key=lambda x: x[0])
        if pairs:
            t, s, d = pairs[0]
            row.update({'Systolic BP': round(s['value']), 'Diastolic BP': round(d['value']),
                        'BP time': T.local(t, s['tz']).strftime('%H:%M')})
        c = nights.get(ds)
        if c:
            b = bed_hours(c)
            row.update({'Sleep (h)': round(c['asleep'] / 3600, 2), 'Bedtime (hrs)': b, 'Bedtime': (b % 24) / 24,
                        'Deep %': _pct(c, 'asleepDeep'), 'REM %': _pct(c, 'asleepREM'), 'Awakenings': c['awakenings'],
                        'Sleep HR': c['hr'], 'Sleep HRV (ms)': c['hrv']})
        w = wk.get(ds)
        if w:
            row.update({'Workouts': int(w['n']) or None, 'Walking min': round(w['walk']) or None, 'Cycling min': round(w['cycle']) or None,
                        'Running min': round(w['run']) or None, 'Strength min': round(w['strength']) or None,
                        'Other workout min': round(w['other']) or None})
        out.append(row)
    return out
