import json
import os
import time
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def connect():
    url = os.getenv('DATABASE_URL')
    if not url:
        raise RuntimeError('يجب ضبط DATABASE_URL على PostgreSQL.')
    if url.startswith('postgres://'):
        url = url.replace('postgres://', 'postgresql://', 1)
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, application_name='apk-analyzer')


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    # Additive migrations preserve existing analyses and never drop/overwrite user data.
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            message TEXT NOT NULL DEFAULT 'بانتظار بدء التحليل', upload_path TEXT, result_path TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        for statement in (
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
        ):
            conn.execute(statement)


def create_analysis(job_id, filename, upload_path, file_size=0, fingerprint=None):
    with db() as conn:
        conn.execute('INSERT INTO analyses (id, filename, upload_path, file_size, fingerprint) VALUES (%s,%s,%s,%s,%s)',
                     (job_id, filename, upload_path, file_size, fingerprint))


def update_analysis(job_id, **fields):
    allowed = {'status', 'message', 'progress', 'result_path', 'cancel_requested'}
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    assignments = ', '.join(f'{key} = %s' for key in values)
    with db() as conn:
        conn.execute(f'UPDATE analyses SET {assignments}, updated_at=NOW() WHERE id=%s', (*values.values(), job_id))


def get_analysis(job_id):
    with db() as conn:
        row = conn.execute('SELECT id,filename,status,message,progress,upload_path,result_path,file_size,fingerprint,cancel_requested,created_at,updated_at FROM analyses WHERE id=%s', (job_id,)).fetchone()
        return dict(row) if row else None


def find_analysis_by_fingerprint(fingerprint):
    with db() as conn:
        row = conn.execute('SELECT id,status FROM analyses WHERE fingerprint=%s ORDER BY created_at DESC LIMIT 1', (fingerprint,)).fetchone()
        return dict(row) if row else None


def list_analyses(limit=20):
    limit = min(max(int(limit), 1), 100)
    with db() as conn:
        rows = conn.execute('SELECT id,filename,status,message,progress,created_at,updated_at FROM analyses ORDER BY created_at DESC LIMIT %s', (limit,)).fetchall()
        return [dict(row) for row in rows]


def mark_interrupted_jobs():
    # Workers are process-local. A deploy may interrupt processing; queued jobs are retried from durable uploads.
    with db() as conn:
        conn.execute("UPDATE analyses SET status='queued', message='استعادة المهمة بعد إعادة تشغيل الخدمة', updated_at=NOW() WHERE status='processing' AND upload_path IS NOT NULL")


def recoverable_jobs():
    with db() as conn:
        rows = conn.execute("SELECT id,filename,upload_path FROM analyses WHERE status='queued' AND upload_path IS NOT NULL ORDER BY created_at LIMIT 100").fetchall()
        return [dict(row) for row in rows]


def database_metrics():
    started = time.perf_counter()
    with db() as conn:
        row = conn.execute('''SELECT current_database() AS database_name,
            pg_size_pretty(pg_database_size(current_database())) AS database_size,
            pg_database_size(current_database()) AS database_bytes,
            current_setting('server_version') AS server_version,
            (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()) AS active_connections,
            current_setting('max_connections')::int AS max_connections''').fetchone()
    result = dict(row)
    result['response_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return result


def create_donation(intent_id, amount_minor, status='pending'):
    with db() as conn:
        conn.execute('INSERT INTO donations (id,intent_id,amount_minor,status) VALUES (%s,%s,%s,%s) ON CONFLICT (intent_id) DO NOTHING',
                     (intent_id, intent_id, amount_minor, status))


def update_donation_status(intent_id, status):
    if status not in {'succeeded', 'failed', 'processing'}:
        return
    with db() as conn:
        conn.execute('UPDATE donations SET status=%s, updated_at=NOW() WHERE intent_id=%s', (status, intent_id))


def get_donation_by_intent(intent_id):
    with db() as conn:
        row = conn.execute('SELECT intent_id,status,amount_minor,created_at FROM donations WHERE intent_id=%s', (intent_id,)).fetchone()
        return dict(row) if row else None
