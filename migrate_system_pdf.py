#!/usr/bin/env python3
"""
Migration Script: Fügt system_pdf_filename Feld zur ReportTemplate Tabelle hinzu
"""
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
import os

# Erstelle eine minimale Flask-App für den Kontext
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'fallback-key-for-migration')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///health_records.db'

db = SQLAlchemy()
db.init_app(app)

def migrate_add_system_pdf_field():
    """Fügt das system_pdf_filename Feld zur ReportTemplate Tabelle hinzu"""
    try:
        with app.app_context():
            # Prüfe ob das Feld bereits existiert
            result = db.engine.execute(text("PRAGMA table_info(report_template)"))
            columns = [row[1] for row in result]
            
            if 'system_pdf_filename' in columns:
                print("✅ system_pdf_filename Feld existiert bereits in ReportTemplate")
                return True
            
            # Füge das neue Feld hinzu
            print("🔄 Füge system_pdf_filename Feld zu ReportTemplate hinzu...")
            db.engine.execute(text(
                "ALTER TABLE report_template ADD COLUMN system_pdf_filename VARCHAR(255)"
            ))
            
            print("✅ Migration erfolgreich: system_pdf_filename Feld hinzugefügt")
            return True
            
    except Exception as e:
        print(f"❌ Fehler bei der Migration: {str(e)}")
        return False

if __name__ == '__main__':
    success = migrate_add_system_pdf_field()
    if success:
        print("\n🎉 Migration abgeschlossen!")
    else:
        print("\n💥 Migration fehlgeschlagen!") 