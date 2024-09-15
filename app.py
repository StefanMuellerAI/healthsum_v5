import eventlet
eventlet.monkey_patch()

import os
import logging
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, abort
from werkzeug.utils import secure_filename
from flask_socketio import SocketIO, emit, join_room
from celery_config import create_celery_app
from models import db, HealthRecord, Report, User, ReportTemplate, TaskMonitor
from celery import chain
from tasks import process_pdfs, create_report, process_record, regenerate_report_task, generate_single_report
from datetime import datetime
from flask_login import login_user, login_required, logout_user, LoginManager, current_user
from werkzeug.security import check_password_hash
from celery.app.control import Inspect
from extensions import socketio
from celery.result import AsyncResult

#Todo:
# - Userid beim create, read, edit und delete von DAtensätzen und Berichten hinzufügen. 


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
    records = HealthRecord.query.filter_by(user_id=current_user.id).order_by(HealthRecord.timestamp.desc()).all()
    #Number of total report_templates
    report_templates = ReportTemplate.query.count()
    return render_template('index.html', records=records, report_templates=report_templates)


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
    user_id = current_user.id  # Aktuelle Benutzer-ID ermitteln

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

        # Starte den process_pdfs Task und übergebe die user_id
        result = process_pdfs.delay(filenames, patient_name, record_id, create_reports, user_id)

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
    records = HealthRecord.query.filter_by(user_id=current_user.id).order_by(HealthRecord.timestamp.desc()).all()
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
    if record.user_id != current_user.id:
        abort(403)  # HTTP 403 Forbidden
    db.session.delete(record)
    db.session.commit()
    return jsonify({"message": f"Record {record_id} and all associated reports deleted successfully"}), 200


@app.route('/get_record/<int:record_id>')
@login_required
def get_record(record_id):
    record = HealthRecord.query.get_or_404(record_id)
    if record.user_id != current_user.id:
        abort(403)
    return jsonify({
        'id': record.id,
        'timestamp': format_timestamp(record.timestamp),
        'filenames': record.filenames,
        'patient_name': record.patient_name or 'Unbekannter Patient',
        'medical_history_begin': record.medical_history_begin.year if record.medical_history_begin else None,
        'medical_history_end': record.medical_history_end.year if record.medical_history_end else None
    }), 200


@app.route('/read_reports')
@login_required
def read_reports():
    records = HealthRecord.query.filter_by(user_id=current_user.id).order_by(HealthRecord.timestamp.desc()).all()
    return render_template('read_reports.html', records=records)

@app.route('/get_reports/<int:record_id>')
@login_required
def get_reports(record_id):
    record = HealthRecord.query.get_or_404(record_id)
    if record.user_id != current_user.id:
        abort(403)

    # Alle verfügbaren ReportTemplates abrufen
    report_templates = ReportTemplate.query.all()

    # Bereits existierende Berichte für diesen Datensatz abrufen
    existing_reports = Report.query.filter_by(health_record_id=record_id).all()
    existing_reports_map = {(report.report_template_id): report for report in existing_reports}

    reports = []
    for template in report_templates:
        report = existing_reports_map.get(template.id)
        if report:
            # Bericht existiert bereits für dieses Template und Datensatz
            reports.append({
                'id': report.id,
                'report_type': template.template_name,
                'created_at': report.created_at.isoformat(),
                'exists': True,
                'report_template_id': template.id
            })
        else:
            # Bericht existiert noch nicht
            reports.append({
                'id': None,
                'report_type': template.template_name,
                'created_at': None,
                'exists': False,
                'report_template_id': template.id
            })
    return jsonify({'reports': reports})


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
    
    if record.user_id != current_user.id:
        abort(403)
    
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
            'user_id': report.health_record.user_id,
            'content': report.content,
            'output_format': report.report_template.output_format
        }
    return None

@app.route('/report/<int:report_id>')
@login_required
def view_report(report_id):
    report = get_report(report_id)
    if report:
        if report['user_id'] != current_user.id:
            abort(403)
        if report['output_format'].lower() == 'text':
            return render_template('view_report_text.html', report=report)
        else:
            return render_template('view_report_json.html', report=report)
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

@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@socketio.on('connect', namespace='/tasks')
def test_connect():
    print('Client verbunden')

@socketio.on('join_task_room', namespace='/tasks')
def on_join_task_room(data):
    task_id = data.get('task_id')
    if task_id:
        join_room(task_id)
        print(f'Client joined room for task_id: {task_id}')

@app.route('/edit_report_templates')
@login_required
def edit_report_templates():
    templates = ReportTemplate.query.order_by(ReportTemplate.template_name).all()
    return render_template('edit_report_templates.html', templates=templates)

@app.route('/get_template/<int:template_id>')
@login_required
def get_template(template_id):
    template = ReportTemplate.query.get_or_404(template_id)
    return jsonify({
        'id': template.id,
        'template_name': template.template_name,
        'output_format': template.output_format,
        'example_structure': template.example_structure,
        'system_prompt': template.system_prompt,
        'prompt': template.prompt,
        'summarizer': template.summarizer
    })

@app.route('/update_template', methods=['POST'])
@login_required
def update_template():
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({'error': 'Ungültige Daten'}), 400

    template_id = data['id']
    template = ReportTemplate.query.get_or_404(template_id)
    template.template_name = data.get('template_name', template.template_name)
    template.output_format = data.get('output_format', template.output_format)
    template.example_structure = data.get('example_structure', template.example_structure)
    template.system_prompt = data.get('system_prompt', template.system_prompt)
    template.prompt = data.get('prompt', template.prompt)
    template.summarizer = data.get('summarizer', template.summarizer)
    template.last_updated = datetime.utcnow()

    try:
        db.session.commit()
        return jsonify({'message': 'Template erfolgreich aktualisiert'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/regenerate_report/<int:report_id>', methods=['POST'])
@login_required
def regenerate_report_route(report_id):
    report = Report.query.get_or_404(report_id)
    if report.health_record.user_id != current_user.id:
        abort(403)
    
    # Starten des Celery-Tasks zum Neu-Generieren des Berichts
    task = regenerate_report_task.apply_async(args=[report_id])
    
    return jsonify({'message': 'Der Bericht wird neu generiert. Diese Aktion kann einige Zeit dauern.'})


@app.route('/create_template', methods=['POST'])
@login_required
def create_template():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Ungültige Daten'}), 400

    new_template = ReportTemplate(
        template_name=data.get('template_name'),
        output_format=data.get('output_format'),
        example_structure=data.get('example_structure'),
        system_prompt=data.get('system_prompt'),
        prompt=data.get('prompt'),
        summarizer=data.get('summarizer'),
        created_at=datetime.utcnow()
    )

    db.session.add(new_template)
    try:
        db.session.commit()
        return jsonify({'message': 'Template erfolgreich erstellt', 'template_id': new_template.id}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
    

# Fügen Sie folgende Route hinzu
@app.route('/kpi')
@login_required
def kpi():
    from sqlalchemy import func
    # Berechnungen für create_reports = True
    tasks_with_reports = TaskMonitor.query.join(HealthRecord).filter(HealthRecord.create_reports == True, TaskMonitor.end_date != None).all()
    total_tokens_reports = 0
    total_time_reports = 0
    for task in tasks_with_reports:
        if task.health_record.token_count and task.start_date and task.end_date:
            duration = (task.end_date - task.start_date).total_seconds()
            total_tokens_reports += task.health_record.token_count
            total_time_reports += duration
    if total_time_reports > 0:
        tps_reports = total_tokens_reports / total_time_reports
    else:
        tps_reports = 0

    # Berechnungen für create_reports = False
    tasks_without_reports = TaskMonitor.query.join(HealthRecord).filter(HealthRecord.create_reports == False, TaskMonitor.end_date != None).all()
    total_tokens_no_reports = 0
    total_time_no_reports = 0
    for task in tasks_without_reports:
        if task.health_record.token_count and task.start_date and task.end_date:
            duration = (task.end_date - task.start_date).total_seconds()
            total_tokens_no_reports += task.health_record.token_count
            total_time_no_reports += duration
    if total_time_no_reports > 0:
        tps_no_reports = total_tokens_no_reports / total_time_no_reports
    else:
        tps_no_reports = 0

    return render_template('kpi.html', tps_reports=tps_reports, tps_no_reports=tps_no_reports)

@app.route('/generate_report/<int:record_id>/<int:template_id>', methods=['POST'])
@login_required
def generate_report_route(record_id, template_id):
    record = HealthRecord.query.get_or_404(record_id)
    if record.user_id != current_user.id:
        abort(403)

    # Überprüfen, ob der Bericht bereits existiert
    existing_report = Report.query.filter_by(health_record_id=record_id, report_template_id=template_id).first()
    if existing_report:
        return jsonify({'message': 'Der Bericht existiert bereits. Bitte nutzen Sie die Funktion "Neu generieren".'}), 400

    # Starten des Celery-Tasks zum Generieren des Berichts
    task = generate_single_report.apply_async(args=[record_id, template_id])

    return jsonify({'message': 'Der Bericht wird generiert. Diese Aktion kann einige Zeit dauern.'})

@app.route('/delete_template', methods=['POST'])
@login_required
def delete_template():
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({'error': 'Ungültige Daten'}), 400

    template_id = data['id']
    template = ReportTemplate.query.get_or_404(template_id)

    # Überprüfen, ob das Template existiert
    if not template:
        return jsonify({'error': 'Template nicht gefunden.'}), 404

    try:
        # Löschen des Templates (zugehörige Reports werden durch Cascade automatisch gelöscht)
        db.session.delete(template)
        db.session.commit()
        return jsonify({'message': 'Template und zugehörige Berichte erfolgreich gelöscht.'}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'Fehler beim Löschen des Templates: {str(e)}'}), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    socketio.run(app, debug=True, host='127.0.0.1', port=5000)
