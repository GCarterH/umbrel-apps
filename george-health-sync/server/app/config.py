"""Settings (from environment variables) and the API token."""
import os, secrets

DATA_DIR = os.environ.get('HEALTHSYNC_DATA_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data'))
DB_PATH = os.path.join(DATA_DIR, 'health.db')
TOKEN_PATH = os.path.join(DATA_DIR, 'token')

# Home time zone: used when a record has no tz, and to decide which days count as "travelling".
HOME_TZ = os.environ.get('HEALTHSYNC_HOME_TZ', 'Europe/London')
# Address the phone should use; goes into the QR code. Blank = the address the setup page was opened on.
PUBLIC_URL = os.environ.get('HEALTHSYNC_PUBLIC_URL', '').rstrip('/')
# The setup page at / shows the token. Only turn it on where something else guards that page
# (on Umbrel the app proxy asks for the Umbrel login; /api/* is whitelisted so the phone can reach it).
SETUP_PAGE = os.environ.get('HEALTHSYNC_SETUP_PAGE', '0').lower() in ('1', 'true', 'yes', 'on')
# Biggest request body after un-gzipping. 5,000 samples are about 3 MB.
MAX_BODY = int(os.environ.get('HEALTHSYNC_MAX_BODY_MB', '200')) * 1024 * 1024


def get_token():
    """Read the token, creating one on first start."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH) as f:
            t = f.read().strip()
        if t:
            return t
    return rotate_token()


def rotate_token():
    os.makedirs(DATA_DIR, exist_ok=True)
    t = secrets.token_urlsafe(32)
    tmp = TOKEN_PATH + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(t + '\n')
    os.replace(tmp, TOKEN_PATH)
    return t
