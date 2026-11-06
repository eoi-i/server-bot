
import os
import uuid
import shutil
import threading
import subprocess
import zipfile
import json
from pathlib import Path
from flask import (
    Flask, request, jsonify, render_template,
    send_file, session, abort
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "change-this-password")
BASE_DIR = Path(os.environ.get("WORK_DIR", "/tmp/apk-recovery"))
MAX_SIZE = 150 * 1024 * 1024
TIMEOUT = 900

BASE_DIR.mkdir(parents=True, exist_ok=True)

jobs = {}
lock = threading.Lock()


def run_tool(command, timeout=TIMEOUT):
    """تشغيل أداة خارجية مع التقاط الأخطاء."""
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-3000:] or "فشلت عملية التحليل"
        )

    return result.stdout


def make_report(folder, report):
    """حفظ تقرير التحليل بصيغة JSON."""
    (folder / "recovery-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def analyze_apk(job_id, apk_path):
    job_dir = BASE_DIR / job_id
    output = job_dir / "output"

    try:
        output.mkdir(parents=True, exist_ok=True)

        def update(progress, message):
            with lock:
                jobs[job_id].update({
                    "progress": progress,
                    "message": message
                })

        report = {
            "apk": apk_path.name,
            "tools": {},
            "warnings": []
        }

        # المرحلة الأولى: استخراج الشفرة المفككة
        update(10, "تحليل ملفات DEX باستخدام JADX")

        jadx_dir = output / "jadx"
        jadx_dir.mkdir()

        run_tool([
            os.environ.get("JADX_BIN", "jadx"),
            "--deobf",
            "--show-bad-code",
            "-d", str(jadx_dir),
            str(apk_path)
        ])

        report["tools"]["jadx"] = "completed"

        # المرحلة الثانية: استخراج الموارد والواجهات
        update(55, "استخراج الموارد باستخدام APKTool")

        apktool_dir = output / "apktool"
        run_tool([
            os.environ.get("APKTOOL_BIN", "apktool"),
            "d", "-f",
            str(apk_path),
            "-o", str(apktool_dir)
        ])

        report["tools"]["apktool"] = "completed"

        # المرحلة الثالثة: إنشاء هيكل مشروع أولي
        update(80, "تجهيز هيكل المشروع")

        project = output / "android-project"
        project.mkdir()

        shutil.copytree(
            jadx_dir / "sources",
            project / "app" / "src" / "main" / "java",
            dirs_exist_ok=True
        )

        if (apktool_dir / "res").exists():
            shutil.copytree(
                apktool_dir / "res",
                project / "app" / "src" / "main" / "res",
                dirs_exist_ok=True
            )

        if (apktool_dir / "AndroidManifest.xml").exists():
            manifest = project / "app" / "src" / "main" / "AndroidManifest.xml"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(apktool_dir / "AndroidManifest.xml", manifest)

        # نسخ موارد APKTool كاملة للاحتفاظ بها
        shutil.copytree(
            apktool_dir,
            project / "apktool-full",
            dirs_exist_ok=True
        )

        # ملف Gradle أولي، وليس نسخة من إعدادات البناء الأصلية
        (project / "settings.gradle.kts").write_text(
            'pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }\n'
            'dependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS); repositories { google(); mavenCentral() } }\n'
            'rootProject.name = "RecoveredApp"\n'
            'include(":app")\n',
            encoding="utf-8"
        )

        (project / "build.gradle.kts").write_text(
            'plugins {\n'
            '    id("com.android.application") version "8.7.3" apply false\n'
            '}\n',
            encoding="utf-8"
        )

        (project / "app" / "build.gradle.kts").write_text(
            'plugins { id("com.android.application") }\n'
            'android {\n'
            '    namespace = "recovered.app"\n'
            '    compileSdk = 35\n'
            '    defaultConfig {\n'
            '        applicationId = "recovered.app"\n'
            '        minSdk = 23\n'
            '        targetSdk = 35\n'
            '        versionCode = 1\n'
            '        versionName = "1.0"\n'
            '    }\n'
            '}\n',
            encoding="utf-8"
        )

        report["warnings"].append(
            "Gradle settings are provisional. Package name, SDK levels, "
            "dependencies and native components require manual verification."
        )

        make_report(project, report)

        # المرحلة الرابعة: ضغط النتائج
        update(92, "إنشاء ملف ZIP")

        zip_path = job_dir / "recovered-project.zip"

        with zipfile.ZipFile(
            zip_path, "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for file in output.rglob("*"):
                if file.is_file():
                    archive.write(
                        file,
                        file.relative_to(output)
                    )

        update(100, "اكتمل التحليل")

        with lock:
            jobs[job_id].update({
                "status": "completed",
                "download": True,
                "message": "تم تجهيز المشروع"
            })

    except subprocess.TimeoutExpired:
        with lock:
            jobs[job_id].update({
                "status": "error",
                "message": "انتهت مهلة التحليل"
            })

    except Exception as error:
        with lock:
            jobs[job_id].update({
                "status": "error",
                "message": str(error)[:2000]
            })


def authenticated():
    return session.get("authenticated") is True


@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    if request.form.get("password") != APP_PASSWORD:
        return jsonify({"error": "كلمة المرور غير صحيحة"}), 401

    if "apk" not in request.files:
        return jsonify({"error": "لم يتم اختيار APK"}), 400

    apk = request.files["apk"]

    if not apk.filename.lower().endswith(".apk"):
        return jsonify({"error": "يجب اختيار ملف APK"}), 400

    job_id = uuid.uuid4().hex

    job_dir = BASE_DIR / job_id
    job_dir.mkdir(parents=True)

    filename = secure_filename(apk.filename) or "application.apk"
    apk_path = job_dir / filename

    apk.save(apk_path)

    if apk_path.stat().st_size > MAX_SIZE:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "حجم الملف أكبر من 150 MB"}), 413

    with lock:
        jobs[job_id] = {
            "status": "processing",
            "progress": 0,
            "message": "بدأ التحليل"
        }

    thread = threading.Thread(
        target=analyze_apk,
        args=(job_id, apk_path),
        daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "العملية غير موجودة"}), 404

    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    with lock:
        job = jobs.get(job_id)

    if not job or job.get("status") != "completed":
        abort(404)

    zip_path = BASE_DIR / job_id / "recovered-project.zip"

    if not zip_path.is_file():
        abort(404)

    return send_file(
        zip_path,
        as_attachment=True,
        download_name="recovered-project.zip"
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        debug=False,
        threaded=True
    )
