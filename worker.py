import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import database

log = logging.getLogger('apk-analyzer.worker')


def validate_apk(path: Path) -> str:
    if not zipfile.is_zipfile(path):
        raise ValueError('الملف ليس APK صالحاً أو أن محتواه تالف.')
    try:
        with zipfile.ZipFile(path) as apk:
            bad = apk.testzip()
            if bad:
                raise ValueError(f'تعذر قراءة جزء من الملف: {bad}')
            if not any(Path(name).name == 'AndroidManifest.xml' for name in apk.namelist()):
                raise ValueError('الحزمة لا تحتوي AndroidManifest.xml، لذلك لا تبدو APK صالحة.')
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError('ملف APK تالف أو غير مكتمل.') from exc
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class AnalysisWorker:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.max_workers = max(1, int(os.getenv('MAX_WORKERS', '2')))
        self.timeout = max(60, int(os.getenv('MAX_ANALYSIS_SECONDS', '1800')))
        self.stop_event = threading.Event()
        self.thread = None
        self.active = set()
        self.lock = threading.Lock()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name='analysis-poller', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                database.recover_stale_jobs()
                with self.lock:
                    capacity = self.max_workers - len(self.active)
                if capacity > 0:
                    for job in database.claim_jobs(capacity):
                        with self.lock:
                            self.active.add(job['id'])
                        threading.Thread(target=self._process_guarded, args=(job,), daemon=True).start()
            except Exception:
                log.exception('Worker poll failed')
            self.stop_event.wait(2)

    def _process_guarded(self, job):
        try:
            self.process(job)
        except InterruptedError as exc:
            try:
                database.update_analysis(job['id'], status='cancelled', message=str(exc))
            except Exception:
                log.exception('Could not persist cancellation')
        except Exception:
            log.exception('Job %s crashed', job['id'])
            try:
                database.update_analysis(job['id'], status='failed', message='تعذر إكمال التحليل بسبب خطأ داخلي في العامل.')
            except Exception:
                log.exception('Could not persist job failure')
        finally:
            try:
                Path(job['upload_path']).unlink(missing_ok=True)
            except OSError:
                log.warning('Could not clean upload for job %s', job['id'])
            with self.lock:
                self.active.discard(job['id'])

    def _run_command(self, command, job_id, timeout):
        # Redirect output to a temporary file: a PIPE can fill and deadlock while poll() waits.
        with tempfile.TemporaryFile(mode='w+b') as output:
            process = subprocess.Popen(
                command,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if time.monotonic() - started > timeout:
                        process.kill()
                        raise TimeoutError(f'انتهت مهلة الأمر بعد {timeout} ثانية.')
                    if database.cancel_requested(job_id):
                        process.kill()
                        raise InterruptedError('ألغى المستخدم المهمة.')
                    time.sleep(0.5)
                output.seek(0)
                text = output.read().decode('utf-8', errors='replace')
                return text[-12000:], process.returncode
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def process(self, job):
        job_id = job['id']
        apk = Path(job['upload_path'])
        job_dir = self.data_dir / 'work' / job_id
        output_dir = self.data_dir / 'results' / job_id
        jadx_dir, apktool_dir = job_dir / 'jadx', job_dir / 'apktool'
        project = job_dir / 'android-project'
        job_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        if not apk.is_file():
            raise FileNotFoundError('ملف APK غير موجود في التخزين الدائم.')
        jadx = shutil.which('jadx')
        apktool = shutil.which('apktool')
        if not jadx or not apktool:
            raise RuntimeError('أدوات JADX وApktool غير مثبتة.')
        with zipfile.ZipFile(apk) as archive:
            dex = sorted(n for n in archive.namelist() if Path(n).name == 'classes.dex' or (Path(n).name.startswith('classes') and Path(n).name.endswith('.dex')))
        database.update_analysis(job_id, progress=8, message='JADX: استخراج المصادر...')
        jadx_log, jadx_code = self._run_command([jadx, '--deobf', '--show-bad-code', '--threads-count', str(min(os.cpu_count() or 2, 4)), '-d', str(jadx_dir), str(apk)], job_id, self.timeout)
        (job_dir / 'jadx.log').write_text(jadx_log, encoding='utf-8')
        warnings = []
        if jadx_code not in (0, 3):
            warnings.append(f'JADX أنهى برمز {jadx_code}; تم الاحتفاظ بأي مخرجات جزئية.')
        database.update_analysis(job_id, progress=45, message='Apktool: استخراج Smali والموارد والـ Manifest...')
        apktool_log, apktool_code = self._run_command([apktool, 'd', '-f', str(apk), '-o', str(apktool_dir)], job_id, self.timeout)
        (job_dir / 'apktool.log').write_text(apktool_log, encoding='utf-8')
        if apktool_code != 0:
            warnings.append(f'Apktool أنهى برمز {apktool_code}; تم الاحتفاظ بالمخرجات المتاحة.')
        java_root = jadx_dir / 'sources'
        java_count = sum(1 for _ in java_root.rglob('*.java')) if java_root.exists() else 0
        smali_count = sum(1 for _ in apktool_dir.rglob('*.smali')) if apktool_dir.exists() else 0
        if not java_count and not smali_count and not (apktool_dir / 'AndroidManifest.xml').exists():
            raise RuntimeError('لم تُنتج الأدوات أي نتائج قابلة للتنزيل.')
        database.update_analysis(job_id, progress=75, message='إنشاء مشروع الاستعادة وضغط النتائج...')
        main = project / 'app' / 'src' / 'main'
        (main / 'java').mkdir(parents=True, exist_ok=True)
        (main / 'res').mkdir(parents=True, exist_ok=True)
        (main / 'assets').mkdir(parents=True, exist_ok=True)
        if java_root.exists():
            shutil.copytree(java_root, main / 'java' / 'recovered', dirs_exist_ok=True)
        for name in ('res', 'assets', 'lib'):
            source = apktool_dir / name
            if source.exists():
                shutil.copytree(source, main / name, dirs_exist_ok=True)
        manifest = apktool_dir / 'AndroidManifest.xml'
        if manifest.exists():
            shutil.copy2(manifest, main / 'AndroidManifest.xml')
        for folder in apktool_dir.glob('smali*'):
            if folder.is_dir():
                shutil.copytree(folder, project / 'smali' / folder.name, dirs_exist_ok=True)
        (project / 'settings.gradle').write_text("pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\ndependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }\nrootProject.name = 'RecoveredProject'\ninclude ':app'\n", encoding='utf-8')
        (project / 'build.gradle').write_text("plugins { id 'com.android.application' version '8.7.3' apply false }\n", encoding='utf-8')
        (project / 'app' / 'build.gradle').write_text("plugins { id 'com.android.application' }\nandroid { namespace 'recovered.project'; compileSdk 35; defaultConfig { applicationId 'recovered.project'; minSdk 23; targetSdk 35; versionCode 1; versionName '1.0' } }\n", encoding='utf-8')
        (project / 'ANALYSIS-REPORT.txt').write_text(f"APK: {job['filename']}\nDEX: {len(dex)}\nJava: {java_count}\nSmali: {smali_count}\n", encoding='utf-8')
        if warnings:
            (project / 'RECOVERY-WARNINGS.txt').write_text('\n'.join(warnings) + '\n', encoding='utf-8')
        zip_path = output_dir / f'{job_id}-recovered-project.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as result:
            for path in project.rglob('*'):
                if path.is_file():
                    result.write(path, path.relative_to(project.parent))
            for prefix, root in (('jadx-output', jadx_dir), ('apktool-output', apktool_dir)):
                if root.exists():
                    for path in root.rglob('*'):
                        if path.is_file():
                            result.write(path, Path(prefix) / path.relative_to(root))
            for path in (job_dir / 'jadx.log', job_dir / 'apktool.log'):
                if path.exists():
                    result.write(path, path.name)
        database.update_analysis(job_id, status='completed', progress=100, message=f'اكتمل التحليل: {java_count} Java، {smali_count} Smali.', result_path=str(zip_path))
