#celery_config.py

from celery import Celery
from celery.schedules import crontab

# Broker URL für Redis
BROKER_URL = 'redis://localhost:6380/0'

# Result Backend URL (Redis)
RESULT_BACKEND = 'redis://localhost:6380/0'

# Zeitzone für Celery (optional, aber empfohlen)
TIMEZONE = 'Europe/Berlin'

# Maximale Anzahl von Aufgaben, die ein Worker gleichzeitig ausführen kann
CELERYD_CONCURRENCY = 4

# Maximale Anzahl von Aufgaben, die ein Worker ausführt, bevor er neu gestartet wird
CELERYD_MAX_TASKS_PER_CHILD = 100

# Aufgaben-Tracking-Einstellungen
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 Minuten Zeitlimit für Tasks

# Queue-Einstellungen
CELERY_DEFAULT_QUEUE = 'default'
CELERY_QUEUES = {
    'default': {},
    'pdf_processing': {},
    'extraction': {},
    'refinement': {},
    'summary': {},
    'regenerate_report': {},
    'notification': {},
    'medical_codes': {},
    'icd_descriptions': {}  # Neue Queue für ICD-Beschreibungen
}

# Routing-Einstellungen
CELERY_ROUTES = {
    'tasks.process_pdfs': {'queue': 'pdf_processing'},
    'tasks.extract_pdf_text': {'queue': 'extraction'},
    'tasks.extract_ocr': {'queue': 'extraction'},
    'tasks.extract_azure_vision': {'queue': 'extraction'},
    'tasks.extract_gpt4_vision': {'queue': 'extraction'},
    'tasks.combine_extractions': {'queue': 'extraction'},
    'tasks.process_record': {'queue': 'refinement'},
    'tasks.create_report': {'queue': 'summary'},
    'tasks.regenerate_report_task': {'queue': 'regenerate_report'},
    'tasks.generate_single_report': {'queue': 'regenerate_report'},
    'tasks.extract_medical_codes': {'queue': 'medical_codes'},
    'tasks.save_medical_codes': {'queue': 'medical_codes'},
    'tasks.send_notifications_task': {'queue': 'notification'},
    'tasks.update_medical_codes_descriptions': {'queue': 'icd_descriptions'},  # Neue Route
}

# Beat-Schedule-Einstellungen
CELERYBEAT_SCHEDULE = {
    'send-notifications-every-minute': {
        'task': 'tasks.send_notifications_task',
        'schedule': crontab(minute='*/1'),  # Jede Minute
    },
    'check-and-create-summaries': {
        'task': 'tasks.check_and_create_summaries',
        'schedule': crontab(minute='*/15'),  # Alle 15 Minuten
    },
}


def create_celery_app(app=None):
    celery = Celery(app.import_name if app else __name__)
    celery.conf.update(
        broker_url=BROKER_URL,
        result_backend=RESULT_BACKEND,
        timezone=TIMEZONE,
        task_track_started=CELERY_TASK_TRACK_STARTED,
        task_time_limit=CELERY_TASK_TIME_LIMIT,
        worker_max_tasks_per_child=CELERYD_MAX_TASKS_PER_CHILD,
        task_queues=CELERY_QUEUES,
        task_routes=CELERY_ROUTES,
        beat_schedule=CELERYBEAT_SCHEDULE
    )

    if app:
        celery.conf.update(app.config)

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery