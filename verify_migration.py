#!/usr/bin/env python3
"""
Verifiziert eine bereits migrierte Datenbank
"""

from flask import Flask
import os
import sys

# Erstelle eine minimale Flask-App für den Kontext
app = Flask(__name__)

# Lade SECRET_KEY
secret_key = os.getenv('SECRET_KEY')
if not secret_key:
    try:
        import config
        secret_key = getattr(config, 'SECRET_KEY', None)
    except ImportError:
        pass

if not secret_key:
    print("\nFEHLER: SECRET_KEY konnte nicht gefunden werden!")
    sys.exit(1)

app.config['SECRET_KEY'] = secret_key
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Definiere Datenbankpfad
db_path = os.path.abspath(os.path.join('instance', 'health_records.db'))

if not os.path.exists(db_path):
    print(f"FEHLER: Datenbank {db_path} nicht gefunden!")
    sys.exit(1)

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

# Importiere Models
from models import (
    db, HealthRecord, Report, ReportTemplate, User,
    TaskMonitor, MedicalCode, TaskLog
)

db.init_app(app)

with app.app_context():
    print("🔍 Verifiziere migrierte Datenbank...")
    print(f"   Datenbankpfad: {db_path}")
    
    # Zähle Datensätze
    health_records_count = HealthRecord.query.count()
    reports_count = Report.query.count()
    users_count = User.query.count()
    templates_count = ReportTemplate.query.count()
    task_logs_count = TaskLog.query.count()
    
    print(f"\n📊 Migrationsergebnis:")
    print(f"  - Users: {users_count}")
    print(f"  - HealthRecords: {health_records_count}")
    print(f"  - Reports: {reports_count}")
    print(f"  - ReportTemplates: {templates_count}")
    print(f"  - TaskLogs: {task_logs_count}")
    
    # Prüfe Status-Felder
    pending_records = HealthRecord.query.filter_by(processing_status='pending').count()
    completed_records = HealthRecord.query.filter_by(processing_status='completed').count()
    processing_records = HealthRecord.query.filter_by(processing_status='processing').count()
    failed_records = HealthRecord.query.filter_by(processing_status='failed').count()
    
    print(f"\n📈 HealthRecord Status-Verteilung:")
    print(f"  - completed: {completed_records}")
    print(f"  - pending: {pending_records}")
    print(f"  - processing: {processing_records}")
    print(f"  - failed: {failed_records}")
    
    # Report Status
    report_pending = Report.query.filter_by(generation_status='pending').count()
    report_completed = Report.query.filter_by(generation_status='completed').count()
    report_generating = Report.query.filter_by(generation_status='generating').count()
    report_failed = Report.query.filter_by(generation_status='failed').count()
    
    print(f"\n📊 Report Status-Verteilung:")
    print(f"  - completed: {report_completed}")
    print(f"  - pending: {report_pending}")
    print(f"  - generating: {report_generating}")
    print(f"  - failed: {report_failed}")
    
    # Prüfe unique_identifiers
    reports_with_id = Report.query.filter(Report.unique_identifier.isnot(None)).count()
    reports_without_id = Report.query.filter(Report.unique_identifier.is_(None)).count()
    
    print(f"\n🔑 Unique Identifiers:")
    print(f"  - Reports mit unique_identifier: {reports_with_id}")
    print(f"  - Reports ohne unique_identifier: {reports_without_id}")
    
    # Zeige ein paar Beispiel-Identifiers
    sample_reports = Report.query.filter(Report.unique_identifier.isnot(None)).limit(3).all()
    if sample_reports:
        print(f"\n📝 Beispiel unique_identifiers:")
        for report in sample_reports:
            print(f"  - {report.unique_identifier}")
    
    print("\n✅ Verifizierung abgeschlossen!")
