"""HTTP API (spec 5.2). Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8787"""
import datetime as dt, hmac, html, json, logging, os, time
from urllib.parse import quote

import segno
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from . import __version__, body, config, daily as D, dailycache, db, idindex, ingest, spool, timeutil as T, weighins

log = logging.getLogger('healthsync')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
app = FastAPI(title='Health Sync', version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2048)

STATE = {}


@app.on_event('startup')
def startup():
    db.init().close()   # first start after an upgrade counts the existing records once (can take a minute)
    spool.start()
    dailycache.start()
    STATE['token'] = config.get_token()
    url = config.PUBLIC_URL or 'http://<this-machine>:8787'
    payload = config_link(url, STATE['token'])
    print(f'\nHealth Sync {__version__}  data: {config.DATA_DIR}\nToken: {STATE["token"]}\nQR payload: {payload}', flush=True)
    try:
        segno.make(payload, error='m').terminal(compact=True)
    except Exception:   # some log viewers can't show the block characters
        pass
    print('Dashboard at /, setup page (token and QR) at /connect' if config.SETUP_PAGE
          else 'Dashboard and setup page off (set HEALTHSYNC_SETUP_PAGE=1 to turn them on)', flush=True)


def config_link(url, token):
    return f'healthsync://config?url={quote(url, safe="")}&token={quote(token, safe="")}'


# ---------------------------------------------------------------- auth and bodies
def auth(request: Request):
    h = request.headers.get('authorization', '')
    scheme, _, given = h.partition(' ')
    if scheme.lower() != 'bearer' or not given or not hmac.compare_digest(given.strip().encode(), STATE['token'].encode()):
        raise HTTPException(401, 'missing or wrong token', headers={'WWW-Authenticate': 'Bearer'})


async def upload(request: Request, kind):
    """Check the upload, spool it to disk and answer; the database write happens in the background (spool.py).
    ?wait=1 waits for the write and returns the real counts (used by import_exports.py and the tests)."""
    auth(request)
    t0 = time.monotonic()
    raw = await request.body()
    t1 = time.monotonic()
    try:
        name, recs, res, (t_check, t_spool) = await run_in_threadpool(
            spool.put, kind, raw, request.headers.get('content-encoding', '').lower().strip())
    except spool.BadBody as e:
        headers = {'Retry-After': '30'} if e.status == 503 else None
        if e.status == 503:
            log.info('POST %s: %s', kind, e)
        raise HTTPException(e.status, str(e), headers=headers)
    if res['rejected']:
        log.info('POST %s: rejected %d, e.g. %s', kind, len(res['rejected']), res['rejected'][0])
    log.info('POST %s: %d records, %d KB: receive %.3fs, check %.3fs, spool %.3fs, total %.3fs', kind, len(recs), len(raw) // 1024,
             t1 - t0, t_check, t_spool, time.monotonic() - t0)
    if name and request.query_params.get('wait') in ('1', 'true'):
        done = await run_in_threadpool(spool.wait, name)
        if done is None:
            raise HTTPException(504, 'still writing; the upload is safely queued')
        res = dict(done, rejected=res['rejected'])
    return res


# ---------------------------------------------------------------- uploads
@app.post('/api/v1/samples')
async def post_samples(request: Request):
    return await upload(request, 'samples')


@app.post('/api/v1/workouts')
async def post_workouts(request: Request):
    return await upload(request, 'workouts')


@app.post('/api/v1/ecgs')
async def post_ecgs(request: Request):
    return await upload(request, 'ecgs')


@app.post('/api/v1/deletions')
async def post_deletions(request: Request):
    return await upload(request, 'deletions')


@app.post('/api/v1/have')
async def post_have(request: Request):
    """Which of these uuids does the server not hold yet? Body (gzip or plain): {"type": "<HK identifier>",
    "uuids": [...]}, at most 50,000. Reply {"missing": [...]}: uuids neither stored nor waiting in the upload queue.
    Any type works (samples, sleep, HKWorkoutTypeIdentifier, HKDataTypeIdentifierElectrocardiogram). 503 with
    Retry-After while the id index is still loading after a restart."""
    auth(request)
    t0 = time.monotonic()
    raw = await request.body()
    enc = request.headers.get('content-encoding', '').lower().strip()

    try:
        body = await run_in_threadpool(_have_body, raw, enc)
    except spool.BadBody as e:
        raise HTTPException(e.status, str(e))
    uuids = body.get('uuids') if isinstance(body, dict) else None
    if not isinstance(uuids, list) or not all(isinstance(u, str) for u in uuids):
        raise HTTPException(400, 'expected {"type": "...", "uuids": ["...", ...]}')
    if len(uuids) > 50000:
        raise HTTPException(413, 'at most 50,000 uuids per request')
    if not idindex.ready():
        raise HTTPException(503, 'still loading record ids after a restart; try again shortly', headers={'Retry-After': '30'})
    miss = await run_in_threadpool(idindex.missing, uuids)
    log.info('have %s: %d asked, %d missing, %.3fs', body.get('type'), len(uuids), len(miss), time.monotonic() - t0)
    return {'missing': miss}


def _have_body(raw, enc):
    if enc not in ('', 'identity', 'gzip'):
        raise spool.BadBody(415, f'unsupported Content-Encoding {enc}')
    if enc == 'gzip' or raw[:2] == b'\x1f\x8b':
        import zlib
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            raw = d.decompress(raw, config.MAX_BODY)
        except zlib.error:
            raise spool.BadBody(400, 'body is not valid gzip')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise spool.BadBody(400, 'body is not valid JSON')


# ---------------------------------------------------------------- reads
def status_data():
    """From the running totals in type_stats: a few rows, however big the database gets."""
    con = db.reader()
    types = {r['type']: dict(count=r['count'], deleted=r['deleted'], newest=r['newest'])
             for r in con.execute('SELECT type, count, deleted, newest FROM type_stats ORDER BY type') if r['count'] or r['deleted']}
    hr = int(db.get_meta(con, 'workout_hr_points', 0))
    last = db.get_meta(con, 'last_upload')
    pages = con.execute('PRAGMA page_count').fetchone()[0] * con.execute('PRAGMA page_size').fetchone()[0]
    wal = config.DB_PATH + '-wal'
    size = pages + (os.path.getsize(wal) if os.path.exists(wal) else 0)
    q_uploads, q_records = spool.queued()
    received, dups = int(db.get_meta(con, 'received_total', 0)), int(db.get_meta(con, 'duplicates_total', 0))
    return dict(version=__version__, total_records=sum(t['count'] for t in types.values()), queued_uploads=q_uploads, queued_records=q_records,
                received_records=received, duplicate_records=dups, counted_since='0.1.9',
                workout_hr_points=hr,
                newest=max((t['newest'] for n, t in types.items() if t['newest'] and not n.startswith(PROFILE)), default=None), last_upload=last,
                database_bytes=size, database_mb=round(size / 1e6, 1), types=types)


@app.get('/api/v1/status')
def get_status(request: Request):
    auth(request)
    return status_data()


PROFILE = 'HKCharacteristicTypeIdentifier'   # profile snapshot (sex, date of birth...): no real time


@app.get('/api/v1/profile')
def get_profile(request: Request):
    """The latest value the phone sent for each profile characteristic, e.g. date of birth."""
    auth(request)
    # A range on type (not LIKE) so SQLite uses the (type, start_utc) index instead of reading every row.
    rows = db.reader().execute('SELECT type, label, value, MAX(received_at) received FROM samples WHERE type >= ? AND type < ? '
                               'AND deleted_at IS NULL GROUP BY type', (PROFILE, PROFILE[:-1] + chr(ord(PROFILE[-1]) + 1))).fetchall()
    return {r['type'][len(PROFILE):]: dict(value=r['label'] if r['label'] is not None else r['value'], received=r['received']) for r in rows}


def _date(s, name):
    try:
        return dt.date.fromisoformat(s).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(400, f'{name} must be YYYY-MM-DD')


@app.post('/api/v1/weighins')
async def post_weighins(request: Request):
    """Scale weigh-ins from the app's Weigh In: {"weighins": [{id, measured_at, time_zone, weight_kg, impedance_ohms,
    bmi, body_fat_pct, lean_mass_kg, muscle_mass_kg, body_water_pct, bone_mass_kg, visceral_fat, bmr_kcal, protein_pct,
    profile{}, scale_model, formula, source, app_version, ...}]}, gzip or plain. Percentages 0-100. An id already stored
    is a duplicate. Fields not listed are kept too (extra). Reply {accepted, duplicates, rejected}."""
    auth(request)
    raw = await request.body()
    enc = request.headers.get('content-encoding', '').lower().strip()

    def work():
        b = _have_body(raw, enc)
        items = b.get('weighins') if isinstance(b, dict) else None
        if not isinstance(items, list):
            raise spool.BadBody(400, 'expected {"weighins": [...]}')
        if len(items) > 5000:
            raise spool.BadBody(413, 'at most 5,000 weigh-ins per request')
        with db.write_lock:
            con = db.writer()
            try:
                with con:
                    return weighins.store(con, items)
            except Exception:
                log.exception('storing weigh-ins failed')
                raise
    try:
        res = await run_in_threadpool(work)
    except spool.BadBody as e:
        raise HTTPException(e.status, str(e))
    log.info('POST weighins: %d new, %d duplicates, %d rejected', res['accepted'], res['duplicates'], len(res['rejected']))
    return res


@app.get('/api/v1/weighins')
def get_weighins(request: Request, from_: str = Query(None, alias='from'), to: str = None):
    """Every scale weigh-in with all its measurements, newest first. from/to are UTC dates (inclusive), both optional."""
    auth(request)
    f = _date(from_, 'from') if from_ else None
    t = _date(to, 'to') if to else None
    return dict(fields=weighins.FIELDS, weighins=weighins.rows(db.reader(), f, t))


@app.get('/api/v1/body')
def get_body(request: Request, from_: str = Query(None, alias='from'), to: str = None):
    """One row per weigh-in: weight, BMI, fat %, lean mass, plus the scale's extra measurements (muscle, water, bone,
    visceral fat, BMR, protein, impedance) when Health Sync's own scale reading has them. from/to are UTC dates
    (inclusive); both optional. Overlapping Zepp Life and Health Sync samples of one weigh-in are merged."""
    auth(request)
    f = _date(from_, 'from') if from_ else None
    t = _date(to, 'to') if to else None
    return dict(fields=body.FIELDS, readings=body.readings(db.reader(), f, t))


@app.get('/api/v1/daily')
def get_daily(request: Request, from_: str = Query(None, alias='from'), to: str = None):
    """from/to are local dates, inclusive. Default: the last 30 days."""
    auth(request)
    today = dt.date.today()
    f = _date(from_, 'from') if from_ else (today - dt.timedelta(days=29)).isoformat()
    t = _date(to, 'to') if to else today.isoformat()
    if f > t:
        raise HTTPException(400, 'from is after to')
    rows = dailycache.rows(db.reader(), f, t)
    return dict(columns=D.COLUMNS, rows=rows)


EXPORT_TABLES = {   # type name -> (table, time column)
    'HKCategoryTypeIdentifierSleepAnalysis': ('sleep_segments', 'start_utc'),
    'HKWorkoutTypeIdentifier': ('workouts', 'start_utc'),
    'HKDataTypeIdentifierElectrocardiogram': ('ecgs', 'time_utc'),
    'weighins': ('weighins', 'measured_at'),
}


def _bound(s, name, end=False):
    if not s:
        return None
    if len(s) == 10:
        d = dt.date.fromisoformat(_date(s, name))
        return (d + dt.timedelta(days=1 if end else 0)).isoformat() + 'T00:00:00Z'
    try:
        return T.norm(s)
    except ValueError:
        raise HTTPException(400, f'{name} must be YYYY-MM-DD or an ISO-8601 time')


@app.get('/api/v1/export')
def get_export(request: Request, from_: str = Query(None, alias='from'), to: str = None, types: str = None,
               include_deleted: bool = False):
    """Raw records as JSON: {"samples": [...], "sleep": [...], "workouts": [...], "ecgs": [...]}.
    from/to are UTC dates (to inclusive) or ISO times (to exclusive). types = comma-separated HealthKit identifiers."""
    auth(request)
    lo, hi = _bound(from_, 'from'), _bound(to, 'to', end=True)
    want = [t.strip() for t in types.split(',') if t.strip()] if types else None
    sample_types = [t for t in want if t not in EXPORT_TABLES] if want else None
    parts = [('samples', 'samples', 'start_utc', sample_types)]
    for name, key in (('HKCategoryTypeIdentifierSleepAnalysis', 'sleep'), ('HKWorkoutTypeIdentifier', 'workouts'),
                      ('HKDataTypeIdentifierElectrocardiogram', 'ecgs'), ('weighins', 'weighins')):
        if not want or name in want:
            parts.append((key, *EXPORT_TABLES[name], None))
    if want and not sample_types:
        parts = parts[1:]

    def gen():
        con = db.connect()
        try:
            yield '{"from":%s,"to":%s' % (json.dumps(lo), json.dumps(hi))
            for key, table, col, tlist in parts:
                where, args = [], []
                if lo: where.append(f'{col} >= ?'); args.append(lo)
                if hi: where.append(f'{col} < ?'); args.append(hi)
                if not include_deleted: where.append('deleted_at IS NULL')
                if tlist: where.append('type IN (%s)' % ','.join('?' * len(tlist))); args += tlist
                sql = f'SELECT * FROM {table}' + (' WHERE ' + ' AND '.join(where) if where else '') + f' ORDER BY {col}'
                yield ',"%s":[' % key
                first, buf = True, []
                for r in con.execute(sql, args):
                    d = dict(r)
                    for k in ('metadata_json', 'weather_json', 'profile_json', 'extra_json'):
                        if k in d:
                            d[k[:-5]] = json.loads(d.pop(k)) if d[k] else None
                    if table == 'workouts':
                        d['hr'] = [{'t': t, 'bpm': b} for t, b in con.execute(
                            'SELECT t_utc, bpm FROM workout_hr WHERE workout_uuid = ? ORDER BY t_utc', (d['uuid'],))]
                    buf.append(('' if first else ',') + json.dumps(d, separators=(',', ':'), ensure_ascii=False))
                    first = False
                    if len(buf) >= 2000:
                        yield ''.join(buf); buf = []
                yield ''.join(buf) + ']'
            yield '}'
        finally:
            con.close()
    return StreamingResponse(gen(), media_type='application/json')


# ---------------------------------------------------------------- setup page (token + QR)
STATIC = os.path.join(os.path.dirname(__file__), 'static')


@app.get('/', response_class=HTMLResponse)
def dashboard_page():
    """The dashboard (static/dashboard.html). Like the setup page, it relies on the Umbrel login in front of it."""
    if not config.SETUP_PAGE:
        return HTMLResponse('<p>Health Sync server {} is running. The dashboard and setup page are turned off.</p>'.format(__version__))
    return FileResponse(os.path.join(STATIC, 'dashboard.html'), media_type='text/html', headers={'Cache-Control': 'no-cache'})


@app.get('/dashboard/data')
def dashboard_data():
    """Every saved day from the daily summary, as compact arrays, plus a little status. Saved days only: days still
    being recalculated are left out rather than worked out on the spot, so the page always loads quickly."""
    if not config.SETUP_PAGE:
        raise HTTPException(404)
    con = db.reader()
    rows = [json.loads(r[0]) for r in con.execute('SELECT row_json FROM daily_cache ORDER BY date')]
    s = status_data()
    return dict(version=__version__, last_upload=s['last_upload'], total_records=s['total_records'],
                queued_records=s['queued_records'], summary_ready=dailycache.ready(con), days_to_prepare=dailycache.pending(con),
                home_tz=config.HOME_TZ,
                columns=D.COLUMNS, rows=[[r.get(c) for c in D.COLUMNS] for r in rows],
                body_fields=body.FIELDS, body=[[w[f] for f in body.FIELDS] for w in body.readings(con)])


@app.get('/connect', response_class=HTMLResponse)
def setup_page(request: Request):
    if not config.SETUP_PAGE:
        return HTMLResponse('<p>Health Sync server {} is running. The setup page is turned off; the token is in the server log.</p>'.format(__version__))
    url = config.PUBLIC_URL or str(request.base_url).rstrip('/')
    link = config_link(url, STATE['token'])
    qr = segno.make(link, error='m').svg_inline(scale=6, border=2, dark='#1F4E5A')
    s = status_data()
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect a phone</title><link rel="icon" href="/icon.png"><link rel="apple-touch-icon" href="/icon.png"><style>
body{{font-family:-apple-system,system-ui,sans-serif;max-width:640px;margin:2rem auto;padding:0 16px;color:#1d2a2e;background:#fff}}
code{{background:#eef3f4;padding:.2rem .4rem;border-radius:4px;word-break:break-all}} .qr svg{{max-width:100%;height:auto}}
button{{font:inherit;padding:.4rem .9rem}} small{{color:#5a6b70}}
@media (prefers-color-scheme:dark){{body{{background:#121a1c;color:#e4eef0}} code{{background:#22313a}} .qr{{background:#fff;display:inline-block;padding:8px;border-radius:8px}}}}
</style></head><body>
<p><a href="/">&larr; Dashboard</a></p>
<h1><img src="/icon.png" alt="" width="48" height="48" style="vertical-align:middle;margin-right:.5rem">Connect a phone</h1>
<p style="margin-top:-.8rem"><small>Version {__version__}</small></p>
<p>In the iPhone app go to <b>Settings</b> and scan this code, or type the address and token.</p>
<div class="qr">{qr}</div>
<p>Server address: <code>{html.escape(url)}</code><br>Token: <code>{html.escape(STATE['token'])}</code></p>
<p><small>Records stored: {s['total_records']:,} &middot; waiting to be written: {s['queued_records']:,} &middot;
duplicates skipped (since 0.1.9): {s['duplicate_records']:,} &middot; newest: {html.escape(s['newest'] or 'none yet')} &middot;
last upload: {html.escape(s['last_upload'] or 'none yet')} &middot; database: {s['database_mb']} MB</small></p>
<form method="post" action="/setup/rotate" onsubmit="return confirm('Make a new token? The phone stops syncing until you scan the new code.')">
<button>Make a new token</button></form>
</body></html>""")


@app.get('/icon.png', include_in_schema=False)
def icon():
    return FileResponse(os.path.join(os.path.dirname(__file__), 'static', 'icon.png'), media_type='image/png')


@app.post('/setup/rotate')
def setup_rotate():
    if not config.SETUP_PAGE:
        raise HTTPException(404)
    STATE['token'] = config.rotate_token()
    log.warning('token rotated from the setup page')
    return RedirectResponse('/connect', status_code=303)
