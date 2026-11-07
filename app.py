import hashlib
import hmac
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import stripe
from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

from database import (
    create_analysis, create_donation, database_metrics, find_analysis_by_fingerprint,
    get_analysis, get_donation_by_intent, init_db, list_analyses,
    mark_interrupted_jobs, recoverable_jobs, update_analysis, update_donation_status,
)

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('apk-analyzer')
ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv('DATA_DIR', '/tmp/apk-analyzer-data')).resolve()
UPLOAD_DIR, WORK_DIR, RESULT_DIR = (DATA_DIR / x for x in ('uploads', 'work', 'results'))
for folder in (UPLOAD_DIR, WORK_DIR, RESULT_DIR):
    folder.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = max(1, int(os.getenv('MAX_UPLOAD_BYTES', 100 * 1024 * 1024)))
MAX_ANALYSIS_SECONDS = max(60, int(os.getenv('MAX_ANALYSIS_SECONDS', 1800)))
MAX_JADX_DEX = max(1, int(os.getenv('MAX_JADX_DEX', 12)))
MAX_WORKERS = max(1, int(os.getenv('MAX_WORKERS', 2)))
BASE_URL = os.getenv('BASE_URL', 'http://localhost:8080').rstrip('/')
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=BASE_URL.startswith('https://'),
)
stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
init_db()
POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix='analysis')
TASKS = {}
TASK_LOCK = threading.Lock()
LIMITS = {}


def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(32)
    return session['csrf']


@app.before_request
def csrf_check():
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.endpoint != 'stripe_webhook':
        expected = session.get('csrf')
        supplied = request.headers.get('X-CSRFToken', '')
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(403, description='تعذر التحقق الأمني من الطلب. حدّث الصفحة وحاول مجدداً.')


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    if request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response


def limited(label, maximum, seconds=60):
    now = time.monotonic()
    hits = LIMITS.setdefault(label, [])
    hits[:] = [t for t in hits if now - t < seconds]
    if len(hits) >= maximum:
        return True
    hits.append(now)
    return False


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def dex_entries(apk):
    with zipfile.ZipFile(apk) as archive:
        return sorted(name for name in archive.namelist()
                      if re.fullmatch(r'classes(?:[2-9]\d*)?\.dex|classes1\.dex', Path(name).name))


def run_tool(args, timeout):
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors='replace', timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'انتهت مهلة الأداة بعد {timeout} ثانية.') from exc
    return result.stdout[-12000:], result.returncode


def safe_update(job_id, **kwargs):
    update_analysis(job_id, **kwargs)


def analyze(job_id, apk_path, filename, cancel):
    job = WORK_DIR / job_id
    output = RESULT_DIR / job_id
    jadx_dir, apktool_dir = job / 'jadx', job / 'apktool'
    logs = []
    try:
        job.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        safe_update(job_id, status='processing', progress=3, message='بدء تحليل الحزمة...')
        if cancel.is_set():
            raise InterruptedError('ألغى المستخدم المهمة.')
        if not zipfile.is_zipfile(apk_path):
            raise ValueError('ملف APK تالف أو ليس ملف ZIP صالحاً.')
        dex = dex_entries(apk_path)
        if len(dex) > MAX_JADX_DEX:
            logs.append(f'عدد ملفات DEX هو {len(dex)}، ويتجاوز حد التنبيه MAX_JADX_DEX={MAX_JADX_DEX}. ستتم متابعة المعالجة ضمن حدود الموارد.')
        jadx = shutil.which('jadx')
        apktool = shutil.which('apktool')
        if not jadx or not apktool:
            raise RuntimeError('أدوات JADX وApktool مطلوبة. تحقق من تثبيت الأدوات وPATH.')
        safe_update(job_id, progress=10, message='JADX: فك الشفرة والمصادر...')
        out, code = run_tool([jadx, '--deobf', '--show-bad-code', '--threads-count', str(min(os.cpu_count() or 2, 4)), '-d', str(jadx_dir), str(apk_path)], MAX_ANALYSIS_SECONDS)
        (job / 'jadx.log').write_text(out, encoding='utf-8')
        if code not in (0, 3):
            logs.append(f'تعثر JADX برمز {code}؛ الاحتفاظ بأي مخرجات جزئية ومتابعة Apktool.')
        if cancel.is_set():
            raise InterruptedError('ألغى المستخدم المهمة بعد JADX.')
        safe_update(job_id, progress=45, message='Apktool: استخراج Smali والموارد والـ Manifest...')
        out, code = run_tool([apktool, 'd', '-f', str(apk_path), '-o', str(apktool_dir)], MAX_ANALYSIS_SECONDS)
        (job / 'apktool.log').write_text(out, encoding='utf-8')
        if code != 0:
            logs.append(f'تعثر Apktool برمز {code}؛ الاحتفاظ بالمخرجات المتاحة.')
        if cancel.is_set():
            raise InterruptedError('ألغى المستخدم المهمة بعد Apktool.')
        sources = jadx_dir / 'sources'
        smali_count = sum(1 for _ in apktool_dir.rglob('*.smali')) if apktool_dir.exists() else 0
        java_count = sum(1 for _ in sources.rglob('*.java')) if sources.exists() else 0
        has_content = java_count or smali_count or (apktool_dir / 'AndroidManifest.xml').exists() or any(apktool_dir.glob('res/*')) if apktool_dir.exists() else bool(java_count)
        if not has_content:
            raise RuntimeError('لم تنتج أدوات التحليل أي ملفات قابلة للاستعادة. راجع سجلات الأدوات.')
        safe_update(job_id, progress=72, message='إعداد مشروع قابل للتحرير وضغط النتائج...')
        project = job / 'android-project'
        main = project / 'app/src/main'
        (main / 'java').mkdir(parents=True, exist_ok=True)
        (main / 'res').mkdir(parents=True, exist_ok=True)
        (main / 'assets').mkdir(parents=True, exist_ok=True)
        if sources.exists():
            shutil.copytree(sources, main / 'java/recovered', dirs_exist_ok=True)
        for name in ('res', 'assets', 'lib'):
            src = apktool_dir / name
            if src.exists():
                shutil.copytree(src, main / name, dirs_exist_ok=True)
        manifest = apktool_dir / 'AndroidManifest.xml'
        if manifest.exists():
            shutil.copy2(manifest, main / 'AndroidManifest.xml')
        for child in apktool_dir.glob('smali*'):
            if child.is_dir():
                shutil.copytree(child, project / 'smali' / child.name, dirs_exist_ok=True)
        (project / 'settings.gradle').write_text("pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\ndependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }\nrootProject.name = 'RecoveredProject'\ninclude ':app'\n", encoding='utf-8')
        (project / 'build.gradle').write_text("plugins { id 'com.android.application' version '8.7.3' apply false }\n", encoding='utf-8')
        (project / 'app/build.gradle').write_text("plugins { id 'com.android.application' }\nandroid { namespace 'recovered.project'; compileSdk 35; defaultConfig { applicationId 'recovered.project'; minSdk 23; targetSdk 35; versionCode 1; versionName '1.0' } }\n", encoding='utf-8')
        (project / 'ANALYSIS-REPORT.txt').write_text(f'APK: {filename}\nDEX files: {len(dex)}\nJava files: {java_count}\nSmali files: {smali_count}\n', encoding='utf-8')
        if logs:
            (project / 'RECOVERY-WARNINGS.txt').write_text('\n'.join(logs), encoding='utf-8')
        archive = output / f'{job_id}-recovered.zip'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for path in project.rglob('*'):
                if path.is_file():
                    zf.write(path, path.relative_to(project.parent))
            for path in (job / 'jadx.log', job / 'apktool.log'):
                if path.exists():
                    zf.write(path, path.name)
            for label, root in (('jadx-output', jadx_dir), ('apktool-output', apktool_dir)):
                if root.exists():
                    for path in root.rglob('*'):
                        if path.is_file():
                            zf.write(path, Path(label) / path.relative_to(root))
        safe_update(job_id, status='completed', progress=100, message=f'اكتمل التحليل: {java_count} Java و{smali_count} Smali.', result_path=str(archive))
    except InterruptedError as exc:
        safe_update(job_id, status='cancelled', message=str(exc))
    except Exception as exc:
        log.exception('Analysis %s failed', job_id)
        safe_update(job_id, status='failed', message=str(exc)[:4000])
    finally:
        try:
            apk_path.unlink(missing_ok=True)
        except OSError:
            log.warning('Unable to remove upload for %s', job_id)
        with TASK_LOCK:
            TASKS.pop(job_id, None)


def start_job(job_id, path, filename):
    cancel = threading.Event()
    with TASK_LOCK:
        future = POOL.submit(analyze, job_id, Path(path), filename, cancel)
        TASKS[job_id] = (cancel, future)


def recover_jobs():
    mark_interrupted_jobs()
    for record in recoverable_jobs():
        path = Path(record.get('upload_path') or '')
        if path.is_file():
            start_job(record['id'], path, record['filename'])
        else:
            update_analysis(record['id'], status='failed', message='服務重啟後找不到上傳檔案，無法自動復原此任務。')


recover_jobs()


@app.get('/')
def index():
    return render_template('index.html', csrf=csrf_token(), history=list_analyses(10), limit=MAX_UPLOAD_BYTES)


@app.get('/history')
def history_page():
    return render_template('history.html', csrf=csrf_token(), history=list_analyses(100))


@app.get('/analysis/<job_id>')
def analysis_page(job_id):
    item = get_analysis(job_id)
    if not item:
        abort(404)
    return render_template('analysis_detail.html', csrf=csrf_token(), analysis=item)


@app.get('/api/history')
def history_api():
    return jsonify(list_analyses(100))


@app.get('/api/status/<job_id>')
def status_api(job_id):
    item = get_analysis(job_id)
    if not item:
        return jsonify(error='عملية التحليل غير موجودة.'), 404
    return jsonify(item)


@app.post('/api/analyze')
def upload_api():
    if limited('upload:' + (request.remote_addr or 'unknown'), 5):
        return jsonify(error='طلبات كثيرة. انتظر قليلاً ثم حاول مجدداً.'), 429
    upload = request.files.get('apk')
    if not upload or not upload.filename:
        return jsonify(error='اختر ملف APK أولاً.'), 400
    if Path(upload.filename).suffix.lower() != '.apk':
        return jsonify(error='يجب أن يكون امتداد الملف APK.'), 400
    name = secure_filename(upload.filename) or 'application.apk'
    job_id = uuid.uuid4().hex
    path = UPLOAD_DIR / f'{job_id}.apk'
    upload.save(path)
    try:
        size = path.stat().st_size
        if size == 0 or size > MAX_UPLOAD_BYTES:
            raise ValueError('حجم الملف فارغ أو يتجاوز الحد المسموح.')
        if not zipfile.is_zipfile(path):
            raise ValueError('الملف ليس APK صالحاً أو أن محتواه تالف.')
        fingerprint = digest_file(path)
        previous = find_analysis_by_fingerprint(fingerprint)
        if previous and previous['status'] in {'queued', 'processing', 'completed'}:
            raise FileExistsError('هذا الملف قيد المعالجة أو سبق تحليله.')
        create_analysis(job_id, name, str(path), size, fingerprint)
        start_job(job_id, path, name)
        return jsonify(id=job_id, status='queued', detail=url_for('analysis_page', job_id=job_id)), 202
    except FileExistsError as exc:
        path.unlink(missing_ok=True)
        return jsonify(error=str(exc)), 409
    except Exception as exc:
        path.unlink(missing_ok=True)
        return jsonify(error=str(exc)), 400


@app.post('/api/cancel/<job_id>')
def cancel_api(job_id):
    item = get_analysis(job_id)
    if not item:
        return jsonify(error='عملية التحليل غير موجودة.'), 404
    task = TASKS.get(job_id)
    if item['status'] not in {'queued', 'processing'} or not task:
        return jsonify(error='لا يمكن إلغاء هذه المهمة الآن.'), 409
    task[0].set()
    update_analysis(job_id, status='cancelled', message='تم طلب إلغاء المهمة؛ سيُوقف التحليل عند أقرب نقطة آمنة.')
    return jsonify(status='cancelled')


@app.get('/api/metrics')
def metrics_api():
    try:
        return jsonify(database=database_metrics(), server_time=datetime.now(timezone.utc).isoformat())
    except Exception:
        log.exception('Metrics query failed')
        return jsonify(error='تعذر قراءة مؤشرات قاعدة البيانات.'), 503


@app.get('/donate')
def donate_page():
    return render_template('donate.html', csrf=csrf_token(), publishable_key=os.getenv('STRIPE_PUBLISHABLE_KEY', ''))


@app.post('/api/create-payment-intent')
def create_payment_intent():
    if limited('donate:' + (request.remote_addr or 'unknown'), 8):
        return jsonify(error='طلبات كثيرة. حاول لاحقاً.'), 429
    if not stripe.api_key or not os.getenv('STRIPE_PUBLISHABLE_KEY'):
        return jsonify(error='الدفع غير مهيأ حالياً. يرجى المحاولة لاحقاً.'), 503
    try:
        intent = stripe.PaymentIntent.create(amount=1000, currency='gbp', automatic_payment_methods={'enabled': True}, description='تبرع لمشروع APK Analyzer')
        create_donation(intent.id, 1000, 'pending')
        return jsonify(client_secret=intent.client_secret, payment_intent_id=intent.id)
    except stripe.error.StripeError:
        log.exception('Stripe PaymentIntent creation failed')
        return jsonify(error='تعذر بدء الدفع بأمان. لم يتم حفظ بيانات البطاقة.'), 502


@app.post('/api/stripe/webhook')
def stripe_webhook():
    secret = os.getenv('STRIPE_WEBHOOK_SECRET', '')
    if not secret:
        return jsonify(error='Webhook غير مهيأ.'), 503
    try:
        event = stripe.Webhook.construct_event(request.get_data(), request.headers.get('Stripe-Signature', ''), secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify(error='توقيع Webhook غير صالح.'), 400
    obj = event['data']['object']
    mapping = {'payment_intent.succeeded': 'succeeded', 'payment_intent.payment_failed': 'failed', 'payment_intent.processing': 'processing'}
    if event['type'] in mapping:
        update_donation_status(obj['id'], mapping[event['type']])
    return jsonify(received=True)


@app.get('/donation-status')
def donation_status():
    intent_id = request.args.get('payment_intent') or request.args.get('payment_intent_id', '')
    donation = get_donation_by_intent(intent_id) if intent_id else None
    return render_template('donation_status.html', donation=donation)


@app.get('/download/<job_id>')
def download(job_id):
    record = get_analysis(job_id)
    if not record or record['status'] != 'completed' or not record.get('result_path'):
        abort(404)
    root = RESULT_DIR.resolve()
    target = Path(record['result_path']).resolve()
    if root not in target.parents or not target.is_file():
        abort(404)
    return send_file(target, as_attachment=True, download_name=f'apk-recovered-{job_id[:8]}.zip')


@app.get('/health')
def health():
    return jsonify(ok=True, service='apk-analyzer')


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error=f'الملف أكبر من الحد المسموح ({MAX_UPLOAD_BYTES} bytes).'), 413


@app.errorhandler(403)
def forbidden(error):
    return jsonify(error=error.description or 'الطلب غير مسموح.'), 403


@app.errorhandler(404)
def missing(_error):
    if request.path.startswith('/api/'):
        return jsonify(error='المورد المطلوب غير موجود.'), 404
    return render_template('error.html', message='الصفحة المطلوبة غير موجودة.'), 404


@app.errorhandler(500)
def internal_error(_error):
    log.exception('Unhandled request error')
    return jsonify(error='حدث خطأ داخلي. يرجى المحاولة لاحقاً.'), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
