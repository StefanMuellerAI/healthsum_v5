#!/usr/bin/env python3
"""
Production Database Migration Script
Migriert health_records_old.db von der alten Struktur (models_old.py) 
zur neuen Struktur (models.py) und benennt sie in health_records.db um.

Neue Felder in dieser Migration:
- HealthRecord: processing_status, processing_completed_at, processing_error_message
- Report: unique_identifier, generation_status, generation_started_at, generation_completed_at, generation_error_message
- User: mfa_code_hash, mfa_code_created_at, mfa_code_attempts, mfa_last_request_at (MFA-Support)
- TaskLog: neue Tabelle für Task-Monitoring
"""

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
import os
import sys
import shutil
from models import (
    db as new_db, HealthRecord as NewHealthRecord, Report as NewReport,
    ReportTemplate as NewReportTemplate, User as NewUser,
    TaskMonitor as NewTaskMonitor, MedicalCode as NewMedicalCode,
    TaskLog as NewTaskLog
)

# Importiere get_config aus config.py für Azure Key Vault Zugriff
from config import get_config

# Erstelle eine minimale Flask-App für den Kontext
app = Flask(__name__)

# Lade Konfiguration aus Azure Key Vault
print("🔐 Lade Konfiguration aus Azure Key Vault...")
try:
    secret_key = get_config('SECRET_KEY')
    if not secret_key:
        raise ValueError("SECRET_KEY ist leer")
    print("  ✓ SECRET_KEY erfolgreich aus Key Vault geladen")
except Exception as e:
    print(f"\n❌ FEHLER: Konnte SECRET_KEY nicht aus Azure Key Vault laden!")
    print(f"   Fehlerdetails: {str(e)}")
    print("\n💡 Stellen Sie sicher, dass:")
    print("   1. Die .env Datei existiert und ENVIRONMENT=test oder ENVIRONMENT=prod enthält")
    print("   2. Sie authentifiziert sind (Azure CLI: az login)")
    print("   3. Der Key Vault 'healthsum-vault' existiert und zugänglich ist")
    print("   4. Das Secret 'healthsum-test' oder 'healthsum-prod' existiert")
    sys.exit(1)

app.config['SECRET_KEY'] = secret_key
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Definiere Datenbankpfade
old_db_path = os.path.join('instance', 'health_records_old.db')
new_db_path = os.path.join('instance', 'health_records.db')
backup_db_path = os.path.join('instance', 'health_records_backup.db')

def check_databases():
    """Prüft ob die alte Datenbank existiert"""
    if not os.path.exists(old_db_path):
        print(f"FEHLER: Alte Datenbank {old_db_path} nicht gefunden!")
        return False
    
    if os.path.exists(new_db_path):
        print(f"WARNUNG: Neue Datenbank {new_db_path} existiert bereits!")
        response = input("Möchten Sie die existierende Datenbank überschreiben? (j/n): ")
        if response.lower() != 'j':
            print("Migration abgebrochen.")
            return False
        else:
            # Erstelle Backup der existierenden neuen DB
            print(f"Erstelle Backup unter {backup_db_path}...")
            shutil.copy2(new_db_path, backup_db_path)
    
    return True

def add_new_columns_to_old_db():
    """Fügt die neuen Spalten zur alten Datenbank hinzu"""
    print("\n🔄 Füge neue Spalten zur alten Datenbank hinzu...")
    
    engine = create_engine(f'sqlite:///{old_db_path}')
    
    with engine.connect() as conn:
        # Prüfe existierende Spalten
        inspector = inspect(engine)
        
        # HealthRecord neue Spalten
        health_record_columns = [col['name'] for col in inspector.get_columns('health_record')]
        
        if 'processing_status' not in health_record_columns:
            print("  - Füge processing_status zu health_record hinzu...")
            conn.execute(text("""
                ALTER TABLE health_record 
                ADD COLUMN processing_status VARCHAR(20) DEFAULT 'completed'
            """))
            conn.commit()
        
        if 'processing_completed_at' not in health_record_columns:
            print("  - Füge processing_completed_at zu health_record hinzu...")
            conn.execute(text("""
                ALTER TABLE health_record 
                ADD COLUMN processing_completed_at DATETIME
            """))
            conn.commit()
            
        if 'processing_error_message' not in health_record_columns:
            print("  - Füge processing_error_message zu health_record hinzu...")
            conn.execute(text("""
                ALTER TABLE health_record 
                ADD COLUMN processing_error_message TEXT
            """))
            conn.commit()
        
        # Report neue Spalten
        report_columns = [col['name'] for col in inspector.get_columns('report')]
        
        if 'unique_identifier' not in report_columns:
            print("  - Füge unique_identifier zu report hinzu...")
            # SQLite erlaubt kein UNIQUE bei ALTER TABLE, also erst Spalte, dann Index
            conn.execute(text("""
                ALTER TABLE report 
                ADD COLUMN unique_identifier VARCHAR(255)
            """))
            conn.commit()
            
            # Erstelle UNIQUE Index separat
            print("  - Erstelle UNIQUE Index für unique_identifier...")
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_report_unique_identifier 
                ON report(unique_identifier)
            """))
            conn.commit()
        
        if 'generation_status' not in report_columns:
            print("  - Füge generation_status zu report hinzu...")
            conn.execute(text("""
                ALTER TABLE report 
                ADD COLUMN generation_status VARCHAR(20) DEFAULT 'completed'
            """))
            conn.commit()
        
        if 'generation_started_at' not in report_columns:
            print("  - Füge generation_started_at zu report hinzu...")
            conn.execute(text("""
                ALTER TABLE report 
                ADD COLUMN generation_started_at DATETIME
            """))
            conn.commit()
        
        if 'generation_completed_at' not in report_columns:
            print("  - Füge generation_completed_at zu report hinzu...")
            conn.execute(text("""
                ALTER TABLE report 
                ADD COLUMN generation_completed_at DATETIME
            """))
            conn.commit()
        
        if 'generation_error_message' not in report_columns:
            print("  - Füge generation_error_message zu report hinzu...")
            conn.execute(text("""
                ALTER TABLE report 
                ADD COLUMN generation_error_message TEXT
            """))
            conn.commit()
        
        # User neue Spalten (MFA-Felder)
        user_columns = [col['name'] for col in inspector.get_columns('user')]
        
        if 'mfa_code_hash' not in user_columns:
            print("  - Füge mfa_code_hash zu user hinzu...")
            conn.execute(text("""
                ALTER TABLE user 
                ADD COLUMN mfa_code_hash VARCHAR(128)
            """))
            conn.commit()
        
        if 'mfa_code_created_at' not in user_columns:
            print("  - Füge mfa_code_created_at zu user hinzu...")
            conn.execute(text("""
                ALTER TABLE user 
                ADD COLUMN mfa_code_created_at DATETIME
            """))
            conn.commit()
        
        if 'mfa_code_attempts' not in user_columns:
            print("  - Füge mfa_code_attempts zu user hinzu...")
            conn.execute(text("""
                ALTER TABLE user 
                ADD COLUMN mfa_code_attempts INTEGER DEFAULT 0
            """))
            conn.commit()
        
        if 'mfa_last_request_at' not in user_columns:
            print("  - Füge mfa_last_request_at zu user hinzu...")
            conn.execute(text("""
                ALTER TABLE user 
                ADD COLUMN mfa_last_request_at DATETIME
            """))
            conn.commit()
        
        # Erstelle TaskLog Tabelle wenn nicht existiert
        tables = inspector.get_table_names()
        if 'task_log' not in tables:
            print("  - Erstelle task_log Tabelle...")
            conn.execute(text("""
                CREATE TABLE task_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    health_record_id INTEGER NOT NULL,
                    task_name VARCHAR(100) NOT NULL,
                    task_id VARCHAR(255),
                    status VARCHAR(20) NOT NULL,
                    error_message TEXT,
                    error_type VARCHAR(100),
                    retry_count INTEGER DEFAULT 0,
                    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME,
                    duration_seconds FLOAT,
                    task_metadata TEXT,
                    FOREIGN KEY (health_record_id) REFERENCES health_record (id)
                )
            """))
            conn.commit()

def update_data_in_old_db():
    """Aktualisiert die Daten in der alten Datenbank"""
    print("\n🔄 Aktualisiere Daten in der alten Datenbank...")
    
    engine = create_engine(f'sqlite:///{old_db_path}')
    
    with engine.connect() as conn:
        # Setze processing_status für alle HealthRecords auf 'completed'
        print("  - Setze processing_status='completed' für alle HealthRecords...")
        result = conn.execute(text("""
            UPDATE health_record 
            SET processing_status = 'completed',
                processing_completed_at = timestamp
            WHERE processing_status IS NULL OR processing_status = ''
        """))
        conn.commit()
        print(f"    ✓ {result.rowcount} HealthRecords aktualisiert")
        
        # Setze generation_status für alle Reports auf 'completed'
        print("  - Setze generation_status='completed' für alle Reports...")
        result = conn.execute(text("""
            UPDATE report 
            SET generation_status = 'completed',
                generation_completed_at = created_at
            WHERE generation_status IS NULL OR generation_status = ''
        """))
        conn.commit()
        print(f"    ✓ {result.rowcount} Reports aktualisiert")
        
        # Generiere unique_identifier für alle Reports
        print("  - Generiere unique_identifier für alle Reports...")
        reports = conn.execute(text("""
            SELECT r.id, r.health_record_id, r.report_template_id, r.created_at, hr.user_id
            FROM report r
            JOIN health_record hr ON r.health_record_id = hr.id
            WHERE r.unique_identifier IS NULL
        """)).fetchall()
        
        for report in reports:
            report_id = report[0]
            health_record_id = report[1]
            template_id = report[2]
            created_at = report[3]
            user_id = report[4]
            
            # Parse das Datum
            if created_at:
                try:
                    date_obj = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    date_str = date_obj.strftime('%Y%m%d')
                except:
                    date_str = datetime.utcnow().strftime('%Y%m%d')
            else:
                date_str = datetime.utcnow().strftime('%Y%m%d')
            
            unique_id = f"{user_id}-{health_record_id}-{template_id}-{date_str}-{report_id}"
            
            conn.execute(text("""
                UPDATE report 
                SET unique_identifier = :unique_id
                WHERE id = :report_id
            """), {"unique_id": unique_id, "report_id": report_id})
        
        conn.commit()
        print(f"    ✓ {len(reports)} unique_identifiers generiert")

def rename_database():
    """Benennt die alte Datenbank in die neue um"""
    print("\n🔄 Benenne Datenbank um...")
    
    # Lösche existierende neue DB falls vorhanden
    if os.path.exists(new_db_path):
        os.remove(new_db_path)
    
    # Benenne alte DB um
    shutil.move(old_db_path, new_db_path)
    print(f"  ✓ {old_db_path} wurde zu {new_db_path} umbenannt")

def verify_migration():
    """Verifiziert die Migration"""
    print("\n🔍 Verifiziere Migration...")
    
    # Verwende absoluten Pfad für SQLAlchemy
    abs_new_db_path = os.path.abspath(new_db_path)
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{abs_new_db_path}'
    new_db.init_app(app)
    
    with app.app_context():
        # Zähle Datensätze
        health_records_count = NewHealthRecord.query.count()
        reports_count = NewReport.query.count()
        users_count = NewUser.query.count()
        templates_count = NewReportTemplate.query.count()
        
        print(f"\n📊 Migrationsergebnis:")
        print(f"  - Users: {users_count}")
        print(f"  - HealthRecords: {health_records_count}")
        print(f"  - Reports: {reports_count}")
        print(f"  - ReportTemplates: {templates_count}")
        
        # Prüfe Status-Felder
        pending_records = NewHealthRecord.query.filter_by(processing_status='pending').count()
        completed_records = NewHealthRecord.query.filter_by(processing_status='completed').count()
        
        print(f"\n📈 Status-Verteilung:")
        print(f"  - HealthRecords (completed): {completed_records}")
        print(f"  - HealthRecords (pending): {pending_records}")
        
        # Prüfe unique_identifiers
        reports_with_id = NewReport.query.filter(NewReport.unique_identifier.isnot(None)).count()
        print(f"  - Reports mit unique_identifier: {reports_with_id}")
        
        # Prüfe MFA-Felder bei Users
        print(f"\n🔐 MFA-Felder:")
        print(f"  - Alle User haben jetzt MFA-Felder (mfa_code_hash, mfa_code_created_at, mfa_code_attempts, mfa_last_request_at)")
        print(f"  - MFA ist standardmäßig inaktiv (alle Felder NULL/0)")

def main():
    """Hauptfunktion für die Migration"""
    print("🚀 Starte Production Database Migration")
    print("=" * 50)
    
    # Prüfe Datenbanken
    if not check_databases():
        return 1
    
    try:
        # Schritt 1: Füge neue Spalten hinzu
        add_new_columns_to_old_db()
        
        # Schritt 2: Aktualisiere Daten
        update_data_in_old_db()
        
        # Schritt 3: Benenne Datenbank um
        rename_database()
        
        # Schritt 4: Verifiziere Migration
        verify_migration()
        
        print("\n✅ Migration erfolgreich abgeschlossen!")
        print(f"\nDie migrierte Datenbank befindet sich unter: {new_db_path}")
        
        if os.path.exists(backup_db_path):
            print(f"Ein Backup der vorherigen Datenbank wurde erstellt: {backup_db_path}")
        
        print("\n" + "=" * 50)
        print("⚠️  WICHTIG: MFA-Konfiguration erforderlich")
        print("=" * 50)
        print("\nDie Anwendung verwendet jetzt Multi-Faktor-Authentifizierung.")
        print("Stellen Sie sicher, dass folgende Konfigurationen im Azure Key Vault gesetzt sind:\n")
        print("  MAIL_SERVER (z.B. smtp.gmail.com)")
        print("  MAIL_PORT (z.B. 587)")
        print("  MAIL_USERNAME (z.B. ihre-email@example.com)")
        print("  MAIL_PASSWORD (z.B. ihr-app-passwort)")
        print("  MAIL_USE_TLS (z.B. True)")
        print("  MAIL_USE_SSL (z.B. False)")
        print("\nDiese Werte werden automatisch aus dem Azure Key Vault geladen.")
        print("Ohne diese Konfiguration können sich Benutzer nicht anmelden!")
        print("\nWeitere Informationen: Siehe MFA_IMPLEMENTATION_README.md")
        print("=" * 50)
        
        return 0
        
    except Exception as e:
        print(f"\n❌ Fehler bei der Migration: {str(e)}")
        print("\nStacktrace:")
        import traceback
        traceback.print_exc()
        
        # Versuche Rollback
        if os.path.exists(new_db_path) and os.path.exists(backup_db_path):
            print("\n🔄 Versuche Rollback...")
            os.remove(new_db_path)
            shutil.move(backup_db_path, new_db_path)
            print("  ✓ Rollback erfolgreich")
        
        return 1

if __name__ == '__main__':
    sys.exit(main())
