"""Daily rhythm (GET /api/v1/rhythm and the check-in's rhythm line): how regular sleep is, when heart rate peaks and
dips through the day, and how much later you sleep at weekends.

- Sleep Regularity Index (Phillips et al. 2017, https://doi.org/10.1038/s41598-017-03171-4): the chance of being in
  the same state (asleep or awake) at two moments 24 hours apart, minute by minute, scaled so 100 is the same times
  every day and 0 is no pattern at all: SRI = 200 × P(same) − 100. Only pairs of days that both have a main night
  count, so a night without the Watch isn't read as a night awake. Each day runs noon to noon around the morning
  you woke, and naps from the same source as that day's main night count as sleep. The API gives the SRI; the
  check-in says the plain share it comes from (P(same)), since George doesn't want scores.
- Heart-rate rhythm: a cosinor fit (one 24-hour cosine, least squares) to hourly medians of heart rate, leaving out
  workouts and the half hour after them. It gives the middle level (MESOR), how far it swings either side
  (amplitude), the hour it peaks (acrophase), and the share of the hour-to-hour variation the curve explains.
- Social jet lag (Wittmann et al. 2006): mid-sleep on nights before a weekend day minus mid-sleep on the others.
"""
import datetime as dt, math, statistics
from collections import defaultdict
from . import daily as D, timeutil as T
from .config import HOME_TZ

DAY_MIN = 1440
MIN_PAIRS = 4             # day pairs for a week's SRI
MIN_USUAL_PAIRS = 14      # for the usual SRI, from the 8 weeks before
USUAL_WEEKS = 8
HR_MIN_HOURS = 60         # hourly medians for a fit
HR_MIN_HOURS_OF_DAY = 18  # distinct clock hours among them
HR_AFTER_WORKOUT = 30 * 60
SJL_MIN_NIGHTS = (2, 4)   # weekend, weekday nights
MIN_SAY_SJL = 0.5         # hours of social jet lag worth a sentence


def _utc(d, zone):
    return T.fmt(dt.datetime(d.year, d.month, d.day, tzinfo=zone))


# ---------------------------------------------------------------- sleep regularity
def _sleep_days(con, first, last):
    """{wake date: (window start timestamp, set of asleep minute indexes 0..1439)} for local dates first..last with
    a main night. The window is noon the day before to noon on the wake date, in the night's own time zone."""
    lo = (first - dt.timedelta(days=2)).isoformat() + 'T00:00:00Z'
    hi = (last + dt.timedelta(days=2)).isoformat() + 'T00:00:00Z'
    nights = D.main_nights(con, lo, hi)
    segs = [dict(r) for r in con.execute(
        'SELECT uuid, stage, start_utc, end_utc, tz, source, device FROM sleep_segments '
        'WHERE start_utc >= ? AND start_utc < ? AND deleted_at IS NULL', (lo, hi))]
    by_source = defaultdict(list)   # (imported, source, device) -> [(start, end)] asleep, seconds
    for s in D.sleep_sessions(segs):
        for g in s['segs']:
            if g['stage'] in D.ASLEEP:
                by_source[(s['imported'], s['source'], s['device'])].append(
                    (T.from_db(g['start_utc']).timestamp(), T.from_db(g['end_utc']).timestamp()))
    out = {}
    for ds, c in nights.items():
        d = dt.date.fromisoformat(ds)
        if not first <= d <= last:
            continue
        zone = T.zone(c['tz']) or T.UTC
        w0 = dt.datetime(d.year, d.month, d.day, 12, tzinfo=zone).timestamp() - 86400
        mins = set()
        for s, e in by_source[(c['imported'], c['source'], c['device'])]:
            a, b = max(s, w0), min(e, w0 + DAY_MIN * 60)
            if b > a:
                mins.update(range(int((a - w0) // 60), int(math.ceil((b - w0) / 60))))
        out[d] = (w0, mins)
    return out


def sri(days):
    """(SRI, day pairs) from _sleep_days output; (None, pairs) when there are no pairs."""
    same = total = pairs = 0
    for d, (w0, mins) in days.items():
        prev = days.get(d - dt.timedelta(days=1))
        if not prev:
            continue
        p0, pmins = prev
        shift = round((w0 - 86400 - p0) / 60)   # minutes; 0 unless the clocks or time zone changed
        pairs += 1
        for m in range(DAY_MIN):
            pm = m + shift
            if 0 <= pm < DAY_MIN:
                total += 1
                same += (m in mins) == (pm in pmins)
    return (200 * same / total - 100 if total else None), pairs


# ---------------------------------------------------------------- heart-rate rhythm
def cosinor(points, period=24.0):
    """Least-squares fit of y = M + A·cos(2π(t − φ)/period) to [(clock hour, value)]. Returns {mesor, amplitude,
    peak (hour of day), r2} or None if the points can't pin it down."""
    n = len(points)
    if n < 3:
        return None
    w = 2 * math.pi / period
    rows = [(1.0, math.cos(w * t), math.sin(w * t), y) for t, y in points]
    # Normal equations for [M, b, g] in y = M + b·cos + g·sin.
    a = [[sum(r[i] * r[j] for r in rows) for j in range(3)] for i in range(3)]
    v = [sum(r[i] * r[3] for r in rows) for i in range(3)]
    sol = _solve3(a, v)
    if sol is None:
        return None
    m, b, g = sol
    amp = math.hypot(b, g)
    peak = (math.atan2(g, b) / w) % period
    mean = sum(y for _, y in points) / n
    ss_tot = sum((y - mean) ** 2 for _, y in points)
    ss_res = sum((r[3] - m - b * r[1] - g * r[2]) ** 2 for r in rows)
    return dict(mesor=m, amplitude=amp, peak=peak, r2=1 - ss_res / ss_tot if ss_tot else 0.0)


def _solve3(a, v):
    """Gaussian elimination with partial pivoting for a 3×3 system; None when singular."""
    m = [row[:] + [v[i]] for i, row in enumerate(a)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-9:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(3):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][3] / m[i][i] for i in range(3)]


def hourly_hr(con, lo, hi):
    """[(clock hour + 0.5, median bpm)] for each local hour in [lo, hi) (UTC text) with heart rate outside workouts."""
    busy = [(w['_s'], w['_e'] + HR_AFTER_WORKOUT) for w in D.day_workouts(con, lo, hi)]
    busy.sort()
    hours = defaultdict(list)
    for r in con.execute('SELECT start_utc, tz, value FROM samples WHERE type = ? AND start_utc >= ? AND start_utc < ? '
                         'AND deleted_at IS NULL AND value IS NOT NULL', (D.HEART_RATE, lo, hi)):
        t = T.from_db(r['start_utc'])
        ts = t.timestamp()
        if any(s <= ts <= e for s, e in busy):
            continue
        loc = t.astimezone(T.zone(r['tz']) or T.UTC)
        hours[(loc.date(), loc.hour)].append(r['value'])
    return [(h + 0.5, statistics.median(v)) for (_, h), v in hours.items()]


def hr_rhythm(con, lo, hi):
    pts = hourly_hr(con, lo, hi)
    if len(pts) < HR_MIN_HOURS or len({int(t) for t, _ in pts}) < HR_MIN_HOURS_OF_DAY:
        return None
    return cosinor(pts)


# ---------------------------------------------------------------- social jet lag
def social_jetlag(con, first, last):
    """Mid-sleep on nights before Saturday and Sunday minus the other nights, in hours (positive = later at the
    weekend); None without enough of both."""
    lo = (first - dt.timedelta(days=2)).isoformat() + 'T00:00:00Z'
    hi = (last + dt.timedelta(days=2)).isoformat() + 'T00:00:00Z'
    free, work = [], []
    for ds, c in D.main_nights(con, lo, hi).items():
        d = dt.date.fromisoformat(ds)
        if not first <= d <= last:
            continue
        zone = T.zone(c['tz']) or T.UTC
        mid = (c['start'] + (c['end'] - c['start']) / 2).astimezone(zone)
        h = mid.hour + mid.minute / 60
        h = h - 24 if h >= 18 else h   # hours after midnight; mid-sleep before 18:00 the evening before is negative
        (free if d.weekday() >= 5 else work).append(h)
    if len(free) < SJL_MIN_NIGHTS[0] or len(work) < SJL_MIN_NIGHTS[1]:
        return None
    return statistics.median(free) - statistics.median(work)


# ---------------------------------------------------------------- week summary
def week(con, ws, we):
    """Everything above for the local week ws..we (dates), with your usual from the weeks before."""
    zone = T.zone(HOME_TZ) or T.UTC
    days = _sleep_days(con, ws - dt.timedelta(days=7 * USUAL_WEEKS + 1), we)
    this = {d: v for d, v in days.items() if d >= ws - dt.timedelta(days=1)}
    before = {d: v for d, v in days.items() if d < ws}
    s, pairs = sri(this)
    if pairs < MIN_PAIRS:
        s = None
    u, upairs = sri(before)
    if upairs < MIN_USUAL_PAIRS:
        u = None
    hr = hr_rhythm(con, _utc(ws, zone), _utc(we + dt.timedelta(days=1), zone))
    hr_usual = hr_rhythm(con, _utc(ws - dt.timedelta(days=28), zone), _utc(ws, zone))
    sjl = social_jetlag(con, we - dt.timedelta(days=27), we)
    rnd = lambda v, nd=1: None if v is None else round(v, nd)
    hr_out = lambda h: None if h is None else dict(mesor=rnd(h['mesor']), amplitude=rnd(h['amplitude']), peak=rnd(h['peak'], 2),
                                                   low=rnd((h['peak'] + 12) % 24, 2), r2=rnd(h['r2'], 2))
    return dict(week_start=ws.isoformat(), week_end=we.isoformat(), sri=rnd(s), sri_pairs=pairs, sri_usual=rnd(u),
                sri_usual_pairs=upairs, heart_rate=hr_out(hr), heart_rate_usual=hr_out(hr_usual), social_jetlag_h=rnd(sjl, 2))


def _clock(h):
    m = int(round(h * 60)) % DAY_MIN
    return f'{m // 60:02d}:{m % 60:02d}'


def _dur(h):
    m = int(round(abs(h) * 60))
    return f'{m // 60} h {m % 60:02d} min' if m >= 60 else f'{m} min'


def _same_pct(s):
    """The share of minutes in the same state as 24 hours before, from an SRI: P(same) = (SRI + 100) / 200."""
    return (s + 100) / 2


def checkin_line(con, ws, we):
    r = week(con, ws, we)
    parts, tone = [], 'neutral'
    if r['sri'] is not None:
        # Plain words, not a score: the share of the day spent asleep or awake at the same times as the day before.
        text = f'asleep or awake at the same times as the day before for {_same_pct(r["sri"]):.0f}% of the day'
        if r['sri_usual'] is not None:
            d = r['sri'] - r['sri_usual']
            text += f' (usually {_same_pct(r["sri_usual"]):.0f}%)'
            tone = 'good' if d >= 5 else 'watch' if d <= -10 else 'neutral'
        parts.append(text)
    h, hu = r['heart_rate'], r['heart_rate_usual']
    if h and h['r2'] >= 0.2:
        text = f'heart rate lowest around {_clock(h["low"])} and highest around {_clock(h["peak"])}'
        if hu and hu['r2'] >= 0.2:
            shift = (h['peak'] - hu['peak'] + 12) % 24 - 12
            if abs(shift) >= 0.75:
                text += f', {_dur(shift)} {"later" if shift > 0 else "earlier"} than usual'
        parts.append(text)
    sjl = r['social_jetlag_h']
    if sjl is not None and abs(sjl) >= MIN_SAY_SJL:
        parts.append(f'over 4 weeks, the middle of your sleep was {_dur(sjl)} {"later" if sjl > 0 else "earlier"} '
                     f'at weekends than on weekdays')
    if not parts:
        return None
    return dict(kind='rhythm', text='Rhythm: ' + '; '.join(parts) + '.', tone=tone)
