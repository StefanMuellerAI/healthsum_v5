import os
import time
from flask import Flask, render_template
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from dotenv import load_dotenv
from datetime import datetime
from models import db, TaskMonitor, User, HealthRecord

# Umgebungsvariablen laden
load_dotenv()

# Flask-App und Konfiguration
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///health_records.db'

# Mail-Konfiguration aus .env-Datei
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT'))
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS') == 'True'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL') == 'True'

# Initialisierung von Datenbank und Mail
db.init_app(app)
mail = Mail(app)

def send_notifications():
    with app.app_context():
        # Abfrage der TaskMonitors, bei denen end_date gesetzt ist und notification_sent False ist
        pending_notifications = TaskMonitor.query.filter(
            TaskMonitor.end_date.isnot(None),
            TaskMonitor.notification_sent == False
        ).all()

        for task in pending_notifications:
            health_record = task.health_record
            user = health_record.user
            duration = None
            if task.start_date and task.end_date:
                duration_td = task.end_date - task.start_date
                total_seconds = int(duration_td.total_seconds())
                hours, remainder = divmod(total_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

            subject = f"Verarbeitung abgeschlossen für Datensatz ID {health_record.id}"

            # Rendern der E-Mail-Vorlage
            html_body = render_template(
                'e-mails/notification_template.html',
                user=user,
                health_record=health_record,
                duration=duration
            )

            # E-Mail erstellen und senden
            msg = Message(subject=subject, sender=app.config['MAIL_USERNAME'], recipients=[user.email])
            msg.html = html_body
            try:
                mail.send(msg)
                print(f"E-Mail an {user.email} gesendet.")
            except Exception as e:
                print(f"Fehler beim Senden der E-Mail an {user.email}: {e}")
                continue  # Fährt mit dem nächsten Task fort, ohne notification_sent zu setzen

            # Aktualisieren des notification_sent Feldes
            task.notification_sent = True
            db.session.commit()

if __name__ == '__main__':
    while True:
        print("Mail Sender läuft")
        send_notifications()
        # Wartezeit von 60 Sekunden vor dem nächsten Durchlauf
        time.sleep(60)
