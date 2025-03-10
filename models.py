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
    
    user = db.relationship('User', back_populates='health_records')
    reports = db.relationship('Report', back_populates='health_record', cascade='all, delete-orphan')
    task_monitors = db.relationship('TaskMonitor', back_populates='health_record', cascade='all, delete-orphan')
    medical_codes = db.relationship('MedicalCode', back_populates='health_record', cascade='all, delete-orphan')

class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    health_record_id = db.Column(db.Integer, db.ForeignKey('health_record.id'), nullable=False)
    report_template_id = db.Column(db.Integer, db.ForeignKey('report_template.id'), nullable=False)
    content = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    report_type = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
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