import os
import time
import psycopg
from psycopg.rows import dict_row


def connect():
    url = os.getenv('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL غير مضبوط في متغيرات Fly.io.')
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=8)


def init_db():
    with connect() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                message TEXT NOT NULL DEFAULT 'بانتظار بدء التحليل',
                upload_path TEXT,
                result_path TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS analyses_created_at_idx ON analyses (created_at DESC)')
        conn.execute('CREATE INDEX IF NOT EXISTS analyses_status_idx ON analyses (status)')


def create_analysis(analysis_id, filename, upload_path):
    with connect() as conn:
        conn.execute('INSERT INTO analyses (id, filename, upload_path) VALUES (%s, %s, %s)',
                     (analysis_id, filename, upload_path))


def update_analysis(analysis_id, **fields):
    allowed = {'status', 'message', 'result_path'}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    columns = list(updates)
    assignments = ', '.join(f'{column} = %s' for column in columns)
    values = [updates[column] for column in columns]
    with connect() as conn:
        conn.execute(f'UPDATE analyses SET {assignments}, updated_at = NOW() WHERE id = %s', (*values, analysis_id))


def get_analysis(analysis_id):
    with connect() as conn:
        row = conn.execute('''SELECT id, filename, status, message, result_path,
                              created_at, updated_at FROM analyses WHERE id = %s''', (analysis_id,)).fetchone()
        return dict(row) if row else None


def list_analyses(limit=20):
    with connect() as conn:
        rows = conn.execute('''SELECT id, filename, status, message, created_at, updated_at
                               FROM analyses ORDER BY created_at DESC LIMIT %s''', (limit,)).fetchall()
        return [dict(row) for row in rows]


def database_metrics():
    started = time.perf_counter()
    with connect() as conn:
        row = conn.execute('''
            SELECT current_database() AS database_name,
                   pg_size_pretty(pg_database_size(current_database())) AS database_size,
                   pg_database_size(current_database()) AS database_bytes,
                   current_setting('server_version') AS server_version,
                   (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS active_connections,
                   current_setting('max_connections')::int AS max_connections
        ''').fetchone()
    row = dict(row)
    row['response_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return row


def clear_all_analyses():
    with connect() as conn:
        active = conn.execute("SELECT count(*) AS n FROM analyses WHERE status IN ('queued', 'processing')").fetchone()['n']
        if active:
            return False, active
        deleted = conn.execute('DELETE FROM analyses').rowcount
    return True, deleted
