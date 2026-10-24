"""Personal baselines (GET /api/v1/baselines) and the weekly check-in (GET /api/v1/checkin), worked out from the
daily summary, weigh-ins, the food target and the dose log. Plain numbers and plain sentences only, no scores.

Baselines: for each daily measure, your usual range is the mean ± 1.5 standard deviations of the days 8 to 97
before today (at least 30 days with data); "recent" is the mean of the last 7 days (yesterday and the 6 before;
today is only part of a day). A measure is "high" or "low" when recent is outside the range, and days_out counts the
most recent days with data in a row that were outside it.
"""
import datetime as dt, statistics
from . import body, dailycache, doses as DOSES, food, timeutil as T, training
from .config import HOME_TZ

# key, daily column, name, unit, which way is good, decimals
METRICS = [
    ('resting_hr', 'Resting HR', 'Resting heart rate', 'bpm', 'down', 0),
    ('hrv', 'Sleep HRV (ms)', 'Heart rate variability, asleep', 'ms', 'up', 0),
    ('sleep', 'Sleep (h)', 'Sleep', 'h', 'up', 2),
    ('steps', 'Steps', 'Steps', 'steps', 'up', 0),
    ('active_kcal', 'Active kcal', 'Active energy', 'kcal', 'up', 0),
    ('walking_hr', 'Walking HR', 'Walking heart rate', 'bpm', 'down', 0),
    ('blood_oxygen', 'Blood oxygen %', 'Blood oxygen', '%', 'up', 1),
    ('weight', 'Weight (kg)', 'Weight', 'kg', None, 1),
    ('systolic', 'Systolic BP', 'Systolic blood pressure', 'mmHg', 'down', 0),
    ('diastolic', 'Diastolic BP', 'Diastolic blood pressure', 'mmHg', 'down', 0),
    ('calories_in', 'Calories in', 'Calories in', 'kcal', None, 0),
]
RECENT_DAYS, BASE_FROM, BASE_TO, MIN_BASE_DAYS, SPREAD = 7, 8, 97, 30, 1.5
PACE_PCT = 0.5          # a steady weight-loss pace, % of body weight a week, unless the phone sends its own
PACE_KEYS = ('weekly_loss_pct', 'pace_pct_per_week')   # read from the food target's extra fields
LEAN_SHARE = 0.25       # flag when lean mass is more than this share of the weight lost


def today():
    return dt.datetime.now(T.zone(HOME_TZ) or T.UTC).date()


def _iso(d):
    return d.isoformat()


def _rnd(v, nd):
    return None if v is None else (int(round(v)) if nd == 0 else round(v, nd))


def baselines(con, as_of=None):
    """{as_of, metrics: [...]} with every measure that has a baseline (see the module docstring)."""
    as_of = as_of or today()
    rows = dailycache.rows(con, _iso(as_of - dt.timedelta(days=BASE_TO)), _iso(as_of - dt.timedelta(days=1)))
    by_date = {r['Date']: r for r in rows}
    recent_from = _iso(as_of - dt.timedelta(days=RECENT_DAYS))
    base_from, base_to = _iso(as_of - dt.timedelta(days=BASE_TO)), _iso(as_of - dt.timedelta(days=BASE_FROM))
    out = []
    for key, col, name, unit, good, nd in METRICS:
        base = [r[col] for d, r in by_date.items() if base_from <= d <= base_to and r.get(col) is not None]
        if len(base) < MIN_BASE_DAYS:
            continue
        mean, sd = statistics.mean(base), statistics.stdev(base)
        low, high = mean - SPREAD * sd, mean + SPREAD * sd
        rec = [r[col] for d, r in by_date.items() if d >= recent_from and r.get(col) is not None]
        recent = statistics.mean(rec) if rec else None
        status = None if recent is None else 'high' if recent > high else 'low' if recent < low else 'normal'
        days_out = 0
        for d in sorted(by_date, reverse=True):   # newest first: days with data in a row outside the range
            v = by_date[d].get(col)
            if v is None:
                continue
            if low <= v <= high:
                break
            days_out += 1
        out.append(dict(key=key, name=name, unit=unit, recent=_rnd(recent, nd), recent_days=len(rec),
                        baseline_mean=_rnd(mean, nd), baseline_sd=_rnd(sd, max(nd, 1)), low=_rnd(low, nd), high=_rnd(high, nd),
                        baseline_days=len(base), status=status, days_out=days_out, direction_good=good))
    return dict(as_of=_iso(as_of), metrics=out)


# ---------------------------------------------------------------- weekly check-in
def _fit(points):
    """Least-squares slope (per day) and mean of [(day number, value)]."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return (sum((x - mx) * (y - my) for x, y in points) / sxx if sxx else 0.0), my


def _day_num(w):
    return T.from_db(w['time']).timestamp() / 86400


def _hm(h):
    m = int(round(h * 60))
    return f'{m // 60} h {m % 60:02d} min'


def _mins(h):
    m = int(round(abs(h) * 60))
    return f'{m // 60} h {m % 60:02d} min' if m >= 60 else f'{m} min'


def _fmt_day(d):
    return f'{d.strftime("%a")} {d.day} {d.strftime("%b")}'


def _weighins(con, a, b):
    """Weigh-ins with a weight on local dates a..b (dates)."""
    ws = body.readings(con, _iso(a - dt.timedelta(days=1)), _iso(b + dt.timedelta(days=1)))
    return [w for w in ws if w['weight_kg'] is not None and _iso(a) <= w['local'][:10] <= _iso(b)]


def _weight_line(con, ws, we, target):
    pts = _weighins(con, ws, we)
    span = 'this week'
    if len(pts) < 2 or _day_num(pts[-1]) - _day_num(pts[0]) < 2:
        pts, span = _weighins(con, we - dt.timedelta(days=13), we), 'over the last two weeks'
        if len(pts) < 3 or _day_num(pts[-1]) - _day_num(pts[0]) < 5:
            return None
    slope, mean = _fit([(_day_num(w), w['weight_kg']) for w in pts])
    week = slope * 7
    pct = -week / mean * 100   # loss as % of body weight a week (negative = gaining)
    pace = PACE_PCT
    extra = (target or {}).get('extra') or {}
    for k in PACE_KEYS:
        if isinstance(extra.get(k), (int, float)) and not isinstance(extra.get(k), bool) and extra[k] > 0:
            pace = float(extra[k])
    if abs(week) < 0.1:
        change = 'about level'
    else:
        change = f'{"down" if week < 0 else "up"} {abs(week):.1f} kg a week ({abs(pct):.1f}% of body weight)'
    text = (f'Weight trend {span}: {change}, from {len(pts)} weigh-ins. '
            f'{"Your" if pace != PACE_PCT else "A steady"} pace is {pace:g}% a week (about {mean * pace / 100:.1f} kg).')
    tone = 'good' if pace * 0.5 <= pct <= pace * 2 else 'watch' if pct > pace * 2 or pct < -0.25 else 'neutral'
    return dict(kind='weight', text=text, tone=tone)


def _body_line(con, we):
    start = we - dt.timedelta(days=27)
    pts = [w for w in _weighins(con, start, we) if w['fat_pct'] is not None]
    if len(pts) < 3 or _day_num(pts[-1]) - _day_num(pts[0]) < 14:
        return None
    days = _day_num(pts[-1]) - _day_num(pts[0])
    fat = [(_day_num(w), w['weight_kg'] * w['fat_pct'] / 100) for w in pts]
    lean = [(_day_num(w), w['lean_kg'] if w['lean_kg'] is not None else w['weight_kg'] * (1 - w['fat_pct'] / 100)) for w in pts]
    d_fat, d_lean = _fit(fat)[0] * days, _fit(lean)[0] * days
    d_w = d_fat + d_lean
    weeks = max(1, round(days / 7))
    word = lambda v: f'{"down" if v < 0 else "up"} {abs(v):.1f} kg'
    text = f'Over the last {weeks} weeks ({len(pts)} weigh-ins), by the scale\'s estimate: fat {word(d_fat)}, lean mass {word(d_lean)}.'
    tone = 'neutral'
    if d_w < -0.3:
        share = max(0.0, -d_lean) / -d_w
        text += f' Lean mass is {share * 100:.0f}% of the {abs(d_w):.1f} kg lost.'
        tone = 'watch' if share > LEAN_SHARE else 'good' if d_fat < 0 else 'neutral'
        if tone == 'watch':
            text += ' More protein and strength training help keep it.'
    return dict(kind='body', text=text, tone=tone)


def _food_line(rows, target):
    logged = [r for r in rows if r.get('Calories in')]
    if not logged:
        return None
    avg = statistics.mean(r['Calories in'] for r in logged)
    t = target or {}
    text = f'Food: logged on {len(logged)} of 7 days, {avg:,.0f} kcal a day on average'
    tone = 'neutral'
    if t.get('kcal'):
        text += f' against a {t["kcal"]:,.0f} kcal target'
        off = (avg - t['kcal']) / t['kcal']
        tone = 'watch' if off > 0.10 else 'good' if abs(off) <= 0.10 else 'neutral'
    text += '.'
    if t.get('protein_g'):
        hit = sum(1 for r in logged if r.get('Protein g') is not None and r['Protein g'] >= t['protein_g'])
        text += f' Protein target ({t["protein_g"]:.0f} g) reached on {hit} of {len(logged)} days.'
        if hit * 2 < len(logged) and tone == 'good':
            tone = 'neutral'
    return dict(kind='food', text=text, tone=tone)


def _sleep_line(rows, base):
    nights = [r['Sleep (h)'] for r in rows if r.get('Sleep (h)')]
    if not nights:
        return None
    avg = statistics.mean(nights)
    text = f'Sleep: {_hm(avg)} a night on average ({len(nights)} night{"s" if len(nights) > 1 else ""})'
    tone = 'neutral'
    b = next((m for m in base['metrics'] if m['key'] == 'sleep'), None)
    if b:
        d = avg - b['baseline_mean']
        text += (', about your usual' if abs(d) < 10 / 60 else f', {_mins(d)} {"more" if d > 0 else "less"} than your usual {_hm(b["baseline_mean"])}')
        tone = 'watch' if d < -0.5 else 'good' if avg >= 7 and d > -10 / 60 else 'neutral'
    return dict(kind='sleep', text=text + '.', tone=tone)


def _baseline_lines(base):
    out = []
    for m in base['metrics']:
        if m['key'] in ('sleep', 'weight', 'calories_in') or m['days_out'] < 3 or m['status'] in (None, 'normal'):
            continue
        side = 'above' if m['status'] == 'high' else 'below'
        unit = '' if m['unit'] in ('steps',) else (m['unit'] if m['unit'] == '%' else ' ' + m['unit'])
        rng = f'{m["low"]:,}–{m["high"]:,}{unit}'
        text = f'{m["name"]} has been {side} your usual range ({rng}) for {m["days_out"]} days; this week averaged {m["recent"]:,}{unit}.'
        good = m['direction_good']
        tone = 'neutral' if good is None else 'good' if (good == 'up') == (m['status'] == 'high') else 'watch'
        out.append(dict(kind='baseline', text=text, tone=tone))
    return out


def _dose_line(con, ws, we):
    ds = [d for d in DOSES.rows(con, _iso(ws - dt.timedelta(days=1)), _iso(we + dt.timedelta(days=1)), newest_first=False)
          if _iso(ws) <= d['local'][:10] <= _iso(we)]
    if not ds:
        return None
    parts = []
    for d in ds:
        day = dt.date.fromisoformat(d['local'][:10])
        amount = f' {d["dose_mg"]:g} mg' if d['dose_mg'] is not None else ''
        site = f' ({d["site"].replace("-", ", ")})' if d['site'] else ''
        parts.append(f'{d["drug"]}{amount} on {_fmt_day(day)}{site}')
    return dict(kind='doses', text=f'Dose{"s" if len(ds) > 1 else ""}: ' + '; '.join(parts) + '.', tone='neutral')


def _training_line(con, ws, we):
    zone = T.zone(HOME_TZ) or T.UTC
    utc = lambda d: T.fmt(dt.datetime(d.year, d.month, d.day, tzinfo=zone))
    w = training.week_summary(con, utc(ws), utc(we + dt.timedelta(days=1)))
    if not w:
        return None
    text = (f'Training: {w["sessions"]} session{"s" if w["sessions"] != 1 else ""}, {w["working_sets"]} working sets'
            + (f', {w["volume_kg"]:,} kg lifted in total' if w['volume_kg'] else '') + '.')
    if w['prs']:
        best = [f'{p["exercise"]} {p["weight_kg"]:g} kg × {p["reps"]}' for p in w['prs'][:3]]
        more = len(w['prs']) - len(best)
        text += f' New best{"s" if len(w["prs"]) > 1 else ""} (estimated one-rep max): ' + ', '.join(best) + (f' and {more} more' if more > 0 else '') + '.'
    return dict(kind='training', text=text, tone='good' if w['prs'] else 'neutral')


def week_bounds(week=None):
    """Monday and Sunday of the week starting on (or containing) date `week`; default the last complete week."""
    if week is None:
        t = today()
        start = t - dt.timedelta(days=t.weekday() + 7)
    else:
        start = week - dt.timedelta(days=week.weekday())
    return start, start + dt.timedelta(days=6)


def checkin(con, week=None):
    ws, we = week_bounds(week)
    rows = dailycache.rows(con, _iso(ws), _iso(we))
    target = food.target(con)
    base = baselines(con, we + dt.timedelta(days=1))   # "recent" is then exactly this week
    lines = [_weight_line(con, ws, we, target), _body_line(con, we), _food_line(rows, target), _sleep_line(rows, base)]
    lines += _baseline_lines(base)
    lines.append(_training_line(con, ws, we))
    lines.append(_dose_line(con, ws, we))
    return dict(week_start=_iso(ws), week_end=_iso(we), generated_at=T.now(), lines=[l for l in lines if l])
