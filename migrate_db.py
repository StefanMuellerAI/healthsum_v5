from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, Text, Enum, ForeignKey
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.orm import declarative_base  # Neue Import-Methode für SQLAlchemy 2.0
from models import User, Report, ReportTemplate, TaskMonitor as NewTaskMonitor, MedicalCode
import os
from sqlalchemy_utils.types.encrypted.encrypted_type import AesEngine
from sqlalchemy_utils import EncryptedType, StringEncryptedType  # StringEncryptedType hinzugefügt

# Erstelle eine minimale Flask-App für den Kontext
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')

# Konfiguriere die Datenbank-URLs
old_db_path = os.path.join('instance', 'health_records_old.db')
new_db_path = os.path.join('instance', 'health_records.db')
app.config['SQLALCHEMY_BINDS'] = {
    'old': f'sqlite:///{old_db_path}',
    'new': f'sqlite:///{new_db_path}'
}

# Definiere alte Models ohne die neuen Spalten
Base = declarative_base()

class OldHealthRecord(Base):
    __tablename__ = 'health_record'
    
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    text = Column(StringEncryptedType(Text, lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    filenames = Column(StringEncryptedType(Text, lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    token_count = Column(Integer)
    patient_name = Column(StringEncryptedType(String(100), lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    medical_history_begin = Column(DateTime)
    medical_history_end = Column(DateTime)
    create_reports = Column(Boolean, default=False)
    expiration_date = Column(DateTime, nullable=True)
    user_id = Column(Integer, ForeignKey('user.id'), nullable=False)

class OldReportTemplate(Base):
    __tablename__ = 'report_template'
    
    id = Column(Integer, primary_key=True)
    template_name = Column(String(100), nullable=False)
    output_format = Column(Enum('JSON', 'TEXT', name='output_formats'), nullable=False)
    example_structure = Column(StringEncryptedType(Text, lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    system_prompt = Column(StringEncryptedType(Text, lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    prompt = Column(StringEncryptedType(Text, lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    summarizer = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_updated = Column(DateTime, nullable=True)

class OldReport(Base):
    __tablename__ = 'report'
    
    id = Column(Integer, primary_key=True)
    health_record_id = Column(Integer, ForeignKey('health_record.id'), nullable=False)
    report_template_id = Column(Integer, ForeignKey('report_template.id'), nullable=False)
    content = Column(StringEncryptedType(Text, lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    report_type = Column(String(50))
    created_at = Column(DateTime, default=datetime.utcnow)

class OldMedicalCode(Base):
    __tablename__ = 'medical_code'
    
    id = Column(Integer, primary_key=True)
    health_record_id = Column(Integer, ForeignKey('health_record.id'), nullable=False)
    code = Column(StringEncryptedType(String(20), lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'))
    code_type = Column(Enum('ICD10', 'ICD11', 'OPS', name='code_types'), nullable=False)
    description = Column(StringEncryptedType(Text, lambda: app.config['SECRET_KEY'], AesEngine, 'pkcs5'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

def clear_new_database(session):
    """Löscht alle Daten aus der neuen Datenbank"""
    session.query(NewTaskMonitor).delete()
    session.query(Report).delete()
    session.query(ReportTemplate).delete()
    session.query(MedicalCode).delete()
    session.query(User).delete()
    session.commit()

def migrate_data():
    old_engine = create_engine(app.config['SQLALCHEMY_BINDS']['old'])
    new_engine = create_engine(app.config['SQLALCHEMY_BINDS']['new'])
    
    OldSession = sessionmaker(bind=old_engine)
    NewSession = sessionmaker(bind=new_engine)
    
    old_session = OldSession()
    new_session = NewSession()
    
    try:
        with app.app_context():
            # Lösche zuerst alle Daten in der neuen Datenbank
            print("Lösche bestehende Daten in der neuen Datenbank...")
            clear_new_database(new_session)
            
            # Migriere User
            print("Migriere User...")
            for old_user in old_session.query(User).all():
                new_user = User(
                    id=old_user.id,
                    vorname=old_user.vorname,
                    nachname=old_user.nachname,
                    username=old_user.username,
                    email=old_user.email,
                    password_hash=old_user.password_hash,
                    level=old_user.level,
                    is_active=old_user.is_active
                )
                new_session.add(new_user)
            new_session.commit()
            
            # Migriere ReportTemplates mit dem alten Model
            for old_template in old_session.query(OldReportTemplate).all():
                new_template = ReportTemplate(
                    id=old_template.id,
                    template_name=old_template.template_name,
                    output_format=old_template.output_format,
                    example_structure=old_template.example_structure,
                    system_prompt=old_template.system_prompt,
                    prompt=old_template.prompt,
                    summarizer=old_template.summarizer,
                    created_at=old_template.created_at,
                    last_updated=old_template.last_updated,
                    use_custom_instructions=False  # Neue Spalte mit Standardwert
                )
                new_session.add(new_template)
            
            # Migriere HealthRecords mit dem alten Model
            for old_record in old_session.query(OldHealthRecord).all():
                from models import HealthRecord
                new_record = HealthRecord(
                    id=old_record.id,
                    timestamp=old_record.timestamp,
                    text=old_record.text,
                    filenames=old_record.filenames,
                    token_count=old_record.token_count,
                    patient_name=old_record.patient_name,
                    birth_date=None,  # Neue Spalte mit None initialisiert
                    medical_history_begin=old_record.medical_history_begin,
                    medical_history_end=old_record.medical_history_end,
                    create_reports=old_record.create_reports,
                    expiration_date=old_record.expiration_date,
                    user_id=old_record.user_id,
                    custom_instructions=None
                )
                new_session.add(new_record)
            
            # Migriere Reports mit dem alten Model
            for old_report in old_session.query(OldReport).all():
                new_report = Report(
                    id=old_report.id,
                    health_record_id=old_report.health_record_id,
                    report_template_id=old_report.report_template_id,
                    content=old_report.content,
                    report_type=old_report.report_type,
                    created_at=old_report.created_at
                )
                new_session.add(new_report)
            
            # Migriere TaskMonitors
            for old_monitor in old_session.query(NewTaskMonitor).all():
                new_monitor = NewTaskMonitor(
                    id=old_monitor.id,
                    created_at=old_monitor.created_at,
                    health_record_id=old_monitor.health_record_id,
                    health_record_token_count=old_monitor.health_record_token_count,
                    start_date=old_monitor.start_date,
                    end_date=old_monitor.end_date,
                    notification_sent=old_monitor.notification_sent
                )
                new_session.add(new_monitor)
            
            # Migriere MedicalCodes nur wenn die Tabelle existiert
            try:
                for old_code in old_session.query(OldMedicalCode).all():
                    new_code = MedicalCode(
                        id=old_code.id,
                        health_record_id=old_code.health_record_id,
                        code=old_code.code,
                        code_type=old_code.code_type,
                        description=old_code.description,
                        created_at=old_code.created_at
                    )
                    new_session.add(new_code)
                print("MedicalCodes erfolgreich migriert!")
            except Exception as e:
                print("Überspringe MedicalCode Migration - Tabelle existiert vermutlich noch nicht")
                print(f"Details: {str(e)}")
            
            # Commit alle Änderungen
            new_session.commit()
            print("Migration erfolgreich abgeschlossen!")
            
    except Exception as e:
        print(f"Fehler bei der Migration: {str(e)}")
        new_session.rollback()
        raise e
    finally:
        old_session.close()
        new_session.close()

if __name__ == '__main__':
    migrate_data() 