"""Time helpers. Stored times are always 'YYYY-MM-DDTHH:MM:SSZ' (UTC), so text order is time order."""
import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
FMT = '%Y-%m-%dT%H:%M:%SZ'


def parse_utc(s):
    """ISO-8601 text -> aware UTC datetime. A time without an offset is taken as UTC."""
    if not isinstance(s, str) or not s:
        raise ValueError('missing time')
    t = s.strip()
    if t.endswith('Z') or t.endswith('z'):
        t = t[:-1] + '+00:00'
    d = dt.datetime.fromisoformat(t)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def fmt(d):
    return d.astimezone(UTC).strftime(FMT)


def norm(s):
    return fmt(parse_utc(s))


def from_db(s):
    return dt.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]), int(s[17:19]), tzinfo=UTC)


@lru_cache(maxsize=None)
def zone(name):
    """ZoneInfo for a tz name, or None if the name isn't a real zone."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


def local(s, tz):
    """Stored UTC text -> local datetime in the record's zone."""
    return from_db(s).astimezone(zone(tz) or UTC)


def now():
    return fmt(dt.datetime.now(UTC))
