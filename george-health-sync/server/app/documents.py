"""Documents: reports and results that belong with the health data but aren't Apple Health records, such as the
pharmacogenomic (DNA medication) report. Each has metadata, an optional structured JSON summary, and an optional
file (a PDF), kept in data/documents/ under its SHA-256. Documents never expire.

PUT /api/v1/documents/{id} stores or replaces one; GET /api/v1/documents lists them; GET .../{id} returns one;
GET .../{id}/file returns the file.
"""
import base64, binascii, hashlib, json, os, re
from . import config, timeutil as T

DIR = os.path.join(config.DATA_DIR, 'documents')
ID_RE = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
FIELDS = ['id', 'kind', 'title', 'issued_on', 'provider', 'notes', 'file_name', 'content_type', 'file_size', 'file_sha256',
          'structured', 'created_at', 'updated_at']


class Bad(Exception):
    pass


def _text(body, k):
    v = body.get(k)
    if v is not None and not isinstance(v, str):
        raise Bad(f'{k} must be text')
    return v


def save_file(data, name):
    """Write the file once under its SHA-256 (atomic). Returns (sha256, path)."""
    sha = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(name or '')[1].lower()[:10] if name else ''
    os.makedirs(DIR, exist_ok=True)
    path = os.path.join(DIR, sha + ext)
    if not os.path.exists(path):
        with open(path + '.tmp', 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(path + '.tmp', path)
    return sha, path


def put(con, doc_id, body):
    """Store or replace a document. A request without a file keeps the stored one."""
    if not ID_RE.match(doc_id or ''):
        raise Bad('id: letters, digits, dot, dash or underscore, up to 100')
    if not isinstance(body, dict):
        raise Bad('expected a JSON object')
    structured = body.get('structured')
    if structured is not None and not isinstance(structured, (dict, list)):
        raise Bad('structured must be an object or list')
    issued = _text(body, 'issued_on')
    if issued and not re.match(r'^\d{4}-\d{2}-\d{2}$', issued):
        raise Bad('issued_on must be YYYY-MM-DD')
    row = dict(id=doc_id, kind=_text(body, 'kind'), title=_text(body, 'title'), issued_on=issued,
               provider=_text(body, 'provider'), notes=_text(body, 'notes'),
               structured_json=json.dumps(structured, separators=(',', ':'), ensure_ascii=False) if structured is not None else None)
    old = con.execute('SELECT * FROM documents WHERE id = ?', (doc_id,)).fetchone()
    f = body.get('file')
    if f is not None:
        if not isinstance(f, dict) or not isinstance(f.get('base64'), str):
            raise Bad('file must be {"name": ..., "content_type": ..., "base64": ...}')
        try:
            data = base64.b64decode(f['base64'], validate=True)
        except (binascii.Error, ValueError):
            raise Bad('file.base64 is not valid base64')
        sha, _ = save_file(data, f.get('name'))
        row.update(file_name=f.get('name'), content_type=f.get('content_type') or 'application/octet-stream',
                   file_size=len(data), file_sha256=sha)
    elif old is not None:
        row.update(file_name=old['file_name'], content_type=old['content_type'], file_size=old['file_size'], file_sha256=old['file_sha256'])
    else:
        row.update(file_name=None, content_type=None, file_size=None, file_sha256=None)
    now = T.now()
    cols = ['id', 'kind', 'title', 'issued_on', 'provider', 'notes', 'structured_json', 'file_name', 'content_type', 'file_size', 'file_sha256']
    con.execute(f'INSERT INTO documents ({",".join(cols)}, created_at, updated_at) VALUES ({",".join("?" * (len(cols) + 2))}) '
                'ON CONFLICT(id) DO UPDATE SET ' + ', '.join(f'{c} = excluded.{c}' for c in cols[1:]) + ', updated_at = excluded.updated_at',
                [row[c] for c in cols] + [now, now])
    return dict(id=doc_id, created=old is None, file_sha256=row['file_sha256'], file_size=row['file_size'])


def as_dict(r):
    d = {k: r[k] for k in r.keys() if k in FIELDS}
    d['structured'] = json.loads(r['structured_json']) if r['structured_json'] else None
    return {k: d.get(k) for k in FIELDS}


def listing(con, kind=None):
    q, args = 'SELECT * FROM documents WHERE deleted_at IS NULL', []
    if kind:
        q += ' AND kind = ?'; args.append(kind)
    return [as_dict(r) for r in con.execute(q + ' ORDER BY issued_on DESC, id', args)]


def one(con, doc_id):
    r = con.execute('SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL', (doc_id,)).fetchone()
    return as_dict(r) if r else None


def file_path(doc):
    if not doc or not doc.get('file_sha256'):
        return None
    ext = os.path.splitext(doc.get('file_name') or '')[1].lower()[:10]
    p = os.path.join(DIR, doc['file_sha256'] + ext)
    return p if os.path.exists(p) else None
