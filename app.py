import hmac
import logging
import os
import secrets
import shutil
import time
from pathlib import Path

import stripe
from flask import Flask, abort, jsonify, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

import database
from worker import AnalysisWorker, validate_apk

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
log = logging.getLogger('apk-analyzer')


def create_app(test_config=None):
    app = Flask(__name__)
    base_url = os.getenv('BASE_URL', 'http://localhost:8080').rstrip('/')
    app.config.update(
        SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=max(1, int(os.getenv('MAX_UPLOAD_BYTES', str(100 * 1024 * 1024)))),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=base_url.startswith('https://'),
        DATA_DIR=os.getenv('DATA_DIR', '/tmp/apk-analyzer-data'),
    )
    if test_config:
        app.config.update(test_config)

    for folder in ('uploads', 'work', 'results'):
        (Path(app.config['DATA_DIR']) / folder).mkdir(parents=True, exist_ok=True)

    stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')
    if os.getenv('DATABASE_URL') and app.config.get('AUTO_INIT_DB', True):
        database.init_db()
    worker = None
    if os.getenv('DATABASE_URL') and app.config.get('RUN_WORKER', True):
        worker = AnalysisWorker(app.config['DATA_DIR'])
        worker.start()
    app.extensions['analysis_worker'] = worker

    @app.context_processor
    def csrf_context():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return {'csrf_token': session['csrf_token']}

    @app.before_request
    def csrf_protect():
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.endpoint != 'stripe_webhook':
            expected = session.get('csrf_token', '')
            provided = request.headers.get('X-CSRFToken') or request.form.get('csrf_token', '')
            if not expected or not provided or not hmac.compare_digest(expected, provided):
                abort(403, description='تعذر التحقق الأمني من الطلب. حدّث الصفحة وحاول مجدداً.')

    @app.after_request
    def secure_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/') else response.headers.get('Cache-Control', 'no-cache')
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    def db_error_response():
        return jsonify(error='الخدمة غير متاحة مؤقتاً. تحقق من اتصال قاعدة البيانات.'), 503

    @app.get('/')
    def index():
        try:
            history = database.list_analyses(8)
        except Exception:
            log.exception('Could not load analyses')
            history = []
        return render_template('index.html', history=history, max_upload=app.config['MAX_CONTENT_LENGTH'], admin_enabled=bool(os.getenv('ADMIN_TOKEN')))

    @app.get('/history')
    def history_page():
        try:
            items = database.list_analyses(100)
        except Exception:
            log.exception('Could not load history')
            items = []
        return render_template('history.html', history=items)

    @app.get('/analysis/<job_id>')
    def analysis_detail(job_id):
        try:
            item = database.get_analysis(job_id)
        except Exception:
            return db_error_response()
        if not item:
            abort(404)
        return render_template('analysis_detail.html', analysis=item)

    @app.get('/api/csrf-token')
    def csrf_token_endpoint():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return jsonify(csrf_token=session['csrf_token'])

    @app.post('/api/analyze')
    def upload_apk():
        remote = request.remote_addr or 'unknown'
        now = time.time()
        window = app.extensions.setdefault('upload_rate', {})
        recent = [t for t in window.get(remote, []) if now - t < 60]
        if len(recent) >= 5:
            return jsonify(error='طلبات كثيرة. انتظر دقيقة ثم حاول مجدداً.'), 429
        recent.append(now)
        window[remote] = recent
        uploaded = request.files.get('apk')
        if not uploaded or not uploaded.filename:
            return jsonify(error='اختر ملف APK أولاً.'), 400
        if Path(uploaded.filename).suffix.lower() != '.apk':
            return jsonify(error='يجب أن يكون امتداد الملف APK.'), 400
        job_id = secrets.token_hex(16)
        filename = secure_filename(uploaded.filename) or 'application.apk'
        target = Path(app.config['DATA_DIR']) / 'uploads' / f'{job_id}.apk'
        try:
            uploaded.save(target)
            if target.stat().st_size == 0 or target.stat().st_size > app.config['MAX_CONTENT_LENGTH']:
                raise ValueError('حجم الملف فارغ أو يتجاوز الحد المسموح.')
            fingerprint = validate_apk(target)
            database.enqueue_analysis(job_id, filename, str(target), target.stat().st_size, fingerprint)
            return jsonify(id=job_id, status='queued', detail=url_for('analysis_detail', job_id=job_id)), 202
        except database.DuplicateAnalysisError as exc:
            target.unlink(missing_ok=True)
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            target.unlink(missing_ok=True)
            return jsonify(error=str(exc)), 400
        except Exception:
            target.unlink(missing_ok=True)
            log.exception('Upload failed')
            return db_error_response()

    @app.get('/api/history')
    def history_api():
        try:
            return jsonify(database.list_analyses(100))
        except Exception:
            log.exception('History API failed')
            return db_error_response()

    @app.get('/api/status/<job_id>')
    def status_api(job_id):
        try:
            item = database.get_analysis(job_id)
        except Exception:
            return db_error_response()
        if not item:
            return jsonify(error='عملية التحليل غير موجودة.'), 404
        return jsonify(item)

    @app.post('/api/cancel/<job_id>')
    def cancel_api(job_id):
        try:
            changed = database.request_cancel(job_id)
        except Exception:
            log.exception('Cancel request failed')
            return db_error_response()
        if not changed:
            return jsonify(error='لا يمكن إلغاء هذه المهمة؛ ربما انتهت بالفعل.'), 409
        return jsonify(status='cancel_requested')

    @app.post('/api/admin/clear-database')
    def clear_database_api():
        expected = os.getenv('ADMIN_TOKEN', '')
        supplied = request.headers.get('X-Admin-Token', '')
        if not expected:
            return jsonify(error='ميزة مسح قاعدة البيانات غير مفعلة. اضبط ADMIN_TOKEN في أسرار Fly.io.'), 503
        if not supplied or not hmac.compare_digest(supplied, expected):
            return jsonify(error='رمز الإدارة غير صحيح.'), 403
        if request.get_json(silent=True, force=False) is None or request.get_json(silent=True).get('confirm') != 'DELETE':
            return jsonify(error='أرسل تأكيداً صريحاً بقيمة DELETE لإكمال المسح.'), 400
        try:
            cleared, active = database.clear_all_database_data()
            if not cleared:
                return jsonify(error=f'لم يتم المسح: توجد {active} مهام قيد التنفيذ. ألغها أو انتظر انتهاءها.'), 409
            removed = 0
            data_root = Path(app.config['DATA_DIR']).resolve()
            for name in ('uploads', 'work', 'results'):
                folder = (data_root / name).resolve()
                if data_root not in folder.parents or not folder.is_dir():
                    continue
                for item in folder.iterdir():
                    try:
                        if item.is_dir() and not item.is_symlink():
                            shutil.rmtree(item)
                        else:
                            item.unlink(missing_ok=True)
                        removed += 1
                    except OSError:
                        log.exception('Unable to remove analysis artifact %s', item)
            return jsonify(ok=True, deleted_rows=cleared, removed_files=removed,
                           warning='تم حذف جميع سجلات التحليلات والتبرعات وأحداث Stripe. لم يتم حذف المخطط أو قاعدة البيانات نفسها.')
        except Exception:
            log.exception('Full database data clear failed')
            return jsonify(error='تعذر مسح البيانات. راجع سجلات الخادم.'), 503

    @app.get('/download/<job_id>')
    def download(job_id):
        try:
            item = database.get_analysis(job_id)
        except Exception:
            return db_error_response()
        if not item or item['status'] != 'completed' or not item.get('result_path'):
            abort(404)
        root = (Path(app.config['DATA_DIR']) / 'results').resolve()
        target = Path(item['result_path']).resolve()
        if root not in target.parents or not target.is_file():
            abort(404, description='ملف النتيجة غير موجود.')
        return send_file(target, as_attachment=True, download_name=f'apk-recovered-{job_id[:8]}.zip')

    @app.get('/donate')
    def donate():
        return render_template('donate.html', publishable_key=os.getenv('STRIPE_PUBLISHABLE_KEY', ''))

    @app.post('/api/create-payment-intent')
    def create_payment_intent():
        secret_key = os.getenv('STRIPE_SECRET_KEY', '').strip()
        publishable_key = os.getenv('STRIPE_PUBLISHABLE_KEY', '').strip()
        if not secret_key or not publishable_key:
            log.error('Stripe configuration is missing')
            return jsonify(error='الدفع غير مهيأ. تحقق من مفاتيح Stripe في أسرار Fly.io.'), 503
        if secret_key.startswith('sk_test_') != publishable_key.startswith('pk_test_'):
            return jsonify(error='مفاتيح Stripe غير متطابقة بين وضع الاختبار والوضع الفعلي.'), 503
        try:
            stripe.api_key = secret_key
            intent = stripe.PaymentIntent.create(
                amount=1000, currency='gbp',
                automatic_payment_methods={'enabled': True},
                description='تبرع لدعم APK Analyzer',
                metadata={'purpose': 'apk-analyzer-donation'},
            )
        except Exception as exc:
            log.exception('Stripe PaymentIntent creation failed (%s)', type(exc).__name__)
            return jsonify(error='رفض Stripe إنشاء عملية الدفع. تحقق من المفاتيح ووضع Test/Live، ثم راجع سجلات Fly.'), 502
        try:
            database.create_donation(intent.id, 1000)
        except Exception:
            log.exception('Could not persist Stripe intent %s', intent.id)
            try:
                stripe.PaymentIntent.cancel(intent.id)
            except Exception:
                log.exception('Could not cancel unrecorded intent')
            return jsonify(error='تعذر حفظ سجل الدفع في قاعدة البيانات؛ حاول لاحقاً.'), 503
        return jsonify(client_secret=intent.client_secret, payment_intent_id=intent.id)

    @app.post('/api/stripe/webhook')
    def stripe_webhook():
        secret = os.getenv('STRIPE_WEBHOOK_SECRET', '')
        if not secret:
            return jsonify(error='Webhook غير مهيأ.'), 503
        try:
            event = stripe.Webhook.construct_event(request.get_data(), request.headers.get('Stripe-Signature', ''), secret)
        except (ValueError, stripe.error.SignatureVerificationError):
            return jsonify(error='توقيع Stripe غير صالح.'), 400
        transitions = {'payment_intent.succeeded': 'succeeded', 'payment_intent.payment_failed': 'failed', 'payment_intent.processing': 'processing'}
        obj = event['data']['object']
        if event['type'] in transitions:
            if obj.get('currency') != 'gbp' or obj.get('amount') != 1000 or obj.get('metadata', {}).get('purpose') != 'apk-analyzer-donation':
                log.warning('Ignoring unexpected donation intent %s', obj.get('id'))
                return jsonify(received=True)
            try:
                database.apply_stripe_event(event['id'], obj['id'], transitions[event['type']])
            except Exception:
                log.exception('Could not persist Stripe event')
                return jsonify(error='تعذر تسجيل حالة الدفع؛ سيعيد Stripe إرسال الحدث.'), 503
        return jsonify(received=True)

    @app.get('/donation-status')
    def donation_status():
        intent_id = request.args.get('payment_intent', '')
        if not intent_id:
            client_secret = request.args.get('payment_intent_client_secret', '')
            if '_secret_' in client_secret:
                intent_id = client_secret.split('_secret_', 1)[0]
        try:
            donation = database.get_donation(intent_id) if intent_id else None
        except Exception:
            log.exception('Could not load donation status')
            donation = None
        return render_template('donation_status.html', donation=donation)

    @app.get('/health')
    def health():
        return jsonify(ok=True, service='apk-analyzer')

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error='الملف أكبر من الحد المسموح به.'), 413

    @app.errorhandler(403)
    def forbidden(error):
        return jsonify(error=error.description or 'الطلب غير مسموح.'), 403

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith('/api/'):
            return jsonify(error='المورد المطلوب غير موجود.'), 404
        return render_template('error.html', message=getattr(error, 'description', None) or 'الصفحة المطلوبة غير موجودة.'), 404

    @app.errorhandler(500)
    def internal_error(_error):
        log.exception('Unhandled request error')
        return jsonify(error='حدث خطأ داخلي. يرجى المحاولة لاحقاً.'), 500

    return app


app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')))
