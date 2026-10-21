"""One row per weigh-in, for GET /api/v1/body and the dashboard.

A weigh-in is the BodyMass, BodyMassIndex, BodyFatPercentage and LeanBodyMass samples that share (almost) the same
time. The Health Sync app's own scale reading also puts the scale's extra measurements in the BodyMass sample's
metadata (HSImpedanceOhms, HSMuscleMassKg, HSBodyWaterPct, ...).

While both Zepp Life and Health Sync write the same weigh-in, there are two sets of samples a few seconds apart: they
are merged into one weigh-in, preferring Health Sync's values (they carry the extra measurements).
"""
import json
from . import timeutil as T

HK = 'HKQuantityTypeIdentifier'
TYPES = {HK + 'BodyMass': 'weight_kg', HK + 'BodyMassIndex': 'bmi', HK + 'BodyFatPercentage': 'fat_pct',
         HK + 'LeanBodyMass': 'lean_kg'}
# metadata key on the BodyMass sample -> field, and how to round it
EXTRA = {'HSMuscleMassKg': ('muscle_kg', 1), 'HSBodyWaterPct': ('water_pct', 1), 'HSBoneMassKg': ('bone_kg', 2),
         'HSVisceralFat': ('visceral_fat', 1), 'HSBMRKcal': ('bmr_kcal', 0), 'HSProteinPct': ('protein_pct', 1),
         'HSImpedanceOhms': ('impedance_ohms', 0)}
FIELDS = ['time', 'local', 'tz', 'source', 'weight_kg', 'bmi', 'fat_pct', 'lean_kg'] + [f for f, _ in EXTRA.values()] + ['scale', 'formula']
SAME_WEIGH_IN = 180          # seconds: samples this close together are one weigh-in
OWN_SOURCE = 'Health Sync'   # the app's own scale readings win when sources overlap


def _num(v, nd):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
    return int(round(v)) if nd == 0 else round(float(v), nd)


def readings(con, date_from=None, date_to=None):
    """Weigh-ins between two UTC dates (inclusive), oldest first."""
    where, args = ['type IN (%s)' % ','.join('?' * len(TYPES)), 'deleted_at IS NULL'], list(TYPES)
    if date_from:
        where.append('start_utc >= ?'); args.append(date_from + 'T00:00:00Z')
    if date_to:
        where.append('start_utc <= ?'); args.append(date_to + 'T23:59:59Z')
    rows = con.execute('SELECT type, start_utc, tz, value, source, metadata_json FROM samples WHERE '
                       + ' AND '.join(where) + ' ORDER BY start_utc', args).fetchall()
    # Health Export Kit imports are one value per day (timed at midnight): skip them on days with real readings.
    real_days = {T.local(r[1], r[2]).date() for r in rows if (r[4] or '') != 'Health Export Kit'}
    rows = [r for r in rows if (r[4] or '') != 'Health Export Kit' or T.local(r[1], r[2]).date() not in real_days]
    # group samples that are close in time into weigh-ins
    groups = []
    for r in rows:
        t = T.from_db(r[1]).timestamp()
        if groups and t - groups[-1]['t'] <= SAME_WEIGH_IN:
            groups[-1]['rows'].append(r)
        else:
            groups.append({'t': t, 'rows': [r]})
    out = []
    for g in groups:
        rs = g['rows']
        own = [r for r in rs if OWN_SOURCE.lower() in (r[4] or '').lower()]
        rs = own + [r for r in rs if r not in own]   # Health Sync's values first
        w = {f: None for f in FIELDS}
        first = next((r for r in rs if r[0] == HK + 'BodyMass'), rs[0])
        w['time'], w['tz'], w['source'] = first[1], first[2], first[4]
        w['local'] = T.local(first[1], first[2]).strftime('%Y-%m-%d %H:%M')
        for r in rs:
            f = TYPES[r[0]]
            if w[f] is None and r[3] is not None:
                v = float(r[3])
                if f == 'fat_pct' and v <= 1:   # a 0-1 fraction slipped through: show it as a percentage
                    v *= 100
                w[f] = round(v, 2 if f != 'fat_pct' else 1)
            if r[0] == HK + 'BodyMass' and r[5]:
                try:
                    md = json.loads(r[5])
                except ValueError:
                    md = {}
                for key, (field, nd) in EXTRA.items():
                    if w[field] is None and md.get(key) is not None:
                        w[field] = _num(md[key], nd)
                w['scale'] = w['scale'] or md.get('HSScaleModel')
                w['formula'] = w['formula'] or md.get('HSFormula')
        out.append(w)
    return _with_weighins(con, out, date_from, date_to)


# weighins table column -> field here
FROM_WEIGHINS = {'weight_kg': 'weight_kg', 'bmi': 'bmi', 'body_fat_pct': 'fat_pct', 'lean_mass_kg': 'lean_kg',
                 'muscle_mass_kg': 'muscle_kg', 'body_water_pct': 'water_pct', 'bone_mass_kg': 'bone_kg',
                 'visceral_fat': 'visceral_fat', 'bmr_kcal': 'bmr_kcal', 'protein_pct': 'protein_pct',
                 'impedance_ohms': 'impedance_ohms', 'scale_model': 'scale', 'formula': 'formula'}


def _with_weighins(con, out, date_from, date_to):
    """Scale weigh-ins (weighins table) are the fullest record: they replace the matching Apple Health weigh-in
    (within SAME_WEIGH_IN seconds) and are added where Apple Health has none."""
    from . import weighins
    times = [T.from_db(w['time']).timestamp() for w in out]
    for s in weighins.rows(con, date_from, date_to, newest_first=False):
        t = T.from_db(s['measured_at']).timestamp()
        match = next((i for i, x in enumerate(times) if abs(x - t) <= SAME_WEIGH_IN), None)
        if match is None:
            w = {f: None for f in FIELDS}
            out.append(w); times.append(t)
        else:
            w = out[match]
        w.update(time=s['measured_at'], tz=s['time_zone'], local=s['local'], source=s['source'] or OWN_SOURCE)
        for col, f in FROM_WEIGHINS.items():
            if s[col] is not None:
                w[f] = s[col]
    out.sort(key=lambda w: w['time'])
    return out
