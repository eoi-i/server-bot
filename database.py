import os
import time

import psycopg
from psycopg.rows import dict_row


class DuplicateAnalysisError(ValueError):
    pass


def connect():
    url = os.getenv('DATABASE_URL', '')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    if not url.startswith('postgresql://'):
        raise RuntimeError('DATABASE_URL يجب أن يشير إلى PostgreSQL.')
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, application_name='apk-analyzer')


def init_db():
    """Apply additive schema changes only; existing analyses and records are retained."""
    with connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            message TEXT NOT NULL DEFAULT 'بانتظار بدء التحليل', upload_path TEXT, result_path TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        for sql in (
            'ALTER TABLE analyses ADD COLUMN IF NOT EXISTS progress INTEGER NOT NULL DEFAULT 0',
            'ALTER TABLE analyses ADD COLUMN IF NOT EXISTS file_size BIGINT NOT NULL DEFAULT 0',
            'ALTER TABLE analyses ADD COLUMN IF NOT EXISTS fingerprint TEXT',
            'ALTER TABLE analyses ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE',
            'CREATE INDEX IF NOT EXISTS analyses_created_at_idx ON analyses (created_at DESC)',
            'CREATE INDEX IF NOT EXISTS analyses_status_idx ON analyses (status)',
            'CREATE INDEX IF NOT EXISTS analyses_fingerprint_idx ON analyses (fingerprint)',
            '''CREATE TABLE IF NOT EXISTS donations (
                id TEXT PRIMARY KEY, intent_id TEXT UNIQUE NOT NULL, amount_minor INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
            '''CREATE TABLE IF NOT EXISTS stripe_events (
                event_id TEXT PRIMARY KEY, received_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''',
        ):
            conn.execute(sql)


def enqueue_analysis(job_id, filename, upload_path, size, fingerprint):
    with connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (fingerprint,))
        duplicate = conn.execute(
            "SELECT id FROM analyses WHERE fingerprint=%s AND status IN ('queued','processing','completed') LIMIT 1",
            (fingerprint,),
        ).fetchone()
        if duplicate:
            raise DuplicateAnalysisError('هذا الملف قيد المعالجة أو سبق تحليله بنجاح.')
        conn.execute(
            "INSERT INTO analyses (id,filename,status,message,upload_path,file_size,fingerprint) VALUES (%s,%s,'queued',%s,%s,%s,%s)",
            (job_id, filename, 'في قائمة الانتظار.', upload_path, size, fingerprint),
        )


def claim_jobs(limit=1):
    claimed = []
    with connect() as conn:
        rows = conn.execute('''SELECT id,filename,upload_path FROM analyses
            WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s''', (limit,)).fetchall()
        for row in rows:
            conn.execute("UPDATE analyses SET status='processing', progress=1, message='بدأ التحليل.', updated_at=NOW() WHERE id=%s", (row['id'],))
            claimed.append(dict(row))
    return claimed


def update_analysis(job_id, **fields):
    allowed = {'status', 'message', 'progress', 'result_path'}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    assignments = ', '.join(f'{key}=%s' for key in updates)
    with connect() as conn:
        conn.execute(f'UPDATE analyses SET {assignments}, updated_at=NOW() WHERE id=%s', (*updates.values(), job_id))


def get_analysis(job_id):
    with connect() as conn:
        row = conn.execute('''SELECT id,filename,status,message,progress,upload_path,result_path,file_size,
            fingerprint,cancel_requested,created_at,updated_at FROM analyses WHERE id=%s''', (job_id,)).fetchone()
        return dict(row) if row else None


def list_analyses(limit=20):
    limit = min(max(int(limit), 1), 100)
    with connect() as conn:
        rows = conn.execute('''SELECT id,filename,status,message,progress,created_at,updated_at
            FROM analyses ORDER BY created_at DESC LIMIT %s''', (limit,)).fetchall()
        return [dict(row) for row in rows]


def request_cancel(job_id):
    with connect() as conn:
        cursor = conn.execute("UPDATE analyses SET cancel_requested=TRUE, updated_at=NOW() WHERE id=%s AND status IN ('queued','processing')", (job_id,))
        return cursor.rowcount == 1


def cancel_requested(job_id):
    with connect() as conn:
        row = conn.execute('SELECT cancel_requested FROM analyses WHERE id=%s', (job_id,)).fetchone()
        return bool(row and row['cancel_requested'])


def clear_analysis_history():
    """Delete analysis rows only, atomically refusing to clear queued/running jobs. Donations/events stay intact."""
    with connect() as conn:
        conn.execute('LOCK TABLE analyses IN EXCLUSIVE MODE')
        active = conn.execute("SELECT count(*) AS n FROM analyses WHERE status IN ('queued','processing')").fetchone()['n']
        if active:
            return False, active
        deleted = conn.execute('DELETE FROM analyses').rowcount
    return True, deleted


def recover_stale_jobs(stale_seconds=3600):
    """Requeue old processing work only when its input is durable and still present."""
    with connect() as conn:
        rows = conn.execute("SELECT id,upload_path FROM analyses WHERE status='processing' AND updated_at < NOW() - (%s * INTERVAL '1 second')", (stale_seconds,)).fetchall()
        for row in rows:
            if row['upload_path'] and os.path.isfile(row['upload_path']):
                conn.execute("UPDATE analyses SET status='queued', message='استعادة مهمة متوقفة.', updated_at=NOW() WHERE id=%s", (row['id'],))
            else:
                conn.execute("UPDATE analyses SET status='failed', message='تعذر استعادة المهمة: ملف الرفع غير موجود.', updated_at=NOW() WHERE id=%s", (row['id'],))


def database_metrics():
    started = time.perf_counter()
    with connect() as conn:
        row = conn.execute('''SELECT current_database() AS database_name,
            pg_size_pretty(pg_database_size(current_database())) AS database_size,
            pg_database_size(current_database()) AS database_bytes,
            current_setting('server_version') AS server_version,
            (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()) AS active_connections,
            current_setting('max_connections')::int AS max_connections''').fetchone()
    result = dict(row)
    result['response_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return result


def create_donation(intent_id, amount_minor):
    with connect() as conn:
        conn.execute("INSERT INTO donations (id,intent_id,amount_minor,status) VALUES (%s,%s,%s,'pending') ON CONFLICT (intent_id) DO NOTHING", (intent_id, intent_id, amount_minor))


def apply_stripe_event(event_id, intent_id, status):
    with connect() as conn:
        inserted = conn.execute('INSERT INTO stripe_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING event_id', (event_id,)).fetchone()
        if inserted:
            conn.execute('UPDATE donations SET status=%s, updated_at=NOW() WHERE intent_id=%s', (status, intent_id))


def get_donation(intent_id):
    with connect() as conn:
        row = conn.execute('SELECT intent_id,status,amount_minor,created_at FROM donations WHERE intent_id=%s', (intent_id,)).fetchone()
        return dict(row) if row else None
