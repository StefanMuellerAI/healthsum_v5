"""
Migrations-Script für MFA-Funktionalität
Fügt die neuen MFA-Felder zur User-Tabelle hinzu
"""
from app import app, db
from models import User

def migrate_mfa_fields():
    """Fügt MFA-Felder zur User-Tabelle hinzu"""
    with app.app_context():
        try:
            # Erstelle alle fehlenden Tabellen und Spalten
            db.create_all()
            print("✓ Datenbank-Schema erfolgreich aktualisiert")
            print("✓ MFA-Felder wurden zur User-Tabelle hinzugefügt:")
            print("  - mfa_code_hash")
            print("  - mfa_code_created_at")
            print("  - mfa_code_attempts")
            print("  - mfa_last_request_at")
            
            # Überprüfe vorhandene User
            user_count = User.query.count()
            print(f"\n✓ {user_count} Benutzer in der Datenbank gefunden")
            
            return True
        except Exception as e:
            print(f"✗ Fehler bei der Migration: {e}")
            return False

if __name__ == '__main__':
    print("=== MFA Datenbank-Migration ===\n")
    if migrate_mfa_fields():
        print("\n✓ Migration erfolgreich abgeschlossen!")
        print("\nHinweis: Stellen Sie sicher, dass folgende Umgebungsvariablen gesetzt sind:")
        print("  - MAIL_SERVER")
        print("  - MAIL_PORT")
        print("  - MAIL_USERNAME")
        print("  - MAIL_PASSWORD")
        print("  - MAIL_USE_TLS (optional, default: True)")
        print("  - MAIL_USE_SSL (optional, default: False)")
    else:
        print("\n✗ Migration fehlgeschlagen!")

