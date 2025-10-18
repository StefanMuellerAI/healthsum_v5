"""
Celery Worker Start-Script mit eventlet monkey patching

WICHTIG: eventlet.monkey_patch() MUSS vor allen anderen Imports stehen!
"""
import eventlet
eventlet.monkey_patch()

# Jetzt erst die App importieren
from app import celery

if __name__ == '__main__':
    celery.start()

