import os
import zipfile
import tempfile
from flask import Flask, render_template, request, send_file, redirect

# تحديد المسار الحالي للملف ليتم قراءة المجلدات بشكل صحيح بغض النظر عن مكان التشغيل
basedir = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__, 
            template_folder=os.path.join(basedir, 'templates'))

UPLOAD_FOLDER = tempfile.gettempdir()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'apk_file' not in request.files:
        return redirect(request.url)
    
    file = request.files['apk_file']
    if file.filename == '':
        return redirect(request.url)
    
    if file and file.filename.endswith('.apk'):
        apk_path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(apk_path)
        
        extract_dir = os.path.join(UPLOAD_FOLDER, 'extracted_' + os.path.splitext(file.filename)[0])
        os.makedirs(extract_dir, exist_ok=True)
        
        try:
            with zipfile.ZipFile(apk_path, 'r') as zip_ref:
                zip_ref.extractall(extract_dir)
        except Exception as e:
            return f"خطأ في تحليل الملف: {str(e)}"
        
        zip_output_path = os.path.join(UPLOAD_FOLDER, f"source_{os.path.splitext(file.filename)[0]}.zip")
        with zipfile.ZipFile(zip_output_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(extract_dir):
                for file_name in files:
                    absolute_path = os.path.join(root, file_name)
                    relative_path = os.path.relpath(absolute_path, extract_dir)
                    zipf.write(absolute_path, relative_path)
                    
        return render_template('index.html', download_ready=True, filename=os.path.basename(zip_output_path))
        
    return "يرجى رفع ملف بصيغة APK صالح."

@app.route('/download/<filename>')
def download_file(filename):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    return send_file(file_path, as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
