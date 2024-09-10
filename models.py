# models.py
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from sqlalchemy_utils.types.encrypted.encrypted_type import AesEngine
from sqlalchemy_utils import EncryptedType
from flask import current_app

db = SQLAlchemy()

class HealthRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    text = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    filenames = db.Column(db.Text)
    token_count = db.Column(db.Integer)
    patient_name = db.Column(EncryptedType(db.String(100), lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    medical_history_begin = db.Column(db.DateTime)
    medical_history_end = db.Column(db.DateTime)
    create_reports = db.Column(db.Boolean, default=False)
    expiration_date = db.Column(db.DateTime, nullable=True)
    
    reports = db.relationship('Report', back_populates='health_record', cascade='all, delete-orphan')

class Report(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    health_record_id = db.Column(db.Integer, db.ForeignKey('health_record.id'), nullable=False)
    content = db.Column(EncryptedType(db.Text, lambda: current_app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    report_type = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Fügen Sie diese Zeile hinzu
    health_record = db.relationship('HealthRecord', back_populates='reports')

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    vorname = db.Column(db.String(50), nullable=False)
    nachname = db.Column(db.String(50), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    level = db.Column(db.Enum('user', 'admin', name='user_levels'), default='user')
    is_active = db.Column(db.Boolean, default=True)

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