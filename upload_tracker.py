import logging
from pathlib import Path
from datetime import datetime

# Erstellen Sie einen speziellen Logger für den Upload-Tracker
upload_logger = logging.getLogger('upload_tracker')
upload_logger.setLevel(logging.INFO)

# Definieren Sie den Pfad für die Log-Datei
log_dir = Path(__file__).parent / 'logs'
log_dir.mkdir(exist_ok=True)
log_file = log_dir / 'upload_tracker.log'

# Erstellen Sie einen FileHandler und fügen Sie ihn dem Logger hinzu
file_handler = logging.FileHandler(log_file)
file_handler.setLevel(logging.INFO)

# Erstellen Sie ein Formatter-Objekt
formatter = logging.Formatter('%(asctime)s - %(message)s')
file_handler.setFormatter(formatter)

# Fügen Sie den Handler dem Logger hinzu
upload_logger.addHandler(file_handler)

def log_upload(record_id, token_volume, duration):
    duration_str = str(duration).split('.')[0]  # Entfernt Mikrosekunden
    log_message = f"Record ID: {record_id}, Token Volume: {token_volume}, Duration: {duration_str}"
    upload_logger.info(log_message)
