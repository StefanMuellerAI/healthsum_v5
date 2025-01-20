import os
from app import app, db
from models import HealthRecord, ReportTemplate
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy_utils.types.encrypted.encrypted_type import AesEngine
from sqlalchemy_utils import EncryptedType
import logging
import shutil
from datetime import datetime

# Konfigurieren des Loggings
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('database_migration.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def create_backup(db_path):
    """Erstellt ein Backup der Datenbank"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = f"{db_path}.backup_{timestamp}"
    try:
        shutil.copy2(db_path, backup_path)
        logger.info(f"Backup erstellt: {backup_path}")
        return True
    except Exception as e:
        logger.error(f"Fehler beim Erstellen des Backups: {str(e)}")
        return False

def migrate_database():
    # Definiere den Pfad zur Datenbank
    db_path = os.path.join('instance', 'health_records.db')
    
    # Erstelle ein Backup
    if not create_backup(db_path):
        logger.error("Migration abgebrochen wegen Backup-Fehler")
        return False

    with app.app_context():
        try:
            logger.info("Starte Datenbank-Migration...")
            
            # Speichere alle existierenden Daten
            logger.info("Sichere bestehende Daten...")
            health_records = HealthRecord.query.all()
            old_data = []
            for record in health_records:
                old_data.append({
                    'id': record.id,
                    'filenames': record.filenames,  # Speichere die unverschlüsselten Dateinamen
                    'custom_instructions': None
                })
            logger.info(f"Gesicherte Datensätze: {len(old_data)}")

            # Füge neue Spalte zu ReportTemplate hinzu
            logger.info("Füge use_custom_instructions zu ReportTemplate hinzu...")
            try:
                with db.engine.connect() as conn:
                    conn.execute('ALTER TABLE report_template ADD COLUMN use_custom_instructions BOOLEAN DEFAULT FALSE')
                logger.info("Spalte use_custom_instructions erfolgreich hinzugefügt")
            except Exception as e:
                logger.info(f"Spalte existiert möglicherweise bereits: {str(e)}")

            # Füge neue Spalte zu HealthRecord hinzu
            logger.info("Füge custom_instructions zu HealthRecord hinzu...")
            try:
                with db.engine.connect() as conn:
                    conn.execute('ALTER TABLE health_record ADD COLUMN custom_instructions TEXT')
                logger.info("Spalte custom_instructions erfolgreich hinzugefügt")
            except Exception as e:
                logger.info(f"Spalte existiert möglicherweise bereits: {str(e)}")

            # Migriere die Daten
            logger.info("Migriere Datensätze...")
            for backup in old_data:
                try:
                    record = HealthRecord.query.get(backup['id'])
                    if record:
                        # Verschlüssele die Dateinamen neu
                        if backup['filenames']:
                            record.filenames = backup['filenames']
                        logger.info(f"Datensatz {record.id} erfolgreich migriert")
                except Exception as e:
                    logger.error(f"Fehler bei der Migration von Datensatz {backup['id']}: {str(e)}")
                    continue

            # Setze Standardwerte für ReportTemplates
            templates = ReportTemplate.query.all()
            for template in templates:
                if template.use_custom_instructions is None:
                    template.use_custom_instructions = False
            
            # Commit aller Änderungen
            db.session.commit()
            logger.info("Datenbank-Migration erfolgreich abgeschlossen")
            return True
            
        except Exception as e:
            logger.error(f"Fehler bei der Migration: {str(e)}")
            db.session.rollback()
            return False

if __name__ == "__main__":
    print("Starte Datenbank-Migration...")
    print("Ein Backup wird automatisch erstellt.")
    user_input = input("Möchten Sie fortfahren? (j/n): ")
    
    if user_input.lower() == 'j':
        success = migrate_database()
        if success:
            print("Migration erfolgreich abgeschlossen. Siehe database_migration.log für Details.")
        else:
            print("Migration fehlgeschlagen. Siehe database_migration.log für Details.")
    else:
        print("Migration abgebrochen.") 