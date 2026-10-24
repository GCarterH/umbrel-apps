# Health Sync server

A small self-hosted server that keeps your Apple Health data at home: steps, heart rate, sleep, workouts, weight, blood pressure, food and more, in one SQLite database on your own machine. It comes with a dashboard that shows trends, a single day in detail, a weekly check-in in plain words, and how your numbers compare with published norms. Nothing is sent to any cloud service.

It runs as an app on [umbrelOS](https://umbrel.com) (this repo is an Umbrel community app store), or on any machine with Docker.

> **Before you start:** the iPhone app that sends Health data to this server (Health Sync) is not published on the App Store. On your own you can still run the server and send data to it through its HTTP API (below), for example from a script, an iOS Shortcut or your own app. Everything else in this guide works as described.

---

## Setting it up with Claude (or another AI assistant)

Copy this whole README, or just the block below, into a chat with Claude and say where you want it installed. The block gives an assistant everything it needs.

```text
Please help me install the "Health Sync" server from https://github.com/GCarterH/umbrel-apps on my home server.

Facts about it:
- It's a FastAPI + SQLite server (Python 3.9+, packages: fastapi, uvicorn, segno). It listens on port 8787.
- All data (health.db and the API token) lives in one data folder. Back that folder up; never delete it to "fix" something.
- On umbrelOS: add https://github.com/GCarterH/umbrel-apps as a community app store (App Store → ⋯ → Community App Stores),
  then install "Health Sync". The dashboard opens from the Umbrel home screen, behind the Umbrel login.
  Uninstalling the app on Umbrel deletes its data, so update it instead of reinstalling.
- On any other Linux/macOS machine with Docker: clone the repo and use docker-compose.standalone.yml in its root
  (instructions in the README, section "Option B"). Set HEALTHSYNC_HOME_TZ to my time zone and HEALTHSYNC_PUBLIC_URL
  to the address my phone will use. Keep HEALTHSYNC_SETUP_PAGE at "0" unless only I can reach the server
  (home network or Tailscale) or a login sits in front of it, because the dashboard and setup page have no login of their own.
- The API needs the bearer token from the data folder (file "token", also printed in the container log at start).
- Don't expose port 8787 to the internet. For access away from home, use Tailscale or a VPN.

Steps I'd like:
1. Ask me whether I'm on umbrelOS or another machine, and my time zone.
2. Install it, start it, and check GET /api/v1/status with the token answers.
3. Show me where the token is, and how to send a test record (POST /api/v1/samples) and read it back (GET /api/v1/daily).
4. Explain how to back up the data folder.
```

---

## Option A: umbrelOS

1. In Umbrel, open **App Store → ⋯ (top right) → Community App Stores**.
2. Paste `https://github.com/GCarterH/umbrel-apps` and add it.
3. Open **George's apps** and install **Health Sync**.
4. Open Health Sync from the home screen. The dashboard is behind your Umbrel login. **Connect a phone** (top right) shows the server address and API token as text and as a QR code.

Updates arrive like any other Umbrel app. The first start after an update can take a minute or two: the server downloads its code, checks it against a fixed checksum, and on big databases loads its record index before accepting writes.

**Don't uninstall to fix problems:** on Umbrel, uninstalling an app deletes its data folder. Restart or update it instead.

Fixed settings in the Umbrel package: the home time zone is `Europe/London` and the phone address is `http://umbrel.local:8787`. The time zone only decides which days count as "away" and the zone for records that don't say. To change either, fork this repo, edit `george-health-sync/docker-compose.yml`, and add your fork as the community store.

## Option B: any server with Docker

```bash
git clone https://github.com/GCarterH/umbrel-apps.git health-sync
cd health-sync
# edit docker-compose.standalone.yml: HEALTHSYNC_HOME_TZ and HEALTHSYNC_PUBLIC_URL
docker compose -f docker-compose.standalone.yml up -d
docker compose -f docker-compose.standalone.yml logs | grep -A1 Token
```

The data goes into `./data` next to the compose file. The token is in `./data/token` and in the log.

Without Docker, use Python 3.9 or newer:

```bash
cd health-sync/george-health-sync/server
python3 -m venv venv && venv/bin/pip install -r requirements.txt
HEALTHSYNC_DATA_DIR=$HOME/health-sync-data HEALTHSYNC_HOME_TZ=Europe/London \
  venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8787
```

**Dashboard on a plain server:** the dashboard (`/`) and setup page (`/connect`) are off unless `HEALTHSYNC_SETUP_PAGE=1`. They have no login of their own (on Umbrel, the Umbrel login protects them), and the setup page shows your token. Only turn them on if the server can't be reached by anyone but you (home network, Tailscale), or put a login in front with a reverse proxy.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `HEALTHSYNC_DATA_DIR` | `./data` | Where `health.db` and `token` live |
| `HEALTHSYNC_HOME_TZ` | `Europe/London` | Your home time zone (IANA name, e.g. `America/New_York`) |
| `HEALTHSYNC_PUBLIC_URL` | address the page was opened on | The address a phone should use; it goes into the QR code |
| `HEALTHSYNC_SETUP_PAGE` | `0` | `1` turns on the dashboard and setup page (see above) |
| `HEALTHSYNC_MAX_BODY_MB` | `200` | Largest upload after unzipping |

## Sending data

Every `/api/v1/*` call needs `Authorization: Bearer <token>`. Request bodies can be plain or gzipped JSON (`Content-Encoding: gzip`). Records use Apple HealthKit identifiers and UTC times, and each record has a unique `uuid`, so sending the same record twice is harmless.

```bash
TOKEN=$(cat data/token)
curl -X POST "http://localhost:8787/api/v1/samples?wait=1" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"records":[{"uuid":"test-1","type":"HKQuantityTypeIdentifierStepCount",
       "start":"2026-10-18T09:00:00Z","end":"2026-10-18T09:10:00Z","value":1000,"unit":"count",
       "tz":"Europe/London","source":"My script","device":null,"metadata":{}}]}'
# → {"accepted":1,"duplicates":0,"rejected":[]}
curl -H "Authorization: Bearer $TOKEN" "http://localhost:8787/api/v1/daily?from=2026-10-18&to=2026-10-18"
```

| Endpoint | Body |
|---|---|
| `POST /api/v1/samples` | `{"records": [{uuid, type, start, end, value, unit, label, tz, source, device, metadata}]}`. Quantity and category samples. Sleep is `HKCategoryTypeIdentifierSleepAnalysis` with `label` set to a stage (`asleepCore`, `asleepDeep`, `asleepREM`, `awake`, ...) |
| `POST /api/v1/workouts` | `{"records": [{uuid, activity_type, start, end, tz, duration_s, energy_kcal, distance_m, source, device, hr: [{t, bpm}]}]}` |
| `POST /api/v1/ecgs` | `{"records": [{uuid, time, tz, classification, avg_hr, symptoms, source, device}]}` |
| `POST /api/v1/deletions` | `{"records": [{uuid, type}]}`: marks records deleted (rows are kept) |
| `POST /api/v1/weighins` | Smart-scale weigh-ins with every measurement (weight, fat %, muscle, water, bone, visceral fat, ...) |
| `POST /api/v1/food`, `PUT /api/v1/food/target` | Food log entries (meal, name, kcal, macros) and the daily calorie/macro target |
| `POST /api/v1/doses` | Medication doses (drug, dose_mg, injection site) |

Uploads are written to disk before the reply, so a reply means the data is safe. `?wait=1` waits until it's in the database, which is handy for testing.

## Reading data

| Endpoint | Returns |
|---|---|
| `GET /api/v1/status` | Record counts per type, last upload, database size |
| `GET /api/v1/daily?from=&to=` | One row per day: steps, distance, exercise, energy, resting/walking HR, VO₂ max, blood oxygen, sleep (hours, bedtime, deep/REM %), HRV, weight, body fat, workouts, blood pressure, calories and macros |
| `GET /api/v1/day?date=` | One day in detail: hourly steps, heart rate through the day, sleep stages, workouts, every daily Watch measure |
| `GET /api/v1/body` | Weigh-ins with body composition |
| `GET /api/v1/baselines` | Your usual range for each daily measure (from your own last 90 days) and whether you're in it |
| `GET /api/v1/checkin?week=` | A weekly check-in in plain sentences |
| `GET /api/v1/export?from=&to=&types=` | Raw records, for backups or your own analysis |

How days are counted: each record goes on the day of its own time zone. When the Watch and iPhone both counted steps, the Watch wins and the iPhone only fills the gaps (as the Health app does). Sleep counts on the morning you woke up.

## The dashboard

Tabs: **Overview** (your week, what changed, every measure at a glance), **Day** (any single day: every Watch measure with a midnight-to-midnight chart, sleep stages, workouts, food), **Activity**, **Sleep**, **Body**, **Food** and **Compared** (your numbers next to published norms for your age; some of these norms are for men only, and each has its source linked).

The dashboard gives plain numbers and ranges, not scores, and it isn't medical advice.

## Backups and safety

- **Back up the data folder** (`health.db` and `token`). While the server runs, copy it with `sqlite3 data/health.db ".backup backup.db"`, or stop the server first. On Umbrel, the folder is the app's data folder: `~/umbrel/app-data/george-health-sync/data`.
- **The token is a password.** Anyone with it can read all your health data. If it leaks, the setup page's **Make a new token** button replaces it (you'll then need to reconnect your phone or scripts).
- **Keep port 8787 off the internet.** Use Tailscale or a VPN to reach it away from home. The API has no rate limiting and no HTTPS of its own.

## Repo layout

- `george-health-sync/`: the Umbrel app (`umbrel-app.yml`, `docker-compose.yml`, icon).
- `george-health-sync/server/`: the server code (`app/`) and `requirements.txt`. The Umbrel compose file pins one exact commit of this folder by checksum.
- `docker-compose.standalone.yml`: running it with plain Docker.
- `umbrel-app-store.yml`: the community store's id and name.
