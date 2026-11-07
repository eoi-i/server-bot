import os
import re
import uuid
import threading
import subprocess
import shutil
import zipfile
import time
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, send_file, abort
from werkzeug.utils import secure_filename
from database import (init_db, create_analysis, update_analysis, get_analysis,
                      list_analyses, database_metrics, clear_all_analyses)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv('DATA_DIR', '/tmp/apk-analyzer-data'))
UPLOAD_DIR = DATA_DIR / 'uploads'
WORK_DIR = DATA_DIR / 'work'
RESULT_DIR = DATA_DIR / 'results'
MAX_FAILED_DEX = 12
ALLOWED_EXTENSIONS = {'.apk'}

for directory in (UPLOAD_DIR, WORK_DIR, RESULT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
# No application-level APK size limit. Actual limits may still apply at the hosting/network layer.
app.config['MAX_CONTENT_LENGTH'] = None
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-this-secret-before-production')

init_db()


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def tool_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f'أداة {name} غير مثبتة أو غير موجودة في PATH')
    return path


def run_command(command, cwd=None, timeout=2400, allowed_returncodes=(0,)):
    result = subprocess.run(command, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors='replace',
                            timeout=timeout, check=False)
    output = result.stdout or ''
    if result.returncode not in allowed_returncodes:
        raise RuntimeError(f"فشل تنفيذ الأمر (exit {result.returncode}):\n{output[-9000:]}")
    return output, result.returncode


def dex_entries(apk_path: Path):
    try:
        with zipfile.ZipFile(apk_path) as zf:
            return sorted(n for n in zf.namelist()
                          if re.fullmatch(r'classes(?:\d+)?\.dex', Path(n).name))
    except Exception:
        return []


def expected_smali_dir(apktool_dir: Path, dex_name: str):
    if dex_name == 'classes.dex':
        return apktool_dir / 'smali'
    number = re.fullmatch(r'classes(\d+)\.dex', dex_name)
    return apktool_dir / f"smali_classes{number.group(1)}" if number else apktool_dir / 'smali'


def make_project_skeleton(project_dir: Path, package_name: str):
    app_dir = project_dir / 'app' / 'src' / 'main'
    (app_dir / 'java' / 'recovered').mkdir(parents=True, exist_ok=True)
    (app_dir / 'res').mkdir(parents=True, exist_ok=True)
    (app_dir / 'assets').mkdir(parents=True, exist_ok=True)
    manifest = f'''<?xml version="1.0" encoding="utf-8"?>\n<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="{package_name}">\n    <application android:label="Recovered APK" android:allowBackup="false" android:supportsRtl="true">\n    </application>\n</manifest>\n'''
    (app_dir / 'AndroidManifest.xml').write_text(manifest, encoding='utf-8')
    (project_dir / 'settings.gradle').write_text("pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\ndependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }\nrootProject.name = 'RecoveredAPK'\ninclude ':app'\n", encoding='utf-8')
    (project_dir / 'build.gradle').write_text("plugins { id 'com.android.application' version '8.7.3' apply false }\n", encoding='utf-8')
    (project_dir / 'app' / 'build.gradle').write_text("plugins { id 'com.android.application' }\n\nandroid {\n    namespace 'recovered.project'\n    compileSdk 35\n    defaultConfig { applicationId 'recovered.project'; minSdk 23; targetSdk 35; versionCode 1; versionName '1.0' }\n}\n", encoding='utf-8')
    (project_dir / 'README-REBUILD.txt').write_text(
        'مشروع استعادة أولي للموارد والشفرة المفككة.\n'
        'JADX ينتج غالباً Java-like حتى لو كان التطبيق الأصلي Kotlin.\n'
        'ملفات Smali هي بديل احتياطي للفئات التي تعذر تحويلها إلى Java.\n'
        'قد تحتاج إلى إصلاح التبعيات والموارد يدوياً قبل نجاح البناء.\n', encoding='utf-8')


def analyze_job(analysis_id: str, apk_path: Path, original_name: str):
    job_dir = WORK_DIR / analysis_id
    output_dir = RESULT_DIR / analysis_id
    jadx_dir = job_dir / 'jadx'
    apktool_dir = job_dir / 'apktool'
    project_dir = job_dir / 'android-project'
    logs = []
    failed_dex = []
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        update_analysis(analysis_id, status='processing', message='جارٍ فحص ملف APK...')
        jadx = tool_path('jadx')
        apktool = tool_path('apktool')
        dex_files = dex_entries(apk_path)
        update_analysis(analysis_id, message='المرحلة 1/3: فك الشفرة باستخدام JADX...')

        # JADX code 3 means partial decompilation; keep all generated sources and try Apktool next.
        jadx_log, jadx_code = run_command(
            [jadx, '--deobf', '--show-bad-code', '--threads-count', str(max(2, min(os.cpu_count() or 2, 8))),
             '-d', str(jadx_dir), str(apk_path)], allowed_returncodes=(0, 3))
        (job_dir / 'jadx-log.txt').write_text(jadx_log, encoding='utf-8', errors='replace')
        if jadx_code == 3:
            logs.append('JADX أنهى الفك جزئياً؛ يجري الآن استخدام Apktool كأداة بديلة لاستخراج Smali والموارد.')

        update_analysis(analysis_id, message='المرحلة 2/3: محاولة الاستعادة البديلة باستخدام Apktool...')
        apktool_error = None
        try:
            apktool_log, _ = run_command([apktool, 'd', '-f', str(apk_path), '-o', str(apktool_dir)], timeout=2400)
            (job_dir / 'apktool-log.txt').write_text(apktool_log, encoding='utf-8', errors='replace')
        except Exception as exc:
            apktool_error = str(exc)
            logs.append('تعذر على Apktool فك بعض محتويات الحزمة: ' + apktool_error[-2500:])

        # Count failed DEX containers only when neither decompiler produced the alternate Smali output.
        for dex_name in dex_files:
            smali_dir = expected_smali_dir(apktool_dir, dex_name)
            has_smali = smali_dir.exists() and any(smali_dir.rglob('*.smali'))
            if not has_smali:
                failed_dex.append(dex_name)
        if len(failed_dex) > MAX_FAILED_DEX:
            raise RuntimeError(
                f'فشلت استعادة {len(failed_dex)} ملفات DEX، والحد الأقصى المسموح {MAX_FAILED_DEX}. '
                'أُلغيت العملية بالكامل. الملفات: ' + ', '.join(failed_dex[:30]))
        if failed_dex:
            logs.append(f'تم تجاوز {len(failed_dex)} ملفات DEX تعذر فكها بالأداتين: {", ".join(failed_dex)}')
        if apktool_error and not any(apktool_dir.rglob('*')):
            raise RuntimeError('فشل JADX وApktool ولم ينتج أي محتوى بديل. ' + apktool_error[-2500:])

        update_analysis(analysis_id, message='المرحلة 3/3: تجهيز المشروع وضغط النتائج...')
        project_dir.mkdir(parents=True, exist_ok=True)
        package_name = 'recovered.project'
        manifest_path = apktool_dir / 'AndroidManifest.xml'
        if manifest_path.exists():
            match = re.search(r'package="([A-Za-z0-9_.]+)"', manifest_path.read_text(encoding='utf-8', errors='ignore'))
            if match:
                package_name = match.group(1)
        make_project_skeleton(project_dir, package_name)

        if (jadx_dir / 'sources').exists():
            shutil.copytree(jadx_dir / 'sources', project_dir / 'app/src/main/java/recovered/sources', dirs_exist_ok=True)
        if (apktool_dir / 'res').exists():
            shutil.copytree(apktool_dir / 'res', project_dir / 'app/src/main/res', dirs_exist_ok=True)
        if manifest_path.exists():
            shutil.copy2(manifest_path, project_dir / 'app/src/main/AndroidManifest.xml')
        for name in ('assets', 'lib'):
            src = apktool_dir / name
            if src.exists():
                shutil.copytree(src, project_dir / 'app/src/main' / name, dirs_exist_ok=True)
        for smali_dir in sorted(apktool_dir.glob('smali*')):
            if smali_dir.is_dir():
                shutil.copytree(smali_dir, project_dir / 'smali' / smali_dir.name, dirs_exist_ok=True)
        if (apktool_dir / 'original').exists():
            shutil.copytree(apktool_dir / 'original', project_dir / 'apktool-original', dirs_exist_ok=True)

        java_count = sum(1 for _ in (jadx_dir / 'sources').rglob('*.java')) if (jadx_dir / 'sources').exists() else 0
        smali_count = sum(1 for _ in apktool_dir.rglob('*.smali')) if apktool_dir.exists() else 0
        (project_dir / 'ANALYSIS-REPORT.txt').write_text(
            f'APK: {original_name}\nملفات DEX: {len(dex_files)}\nملفات Java الناتجة: {java_count}\nملفات Smali المستعادة: {smali_count}\nملفات DEX الفاشلة: {len(failed_dex)} من الحد الأقصى {MAX_FAILED_DEX}\n\n' + '\n'.join(logs),
            encoding='utf-8')
        if logs:
            (project_dir / 'RECOVERY-WARNINGS.txt').write_text('\n'.join(logs) + '\n', encoding='utf-8')

        archive_path = output_dir / f'{analysis_id}-recovered-project.zip'
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for file_path in project_dir.rglob('*'):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(project_dir.parent))
            for log_name in ('jadx-log.txt', 'apktool-log.txt'):
                log_path = job_dir / log_name
                if log_path.exists():
                    archive.write(log_path, log_name)
            for root_name, root in [('jadx-output', jadx_dir), ('apktool-output', apktool_dir)]:
                if root.exists():
                    for file_path in root.rglob('*'):
                        if file_path.is_file():
                            archive.write(file_path, Path(root_name) / file_path.relative_to(root))

        message = f'اكتمل التحليل. Java: {java_count}، Smali: {smali_count}، ملفات DEX المتجاوزة: {len(failed_dex)}.'
        if logs:
            message += ' توجد ملاحظات داخل RECOVERY-WARNINGS.txt.'
        update_analysis(analysis_id, status='completed', message=message, result_path=str(archive_path))
    except Exception as exc:
        update_analysis(analysis_id, status='failed', message=str(exc)[:8000])
    finally:
        try:
            apk_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.get('/')
def index():
    return render_template('index.html', history=list_analyses(limit=10), admin_enabled=bool(os.getenv('ADMIN_TOKEN')))


@app.post('/api/analyze')
def upload_apk():
    uploaded = request.files.get('apk')
    if not uploaded or not uploaded.filename:
        return jsonify({'error': 'اختر ملف APK أولاً'}), 400
    if not allowed_file(uploaded.filename):
        return jsonify({'error': 'الملف يجب أن يكون بامتداد APK'}), 400
    analysis_id = uuid.uuid4().hex
    safe_name = secure_filename(uploaded.filename) or 'application.apk'
    stored_path = UPLOAD_DIR / f'{analysis_id}-{safe_name}'
    uploaded.save(stored_path)
    try:
        create_analysis(analysis_id, safe_name, str(stored_path))
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise
    threading.Thread(target=analyze_job, args=(analysis_id, stored_path, safe_name), daemon=True).start()
    return jsonify({'id': analysis_id, 'status': 'queued'})


@app.get('/api/status/<analysis_id>')
def status(analysis_id):
    record = get_analysis(analysis_id)
    if not record:
        return jsonify({'error': 'العملية غير موجودة'}), 404
    return jsonify(record)


@app.get('/api/history')
def history():
    return jsonify(list_analyses(limit=50))


@app.get('/api/metrics')
def metrics():
    try:
        db = database_metrics()
        try:
            import psutil
            process = psutil.Process(os.getpid())
            memory = process.memory_info().rss
            system = psutil.virtual_memory()
            memory_info = {'process_bytes': memory, 'process': _human_bytes(memory),
                           'system_total': _human_bytes(system.total),
                           'system_available': _human_bytes(system.available),
                           'system_percent': system.percent}
        except Exception:
            memory_info = {'process': 'غير متاح', 'system_total': 'غير متاح', 'system_available': 'غير متاح'}
        disk = shutil.disk_usage(DATA_DIR)
        db['connected'] = True
        return jsonify({'database': db, 'memory': memory_info,
                        'disk': {'total': _human_bytes(disk.total), 'free': _human_bytes(disk.free),
                                 'used_percent': round(disk.used / disk.total * 100, 1) if disk.total else 0},
                        'server_time': datetime.now(timezone.utc).isoformat()})
    except Exception as exc:
        return jsonify({'database': {'connected': False, 'error': str(exc)[:500]}, 'server_time': datetime.now(timezone.utc).isoformat()}), 503


def _human_bytes(value):
    value = float(value)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1024 or unit == 'TB':
            return f'{value:.1f} {unit}'
        value /= 1024


@app.post('/api/admin/clear-database')
def clear_database():
    expected = os.getenv('ADMIN_TOKEN')
    supplied = request.headers.get('X-Admin-Token', '')
    if not expected:
        return jsonify({'error': 'ميزة المسح غير مفعلة. أضف ADMIN_TOKEN إلى أسرار التطبيق.'}), 503
    import hmac
    if not hmac.compare_digest(supplied, expected):
        return jsonify({'error': 'رمز الإدارة غير صحيح.'}), 403
    try:
        ok, count = clear_all_analyses()
        if not ok:
            return jsonify({'error': f'لا يمكن المسح حالياً؛ توجد {count} عمليات قيد التنفيذ.'}), 409
        removed_files = 0
        for folder in (UPLOAD_DIR, WORK_DIR, RESULT_DIR):
            for item in folder.iterdir():
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink(missing_ok=True)
                    removed_files += 1
                except Exception:
                    pass
        return jsonify({'ok': True, 'deleted_records': count, 'removed_items': removed_files})
    except Exception as exc:
        return jsonify({'error': str(exc)[:1000]}), 500


@app.get('/download/<analysis_id>')
def download(analysis_id):
    record = get_analysis(analysis_id)
    if not record or record.get('status') != 'completed' or not record.get('result_path'):
        abort(404)
    path = Path(record['result_path'])
    if not path.is_file():
        abort(404, description='ملف النتيجة غير موجود؛ مساحة الملفات المؤقتة قد تكون أُعيد إنشاؤها.')
    return send_file(path, as_attachment=True, download_name=f'apk-recovered-{analysis_id[:8]}.zip')


@app.errorhandler(413)
def too_large(_error):
    return jsonify({'error': 'تعذر استقبال الملف بسبب حدّ مفروض من الخادم أو وسيط الشبكة، وليس من حد داخل التطبيق.'}), 413


@app.get('/health')
def health():
    return jsonify({'ok': True, 'service': 'apk-analyzer', 'time': datetime.now(timezone.utc).isoformat()})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')), debug=False)
