import os
import uuid
import threading
import subprocess
import shutil
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, send_file, abort
from werkzeug.utils import secure_filename
from database import init_db, create_analysis, update_analysis, get_analysis, list_analyses

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv('DATA_DIR', '/tmp/apk-analyzer-data'))
UPLOAD_DIR = DATA_DIR / 'uploads'
WORK_DIR = DATA_DIR / 'work'
RESULT_DIR = DATA_DIR / 'results'
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '150'))
ALLOWED_EXTENSIONS = {'.apk'}

for directory in (UPLOAD_DIR, WORK_DIR, RESULT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-this-secret-before-production')

init_db()


def allowed_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def tool_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f'أداة {name} غير مثبتة أو غير موجودة في PATH')
    return path


def run_command(command, cwd=None, timeout=1800):
    result = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors='replace',
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        tail = (result.stdout or '')[-5000:]
        raise RuntimeError(f"فشل تنفيذ الأمر (exit {result.returncode}):\n{tail}")
    return result.stdout or ''


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
        'هذا مشروع استعادة أولي للموارد والشفرة المفككة.\n'
        'مخرجات JADX غالباً Java-like حتى لو كان التطبيق الأصلي مكتوباً بـ Kotlin.\n'
        'قد تحتاج إلى إصلاح imports والموارد والتبعيات يدوياً قبل نجاح البناء.\n'
        'المشروع ليس ضماناً لاستعادة المصدر الأصلي حرفياً.\n', encoding='utf-8')


def analyze_job(analysis_id: str, apk_path: Path, original_name: str):
    job_dir = WORK_DIR / analysis_id
    output_dir = RESULT_DIR / analysis_id
    jadx_dir = job_dir / 'jadx'
    apktool_dir = job_dir / 'apktool'
    project_dir = job_dir / 'android-project'
    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        update_analysis(analysis_id, status='processing', message='جارٍ فحص ملف APK...')

        jadx = tool_path('jadx')
        apktool = tool_path('apktool')
        update_analysis(analysis_id, message='استخراج الشفرة باستخدام JADX...')
        run_command([jadx, '--deobf', '--show-bad-code', '-d', str(jadx_dir), str(apk_path)])

        update_analysis(analysis_id, message='استخراج الموارد وملف Manifest باستخدام Apktool...')
        run_command([apktool, ' d', '-f', str(apk_path), '-o', str(apktool_dir)]) if False else run_command([apktool, 'd', '-f', str(apk_path), '-o', str(apktool_dir)])

        update_analysis(analysis_id, message='تجهيز مجلد مشروع Android...')
        project_dir.mkdir(parents=True, exist_ok=True)
        package_name = 'recovered.project'
        manifest_path = apktool_dir / 'AndroidManifest.xml'
        if manifest_path.exists():
            try:
                import re
                match = re.search(r'package="([A-Za-z0-9_.]+)"', manifest_path.read_text(encoding='utf-8', errors='ignore'))
                if match:
                    package_name = match.group(1)
            except Exception:
                pass
        make_project_skeleton(project_dir, package_name)

        # Copy decompiled source and decoded resources into clearly separated folders.
        if (jadx_dir / 'sources').exists():
            shutil.copytree(jadx_dir / 'sources', project_dir / 'app' / 'src' / 'main' / 'java' / 'recovered' / 'sources', dirs_exist_ok=True)
        if (apktool_dir / 'res').exists():
            shutil.copytree(apktool_dir / 'res', project_dir / 'app' / 'src' / 'main' / 'res', dirs_exist_ok=True)
        if manifest_path.exists():
            shutil.copy2(manifest_path, project_dir / 'app' / 'src' / 'main' / 'AndroidManifest.xml')
        if (apktool_dir / 'assets').exists():
            shutil.copytree(apktool_dir / 'assets', project_dir / 'app' / 'src' / 'main' / 'assets', dirs_exist_ok=True)
        if (apktool_dir / 'original').exists():
            shutil.copytree(apktool_dir / 'original', project_dir / 'apktool-original', dirs_exist_ok=True)
        if (apktool_dir / 'smali').exists():
            shutil.copytree(apktool_dir / 'smali', project_dir / 'smali', dirs_exist_ok=True)

        update_analysis(analysis_id, message='ضغط النتائج وتجهيز رابط التنزيل...')
        archive_path = output_dir / f'{analysis_id}-recovered-project.zip'
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            for file_path in project_dir.rglob('*'):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(project_dir.parent))
            # Include full Apktool decode and JADX reports for deeper manual recovery.
            for root_name, root in [('jadx-output', jadx_dir), ('apktool-output', apktool_dir)]:
                if root.exists():
                    for file_path in root.rglob('*'):
                        if file_path.is_file():
                            archive.write(file_path, Path(root_name) / file_path.relative_to(root))

        update_analysis(analysis_id, status='completed', message='اكتمل التحليل بنجاح', result_path=str(archive_path))
    except Exception as exc:
        update_analysis(analysis_id, status='failed', message=str(exc)[:6000])
    finally:
        try:
            apk_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.get('/')
def index():
    return render_template('index.html', max_upload_mb=MAX_UPLOAD_MB, history=list_analyses(limit=10))


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
    create_analysis(analysis_id, safe_name, str(stored_path))
    thread = threading.Thread(target=analyze_job, args=(analysis_id, stored_path, safe_name), daemon=True)
    thread.start()
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


@app.get('/download/<analysis_id>')
def download(analysis_id):
    record = get_analysis(analysis_id)
    if not record or record.get('status') != 'completed' or not record.get('result_path'):
        abort(404)
    path = Path(record['result_path'])
    if not path.is_file():
        abort(404, description='ملف النتيجة غير موجود؛ قد تكون مساحة الملفات المؤقتة قد أُعيد إنشاؤها.')
    return send_file(path, as_attachment=True, download_name=f'apk-recovered-{analysis_id[:8]}.zip')


@app.errorhandler(413)
def too_large(_error):
    return jsonify({'error': f'حجم الملف أكبر من الحد المسموح ({MAX_UPLOAD_MB} MB)'}), 413


@app.get('/health')
def health():
    return jsonify({'ok': True, 'service': 'apk-analyzer', 'time': datetime.now(timezone.utc).isoformat()})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8080')), debug=False)
