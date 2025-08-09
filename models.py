# models.py
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from sqlalchemy_utils.types.encrypted.encrypted_type import AesEngine
from sqlalchemy_utils import EncryptedType
from flask import current_app
from sqlalchemy import Sequence

db = SQLAlchemy()

class HealthRecord(db.Model):
    __table_args__ = {'sqlite_autoincrement': True}
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    text = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    filenames = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    token_count = db.Column(db.Integer)
    patient_name = db.Column(EncryptedType(db.String(100), lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    birth_date = db.Column(EncryptedType(db.DateTime, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'), nullable=True)
    medical_history_begin = db.Column(db.DateTime)
    medical_history_end = db.Column(db.DateTime)
    create_reports = db.Column(db.Boolean, default=False)
    expiration_date = db.Column(db.DateTime, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    custom_instructions = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'), nullable=True)
    
    # Dauerhafter Verarbeitungsstatus
    processing_status = db.Column(db.Enum('pending', 'processing', 'completed', 'failed', name='processing_statuses'), default='pending')
    processing_completed_at = db.Column(db.DateTime, nullable=True)
    processing_error_message = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'), nullable=True)
    
    user = db.relationship('User', back_populates='health_records')
    reports = db.relationship('Report', back_populates='health_record', cascade='all, delete-orphan')
    task_monitors = db.relationship('TaskMonitor', back_populates='health_record', cascade='all, delete-orphan')
    medical_codes = db.relationship('MedicalCode', back_populates='health_record', cascade='all, delete-orphan')
    task_logs = db.relationship('TaskLog', back_populates='health_record', cascade='all, delete-orphan')

class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    health_record_id = db.Column(db.Integer, db.ForeignKey('health_record.id'), nullable=False)
    report_template_id = db.Column(db.Integer, db.ForeignKey('report_template.id'), nullable=False)
    content = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    report_type = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Eindeutige Bezeichnung
    unique_identifier = db.Column(db.String(255), nullable=True, unique=True)
    
    # Report-Generierungsstatus
    generation_status = db.Column(db.Enum('pending', 'generating', 'completed', 'failed', name='report_statuses'), default='pending')
    generation_started_at = db.Column(db.DateTime, nullable=True)
    generation_completed_at = db.Column(db.DateTime, nullable=True)
    generation_error_message = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'), nullable=True)
    
    health_record = db.relationship('HealthRecord', back_populates='reports')
    report_template = db.relationship('ReportTemplate', back_populates='reports')

class ReportTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    template_name = db.Column(db.String(100), nullable=False)
    output_format = db.Column(db.Enum('JSON', 'TEXT', name='output_formats'), nullable=False)
    example_structure = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    system_prompt = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    prompt = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    summarizer = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_updated = db.Column(db.DateTime, nullable=True)
    use_custom_instructions = db.Column(db.Boolean, nullable=True)
    system_pdf_filename = db.Column(db.String(255), nullable=True)  # Dateiname der System-PDF
    
    reports = db.relationship('Report', back_populates='report_template', cascade='all, delete-orphan')

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    vorname = db.Column(db.String(50), nullable=False)
    nachname = db.Column(db.String(50), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    level = db.Column(db.Enum('user', 'admin', name='user_levels'), default='user')
    is_active = db.Column(db.Boolean, default=True)
    
    health_records = db.relationship('HealthRecord', back_populates='user')
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def get_id(self):
        return str(self.id)
    
    @property
    def is_authenticated(self):
        return True
    
    @property
    def is_anonymous(self):
        return False

class TaskMonitor(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    health_record_id = db.Column(db.Integer, db.ForeignKey('health_record.id'), nullable=False)
    health_record_token_count = db.Column(db.Integer, nullable=True)
    start_date = db.Column(db.DateTime, nullable=True)
    end_date = db.Column(db.DateTime, nullable=True)
    notification_sent = db.Column(db.Boolean, default=False)
    
    health_record = db.relationship('HealthRecord', back_populates='task_monitors')

class MedicalCode(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    health_record_id = db.Column(db.Integer, db.ForeignKey('health_record.id'), nullable=False)
    code = db.Column(EncryptedType(db.String(20), lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    code_type = db.Column(db.Enum('ICD10', 'ICD11', 'OPS', name='code_types'), nullable=False)
    description = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    health_record = db.relationship('HealthRecord', back_populates='medical_codes')

class TaskLog(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    health_record_id = db.Column(db.Integer, db.ForeignKey('health_record.id'), nullable=False)
    task_name = db.Column(db.String(100), nullable=False)  # z.B. 'process_pdfs', 'extract_ocr_optimized'
    task_id = db.Column(db.String(255), nullable=True)  # Celery Task ID
    status = db.Column(db.Enum('started', 'success', 'failed', 'retry', name='task_statuses'), nullable=False)
    error_message = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'), nullable=True)
    error_type = db.Column(db.String(100), nullable=True)  # Exception class name
    retry_count = db.Column(db.Integer, default=0)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    duration_seconds = db.Column(db.Float, nullable=True)
    
    # Zusätzliche Metadaten als JSON
    task_metadata = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'), nullable=True)
    
    health_record = db.relationship('HealthRecord', back_populates='task_logs')
    
    def to_dict(self):
        """Konvertiert TaskLog zu Dictionary für Frontend"""
        return {
            'id': self.id,
            'task_name': self.task_name,
            'task_id': self.task_id,
            'status': self.status,
            'error_message': self.error_message,
            'error_type': self.error_type,
            'retry_count': self.retry_count,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'duration_seconds': self.duration_seconds
        }


# SQLAlchemy Event Listener für automatische Generierung der eindeutigen Bezeichnung
from sqlalchemy import event, text
from sqlalchemy.orm import object_session

def generate_report_identifier(report):
    """
    Hilfsfunktion zur Generierung der eindeutigen Bezeichnung
    Format: UserID-HealthRecordID-TemplateID-YYYYMMDD-ReportID
    """
    # Hole das HealthRecord über die Session
    session = object_session(report)
    if session:
        health_record = session.query(HealthRecord).get(report.health_record_id)
    else:
        # Fallback wenn keine Session verfügbar
        health_record = HealthRecord.query.get(report.health_record_id)
    
    if health_record:
        user_id = health_record.user_id
        health_record_id = report.health_record_id
        template_id = report.report_template_id
        datum = report.created_at.strftime('%Y%m%d') if report.created_at else datetime.utcnow().strftime('%Y%m%d')
        report_id = report.id
        
        return f"{user_id}-{health_record_id}-{template_id}-{datum}-{report_id}"
    
    return None


@event.listens_for(Report, 'before_insert')
def generate_unique_identifier(mapper, connection, target):
    """
    Generiert automatisch die eindeutige Bezeichnung beim Erstellen eines Reports
    Format: UserID-HealthRecordID-TemplateID-YYYYMMDD-ReportID
    """
    if target.unique_identifier is None:  # Nur generieren wenn noch nicht gesetzt
        # Verwende einen temporären Platzhalter, der nach dem Insert aktualisiert wird
        target.unique_identifier = "TEMP_IDENTIFIER"


@event.listens_for(Report, 'after_insert')
def update_unique_identifier_with_id(mapper, connection, target):
    """
    Aktualisiert die eindeutige Bezeichnung mit der finalen Report ID nach dem Insert
    """
    if target.unique_identifier == "TEMP_IDENTIFIER":
        identifier = generate_report_identifier(target)
        if identifier:
            # Verwende text() für SQL-Statements
            connection.execute(
                text("UPDATE report SET unique_identifier = :identifier WHERE id = :report_id"),
                {"identifier": identifier, "report_id": target.id}
            )


@event.listens_for(Report, 'before_update')
def update_unique_identifier_on_update(mapper, connection, target):
    """
    Aktualisiert die eindeutige Bezeichnung beim Update eines Reports
    (z.B. bei Neugenerierung)
    """
    # Prüfe ob der Report neu generiert wird (Status ändert sich)
    if target.generation_status == 'generating' or target.generation_status == 'completed':
        # Generiere neue Bezeichnung mit aktuellem Datum
        identifier = generate_report_identifier(target)
        if identifier:
            # Aktualisiere das Datum im Identifier mit dem heutigen Datum
            # Format: UserID-HealthRecordID-TemplateID-YYYYMMDD-ReportID
            parts = identifier.split('-')
            if len(parts) >= 5:
                # Das Datum ist der 4. Teil (Index 3)
                datum = datetime.utcnow().strftime('%Y%m%d')
                parts[3] = datum  # Update das Datum
                target.unique_identifier = '-'.join(parts)