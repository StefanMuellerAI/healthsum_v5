import eventlet
eventlet.monkey_patch()

import os
import logging
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit, join_room
from celery_config import create_celery_app
from models import db, HealthRecord, Report, User
from celery import chain
from tasks import process_pdfs, create_report, process_record
from datetime import datetime
from flask_login import login_user, login_required, logout_user, LoginManager, current_user
from werkzeug.security import check_password_hash
from celery.app.control import Inspect
from extensions import socketio
from celery.result import AsyncResult


# Hilfsfunktion für das Formatieren des Timestamps
def format_timestamp(timestamp):
    return timestamp.strftime('%Y-%m-%d %H:%M')

# Konfigurieren des Loggings
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')  # Fügen Sie diese Zeile hinzu
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///health_records.db'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6380/0'
app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6380/0'
socketio.init_app(app)
db.init_app(app)

# Erstellen der Celery-Instanz
celery = create_celery_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
@login_required
def index():
    records = HealthRecord.query.order_by(HealthRecord.timestamp.desc()).all()
    return render_template('index.html', records=records)


@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    logger.info("Received upload request")
    if 'files[]' not in request.files:
        logger.warning("No file part in the request")
        return jsonify({'error': 'No file part'}), 400

    files = request.files.getlist('files[]')
    filenames = []

    record_id = request.form.get('record_id')
    create_reports = request.form.get('createReports') == 'on'
    if record_id:
        # Dokumente zu einem bestehenden Datensatz hinzufügen
        record = HealthRecord.query.get(record_id)
        if not record:
            return jsonify({'error': 'Record not found'}), 404
        patient_name = record.patient_name
        record.create_reports = create_reports
    else:
        # Neuen Datensatz erstellen
        first_name = request.form.get('firstName')
        last_name = request.form.get('lastName')
        if not first_name or not last_name:
            logger.warning("First name or last name is missing")
            return jsonify({'error': 'First name and last name are required'}), 400
        patient_name = f"{first_name} {last_name}"

    for file in files:
        if file and file.filename.endswith('.pdf'):
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            filenames.append(filename)
            logger.info(f"Saved file: {filename}")

    if filenames:
        logger.info(f"Starting process_pdfs task for files: {filenames}")

        # Starte den process_pdfs Task
        result = process_pdfs.delay(filenames, patient_name, record_id, create_reports)

        logger.info(f"Started process_pdfs task for patient: {patient_name}")

        return jsonify({
            'task_id': result.id,
            'patient_name': patient_name
        }), 202
    else:
        logger.warning("No valid PDF files uploaded")
        return jsonify({'error': 'No valid PDF files uploaded'}), 400

@app.route('/task_status/<task_id>')
def task_status(task_id):
    result = AsyncResult(task_id)
    return jsonify({'state': result.state})

@app.route('/get_datasets')
@login_required
def get_datasets():
    records = HealthRecord.query.order_by(HealthRecord.timestamp.desc()).all()
    return jsonify([
        {
            'id': record.id,
            'timestamp': format_timestamp(record.timestamp),
            'filenames': record.filenames,
            'patient_name': record.patient_name,
            'create_reports': record.create_reports,
            'medical_history_begin': record.medical_history_begin.year if record.medical_history_begin else None,
            'medical_history_end': record.medical_history_end.year if record.medical_history_end else None
        } for record in records
    ])


@app.route('/delete_record/<int:record_id>', methods=['DELETE'])
@login_required
def delete_record(record_id):
    record = HealthRecord.query.get_or_404(record_id)
    db.session.delete(record)
    db.session.commit()
    return jsonify({"message": f"Record {record_id} and all associated reports deleted successfully"}), 200


@app.route('/get_record/<int:record_id>')
@login_required
def get_record(record_id):
    record = HealthRecord.query.get(record_id)
    if record:
        return jsonify({
            'id': record.id,
            'timestamp': format_timestamp(record.timestamp),
            'filenames': record.filenames,
            'patient_name': record.patient_name or 'Unbekannter Patient',
            'medical_history_begin': record.medical_history_begin.year if record.medical_history_begin else None,
            'medical_history_end': record.medical_history_end.year if record.medical_history_end else None
        }), 200
    return jsonify({"error": "Datensatz nicht gefunden"}), 404


@app.route('/read_reports')
@login_required
def read_reports():
    patients = []
    records = HealthRecord.query.filter_by(create_reports=True).order_by(HealthRecord.timestamp.desc()).all()
    for record in records:
        reports = Report.query.filter_by(health_record_id=record.id).all()
        if reports:
            patient = {
                'id': record.id,
                'name': record.patient_name,
                'last_update': record.timestamp,  # Behalten Sie das datetime-Objekt
                'reports': []
            }
            for report in reports:
                patient['reports'].append({
                    'id': report.id,
                    'type': report.report_type,
                    'created_at': report.created_at  # Behalten Sie das datetime-Objekt
                })
            patients.append(patient)
    return render_template('read_reports.html', patients=patients)


@app.route('/impressum')
def impressum():
    return render_template('impressum.html')


@app.route('/datenschutz')
def datenschutz():
    return render_template('datenschutz.html')


@app.route('/create_reports/<int:record_id>', methods=['POST'])
@login_required
def create_reports(record_id):
    record = HealthRecord.query.get(record_id)
    if not record:
        return jsonify({'success': False, 'error': 'Record not found'}), 404
    
    if record.create_reports:
        return jsonify({'success': False, 'error': 'Reports already created or in progress'}), 400
    
    record.create_reports = True
    db.session.commit()
    
    # Starte den Prozess zur Erstellung der Berichte
    chain(process_record.s({'record_id': record_id}), create_report.s()).apply_async()
    
    return jsonify({'success': True}), 200


def get_report(report_id):
    report = Report.query.get(report_id)
    if report:
        return {
            'id': report.id,
            'type': report.report_type,
            'created_at': format_timestamp(report.created_at),
            'patient_name': report.health_record.patient_name,
            'patient_id': report.health_record.id,
            'content': report.content
        }
    return None

@app.route('/report/<int:report_id>')
@login_required
def view_report(report_id):
    report = get_report(report_id)
    if report:
        return render_template('view_report.html', report=report)
    return jsonify({"error": "Report nicht gefunden"}), 404

@app.template_filter('format_timestamp')
def format_timestamp_filter(timestamp):
    return timestamp.strftime('%Y-%m-%d %H:%M')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        else:
            flash('Ungültige Anmeldedaten.', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))


def are_tasks_running():
    i = Inspect(app=celery)
    active_tasks = i.active()
    return any(active_tasks.values()) if active_tasks else False

@app.context_processor
def inject_tasks_status():
    return dict(tasks_running=are_tasks_running())

@app.route('/status/check_active')
@login_required
def check_active_tasks():
    return jsonify({'active_tasks': are_tasks_running()})

@app.errorhandler(403)
def forbidden(e):
    return render_template('403.html'), 403

@socketio.on('connect', namespace='/tasks')
def test_connect():
    print('Client verbunden')

@socketio.on('join_task_room', namespace='/tasks')
def on_join_task_room(data):
    task_id = data.get('task_id')
    if task_id:
        join_room(task_id)
        print(f'Client joined room for task_id: {task_id}')



if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True, host='127.0.0.1', port=5000)
