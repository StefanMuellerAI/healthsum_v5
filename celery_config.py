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
# Mit eventlet können wir viel höhere Concurrency nutzen
CELERYD_CONCURRENCY = 500  # Eventlet erlaubt hohe Concurrency für I/O-Tasks

# Maximale Anzahl von Aufgaben, die ein Worker ausführt, bevor er neu gestartet wird
CELERYD_MAX_TASKS_PER_CHILD = 50  # Reduziert für bessere Speicherverwaltung

# Aufgaben-Tracking-Einstellungen
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 Minuten Zeitlimit für Tasks
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # Soft limit vor hard limit

# Worker-Pool-Optimierungen
# Eventlet ist ideal für I/O-intensive Tasks wie API-Calls
CELERY_WORKER_POOL = 'eventlet'
CELERY_WORKER_POOL_RESTARTS = True

# Task-Optimierungen
CELERY_TASK_COMPRESSION = 'gzip'  # Komprimiere große Ergebnisse
CELERY_WORKER_PREFETCH_MULTIPLIER = 2  # Reduziert von Standard 4

# Task-Fehlerbehandlung
CELERY_TASK_ACKS_LATE = True  # Tasks werden erst nach Completion acknowledged
CELERY_TASK_REJECT_ON_WORKER_LOST = True  # Rejected tasks bei Worker-Verlust
CELERY_TASK_IGNORE_RESULT = False  # Behalte alle Ergebnisse für Debugging

# Queue-Einstellungen mit Prioritäten
CELERY_DEFAULT_QUEUE = 'default'
CELERY_QUEUES = {
    'default': {'priority': 5},
    'pdf_processing': {'priority': 10},
    'extraction': {'priority': 8, 'max_tasks_per_child': 30},
    'refinement': {'priority': 6},
    'summary': {'priority': 4},
    'regenerate_report': {'priority': 3},
    'notification': {'priority': 2},
    'medical_codes': {'priority': 7},
    'icd_descriptions': {'priority': 1}
}

# Routing-Einstellungen
CELERY_ROUTES = {
    'tasks.process_pdfs': {'queue': 'pdf_processing'},
    'tasks.convert_pdf_to_images': {'queue': 'extraction'},
    'tasks.distribute_extraction_tasks': {'queue': 'extraction'},
    'tasks.aggregate_extraction_results': {'queue': 'extraction'},
    'tasks.extract_pdf_text': {'queue': 'extraction'},
    'tasks.extract_ocr_optimized': {'queue': 'extraction'},
    'tasks.extract_azure_vision_optimized': {'queue': 'extraction'},
    #'tasks.extract_gemini_vision_optimized': {'queue': 'extraction'},
    'tasks.extract_gpt4_vision_optimized': {'queue': 'extraction'},
    'tasks.combine_extractions': {'queue': 'extraction'},
    'tasks.process_record': {'queue': 'refinement'},
    'tasks.create_report': {'queue': 'summary'},
    'tasks.regenerate_report_task': {'queue': 'regenerate_report'},
    'tasks.generate_single_report': {'queue': 'regenerate_report'},
    'tasks.extract_medical_codes': {'queue': 'medical_codes'},
    'tasks.save_medical_codes': {'queue': 'medical_codes'},
    'tasks.send_notifications_task': {'queue': 'notification'},
    'tasks.update_medical_codes_descriptions': {'queue': 'icd_descriptions'}
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
        task_soft_time_limit=CELERY_TASK_SOFT_TIME_LIMIT,
        worker_max_tasks_per_child=CELERYD_MAX_TASKS_PER_CHILD,
        worker_pool=CELERY_WORKER_POOL,
        worker_pool_restarts=CELERY_WORKER_POOL_RESTARTS,
        task_compression=CELERY_TASK_COMPRESSION,
        worker_prefetch_multiplier=CELERY_WORKER_PREFETCH_MULTIPLIER,
        worker_concurrency=CELERYD_CONCURRENCY,
        task_queues=CELERY_QUEUES,
        task_routes=CELERY_ROUTES,
        beat_schedule=CELERYBEAT_SCHEDULE,
        task_acks_late=CELERY_TASK_ACKS_LATE,
        task_reject_on_worker_lost=CELERY_TASK_REJECT_ON_WORKER_LOST,
        task_ignore_result=CELERY_TASK_IGNORE_RESULT
    )

    if app:
        celery.conf.update(app.config)

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery