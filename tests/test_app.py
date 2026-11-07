import io
import zipfile

import pytest

from app import create_app
from worker import ToolTimeoutError, validate_apk


@pytest.fixture
def app_client(tmp_path):
    app = create_app({'TESTING': True, 'AUTO_INIT_DB': False, 'RUN_WORKER': False, 'DATA_DIR': str(tmp_path), 'MAX_CONTENT_LENGTH': 1024 * 1024})
    with app.test_client() as client:
        yield client


def csrf(client):
    response = client.get('/')
    assert response.status_code == 200
    with client.session_transaction() as session:
        return session['csrf_token']


def test_health_is_available(app_client):
    response = app_client.get('/health')
    assert response.status_code == 200
    assert response.json['ok'] is True


def test_homepage_renders_rtl(app_client):
    response = app_client.get('/')
    assert response.status_code == 200
    assert 'dir="rtl"' in response.get_data(as_text=True)


def test_csrf_required_for_upload(app_client):
    response = app_client.post('/api/analyze', data={})
    assert response.status_code == 403


def test_invalid_upload_rejected(app_client):
    response = app_client.post('/api/analyze', data={'apk': (io.BytesIO(b'not apk'), 'bad.apk')}, headers={'X-CSRFToken': csrf(app_client)})
    assert response.status_code == 400


def test_non_apk_extension_rejected(app_client):
    response = app_client.post('/api/analyze', data={'apk': (io.BytesIO(b'x'), 'bad.zip')}, headers={'X-CSRFToken': csrf(app_client)})
    assert response.status_code == 400


def test_validate_apk_requires_manifest(tmp_path):
    fake = tmp_path / 'fake.apk'
    with zipfile.ZipFile(fake, 'w') as archive:
        archive.writestr('classes.dex', b'dex\0')
    with pytest.raises(ValueError, match='AndroidManifest'):
        validate_apk(fake)


def test_timeout_error_keeps_tool_log():
    exc = ToolTimeoutError('JADX', 42, 'partial output')
    assert exc.seconds == 42
    assert exc.output == 'partial output'
    assert 'JADX' in str(exc)


def test_clear_database_requires_admin_secret_and_confirmation(app_client, monkeypatch):
    monkeypatch.setenv('ADMIN_TOKEN', 'test-admin-token')
    token = csrf(app_client)
    response = app_client.post('/api/admin/clear-database', json={'confirm': 'NO'}, headers={'X-CSRFToken': token, 'X-Admin-Token': 'test-admin-token'})
    assert response.status_code == 400
