import os
import logging
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, abort
from werkzeug.utils import secure_filename
from flask import send_from_directory
from celery_config import create_celery_app
from models import db, HealthRecord, Report, User, ReportTemplate, TaskMonitor, TaskLog
from celery import chain
from tasks import process_pdfs, create_report, process_record, regenerate_report_task, generate_single_report, extract_medical_codes, parse_medical_codes_xml, save_medical_codes, update_medical_codes_descriptions
from datetime import datetime
from flask_login import login_user, login_required, logout_user, LoginManager, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from celery.app.control import Inspect
from celery.result import AsyncResult
from sqlalchemy.exc import IntegrityError
import re
from utils import count_tokens

#Todo:
# - Userid beim create, read, edit und delete von DAtensätzen und Berichten hinzufügen. 


# Hilfsfunktion für das Formatieren des Timestamps
def format_timestamp(timestamp):
    return timestamp.strftime('%Y-%m-%d %H:%M')

# Konfigurieren des Loggings
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Flask's Werkzeug Logger auch auf DEBUG setzen
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.DEBUG)

app = Flask(__name__)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')  # Fügen Sie diese Zeile hinzu
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///health_records.db'
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6380/0'
app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6380/0'
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
    custom_instructions = request.form.get('customInstructions')
    birth_date_str = request.form.get('birthDate')  # Neues Feld
    user_id = current_user.id

    # Konvertiere birth_date_str zu datetime oder None
    birth_date = None
    if birth_date_str:
        try:
            birth_date = datetime.strptime(birth_date_str, '%Y-%m-%d')
        except ValueError:
            logger.warning("Invalid birth date format")
            return jsonify({'error': 'Invalid birth date format'}), 400

    if record_id:
        # Dokumente zu einem bestehenden Datensatz hinzufügen
        record = HealthRecord.query.get(record_id)
        if not record:
            return jsonify({'error': 'Record not found'}), 404
        patient_name = record.patient_name
        # Setze Status auf processing für bestehende Records
        record.processing_status = 'processing'
        db.session.commit()
        
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

        # Wenn es ein neuer Datensatz ist, erstelle ihn sofort
        if record_id is None:
            # Erstelle neuen HealthRecord sofort mit processing_status
            new_record = HealthRecord(
                patient_name=patient_name,
                create_reports=create_reports,
                user_id=user_id,
                custom_instructions=custom_instructions,
                birth_date=birth_date,
                processing_status='processing'  # Setze Status sofort auf processing
            )
            db.session.add(new_record)
            db.session.commit()
            record_id = new_record.id
            logger.info(f"Created new HealthRecord with ID: {record_id}, status: processing")

        # Starte den process_pdfs Task und übergebe die user_id, custom_instructions und birth_date
        result = process_pdfs.delay(
            filenames, 
            patient_name, 
            record_id, 
            create_reports, 
            user_id,
            custom_instructions,
            birth_date
        )

        logger.info(f"Started process_pdfs task for patient: {patient_name}")

        return jsonify({
            'task_id': result.id,
            'patient_name': patient_name,
            'record_id': record_id  # Jetzt immer eine gültige ID
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
    
    # Debug: Log processing_status values
    for record in records:
        print(f"DEBUG: Record {record.id} has processing_status: {record.processing_status}")
    
    result = []
    for record in records:
        # Prüfe ob es fehlgeschlagene Tasks gibt
        failed_tasks = TaskLog.query.filter_by(
            health_record_id=record.id,
            status='failed'
        ).count()
        
        result.append({
            'id': record.id,
            'timestamp': format_timestamp(record.timestamp),
            'filenames': record.filenames,
            'patient_name': record.patient_name,
            'create_reports': record.create_reports,
            'medical_history_begin': record.medical_history_begin.year if record.medical_history_begin else None,
            'medical_history_end': record.medical_history_end.year if record.medical_history_end else None,
            'custom_instructions': record.custom_instructions,
            'birth_date': record.birth_date.isoformat() if record.birth_date else None,
            'processing_status': record.processing_status or 'completed',  # Default für migrierte Records
            'processing_completed_at': record.processing_completed_at.isoformat() if record.processing_completed_at else None,
            'processing_error_message': record.processing_error_message,
            'has_failed_tasks': failed_tasks > 0
        })
    
    return jsonify(result)


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
        'birth_date': record.birth_date.isoformat() if record.birth_date else None,
        'medical_history_begin': record.medical_history_begin.year if record.medical_history_begin else None,
        'medical_history_end': record.medical_history_end.year if record.medical_history_end else None,
        'create_reports': record.create_reports,
        'custom_instructions': record.custom_instructions or 'Keine Custom Instructions definiert',
        'token_count': record.token_count
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

    # Prüfe ob es fehlgeschlagene create_report Tasks für diesen HealthRecord gibt
    failed_report_tasks = TaskLog.query.filter_by(
        health_record_id=record_id,
        task_name='create_report',
        status='failed'
    ).count() > 0

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
                'report_template_id': template.id,
                'generation_status': report.generation_status or 'completed',  # Default für migrierte Reports
                'generation_started_at': report.generation_started_at.isoformat() if report.generation_started_at else None,
                'generation_completed_at': report.generation_completed_at.isoformat() if report.generation_completed_at else None,
                'generation_error_message': report.generation_error_message,
                'unique_identifier': report.unique_identifier or f"MIGRATED-{report.id}"  # Fallback für migrierte Reports
            })
        else:
            # Bericht existiert noch nicht
            reports.append({
                'id': None,
                'report_type': template.template_name,
                'created_at': None,
                'exists': False,
                'report_template_id': template.id,
                'generation_status': 'pending',
                'generation_started_at': None,
                'generation_completed_at': None,
                'generation_error_message': None
            })
    
    return jsonify({
        'reports': reports,
        'has_failed_report_tasks': failed_report_tasks
    })


@app.route('/impressum')
def impressum():
    return render_template('impressum.html')


@app.route('/datenschutz')
def datenschutz():
    return render_template('datenschutz.html')


@app.route('/get_task_logs/<int:record_id>')
@login_required
def get_task_logs(record_id):
    """API-Endpoint um Task-Logs für einen Health Record abzurufen"""
    record = HealthRecord.query.get_or_404(record_id)
    if record.user_id != current_user.id:
        abort(403)
    
    # Hole alle Task-Logs für diesen Record, sortiert nach neuesten zuerst
    task_logs = TaskLog.query.filter_by(health_record_id=record_id)\
                            .order_by(TaskLog.started_at.desc())\
                            .all()
    
    # Analysiere den Status
    has_failed_tasks = any(log.status == 'failed' for log in task_logs)
    
    # Gruppiere wichtige Task-Logs
    critical_tasks = ['process_pdfs', 'create_report', 'combine_extractions', 'process_record']
    critical_logs = [log for log in task_logs if log.task_name in critical_tasks]
    
    # Bestimme den Overall-Status
    overall_status = 'success'
    error_messages = []
    
    if has_failed_tasks:
        overall_status = 'failed'
        failed_logs = [log for log in task_logs if log.status == 'failed']
        error_messages = [
            {
                'task_name': log.task_name,
                'error_type': log.error_type,
                'error_message': log.error_message,
                'failed_at': log.completed_at.isoformat() if log.completed_at else None
            }
            for log in failed_logs
        ]
    
    return jsonify({
        'success': True,
        'overall_status': overall_status,
        'has_failed_tasks': has_failed_tasks,
        'error_messages': error_messages,
        'task_logs': [log.to_dict() for log in task_logs],
        'critical_logs': [log.to_dict() for log in critical_logs],
        'total_tasks': len(task_logs),
        'failed_tasks': len([log for log in task_logs if log.status == 'failed']),
        'successful_tasks': len([log for log in task_logs if log.status == 'success'])
    })

@app.route('/get_processing_status/<int:record_id>')
@login_required
def get_processing_status(record_id):
    """API-Endpoint zum Abrufen des aktuellen Verarbeitungsstatus eines Health Records"""
    try:
        # Prüfe ob der Record dem aktuellen User gehört
        health_record = HealthRecord.query.filter_by(id=record_id, user_id=current_user.id).first()
        if not health_record:
            return jsonify({'error': 'Health record not found or access denied'}), 404
        
        # Hole die letzten Task-Logs (letzten 2 Stunden für bessere Status-Erkennung)
        from datetime import datetime, timedelta
        two_hours_ago = datetime.utcnow() - timedelta(hours=2)
        
        recent_logs = TaskLog.query.filter(
            TaskLog.health_record_id == record_id,
            TaskLog.started_at >= two_hours_ago
        ).order_by(TaskLog.started_at.asc()).all()  # Chronologische Reihenfolge
        
        # Prüfe auf laufende Tasks
        running_tasks = [log for log in recent_logs if log.status == 'started']
        completed_tasks = [log for log in recent_logs if log.status in ['success', 'failed']]
        failed_tasks = [log for log in recent_logs if log.status == 'failed']
        
        # Bestimme aktuellen Status
        if running_tasks:
            current_status = 'processing'
            current_task = running_tasks[0].task_name
        else:
            # Prüfe ob eine vollständige Verarbeitung stattgefunden hat
            # Ein Record gilt als vollständig verarbeitet wenn:
            # 1. Keine laufenden Tasks
            # 2. Mindestens ein erfolgreiches process_pdfs UND combine_extractions
            # 3. Wenn create_reports aktiviert: auch create_report erfolgreich
            successful_logs = [log for log in recent_logs if log.status == 'success']
            core_tasks_completed = any(log.task_name == 'process_pdfs' for log in successful_logs) and \
                                  any(log.task_name == 'combine_extractions' for log in successful_logs)
            
            # Prüfe ob Reports erstellt werden sollten
            reports_needed = health_record.create_reports
            reports_completed = any(log.task_name == 'create_report' for log in successful_logs) if reports_needed else True
            
            if core_tasks_completed and reports_completed and not failed_tasks:
                current_status = 'completed'
                logger.info(f"Record {record_id} marked as COMPLETED: core_tasks={core_tasks_completed}, reports_completed={reports_completed}, failed_tasks={len(failed_tasks)}")
            elif failed_tasks and not running_tasks:
                current_status = 'failed'
                logger.info(f"Record {record_id} marked as FAILED: failed_tasks={len(failed_tasks)}, running_tasks={len(running_tasks)}")
            else:
                current_status = 'idle'
                logger.info(f"Record {record_id} marked as IDLE: core_tasks={core_tasks_completed}, reports_needed={reports_needed}, reports_completed={reports_completed}, failed_tasks={len(failed_tasks)}")
                
            # Debug: Liste alle erfolgreichen Tasks auf
            successful_task_names = [log.task_name for log in successful_logs]
            logger.info(f"Record {record_id} successful tasks: {successful_task_names}")
            
            current_task = None
        
        # Sammle ALLE Tasks für Display (nicht nur bekannte)
        task_progress = []
        
        # Erstelle Task-Map für bessere Performance
        task_map = {}
        for log in recent_logs:
            task_key = f"{log.task_name}_{log.task_id}"
            if task_key not in task_map:
                task_map[task_key] = []
            task_map[task_key].append(log)
        
        # Verarbeite alle gefundenen Tasks
        for task_key, task_logs in task_map.items():
            # Neuester Log für diesen Task
            latest_log = max(task_logs, key=lambda x: x.started_at)
            
            status = latest_log.status
            if status == 'started':
                task_status = 'running'
            elif status == 'success':
                task_status = 'completed'
            elif status == 'failed':
                task_status = 'failed'
            else:
                task_status = 'pending'
            
            # Berechne Dauer falls verfügbar
            duration = None
            if latest_log.duration_seconds:
                duration = round(latest_log.duration_seconds, 2)
            elif latest_log.started_at and latest_log.completed_at:
                duration = (latest_log.completed_at - latest_log.started_at).total_seconds()
                duration = round(duration, 2)
            
            task_progress.append({
                'name': latest_log.task_name,
                'task_id': latest_log.task_id,
                'display_name': get_task_display_name(latest_log.task_name),
                'status': task_status,
                'started_at': latest_log.started_at.isoformat() if latest_log.started_at else None,
                'completed_at': latest_log.completed_at.isoformat() if latest_log.completed_at else None,
                'duration': duration,
                'retry_count': latest_log.retry_count or 0
            })
        
        # Sortiere Tasks chronologisch
        task_progress.sort(key=lambda x: x['started_at'] or '')
        
        return jsonify({
            'record_id': record_id,
            'status': current_status,
            'current_task': current_task,
            'current_task_display': get_task_display_name(current_task) if current_task else None,
            'task_progress': task_progress,
            'total_tasks': len(task_progress),
            'completed_tasks': len([t for t in task_progress if t['status'] == 'completed']),
            'running_tasks': len([t for t in task_progress if t['status'] == 'running']),
            'failed_tasks': len([t for t in task_progress if t['status'] == 'failed'])
        })
        
    except Exception as e:
        logger.error(f"Error getting processing status for record {record_id}: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/debug_processing_status/<int:record_id>')
@login_required
def debug_processing_status(record_id):
    """Debug-Endpoint zum Überprüfen des Processing-Status"""
    try:
        # Hole ALLE Task-Logs für diesen Record
        all_logs = TaskLog.query.filter_by(health_record_id=record_id).order_by(TaskLog.started_at.desc()).all()
        
        # Hole HealthRecord Info
        health_record = HealthRecord.query.get(record_id)
        
        debug_info = {
            'record_id': record_id,
            'health_record_exists': bool(health_record),
            'create_reports': health_record.create_reports if health_record else None,
            'total_logs': len(all_logs),
            'logs': [
                {
                    'task_name': log.task_name,
                    'status': log.status,
                    'started_at': log.started_at.isoformat() if log.started_at else None,
                    'completed_at': log.completed_at.isoformat() if log.completed_at else None,
                } for log in all_logs[:20]  # Nur die ersten 20
            ],
            'successful_tasks': [log.task_name for log in all_logs if log.status == 'success'],
            'failed_tasks': [log.task_name for log in all_logs if log.status == 'failed'],
            'running_tasks': [log.task_name for log in all_logs if log.status == 'started']
        }
        
        return jsonify(debug_info)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def get_task_display_name(task_name):
    """Konvertiert technische Task-Namen zu benutzerfreundlichen Anzeigenamen"""
    if not task_name:
        return None
    
    display_names = {
        'process_pdfs': 'PDF-Verarbeitung starten',
        'convert_pdf_to_images': 'PDF in Bilder konvertieren',
        'distribute_extraction_tasks': 'Extraktions-Tasks verteilen',
        'extract_pdf_text': 'Text aus PDF extrahieren',
        'extract_ocr_optimized': 'OCR-Texterkennung (optimiert)',
        'extract_azure_vision_optimized': 'Azure Vision Analyse (optimiert)',
        'extract_gpt4_vision_optimized': 'GPT-4 Vision Analyse (optimiert)',
        'extract_ocr': 'OCR-Texterkennung',
        'extract_azure_vision': 'Azure Vision Analyse',
        'extract_gpt4_vision': 'GPT-4 Vision Analyse',
        'combine_extractions': 'Extraktionen zusammenführen',
        'process_record': 'Datensatz verarbeiten',
        'extract_medical_codes': 'Medizinische Codes extrahieren',
        'save_medical_codes': 'Medizinische Codes speichern',
        'update_medical_codes_descriptions': 'Code-Beschreibungen aktualisieren',
        'create_report': 'Berichte erstellen',
        'generate_single_report': 'Einzelbericht generieren',
        'regenerate_report_task': 'Bericht neu generieren'
    }
    
    return display_names.get(task_name, f"Task: {task_name}")


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
            'output_format': report.report_template.output_format,
            'example_structure': report.report_template.example_structure,
            'unique_identifier': report.unique_identifier
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

@app.template_filter('format_date')
def format_date_filter(date):
    if isinstance(date, datetime):
        return date.strftime('%d.%m.%Y')
    return ''

@app.template_filter('format_timestamp')
def format_timestamp_filter(timestamp):
    if isinstance(timestamp, datetime):
        return timestamp.strftime('%Y-%m-%d %H:%M')
    return ''

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

@app.route('/edit_report_templates')
@login_required
def edit_report_templates():
    if current_user.level != 'admin':
        flash('Sie haben keine Berechtigung für diese Seite.', 'error')
        return redirect(url_for('index'))
    templates = ReportTemplate.query.order_by(ReportTemplate.template_name).all()
    return render_template('edit_report_templates.html', templates=templates)

@app.route('/get_template/<int:template_id>')
@login_required
def get_template(template_id):
    if current_user.level != 'admin':
        abort(403)
    template = ReportTemplate.query.get_or_404(template_id)
    return jsonify({
        'id': template.id,
        'template_name': template.template_name,
        'output_format': template.output_format,
        'example_structure': template.example_structure,
        'system_prompt': template.system_prompt,
        'prompt': template.prompt,
        'system_pdf_filename': template.system_pdf_filename,
        'summarizer': template.summarizer,
        'use_custom_instructions': template.use_custom_instructions
    })

@app.route('/update_template', methods=['POST'])
@login_required
def update_template():
    data = request.get_json()
    template = ReportTemplate.query.get_or_404(data['id'])
    try:
        template.template_name = data['template_name']
        template.output_format = data['output_format']
        template.example_structure = data['example_structure']
        template.system_prompt = data['system_prompt']
        template.prompt = data['prompt']
        template.system_pdf_filename = data.get('system_pdf_filename')
        template.summarizer = data['summarizer']
        template.use_custom_instructions = data['use_custom_instructions']
        template.last_updated = datetime.utcnow()
        
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

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
    try:
        template = ReportTemplate(
            template_name=data['template_name'],
            output_format=data['output_format'],
            example_structure=data['example_structure'],
            system_prompt=data['system_prompt'],
            prompt=data['prompt'],
            system_pdf_filename=data.get('system_pdf_filename'),
            summarizer=data['summarizer'],
            use_custom_instructions=data['use_custom_instructions']
        )
        db.session.add(template)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})
    

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

    try:
        # Template holen
        template = ReportTemplate.query.get_or_404(template_id)
        
        # Erstelle den Report bereits hier mit Status "generating"
        new_report = Report(
            health_record_id=record_id,
            report_template_id=template_id,
            report_type=template.template_name,
            generation_status='generating',
            generation_started_at=datetime.utcnow()
        )
        db.session.add(new_report)
        db.session.commit()
        report_id = new_report.id
        
        logger.info(f"Created new report {report_id} with status 'generating'")
        
        # Starten des Celery-Tasks zum Generieren des Berichts mit der Report-ID
        task = generate_single_report.apply_async(args=[record_id, template_id, report_id])
        
        return jsonify({
            'message': 'Der Bericht wird generiert. Diese Aktion kann einige Zeit dauern.',
            'task_id': task.id,
            'report_id': report_id  # Gebe die Report-ID zurück für das Polling
        })
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating report: {str(e)}")
        return jsonify({'error': f'Fehler beim Erstellen des Berichts: {str(e)}'}), 500

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

@app.route('/user_management')
@login_required
def user_management():
    if current_user.level != 'admin':
        abort(403)
    users = User.query.all()
    return render_template('user_management.html', users=users, current_user=current_user)

@app.route('/create_user', methods=['POST'])
@login_required
def create_user():
    if current_user.level != 'admin':
        abort(403)
    
    try:
        # Überprüfen, ob Benutzername oder E-Mail bereits existieren
        existing_user = User.query.filter(
            (User.username == request.form['username']) | 
            (User.email == request.form['email'])
        ).first()
        
        if existing_user:
            if existing_user.username == request.form['username']:
                flash('Dieser Benutzername existiert bereits.', 'error')
            else:
                flash('Diese E-Mail-Adresse existiert bereits.', 'error')
            return redirect(url_for('user_management'))

        # Neuen Benutzer erstellen
        user = User(
            vorname=request.form['vorname'],
            nachname=request.form['nachname'],
            username=request.form['username'],
            email=request.form['email'],
            level=request.form['level'],
            is_active='is_active' in request.form
        )
        user.set_password(request.form['password'])
        
        db.session.add(user)
        db.session.commit()
        flash('Benutzer wurde erfolgreich erstellt.', 'success')
        
    except IntegrityError as e:
        db.session.rollback()
        flash('Ein Benutzer mit diesem Benutzernamen oder dieser E-Mail existiert bereits.', 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Erstellen des Benutzers: {str(e)}', 'error')
        print(f"Error creating user: {str(e)}")
    
    return redirect(url_for('user_management'))

@app.route('/edit_user/<int:user_id>', methods=['POST'])
@login_required
def edit_user(user_id):
    if current_user.level != 'admin':
        abort(403)
    
    if not request.form:
        flash('Keine Daten empfangen.', 'error')
        return redirect(url_for('user_management'))

    user = User.query.get_or_404(user_id)
    try:
        # Überprüfen auf Duplikate, aber den aktuellen Benutzer ausschließen
        existing_user = User.query.filter(
            (User.id != user_id) & 
            ((User.username == request.form['username']) | 
             (User.email == request.form['email']))
        ).first()
        
        if existing_user:
            if existing_user.username == request.form['username']:
                flash('Dieser Benutzername existiert bereits.', 'error')
            else:
                flash('Diese E-Mail-Adresse existiert bereits.', 'error')
            return redirect(url_for('user_management'))

        # Benutzer aktualisieren
        user.vorname = request.form['vorname']
        user.nachname = request.form['nachname']
        user.username = request.form['username']
        user.email = request.form['email']
        user.level = request.form['level']
        user.is_active = 'is_active' in request.form

        if request.form.get('password'):
            user.set_password(request.form['password'])
        
        db.session.commit()
        flash('Benutzer wurde erfolgreich aktualisiert.', 'success')
        
    except IntegrityError:
        db.session.rollback()
        flash('Ein Benutzer mit diesem Benutzernamen oder dieser E-Mail existiert bereits.', 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Aktualisieren des Benutzers: {str(e)}', 'error')
        print(f"Error updating user: {str(e)}")
    
    return redirect(url_for('user_management'))

@app.route('/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if current_user.level != 'admin':
        abort(403)
    
    if user_id == current_user.id:
        flash('Sie können sich nicht selbst löschen.', 'error')
        return redirect(url_for('user_management'))
    
    user = User.query.get_or_404(user_id)
    try:
        db.session.delete(user)
        db.session.commit()
        flash('Benutzer wurde erfolgreich gelöscht.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Fehler beim Löschen des Benutzers: {str(e)}', 'error')
        print(f"Error deleting user: {str(e)}")  # Für Debugging
    
    return redirect(url_for('user_management'))

@app.route('/update_record/<int:record_id>', methods=['POST'])
@login_required
def update_record(record_id):
    record = HealthRecord.query.get_or_404(record_id)
    if record.user_id != current_user.id:
        abort(403)

    try:
        # Update Custom Instructions
        custom_instructions = request.form.get('customInstructions')
        if custom_instructions is not None:
            record.custom_instructions = custom_instructions

        # Update Geburtsdatum
        birth_date_str = request.form.get('birthDate')
        if birth_date_str:
            try:
                record.birth_date = datetime.strptime(birth_date_str, '%Y-%m-%d')
            except ValueError:
                return jsonify({'success': False, 'error': 'Ungültiges Datumsformat'}), 400
        else:
            record.birth_date = None

        # Speichere die Änderungen an den Metadaten
        db.session.commit()

        # Handle file upload if files are included
        if 'files[]' in request.files:
            files = request.files.getlist('files[]')
            filenames = []
            
            for file in files:
                if file and file.filename.endswith('.pdf'):
                    filename = secure_filename(file.filename)
                    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(file_path)
                    filenames.append(filename)

            if filenames:
                # Setze Status auf processing bevor der Task startet
                record.processing_status = 'processing'
                db.session.commit()
                
                # Starte den process_pdfs Task für neue Dateien
                # und übergebe dabei auch die aktualisierten Metadaten
                result = process_pdfs.delay(
                    filenames,
                    record.patient_name,
                    record.id,
                    record.create_reports,
                    record.user_id,
                    record.custom_instructions,  # Übergebe die aktualisierten Custom Instructions
                    record.birth_date  # Übergebe das aktualisierte Geburtsdatum
                )
                return jsonify({'success': True, 'task_id': result.id, 'record_id': record.id})

        # Wenn keine Dateien hochgeladen wurden, gib Erfolg zurück
        return jsonify({'success': True})

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/remove_file_from_record/<int:record_id>/<path:filename>', methods=['DELETE'])
@login_required
def remove_file_from_record(record_id, filename):
    record = HealthRecord.query.get_or_404(record_id)
    if record.user_id != current_user.id:
        abort(403)

    try:
        logger.info(f"=== Starting file removal process ===")
        logger.info(f"Removing file {filename} from record {record_id}")
        logger.info(f"Initial text length: {len(record.text) if record.text else 0}")
        logger.info(f"Initial token count: {record.token_count}")

        # Get current filenames
        current_filenames = record.filenames.split(',') if record.filenames else []
        logger.info(f"Current filenames: {current_filenames}")
        
        if filename not in current_filenames:
            logger.warning(f"File {filename} not found in record")
            return jsonify({'success': False, 'error': 'Datei nicht gefunden'}), 404

        # Remove filename from list
        current_filenames.remove(filename)
        record.filenames = ','.join(current_filenames) if current_filenames else None
        logger.info(f"Updated filenames: {record.filenames}")

        # Remove the extracted text for this file
        if record.text:
            # Define patterns for different extraction types
            patterns = [
                f'<extraction method="pdf_text"><document title="{filename}">(.*?)</document>',
                f'<extraction method="ocr"><document title="{filename}">(.*?)</document>',
                f'<extraction method="azure_vision"><document title="{filename}">(.*?)</document>',
                f'<extraction method="gpt4_vision"><document title="{filename}">(.*?)</document>'
            ]
            
            # Remove each pattern from the text
            text = record.text
            logger.info(f"Original text snippet: {text[:500]}...")  # First 500 chars
            
            for pattern in patterns:
                logger.info(f"Applying pattern: {pattern}")
                new_text = re.sub(pattern, '', text, flags=re.DOTALL)
                if new_text != text:
                    logger.info(f"Pattern matched and removed content")
                    logger.info(f"Text length before: {len(text)}, after: {len(new_text)}")
                text = new_text
            
            # Clean up any double newlines that might have been created
            text = re.sub(r'\n{3,}', '\n\n', text.strip())
            logger.info(f"Final text snippet: {text[:500]}...")  # First 500 chars
            
            record.text = text if text.strip() else None

            # Update token count using the utility function
            if record.text:
                old_token_count = record.token_count
                record.token_count = count_tokens(record.text)
                logger.info(f"Token count updated from {old_token_count} to {record.token_count}")
            else:
                record.token_count = 0
                logger.info("Text was empty after removal, token count set to 0")

            # Re-analyze remaining text for medical codes
            if record.text:
                extraction_result = extract_medical_codes.apply_async((record.text,)).get()
                if not isinstance(extraction_result, dict) or 'exc_type' not in extraction_result:
                    parsed_result = parse_medical_codes_xml(extraction_result)
                    if parsed_result:
                        save_result = save_medical_codes.apply_async((parsed_result, record_id)).get()
                        logger.info(f"Updated medical codes after file removal: {save_result}")
                        
                        # Hier die Beschreibungen aktualisieren
                        if save_result.get('status') == 'success':
                            update_result = update_medical_codes_descriptions.apply_async((record_id,)).get()
                            logger.info(f"Updated medical code descriptions after file removal: {update_result}")

        # If this was the last file or no text remains, delete the entire record
        if not current_filenames or not record.text:
            logger.info("Deleting entire record as no files or text remain")
            db.session.delete(record)
            db.session.commit()
            return jsonify({'success': True, 'deleted_record': True})

        logger.info(f"=== File removal process completed ===")
        logger.info(f"Final text length: {len(record.text) if record.text else 0}")
        logger.info(f"Final token count: {record.token_count}")
        
        # Commit the changes to the record
        db.session.commit()

        return jsonify({'success': True, 'deleted_record': False})

    except Exception as e:
        logger.exception("Error in remove_file_from_record")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# System PDF Management Routes
@app.route('/upload_system_pdf/<int:template_id>', methods=['POST'])
@login_required
def upload_system_pdf(template_id):
    if current_user.level != 'admin':
        abort(403)
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Keine Datei ausgewählt'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Keine Datei ausgewählt'}), 400
    
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'success': False, 'error': 'Nur PDF-Dateien sind erlaubt'}), 400
    
    try:
        template = ReportTemplate.query.get_or_404(template_id)
        
        # Erstelle sicheren Dateinamen mit Template-ID als Präfix
        original_filename = secure_filename(file.filename)
        safe_filename = f"template_{template_id}_{original_filename}"
        
        # Stelle sicher, dass der system/prompts Ordner existiert
        system_prompts_dir = os.path.join(app.root_path, 'system', 'prompts')
        os.makedirs(system_prompts_dir, exist_ok=True)
        
        # Lösche alte PDF falls vorhanden
        if template.system_pdf_filename:
            old_path = os.path.join(system_prompts_dir, template.system_pdf_filename)
            if os.path.exists(old_path):
                os.remove(old_path)
        
        # Speichere neue PDF
        file_path = os.path.join(system_prompts_dir, safe_filename)
        file.save(file_path)
        
        # Update Template
        template.system_pdf_filename = safe_filename
        template.last_updated = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'filename': safe_filename,
            'message': 'PDF erfolgreich hochgeladen'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/download_system_pdf/<int:template_id>')
@login_required
def download_system_pdf(template_id):
    if current_user.level != 'admin':
        abort(403)
    
    try:
        template = ReportTemplate.query.get_or_404(template_id)
        
        if not template.system_pdf_filename:
            return jsonify({'error': 'Keine PDF-Datei vorhanden'}), 404
        
        system_prompts_dir = os.path.join(app.root_path, 'system', 'prompts')
        file_path = os.path.join(system_prompts_dir, template.system_pdf_filename)
        
        if not os.path.exists(file_path):
            return jsonify({'error': 'PDF-Datei nicht gefunden'}), 404
        
        return send_from_directory(
            system_prompts_dir, 
            template.system_pdf_filename, 
            as_attachment=True,
            download_name=f"{template.template_name}.pdf"
        )
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/delete_system_pdf/<int:template_id>', methods=['DELETE'])
@login_required
def delete_system_pdf(template_id):
    if current_user.level != 'admin':
        abort(403)
    
    try:
        template = ReportTemplate.query.get_or_404(template_id)
        
        if not template.system_pdf_filename:
            return jsonify({'success': False, 'error': 'Keine PDF-Datei vorhanden'}), 404
        
        # Lösche Datei vom Dateisystem
        system_prompts_dir = os.path.join(app.root_path, 'system', 'prompts')
        file_path = os.path.join(system_prompts_dir, template.system_pdf_filename)
        
        if os.path.exists(file_path):
            os.remove(file_path)
        
        # Update Template in Datenbank
        template.system_pdf_filename = None
        template.last_updated = datetime.utcnow()
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'PDF erfolgreich gelöscht'})
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True, host='127.0.0.1', port=5000)
