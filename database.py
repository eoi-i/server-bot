import os
import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv('DATABASE_URL')


def connect():
    if not DATABASE_URL:
        raise RuntimeError('DATABASE_URL غير مضبوط. أضفه إلى أسرار Fly.io أو متغيرات البيئة.')
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)


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
