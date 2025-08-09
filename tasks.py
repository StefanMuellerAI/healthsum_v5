# tasks.py
import os
import logging
from celery import chain, group, chord, shared_task
from celery.schedules import crontab
from celery.exceptions import Retry, MaxRetriesExceededError, Ignore
from celery_config import create_celery_app
from extractors import PDFTextExtractor, OCRExtractor, AzureVisionExtractor, GPT4VisionExtractor, GeminiVisionExtractor, CodeExtractor, vision_azure_client, openai_client, openai_model, gemini_model
from utils import count_tokens, find_patient_info, update_medical_code_description
from datetime import datetime
import traceback
from models import db, HealthRecord, Report, ReportTemplate, TaskMonitor, MedicalCode, TaskLog
import pytesseract
import io
import base64
from azure.ai.vision.imageanalysis.models import VisualFeatures
from reports import generate_report
from flask import current_app, render_template
from utils import update_task_monitor, create_task_monitor, mark_notification_sent
from flask_mail import Mail, Message
import xml.etree.ElementTree as ET
import time
import tempfile
import pickle
from pdf2image import convert_from_bytes
from functools import wraps
from PIL import Image
import re

# Konfigurieren des Loggings
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
celery = create_celery_app()

# Standard-Fehlerformat für konsistente Fehlerbehandlung
def create_error_response(exc, context=""):
    """Erstellt standardisierte Fehlerantwort"""
    return {
        'status': 'error',
        'exc_type': type(exc).__name__,
        'exc_message': str(exc),
        'context': context,
        'traceback': traceback.format_exc()
    }

def validate_inputs(**validators):
    """Decorator für Input-Validierung"""
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            # Validiere jeden Parameter
            for i, (param_name, validator) in enumerate(validators.items()):
                if i < len(args):
                    value = args[i]
                    if not validator(value):
                        error_msg = f"Invalid {param_name}: {value}"
                        logger.error(error_msg)
                        return create_error_response(ValueError(error_msg), f"Input validation in {func.__name__}")
            return func(self, *args, **kwargs)
        return wrapper
    return decorator

def safe_db_operation(func):
    """Decorator für sichere DB-Operationen mit automatischem Rollback"""
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            result = func(self, *args, **kwargs)
            return result
        except Exception as exc:
            try:
                db.session.rollback()
                logger.error(f"DB rollback executed in {func.__name__}")
            except Exception as rollback_exc:
                logger.error(f"Failed to rollback DB session: {rollback_exc}")
            raise exc
        finally:
            try:
                db.session.close()
            except Exception as close_exc:
                logger.warning(f"Failed to close DB session: {close_exc}")
    return wrapper

def optimize_image_format(image, target_format='webp', quality=85, max_size_kb=None):
    """
    Optimiert Bildformat für bessere Performance und kleinere Dateigröße
    
    :param image: PIL Image object
    :param target_format: 'webp', 'jpeg', oder 'png'
    :param quality: Qualität 1-100 (nur für JPEG/WebP)
    :param max_size_kb: Maximale Dateigröße in KB
    :return: BytesIO stream with optimized image
    """
    try:
        img_bytes = io.BytesIO()
        
        # WebP Support prüfen und Fallback implementieren
        if target_format.lower() == 'webp':
            try:
                # WebP mit optimalen Einstellungen
                image.save(img_bytes, format='WebP', quality=quality, method=6, lossless=False)
                logger.debug(f"Successfully saved image as WebP with quality {quality}")
            except Exception as webp_exc:
                logger.warning(f"WebP conversion failed, falling back to JPEG: {webp_exc}")
                img_bytes = io.BytesIO()
                image.save(img_bytes, format='JPEG', quality=quality, optimize=True)
                target_format = 'jpeg'
        
        elif target_format.lower() == 'jpeg':
            image.save(img_bytes, format='JPEG', quality=quality, optimize=True)
        
        elif target_format.lower() == 'png':
            image.save(img_bytes, format='PNG', optimize=True)
        
        else:
            raise ValueError(f"Unsupported format: {target_format}")
        
        # Größenoptimierung falls nötig
        if max_size_kb:
            current_size_kb = len(img_bytes.getvalue()) / 1024
            if current_size_kb > max_size_kb:
                logger.info(f"Image too large ({current_size_kb:.1f}KB), resizing...")
                
                # Reduziere Qualität schrittweise
                for reduced_quality in [75, 60, 45, 30]:
                    img_bytes = io.BytesIO()
                    if target_format.lower() == 'webp':
                        image.save(img_bytes, format='WebP', quality=reduced_quality, method=6)
                    else:
                        image.save(img_bytes, format='JPEG', quality=reduced_quality, optimize=True)
                    
                    if len(img_bytes.getvalue()) / 1024 <= max_size_kb:
                        logger.info(f"Optimized to {reduced_quality}% quality, size: {len(img_bytes.getvalue())/1024:.1f}KB")
                        break
                else:
                    # Wenn Qualitätsreduktion nicht ausreicht, Größe reduzieren
                    scale_factor = (max_size_kb * 1024 / len(img_bytes.getvalue())) ** 0.5
                    new_size = (int(image.width * scale_factor), int(image.height * scale_factor))
                    resized_image = image.resize(new_size, Image.Resampling.LANCZOS)
                    
                    img_bytes = io.BytesIO()
                    if target_format.lower() == 'webp':
                        resized_image.save(img_bytes, format='WebP', quality=quality, method=6)
                    else:
                        resized_image.save(img_bytes, format='JPEG', quality=quality, optimize=True)
                    
                    logger.info(f"Resized image to {new_size}, final size: {len(img_bytes.getvalue())/1024:.1f}KB")
        
        img_bytes.seek(0)
        return img_bytes
        
    except Exception as exc:
        logger.error(f"Image optimization failed: {exc}")
        # Fallback zu unoptimiertem JPEG
        fallback_bytes = io.BytesIO()
        image.save(fallback_bytes, format='JPEG', quality=85)
        fallback_bytes.seek(0)
        return fallback_bytes

def image_to_base64(image, format='webp', quality=85, max_size_kb=19000):
    """
    Konvertiert PIL Image zu base64 String mit optimaler Kompression
    """
    try:
        optimized_stream = optimize_image_format(image, format, quality, max_size_kb)
        base64_image = base64.b64encode(optimized_stream.getvalue()).decode('utf-8')
        
        final_size_kb = len(base64_image) * 3 / 4 / 1024  # base64 overhead correction
        logger.debug(f"Image converted to base64, final size: {final_size_kb:.1f}KB")
        
        return base64_image
    except Exception as exc:
        logger.error(f"Base64 conversion failed: {exc}")
        # Emergency fallback
        fallback_bytes = io.BytesIO()
        image.save(fallback_bytes, format='JPEG', quality=70)
        return base64.b64encode(fallback_bytes.getvalue()).decode('utf-8')

# Task-Logging-Funktionen
import json

def set_record_processing_status(record_id, status, error_message=None):
    """Setzt den finalen Verarbeitungsstatus eines HealthRecords"""
    try:
        from app import app
        with app.app_context():
            record = HealthRecord.query.get(record_id)
            if record:
                record.processing_status = status
                record.processing_completed_at = datetime.utcnow()
                if error_message:
                    record.processing_error_message = str(error_message)[:1000]  # Begrenzen auf 1000 Zeichen
                
                db.session.commit()
                logger.info(f"Record {record_id} processing status set to: {status}")
                return True
            else:
                logger.error(f"Record {record_id} not found when setting status")
                return False
    except Exception as e:
        logger.error(f"Failed to set processing status for record {record_id}: {e}")
        return False

def log_task_start(health_record_id, task_name, task_id, metadata=None):
    """Loggt den Start eines Tasks"""
    try:
        # Erstelle App-Context für Celery Tasks
        from app import app
        with app.app_context():
            task_log = TaskLog(
                health_record_id=health_record_id,
                task_name=task_name,
                task_id=task_id,
                status='started',
                task_metadata=json.dumps(metadata) if metadata else None
            )
            db.session.add(task_log)
            db.session.commit()
            logger.info(f"Task started: {task_name} for health_record {health_record_id}")
            return task_log.id
    except Exception as e:
        logger.error(f"Failed to log task start: {e}")
        logger.exception("Full traceback:")
        return None

def log_task_success(health_record_id, task_name, task_id, start_time=None, metadata=None):
    """Loggt den erfolgreichen Abschluss eines Tasks"""
    try:
        from app import app
        with app.app_context():
            # Finde den bestehenden TaskLog-Eintrag
            task_log = TaskLog.query.filter_by(
                health_record_id=health_record_id,
                task_name=task_name,
                task_id=task_id
            ).order_by(TaskLog.started_at.desc()).first()
            
            if not task_log:
                # Falls kein Start-Log gefunden wurde, erstelle einen neuen Erfolgs-Log
                task_log = TaskLog(
                    health_record_id=health_record_id,
                    task_name=task_name,
                    task_id=task_id,
                    status='success'
                )
                db.session.add(task_log)
            else:
                task_log.status = 'success'
            
            task_log.completed_at = datetime.utcnow()
            
            # Berechne Dauer falls Start-Zeit verfügbar
            if start_time and task_log.started_at:
                duration = (task_log.completed_at - task_log.started_at).total_seconds()
                task_log.duration_seconds = duration
            elif start_time:
                duration = (task_log.completed_at - start_time).total_seconds()
                task_log.duration_seconds = duration
            
            if metadata:
                existing_metadata = json.loads(task_log.task_metadata) if task_log.task_metadata else {}
                existing_metadata.update(metadata)
                task_log.task_metadata = json.dumps(existing_metadata)
            
            db.session.commit()
            logger.info(f"Task completed successfully: {task_name} for health_record {health_record_id}")
            return task_log.id
    except Exception as e:
        logger.error(f"Failed to log task success: {e}")
        return None

def log_task_failure(health_record_id, task_name, task_id, error, start_time=None, metadata=None):
    """Loggt das Fehlschlagen eines Tasks"""
    try:
        from app import app
        with app.app_context():
            # Finde den bestehenden TaskLog-Eintrag
            task_log = TaskLog.query.filter_by(
                health_record_id=health_record_id,
                task_name=task_name,
                task_id=task_id
            ).order_by(TaskLog.started_at.desc()).first()
            
            if not task_log:
                # Falls kein Start-Log gefunden wurde, erstelle einen neuen Fehler-Log
                task_log = TaskLog(
                    health_record_id=health_record_id,
                    task_name=task_name,
                    task_id=task_id,
                    status='failed'
                )
                db.session.add(task_log)
            else:
                task_log.status = 'failed'
            
            task_log.completed_at = datetime.utcnow()
            task_log.error_message = str(error)
            task_log.error_type = type(error).__name__
            
            # Berechne Dauer falls Start-Zeit verfügbar
            if start_time and task_log.started_at:
                duration = (task_log.completed_at - task_log.started_at).total_seconds()
                task_log.duration_seconds = duration
            elif start_time:
                duration = (task_log.completed_at - start_time).total_seconds()
                task_log.duration_seconds = duration
            
            if metadata:
                existing_metadata = json.loads(task_log.task_metadata) if task_log.task_metadata else {}
                existing_metadata.update(metadata)
                task_log.task_metadata = json.dumps(existing_metadata)
            
            db.session.commit()
            logger.error(f"Task failed: {task_name} for health_record {health_record_id} - {error}")
            return task_log.id
    except Exception as e:
        logger.error(f"Failed to log task failure: {e}")
        return None

def get_health_record_id_from_task_result(result):
    """Extrahiert health_record_id aus Task-Ergebnissen"""
    if isinstance(result, dict):
        return result.get('record_id') or result.get('health_record_id')
    return None

@celery.task
def log_task_chain_error(request, exc, traceback, task_name, record_id):
    """Error callback für Task-Chain-Fehler"""
    logger.error(f"Task chain error in {task_name} for record {record_id}")
    logger.error(f"Task ID: {request.id}")
    logger.error(f"Exception: {exc}")
    logger.error(f"Traceback: {traceback}")
    
    # Log in TaskLog Tabelle
    if record_id:
        from app import app
        with app.app_context():
            try:
                log_task_failure(record_id, f"{task_name}_chain_error", request.id, exc)
            except Exception as log_exc:
                logger.error(f"Failed to log chain error to database: {log_exc}")
    
    return {'status': 'error', 'task': task_name, 'exception': str(exc)}

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
@validate_inputs(file_path=lambda x: isinstance(x, str) and x.strip() and os.path.exists(x))
def convert_pdf_to_images(self, file_path):
    """Konvertiert PDF einmal zu Bildern und speichert sie temporär"""
    logger.info(f"Converting PDF to images: {file_path}")
    temp_file = None
    
    # Versuche record_id aus dem file_path zu extrahieren (optional)
    health_record_id = getattr(self, '_health_record_id', None)
    if health_record_id:
        log_task_start(health_record_id, 'convert_pdf_to_images', self.request.id, {'file_path': file_path})
    
    start_time = datetime.utcnow()
    
    try:
        # Validiere PDF-Datei
        logger.info(f"PDF2IMG: Validating PDF file {file_path}")
        if not file_path.lower().endswith('.pdf'):
            raise ValueError(f"File is not a PDF: {file_path}")
        
        logger.info(f"PDF2IMG: Reading PDF bytes from {file_path}")
        with open(file_path, 'rb') as file:
            pdf_bytes = file.read()
        
        logger.info(f"PDF2IMG: Read {len(pdf_bytes)} bytes from PDF")
        
        if len(pdf_bytes) == 0:
            raise ValueError(f"PDF file is empty: {file_path}")
        
        logger.info(f"PDF2IMG: Converting PDF bytes to images using pdf2image")
        images = convert_from_bytes(pdf_bytes)
        
        logger.info(f"PDF2IMG: Converted to {len(images) if images else 0} images")
        
        if not images:
            raise ValueError(f"No images could be extracted from PDF: {file_path}")
        
        # Speichere Bilder temporär mit pickle
        logger.info(f"PDF2IMG: Creating temporary file for {len(images)} images")
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pkl')
        
        logger.info(f"PDF2IMG: Saving images to pickle file {temp_file.name}")
        with open(temp_file.name, 'wb') as f:
            pickle.dump(images, f)
        
        logger.info(f"PDF2IMG: Successfully saved {len(images)} images to {temp_file.name}")
        
        result = {
            'images_path': temp_file.name,
            'pdf_path': file_path,
            'page_count': len(images)
            # pdf_bytes entfernt - verhindert große Redis-Transfers!
        }
        
        logger.info(f"PDF2IMG: Returning result: {result}")
        
        # Log Success
        if health_record_id:
            log_task_success(health_record_id, 'convert_pdf_to_images', self.request.id, start_time, 
                           {'page_count': len(images)})
        
        return result
        
    except Exception as exc:
        # Cleanup bei Fehler
        if temp_file and os.path.exists(temp_file.name):
            try:
                os.remove(temp_file.name)
                logger.info(f"Cleaned up temp file after error: {temp_file.name}")
            except Exception as cleanup_exc:
                logger.warning(f"Failed to cleanup temp file: {cleanup_exc}")
        
        # Log Failure
        if health_record_id:
            log_task_failure(health_record_id, 'convert_pdf_to_images', self.request.id, exc, start_time)
        
        logger.exception(f"Error converting PDF to images: {file_path}")
        return create_error_response(exc, f"convert_pdf_to_images for {file_path}")

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 30})
@validate_inputs(
    filenames=lambda x: isinstance(x, list) and len(x) > 0 and all(isinstance(f, str) and f.strip() for f in x),
    patient_name=lambda x: isinstance(x, str) and x.strip()
)
@safe_db_operation
def process_pdfs(self, filenames, patient_name, record_id=None, create_reports=False, user_id=None, custom_instructions=None, birth_date=None):
    """
    Verarbeitet PDF-Dateien und extrahiert den Text.
    """
    start_time = datetime.utcnow()
    task_name = 'process_pdfs'
    task_id = self.request.id
    
    try:
        logger.info(f"Starting process_pdfs task for files: {filenames}")
        
        # Validiere zusätzliche Parameter
        if record_id is not None and not isinstance(record_id, int):
            raise ValueError(f"record_id must be integer or None, got: {type(record_id)}")
        
        if user_id is not None and not isinstance(user_id, int):
            raise ValueError(f"user_id must be integer or None, got: {type(user_id)}")
        
        # Validiere, dass alle Dateien existieren
        for filename in filenames:
            file_path = os.path.join('uploads', filename)
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"File not found: {file_path}")
        
        # Status-Update entfernt (WebSockets wurden entfernt)
        
        # Hole den bestehenden Datensatz (wird jetzt immer in app.py erstellt)
        if not record_id:
            raise Exception("record_id is required - should be created in app.py")
        
        record = HealthRecord.query.get(record_id)
        if not record:
            raise Exception(f"Record with ID {record_id} not found")

        # Setze Status auf "processing" beim Start
        set_record_processing_status(record_id, 'processing')
        
        # Log Task Start
        log_task_start(record_id, task_name, task_id, {
            'filenames': filenames,
            'create_reports': create_reports,
            'user_id': user_id
        })

        original_task_id = self.request.id

        extraction_tasks = []

        for i, filename in enumerate(filenames):
            # Status-Update entfernt (WebSockets wurden entfernt)
            file_path = os.path.join('uploads', filename)
            
            # Log einzelne Tasks für bessere Sichtbarkeit
            log_task_start(record_id, 'convert_pdf_to_images', f"convert_{i}_{filename}", 
                         {'file_path': file_path, 'filename': filename})
            log_task_start(record_id, 'extract_pdf_text', f"extract_text_{i}_{filename}", 
                         {'file_path': file_path, 'filename': filename})
            log_task_start(record_id, 'extract_ocr_optimized', f"extract_ocr_{i}_{filename}", 
                         {'file_path': file_path, 'filename': filename})
            log_task_start(record_id, 'extract_azure_vision_optimized', f"extract_azure_{i}_{filename}", 
                         {'file_path': file_path, 'filename': filename})
            # log_task_start(record_id, 'extract_gpt4_vision_optimized', f"extract_gpt4_{i}_{filename}",   # DEAKTIVIERT
            #              {'file_path': file_path, 'filename': filename})
            log_task_start(record_id, 'extract_gemini_vision_optimized', f"extract_gemini_{i}_{filename}", 
                         {'file_path': file_path, 'filename': filename})
            
            # Erstelle Chain: Erst Bilder konvertieren, dann alle Extraktoren parallel
            pdf_chain = (
                convert_pdf_to_images.s(file_path) | 
                distribute_extraction_tasks.s(file_path, record_id)
            )
            extraction_tasks.append(pdf_chain)

            progress = (i + 1) / len(filenames) * 50
           
            logger.info(f"Prepared extraction tasks for file: {filename}")

        # Erzeuge eine Group über alle Datei-Chains (per-file Chord läuft innerhalb der Chain)
        logger.info(f"Creating extraction group from {len(extraction_tasks)} per-file chains")
        extraction_group = group(extraction_tasks)
        logger.info(f"Extraction group created: {extraction_group}")
        
        # Log die Task IDs für besseres Monitoring
        logger.info(f"Extraction tasks details: {[str(task) for task in extraction_tasks]}")

        # Baue die nachgelagerte Chain auf
        logger.info("Building workflow chain from extraction group → combine_extractions → process_record → (optional) create_report...")
        
        # Füge Monitoring-Callbacks hinzu
        combine_sig = combine_extractions.s(
            filenames, patient_name, record_id, create_reports, start_time, original_task_id, user_id
        ).on_error(log_task_chain_error.s(task_name='combine_extractions', record_id=record_id))
        
        process_sig = process_record.s(
            original_task_id=original_task_id
        ).on_error(log_task_chain_error.s(task_name='process_record', record_id=record_id))
        
        workflow_chain = extraction_group | combine_sig | process_sig
        
        logger.info(f"Base workflow chain built: {workflow_chain}")

        if create_reports:
            create_sig = create_report.s(
                original_task_id=original_task_id
            ).on_error(log_task_chain_error.s(task_name='create_report', record_id=record_id))
            workflow_chain |= create_sig
        
        logger.info("Starting extraction workflow")
        logger.info(f"Workflow chain structure: {workflow_chain}")
        
        try:
            result = workflow_chain.apply_async()
            logger.info(f"Extraction workflow started successfully with task id: {result.id}")
        except Exception as workflow_exc:
            logger.error(f"Failed to start extraction workflow: {workflow_exc}")
            logger.exception("Workflow start exception:")
            raise workflow_exc
        
        # Log Task Success
        log_task_success(record_id, task_name, task_id, start_time, {
            'subtask_id': result.id,
            'workflow_started': True
        })
        
        return {'status': 'Extraktionsprozess gestartet', 'subtask_id': result.id, 'start_time': start_time}

    except Exception as exc:
        # Setze Status auf "failed" bei Fehler
        if 'record_id' in locals() and record_id:
            set_record_processing_status(record_id, 'failed', str(exc))
            
            log_task_failure(record_id, task_name, task_id, exc, start_time, {
                'filenames': filenames,
                'create_reports': create_reports
            })
        
        # Status-Update entfernt (WebSockets wurden entfernt)
        logger.exception("Error in process_pdfs task")
        return {
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }
    finally:
        # Status-Update entfernt (WebSockets wurden entfernt)
        pass


@celery.task(bind=True)
@validate_inputs(
    images_info=lambda x: isinstance(x, dict) and 'images_path' in x and 'pdf_path' in x,
    file_path=lambda x: isinstance(x, str) and x.strip()
)
def distribute_extraction_tasks(self, images_info, file_path, record_id=None):
    """Verteilt die Extraktions-Tasks mit den vorkonvertierten Bildern"""
    logger.info(f"Distributing extraction tasks for {file_path}")
    temp_file_path = images_info.get('images_path')
    
    # Log Task Start
    if record_id:
        log_task_start(record_id, 'distribute_extraction_tasks', self.request.id, 
                      {'file_path': file_path, 'images_path': temp_file_path})
    
    start_time = datetime.utcnow()
    
    try:
        # Validiere temporäre Datei
        if not os.path.exists(temp_file_path):
            raise FileNotFoundError(f"Temporary image file not found: {temp_file_path}")
        
        # Erstelle Tasks mit den Bildern und record_id
        header = [
            extract_pdf_text.s(file_path).set(soft_time_limit=300, time_limit=600),
            extract_ocr_optimized.s(images_info).set(soft_time_limit=300, time_limit=600),
            extract_azure_vision_optimized.s(images_info).set(soft_time_limit=300, time_limit=600),
            extract_gpt4_vision_optimized.s(images_info).set(soft_time_limit=300, time_limit=600)
            # extract_gemini_vision_optimized.s(images_info)  # optional
        ]

        logger.info(f"Scheduling chord with {len(header)} extraction tasks for {file_path}")
        # Setze Timeout für den Chord-Callback
        callback_sig = aggregate_extraction_results.s(
            file_path=file_path, 
            record_id=record_id, 
            temp_file_path=temp_file_path
        ).set(soft_time_limit=60, time_limit=120)
        
        # Ersetze diesen Task durch den Chord-Signature (nicht ausführen!), Callback aggregiert die Ergebnisse
        chord_sig = chord(header, callback_sig)
        
        # self.replace() wirft eine Ignore Exception - das ist normal und gewollt!
        # Diese Exception darf NICHT gefangen werden
        return self.replace(chord_sig)
        
    except Ignore:
        # Ignore Exception ist erwartetes Verhalten bei self.replace() - einfach weiterwerfen
        raise
    except Exception as exc:
        # Nur echte Fehler behandeln
        if record_id:
            log_task_failure(record_id, 'distribute_extraction_tasks', self.request.id, exc, start_time)
        
        logger.exception(f"Error in distribute_extraction_tasks for {file_path}")
        
        # Bei echten Fehlern: Retry mit Backoff
        max_retries = 2
        current_retries = self.request.retries if hasattr(self.request, 'retries') else 0
        
        if current_retries < max_retries:
            logger.info(f"Retrying distribute_extraction_tasks (attempt {current_retries + 1}/{max_retries})")
            raise self.retry(exc=exc, countdown=30 * (current_retries + 1))
        
        # Nach max retries: Error Response zurückgeben
        return create_error_response(exc, f"distribute_extraction_tasks for {file_path}")
    finally:
        # Cleanup findet im Callback statt
        pass

@celery.task(bind=True)
def aggregate_extraction_results(self, extraction_results, file_path, record_id=None, temp_file_path=None):
    """Callback für den Chord: wertet die Ergebnisse aus, loggt und bereinigt Ressourcen"""
    start_time = datetime.utcnow()
    try:
        logger.info(f"Aggregating extraction results for {file_path}")
        logger.info(f"Received {len(extraction_results) if extraction_results else 0} extraction results")
        
        # Validiere dass wir Ergebnisse haben
        if not extraction_results:
            logger.error(f"No extraction results received for {file_path}")
            extraction_results = []  # Leere Liste als Fallback
        
        if record_id:
            filename = os.path.basename(file_path)
            file_index = '0'
            task_names = ['extract_pdf_text', 'extract_ocr_optimized', 'extract_azure_vision_optimized', 'extract_gpt4_vision_optimized']
            
            # Stelle sicher, dass wir die richtige Anzahl von Ergebnissen haben
            while len(extraction_results) < len(task_names):
                extraction_results.append(create_error_response(Exception("Task result missing"), "missing_task"))
                
            for task_name, result in zip(task_names, extraction_results):
                is_success = result and (not isinstance(result, dict) or result.get('status') != 'error')
                if is_success:
                    log_task_success(record_id, task_name, f"{task_name}_{file_index}_{filename}", start_time)
                else:
                    error_msg = result.get('exc_message', 'Unknown error') if isinstance(result, dict) else 'No result'
                    log_task_failure(record_id, task_name, f"{task_name}_{file_index}_{filename}", 
                                   Exception(error_msg), start_time)

            # Log convert/distribute success als abgeschlossen
            log_task_success(record_id, 'convert_pdf_to_images', f"convert_{file_index}_{filename}", start_time)
            log_task_success(record_id, 'distribute_extraction_tasks', self.request.id, start_time, 
                           {'extraction_tasks_count': len(extraction_results)})

        return extraction_results
    except Exception as exc:
        logger.exception(f"Error in aggregate_extraction_results for {file_path}")
        # Gib trotzdem die Ergebnisse zurück, damit die Pipeline weiterlaufen kann
        return extraction_results if 'extraction_results' in locals() else []
    finally:
        # Cleanup der temporären Bilddatei mit Verzögerung
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                # Plane das Löschen der temporären Datei nach 5 Minuten
                # Dies gibt allen Tasks genug Zeit, die Datei zu lesen
                cleanup_task = cleanup_temp_file.apply_async(
                    args=[temp_file_path], 
                    countdown=300  # 5 Minuten Verzögerung
                )
                logger.info(f"Scheduled cleanup of temp file {temp_file_path} in 5 minutes (task: {cleanup_task.id})")
            except Exception as e:
                logger.warning(f"Could not schedule temp file cleanup: {e}")

@celery.task(bind=True)
def cleanup_temp_file(self, file_path):
    """Bereinigt temporäre Dateien nach einer Verzögerung"""
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info(f"Successfully deleted temporary file: {file_path}")
            return f"Deleted: {file_path}"
        except Exception as e:
            logger.warning(f"Could not delete temp file {file_path}: {e}")
            return f"Failed to delete: {file_path} - {str(e)}"
    else:
        logger.info(f"Temp file already deleted or doesn't exist: {file_path}")
        return f"File not found: {file_path}"

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 30})
@validate_inputs(file_path=lambda x: isinstance(x, str) and x.strip() and os.path.exists(x))
def extract_pdf_text(self, file_path):
    logger.info(f"Starting PDF text extraction for file: {file_path}")
    
    # Die record_id wird hier über ein anderes System gehandelt
    start_time = datetime.utcnow()
    
    try:
        logger.info(f"Starting PDFTextExtractor for {file_path}")
        extractor = PDFTextExtractor()
        result = extractor.extract(file_path)
        
        logger.info(f"PDFTextExtractor raw result type: {type(result)}")
        logger.info(f"PDFTextExtractor result length: {len(result) if result else 0}")
        
        if not result or not isinstance(result, str):
            logger.error(f"Invalid extraction result from PDFTextExtractor for {file_path}: Type={type(result)}, Value={result}")
            raise ValueError(f"Invalid extraction result from PDFTextExtractor for {file_path}")
        
        logger.info(f"PDF text extraction completed successfully for file: {file_path} - {len(result)} characters")
        
        return result
    except Exception as exc:
        logger.exception(f"Error in PDF text extraction for file: {file_path}")
        logger.error(f"PDFTextExtractor exception details: {str(exc)}")
        
        # Bei max retries, gib IMMER ein Error-Response zurück statt Exception zu werfen
        max_retries = self.max_retries if hasattr(self, 'max_retries') else 3
        current_retries = self.request.retries if hasattr(self.request, 'retries') else 0
        
        if current_retries >= max_retries:
            error_response = create_error_response(exc, f"extract_pdf_text for {file_path}")
            logger.error(f"Max retries reached. Returning error response: {error_response}")
            return error_response
        else:
            # Re-raise für Retry
            raise


@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 45})
@validate_inputs(images_info=lambda x: isinstance(x, dict) and 'images_path' in x and 'pdf_path' in x)
def extract_ocr_optimized(self, images_info):
    """OCR mit vorkonvertierten Bildern"""
    logger.info(f"Starting optimized OCR extraction")
    
    start_time = datetime.utcnow()
    
    try:
        images_path = images_info['images_path']
        logger.info(f"OCR: Starting with images from {images_path}")
        
        if not os.path.exists(images_path):
            # Check if this is a retry after the file was already cleaned up
            if hasattr(self.request, 'retries') and self.request.retries > 0:
                logger.warning(f"Images file not found on retry {self.request.retries}, likely already cleaned up: {images_path}")
                return create_error_response(
                    FileNotFoundError(f"Images file not found (retry {self.request.retries}): {images_path}"),
                    "extract_ocr_optimized"
                )
            else:
                raise FileNotFoundError(f"Images file not found: {images_path}")
        
        # Lade Bilder aus temporärer Datei
        logger.info(f"OCR: Loading images from pickle file {images_path}")
        with open(images_path, 'rb') as f:
            images = pickle.load(f)
        
        logger.info(f"OCR: Loaded {len(images) if images else 0} images from pickle file")
        
        if not images:
            raise ValueError("No images found in pickle file")
        
        extractor = OCRExtractor()
        logger.info(f"OCR: Processing {len(images)} images with pytesseract")
        
        # Nutze die create_structured_output Methode direkt mit den extrahierten Texten
        page_texts = []
        for i, image in enumerate(images):
            try:
                logger.info(f"OCR: Processing image {i+1}/{len(images)}")
                page_text = pytesseract.image_to_string(image, lang='deu')
                logger.info(f"OCR: Image {i} extracted {len(page_text)} characters")
                page_texts.append(page_text)
            except Exception as img_exc:
                logger.warning(f"OCR failed for image {i}: {img_exc}")
                page_texts.append("")  # Leere Seite bei Fehler
        
        logger.info(f"OCR: Creating structured output from {len(page_texts)} page texts")
        result = extractor.create_structured_output("ocr", os.path.basename(images_info['pdf_path']), page_texts)
        
        logger.info(f"OCR: Result type: {type(result)}, Length: {len(result) if result else 0}")
        
        if not result:
            raise ValueError("OCR extraction returned empty result")
        
        logger.info(f"Optimized OCR extraction completed successfully - {len(result)} characters")
        
        return result
    except Exception as exc:
        logger.exception(f"Error in optimized OCR extraction")
        logger.error(f"OCR exception details: {str(exc)}")
        
        # Bei max retries, gib IMMER ein Error-Response zurück statt Exception zu werfen
        max_retries = self.max_retries if hasattr(self, 'max_retries') else 3
        current_retries = self.request.retries if hasattr(self.request, 'retries') else 0
        
        if current_retries >= max_retries:
            error_response = create_error_response(exc, "extract_ocr_optimized")
            logger.error(f"OCR max retries reached. Returning error response: {error_response}")
            return error_response
        else:
            # Re-raise für Retry
            raise

@celery.task(bind=True)
def extract_ocr(self, file_path):
    """Legacy OCR extractor - deprecated, use extract_ocr_optimized instead"""
    logger.warning(f"Using deprecated extract_ocr for {file_path} - consider using extract_ocr_optimized")
    try:
        extractor = OCRExtractor()
        result = extractor.extract(file_path)
        logger.info(f"OCR extraction completed for file: {file_path}")
        return result
    except Exception as exc:
        logger.exception(f"Error in OCR extraction for file: {file_path}")
        return create_error_response(exc, f"extract_ocr for {file_path}")

@celery.task(bind=True)
@validate_inputs(images_info=lambda x: isinstance(x, dict) and 'images_path' in x and 'pdf_path' in x)
def extract_azure_vision_optimized(self, images_info):
    """Azure Vision mit vorkonvertierten Bildern"""
    logger.info(f"Starting optimized Azure Vision extraction")
    try:
        images_path = images_info['images_path']
        logger.info(f"AZURE: Starting with images from {images_path}")
        
        if not os.path.exists(images_path):
            # Check if this is a retry after the file was already cleaned up
            if self.request.retries > 0:
                logger.warning(f"Images file not found on retry {self.request.retries}, likely already cleaned up: {images_path}")
                return create_error_response(
                    FileNotFoundError(f"Images file not found (retry {self.request.retries}): {images_path}"),
                    "extract_azure_vision_optimized"
                )
            else:
                raise FileNotFoundError(f"Images file not found: {images_path}")
        
        # Lade Bilder aus temporärer Datei
        logger.info(f"AZURE: Loading images from pickle file")
        with open(images_path, 'rb') as f:
            images = pickle.load(f)
        
        logger.info(f"AZURE: Loaded {len(images) if images else 0} images from pickle file")
        
        if not images:
            raise ValueError("No images found in pickle file")
        
        extractor = AzureVisionExtractor()
        page_texts = []
        
        logger.info(f"AZURE: Starting to process {len(images)} images")
        
        for i, image in enumerate(images):
            try:
                logger.info(f"AZURE: Processing image {i+1}/{len(images)}")
                
                # Konvertiere PIL Image zu optimiertem Stream (WebP oder PNG als Fallback)
                try:
                    # Versuche WebP zuerst (kleinere Dateigröße)
                    image_stream = optimize_image_format(image, 'webp', quality=90)
                except Exception:
                    # Fallback zu PNG für Azure Vision Kompatibilität
                    image_stream = optimize_image_format(image, 'png')
                
                # API-Call mit Timeout-Handling über Threading
                import threading
                result_container = [None]
                exception_container = [None]
                
                def azure_api_call():
                    try:
                        result_container[0] = vision_azure_client.analyze(
                            image_data=image_stream,
                            visual_features=[VisualFeatures.READ]
                        )
                    except Exception as e:
                        exception_container[0] = e
                
                # Starte API-Call in separatem Thread mit Timeout
                thread = threading.Thread(target=azure_api_call)
                thread.daemon = True
                thread.start()
                thread.join(timeout=30)  # 30 Sekunden Timeout
                
                if thread.is_alive():
                    logger.error(f"Azure Vision API timeout for image {i} after 30 seconds")
                    page_texts.append("")
                    continue
                
                if exception_container[0]:
                    raise exception_container[0]
                
                result = result_container[0]
                
                if result and result.read is not None:
                    page_text = ' '.join(
                        [' '.join([word.text for word in line.words]) for block in result.read.blocks for line in block.lines]
                    )
                    page_texts.append(page_text)
                    logger.info(f"AZURE: Successfully extracted {len(page_text)} characters from image {i}")
                else:
                    logger.warning(f"No text extracted from image {i} via Azure Vision")
                    page_texts.append("")
                    
            except Exception as img_exc:
                logger.error(f"Azure Vision failed for image {i}: {img_exc}")
                page_texts.append("")  # Leere Seite bei Fehler
        
        result = extractor.create_structured_output("azure_vision", os.path.basename(images_info['pdf_path']), page_texts)
        
        # Qualitätsprüfung: wenn zu viele Seiten leer sind, Exception werfen → Celery Autoretry greift
        total_pages = len(images)
        non_empty_pages = sum(1 for t in page_texts if isinstance(t, str) and t.strip())
        if total_pages > 0:
            quality_ratio = non_empty_pages / total_pages
            logger.info(f"AZURE: quality pages {non_empty_pages}/{total_pages} ({quality_ratio:.2f})")
            if non_empty_pages == 0 or quality_ratio < 0.3:
                raise Exception(f"Azure Vision low quality: {non_empty_pages}/{total_pages} non-empty pages")

        if not result:
            raise ValueError("Azure Vision extraction returned empty result")
        
        logger.info(f"Optimized Azure Vision extraction completed")
        return result
    except Exception as exc:
        logger.exception(f"Error in optimized Azure Vision extraction")
        # Manuelles Retry mit Backoff; bei letzter Wiederholung Fehlerobjekt zurückgeben, damit Chord weiterläuft
        max_retries = 4
        try:
            if getattr(self.request, 'retries', 0) < max_retries:
                raise self.retry(exc=exc, countdown=60)
        except Retry:
            # Retry wurde geplant
            return
        # Max Retries erreicht → standardisiertes Fehlerobjekt zurückgeben
        return create_error_response(exc, "extract_azure_vision_optimized")

@celery.task(bind=True)
def extract_azure_vision(self, file_path):
    """Legacy Azure Vision extractor - deprecated, use extract_azure_vision_optimized instead"""
    logger.warning(f"Using deprecated extract_azure_vision for {file_path} - consider using extract_azure_vision_optimized")
    try:
        extractor = AzureVisionExtractor()
        result = extractor.extract(file_path)
        logger.info(f"Azure Vision extraction completed for file: {file_path}")
        return result
    except Exception as exc:
        logger.exception(f"Error in Azure Vision extraction for file: {file_path}")
        return create_error_response(exc, f"extract_azure_vision for {file_path}")

@celery.task(bind=True)
@validate_inputs(images_info=lambda x: isinstance(x, dict) and 'images_path' in x and 'pdf_path' in x)
def extract_gpt4_vision_optimized(self, images_info):
    """GPT-4 Vision mit vorkonvertierten Bildern und Batch-Verarbeitung"""
    logger.info(f"Starting optimized GPT-4 Vision extraction")
    try:
        images_path = images_info['images_path']
        if not os.path.exists(images_path):
            # Check if this is a retry after the file was already cleaned up
            if hasattr(self.request, 'retries') and self.request.retries > 0:
                logger.warning(f"Images file not found on retry {self.request.retries}, likely already cleaned up: {images_path}")
                return create_error_response(
                    FileNotFoundError(f"Images file not found (retry {self.request.retries}): {images_path}"),
                    "extract_gpt4_vision_optimized"
                )
            else:
                raise FileNotFoundError(f"Images file not found: {images_path}")
        
        # Lade Bilder aus temporärer Datei
        with open(images_path, 'rb') as f:
            images = pickle.load(f)
        
        if not images:
            raise ValueError("No images found in pickle file")
        
        extractor = GPT4VisionExtractor()
        
        # Batch-Verarbeitung mit ThreadPoolExecutor
        from concurrent.futures import ThreadPoolExecutor, TimeoutError
        
        def process_image(image_with_index):
            image, index = image_with_index
            try:
                # Konvertiere zu optimiertem base64 (WebP mit JPEG Fallback)
                base64_image = image_to_base64(image, format='webp', quality=85, max_size_kb=19000)
                
                # Bestimme MIME-Type basierend auf dem tatsächlich verwendeten Format
                # Da image_to_base64 intern Fallback macht, verwenden wir data:image/jpeg für Kompatibilität
                data_url = f"data:image/jpeg;base64,{base64_image}"
                
                response = openai_client.chat.completions.create(
                    model=openai_model,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Wandele bitte das Bild in ein Json-Format um."},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": data_url,
                                },
                            },
                        ],
                    }],                
                    timeout=60  # 60 Sekunden Timeout pro API-Call
                )
                
                if not response.choices or not response.choices[0].message.content:
                    logger.warning(f"Empty response from GPT-4 Vision for image {index}")
                    return ""
                    
                return response.choices[0].message.content
                
            except Exception as img_exc:
                logger.error(f"GPT-4 Vision failed for image {index}: {img_exc}")
                return ""  # Leere Antwort bei Fehler
        
        # Parallele API-Calls mit max 3 gleichzeitig und Timeout
        with ThreadPoolExecutor(max_workers=3) as executor:
            # Nummeriere Bilder für besseres Error-Handling
            images_with_index = [(image, i) for i, image in enumerate(images)]
            page_texts = list(executor.map(process_image, images_with_index))
        
        result = extractor.create_structured_output("gpt4_vision", os.path.basename(images_info['pdf_path']), page_texts)
        
        # Qualitätsprüfung: bei zu wenigen Antworten Exception für Retry
        total_pages = len(images)
        non_empty_pages = sum(1 for t in page_texts if isinstance(t, str) and t.strip())
        if total_pages > 0:
            quality_ratio = non_empty_pages / total_pages
            logger.info(f"GPT4: quality pages {non_empty_pages}/{total_pages} ({quality_ratio:.2f})")
            if non_empty_pages == 0 or quality_ratio < 0.3:
                raise Exception(f"GPT-4 Vision low quality: {non_empty_pages}/{total_pages} non-empty pages")

        if not result:
            raise ValueError("GPT-4 Vision extraction returned empty result")
        
        logger.info(f"Optimized GPT-4 Vision extraction completed")
        return result
    except Exception as exc:
        logger.exception(f"Error in optimized GPT-4 Vision extraction")
        # Manuelles Retry mit Backoff; bei letzter Wiederholung Fehlerobjekt zurückgeben, damit Chord weiterläuft
        max_retries = 3
        try:
            if getattr(self.request, 'retries', 0) < max_retries:
                raise self.retry(exc=exc, countdown=120)
        except Retry:
            return
        return create_error_response(exc, "extract_gpt4_vision_optimized")

@celery.task(bind=True)
def extract_gpt4_vision(self, file_path):
    """Legacy GPT-4 Vision extractor - deprecated, use extract_gpt4_vision_optimized instead"""
    logger.warning(f"Using deprecated extract_gpt4_vision for {file_path} - consider using extract_gpt4_vision_optimized")
    try:
        extractor = GPT4VisionExtractor()
        result = extractor.extract(file_path)
        logger.info(f"GPT-4 Vision extraction completed for file: {file_path}")
        return result
    except Exception as exc:
        logger.exception(f"Error in GPT-4 Vision extraction for file: {file_path}")
        return create_error_response(exc, f"extract_gpt4_vision for {file_path}")

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 120})
@validate_inputs(images_info=lambda x: isinstance(x, dict) and 'images_path' in x and 'pdf_path' in x)
def extract_gemini_vision_optimized(self, images_info):
    """Gemini Vision mit vorkonvertierten Bildern und Batch-Verarbeitung"""
    logger.info(f"GEMINI DEBUG: Starting optimized Gemini Vision extraction")
    logger.info(f"GEMINI DEBUG: images_info type: {type(images_info)}, content: {images_info}")
    try:
        images_path = images_info['images_path']
        logger.info(f"GEMINI DEBUG: images_path: {images_path}")
        
        if not os.path.exists(images_path):
            logger.error(f"GEMINI DEBUG: Images file not found: {images_path}")
            raise FileNotFoundError(f"Images file not found: {images_path}")
        
        logger.info(f"GEMINI DEBUG: Opening pickle file...")
        # Lade Bilder aus temporärer Datei
        with open(images_path, 'rb') as f:
            images = pickle.load(f)
        
        logger.info(f"GEMINI DEBUG: Loaded {len(images) if images else 0} images from pickle")
        
        if not images:
            raise ValueError("No images found in pickle file")
        
        logger.info(f"GEMINI DEBUG: Creating GeminiVisionExtractor...")
        extractor = GeminiVisionExtractor()
        logger.info(f"GEMINI DEBUG: Extractor created successfully")
        
        # Batch-Verarbeitung mit ThreadPoolExecutor
        logger.info(f"GEMINI DEBUG: Importing concurrent.futures...")
        from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
        import threading
        logger.info(f"GEMINI DEBUG: Imports successful")
        
        def process_image_with_timeout(image_with_index):
            image, index = image_with_index
            logger.info(f"GEMINI DEBUG: Processing image {index}, type: {type(image)}")
            try:
                # Konvertiere Bild zu Bytes für Gemini
                logger.info(f"GEMINI DEBUG: Creating BytesIO for image {index}")
                img_bytes = io.BytesIO()
                logger.info(f"GEMINI DEBUG: Saving image {index} to BytesIO as JPEG")
                image.save(img_bytes, format='JPEG', quality=85)
                img_bytes.seek(0)
                logger.info(f"GEMINI DEBUG: Image {index} saved successfully, size: {len(img_bytes.getvalue())} bytes")
                
                # Verwende Threading für Timeout-Handling
                result_container = [None]
                exception_container = [None]
                
                def gemini_api_call():
                    try:
                        logger.info(f"GEMINI DEBUG: Starting Gemini API call for image {index}")
                        logger.info(f"GEMINI DEBUG: gemini_model type: {type(gemini_model)}")
                        response = gemini_model.generate_content([
                            {
                                "mime_type": "image/jpeg",
                                "data": img_bytes.read()
                            },
                            "Wandele bitte das Bild in ein Json-Format um."
                        ])
                        logger.info(f"GEMINI DEBUG: Gemini API call successful for image {index}")
                        result_container[0] = response.text
                    except Exception as e:
                        logger.error(f"GEMINI DEBUG: Gemini API call failed for image {index}: {e}")
                        exception_container[0] = e
                
                # Starte API-Call in separatem Thread mit Timeout
                thread = threading.Thread(target=gemini_api_call)
                thread.daemon = True
                thread.start()
                thread.join(timeout=60)  # 60 Sekunden Timeout
                
                if thread.is_alive():
                    logger.error(f"Gemini Vision API timeout for image {index} after 60 seconds")
                    return ""
                
                if exception_container[0]:
                    raise exception_container[0]
                    
                return result_container[0] if result_container[0] else ""
                
            except Exception as img_exc:
                logger.error(f"Gemini Vision failed for image {index}: {img_exc}")
                return ""  # Leere Antwort bei Fehler
        
        # Parallele API-Calls mit max 3 gleichzeitig
        with ThreadPoolExecutor(max_workers=3) as executor:
            # Nummeriere Bilder für besseres Error-Handling
            images_with_index = [(image, i) for i, image in enumerate(images)]
            
            # Verwende as_completed für besseres Monitoring
            futures = {executor.submit(process_image_with_timeout, img): img[1] 
                      for img in images_with_index}
            
            page_texts = [""] * len(images)  # Vorinitialisiere mit leeren Strings
            
            for future in as_completed(futures, timeout=300):  # 5 Minuten Gesamt-Timeout
                index = futures[future]
                try:
                    result = future.result()
                    page_texts[index] = result
                    logger.info(f"Gemini Vision processed image {index+1}/{len(images)}")
                except Exception as e:
                    logger.error(f"Failed to process image {index}: {e}")
                    page_texts[index] = ""
        
        result = extractor.create_structured_output("gemini_vision", os.path.basename(images_info['pdf_path']), page_texts)
        
        if not result:
            raise ValueError("Gemini Vision extraction returned empty result")
        
        logger.info(f"Optimized Gemini Vision extraction completed")
        return result
    except Exception as exc:
        logger.exception(f"Error in optimized Gemini Vision extraction")
        
        # Bei max retries, gib IMMER ein Error-Response zurück statt Exception zu werfen
        max_retries = self.max_retries if hasattr(self, 'max_retries') else 3
        current_retries = self.request.retries if hasattr(self.request, 'retries') else 0
        
        if current_retries >= max_retries:
            error_response = create_error_response(exc, "extract_gemini_vision_optimized")
            logger.error(f"Gemini max retries reached. Returning error response: {error_response}")
            return error_response
        else:
            # Re-raise für Retry
            raise

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 60})
@validate_inputs(
    extraction_results=lambda x: isinstance(x, list) and len(x) > 0,
    filenames=lambda x: isinstance(x, list) and len(x) > 0,
    patient_name=lambda x: isinstance(x, str) and x.strip()
)
@safe_db_operation
def combine_extractions(self, extraction_results, filenames, patient_name, record_id=None, create_reports=False, start_time=None, original_task_id=None, user_id=None):
    logger.info(f"Starting combine_extractions task for files: {filenames}")
    logger.info(f"Received extraction results: {extraction_results}")
    try:
        # Validiere zusätzliche Parameter
        if record_id is not None and not isinstance(record_id, int):
            raise ValueError(f"record_id must be integer or None, got: {type(record_id)}")
        
        if user_id is not None and not isinstance(user_id, int):
            raise ValueError(f"user_id must be integer or None, got: {type(user_id)}")
        
        # Status-Update entfernt (WebSockets wurden entfernt)
        task_id_to_emit = original_task_id or self.request.id

        valid_results = []
        errors = []

        logger.info(f"Processing {len(extraction_results)} extraction results...")
        
        # WICHTIG: Sortiere die Ergebnisse nach Extraktionsmethode für konsistente Reihenfolge
        # Die Reihenfolge in aggregate_extraction_results ist: pdf_text, ocr, azure_vision, gpt4_vision
        extraction_methods = ['pdf_text', 'ocr', 'azure_vision', 'gpt4_vision']
        sorted_results = []
        
        for i, result in enumerate(extraction_results):
            try:
                logger.info(f"Result {i}: Type={type(result)}, Content preview: {str(result)[:200]}")
                
                if isinstance(result, str):
                    # Extrahiere die Methode aus dem XML-Tag
                    method = f"unknown_{i}"  # Default
                    try:
                        if '<extraction method="' in result:
                            start_idx = result.find('<extraction method="') + len('<extraction method="')
                            end_idx = result.find('">', start_idx)
                            if start_idx > 0 and end_idx > start_idx:
                                method = result[start_idx:end_idx]
                    except Exception as method_exc:
                        logger.warning(f"Failed to extract method from result {i}: {method_exc}")
                    
                    sorted_results.append((method, result))
                    logger.info(f"Valid result {i}: Added {method} result of length {len(result)}")
                elif isinstance(result, dict) and 'exc_message' in result:
                    errors.append(result['exc_message'])
                    logger.error(f"Extraction error in result {i}: {result['exc_message']}")
                    logger.error(f"Full error result {i}: {result}")
                else:
                    logger.warning(f"Unexpected extraction result {i}: Type={type(result)}, Value={result}")
                    # Versuch, es trotzdem zu verwenden falls es Text ist
                    if result and str(result).strip():
                        sorted_results.append((f"unknown_{i}", str(result)))
                        logger.info(f"Converting result {i} to string and using it")
            except Exception as process_exc:
                logger.error(f"Exception processing result {i}: {process_exc}")
                # Versuche trotzdem den Inhalt zu verwenden wenn möglich
                if result and str(result).strip():
                    sorted_results.append((f"error_{i}", str(result)))
        
        # Sortiere nach der definierten Reihenfolge
        def sort_key(item):
            method = item[0]
            try:
                return extraction_methods.index(method)
            except ValueError:
                return len(extraction_methods)  # Unbekannte Methoden ans Ende
        
        try:
            sorted_results.sort(key=sort_key)
            valid_results = [result for method, result in sorted_results]
            logger.info(f"Sorted extraction results in consistent order: {[method for method, _ in sorted_results]}")
        except Exception as sort_exc:
            logger.error(f"Failed to sort results: {sort_exc}")
            # Fallback: verwende unsortierte Ergebnisse
            valid_results = [result for method, result in sorted_results]

        logger.info(f"Summary: {len(valid_results)} valid results, {len(errors)} errors")
        
        if not valid_results:
            logger.error("CRITICAL: No valid extraction results received!")
            logger.error(f"All extraction results: {extraction_results}")
            logger.error(f"All errors: {errors}")
            return {'status': 'error', 'message': 'No valid extraction results'}

        combined_extractions = "\n".join(valid_results)
        token_count = count_tokens(combined_extractions)

        if record_id:
            record = HealthRecord.query.get(record_id)
            if not record:
                logger.error(f"Record with id {record_id} not found")
                return {'status': 'error', 'message': 'Record not found'}

            if record.text is None:
                record.text = combined_extractions
            else:
                record.text += "\n\n" + combined_extractions

            # Filenames als String behandeln, da sie verschlüsselt sind
            current_filenames = record.filenames if record.filenames else ""
            new_filenames = ",".join(filenames)
            record.filenames = current_filenames + ("," if current_filenames else "") + new_filenames

            record.token_count = count_tokens(record.text)
            record.timestamp = datetime.utcnow()
            record.create_reports = create_reports
            record.user_id = user_id
            logger.info(f"Updated existing record {record_id} with new extractions")
        else:
            record = HealthRecord(
                text=combined_extractions,
                filenames=",".join(filenames),  # Wird automatisch verschlüsselt
                token_count=token_count,
                patient_name=patient_name,
                medical_history_begin=None,
                medical_history_end=None,
                create_reports=create_reports,
                user_id=user_id
            )
            db.session.add(record)
            logger.info("Created new record with extractions")

        # Sichere DB-Operation mit Retry bei Deadlock
        try:
            db.session.commit()
            record_id = record.id
            logger.info(f"Successfully committed record {record_id} to database")
        except Exception as db_exc:
            db.session.rollback()
            logger.error(f"Database commit failed: {db_exc}")
            raise db_exc

        # Löschen der verarbeiteten PDF-Dateien
        deleted_files = []
        failed_deletions = []
        for filename in filenames:
            file_path = os.path.join('uploads', filename)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    deleted_files.append(file_path)
                    logger.info(f"Deleted file: {file_path}")
                else:
                    logger.warning(f"File not found for deletion: {file_path}")
            except OSError as e:
                failed_deletions.append(file_path)
                logger.warning(f"Error deleting file {file_path}: {e}")
        
        if failed_deletions:
            logger.warning(f"Failed to delete {len(failed_deletions)} files: {failed_deletions}")

        logger.info(f"Saved combined extraction result to database and deleted PDF files for files: {filenames}")
        logger.info(f"combine_extractions task completed successfully for files: {filenames}")

        # Senden von Informationen über Fehler in der Extraktion, falls vorhanden
        if errors:
            logger.error(f"Errors occurred during extraction: {errors}")

        with current_app.app_context():
            logger.info(f"Sende 'combine_extractions_completed' Event für task_id: {task_id_to_emit}")
            # Status-Update entfernt (WebSockets wurden entfernt)

        return {
            'status': 'Verarbeitung abgeschlossen',
            'token_count': record.token_count,
            'record_id': record_id,
            'start_time': start_time,
            'errors': errors  # Fügen Sie Fehlerinformationen hinzu, falls gewünscht
        }
    except Exception as exc:
        logger.exception(f"Error in combine_extractions task for files: {filenames}")
        # Status-Update entfernt (WebSockets wurden entfernt)
        return {
            'status': 'error',
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }
    finally:
        # Status-Update entfernt (WebSockets wurden entfernt)
        pass

@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 30})
@validate_inputs(data=lambda x: isinstance(x, dict))
@safe_db_operation
def process_record(self, data, original_task_id=None):
    logger.info("Starting process_record task")
     
    try:
        # Verwenden der original_task_id
        task_id_to_emit = original_task_id or self.request.id
        # Status-Update entfernt (WebSockets wurden entfernt)

        if 'status' in data and data['status'] == 'error':
            logger.error(f"Previous task failed: {data.get('exc_message', 'Unknown error')}")
            return data

        record_id = data.get('record_id')
        if not record_id:
            logger.error("No record_id provided in previous result")
            return create_error_response(ValueError("No record_id provided"), "process_record")
        
        if not isinstance(record_id, int):
            logger.error(f"Invalid record_id type: {type(record_id)}")
            return create_error_response(ValueError(f"record_id must be integer, got {type(record_id)}"), "process_record")

        record = HealthRecord.query.get(record_id)
        if not record:
            logger.error(f"Record {record_id} not found")
            return create_error_response(ValueError(f"Record {record_id} not found"), "process_record")
        
        try:
            start_year, end_year, patient_name = find_patient_info(record.text, record.token_count)
            
            # Sicherstelle, dass wir immer gültige Datumswerte haben
            current_year = datetime.utcnow().year
            
            if start_year and end_year:
                record.medical_history_begin = datetime(start_year, 1, 1)
                record.medical_history_end = datetime(end_year, 12, 31)
                logger.info(f"Updated medical history dates for record {record_id}: {start_year}-{end_year}")
            else:
                # Fallback: Standard-Zeitraum von 20 Jahren bis heute
                fallback_start_year = current_year - 20
                fallback_end_year = current_year
                record.medical_history_begin = datetime(fallback_start_year, 1, 1)
                record.medical_history_end = datetime(fallback_end_year, 12, 31)
                logger.warning(f"No valid dates found by find_patient_info for record {record_id}. "
                             f"Using fallback dates: {fallback_start_year}-{fallback_end_year}")
            
            # Patient name update nur wenn gefunden
            if patient_name and isinstance(patient_name, str) and patient_name.strip():
                # Optional: Patient name könnte hier aktualisiert werden, falls gewünscht
                logger.info(f"Patient name found for record {record_id}: {patient_name}")
            else:
                logger.warning(f"No valid patient name found for record {record_id}")
                
            # Verwende die neue Hilfsfunktion
            codes_processed = process_codes_for_record(record_id, record.text)
            if codes_processed:
                logger.info(f"Successfully processed medical codes for record {record_id}")
            else:
                logger.warning(f"Failed to process medical codes for record {record_id}")
                
            # Sichere DB-Operation
            db.session.commit()
            logger.info(f"Successfully committed changes for record {record_id}")
            
        except Exception as process_exc:
            logger.exception(f"Error processing record {record_id}")
            db.session.rollback()
            raise process_exc

        if not record.create_reports:
            # Verarbeitung ohne Reports ist abgeschlossen
            set_record_processing_status(record_id, 'completed')
            start_time = data.get('start_time')
            end_time = datetime.utcnow()
            task_monitor = create_task_monitor(record_id)
            update_task_monitor(task_monitor.id, start_time, end_time, token_count=record.token_count)
        
        return {
            'status': 'Record processing completed', 
            'record_id': record_id, 
            'patient_name': record.patient_name,
            'start_time': data.get('start_time')
        }
    except Exception as exc:
        # Status-Update entfernt (WebSockets wurden entfernt)
        logger.exception("Error in process_record task")
        return {
            'status': 'error',
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }
    finally:
        # Status-Update entfernt (WebSockets wurden entfernt)
        pass


@celery.task(bind=True)
def regenerate_report_task(self, report_id):
    start_time = datetime.utcnow()
    
    try:
        report = Report.query.get(report_id)
        if not report:
            logger.error(f"Report mit ID {report_id} nicht gefunden.")
            return f"Report mit ID {report_id} nicht gefunden."

        # Setze Status auf "generating"
        report.generation_status = 'generating'
        report.generation_started_at = start_time
        report.generation_error_message = None  # Reset error message
        db.session.commit()
        
        logger.info(f"Report {report_id} regeneration started")

        # Holen des zugehörigen HealthRecord und ReportTemplate
        health_record = report.health_record
        template = report.report_template

        medical_codes_list = [
            {"code": code.code, "description": code.description}
            for code in health_record.medical_codes
        ]

        # Generieren des Berichts unter Verwendung des aktuellen Templates
        report_content = generate_report(
            template_name=template.template_name,
            output_format=template.output_format,
            example_structure=template.example_structure,
            health_record_custom_instructions=health_record.custom_instructions,
            system_prompt=template.system_prompt,
            prompt=template.prompt,
            health_record_text=health_record.text,
            health_record_token_count=health_record.token_count,
            health_record_begin=health_record.medical_history_begin,
            health_record_end=health_record.medical_history_end,
            use_custom_instructions=template.use_custom_instructions,
            record_id=health_record.id,
            medical_codes_text=medical_codes_list,
            system_pdf_filename=template.system_pdf_filename
        )

        if report_content:
            # Aktualisieren des Berichts
            report.content = report_content
            report.created_at = datetime.utcnow()
            report.generation_status = 'completed'
            report.generation_completed_at = datetime.utcnow()
            
            db.session.commit()
            logger.info(f"Report {report_id} wurde erfolgreich neu generiert.")
            
            return f"Report {report_id} wurde erfolgreich neu generiert."
        else:
            # Report-Regenerierung fehlgeschlagen
            report.generation_status = 'failed'
            report.generation_completed_at = datetime.utcnow()
            report.generation_error_message = "Report-Regenerierung lieferte leeren Inhalt"
            db.session.commit()
            
            logger.error(f"Report {report_id} Regenerierung fehlgeschlagen: Leerer Inhalt")
            return f"Fehler beim Neu-Generieren des Reports: Leerer Inhalt"

    except Exception as e:
        # Bei Fehler: Report als fehlgeschlagen markieren
        try:
            report = Report.query.get(report_id)
            if report:
                report.generation_status = 'failed'
                report.generation_completed_at = datetime.utcnow()
                report.generation_error_message = str(e)[:1000]
                db.session.commit()
        except Exception as db_exc:
            logger.error(f"Failed to update report status after regeneration error: {db_exc}")
        
        db.session.rollback()
        logger.exception(f"Fehler beim Neu-Generieren des Reports {report_id}: {str(e)}")
        return f"Fehler beim Neu-Generieren des Reports: {str(e)}"


@celery.task(bind=True)
def create_report(self, data, original_task_id=None):
    if not isinstance(data, dict):
        return "Ungültige Eingabe: Erwartet ein Dictionary."

    health_record_id = data.get('record_id')
    start_time = data.get('start_time')

    if health_record_id is None or start_time is None:
        logger.error(f"Fehlende 'record_id' oder 'start_time' im Eingabe-Dictionary. Erhaltene Daten: {data}")
        return "Fehlende 'record_id' oder 'start_time' im Eingabe-Dictionary."

    try:
        health_record_id = int(health_record_id)
    except ValueError:
        return f"Ungültige health_record_id: {health_record_id}. Muss eine ganze Zahl sein."

    task_name = 'create_report'
    task_id = original_task_id or self.request.id
    task_start_time = datetime.utcnow()

    try:
        # Log Task Start
        log_task_start(health_record_id, task_name, task_id, {
            'original_task_id': original_task_id,
            'start_time': start_time.isoformat() if isinstance(start_time, datetime) else str(start_time)
        })
        
        task_id_to_emit = original_task_id or self.request.id
        # Status-Update entfernt (WebSockets wurden entfernt)

        # Lade den HealthRecord
        health_record = HealthRecord.query.get(health_record_id)
        if not health_record:
            return f"HealthRecord mit ID {health_record_id} nicht gefunden."
        
        medical_codes_list = [
            {"code": code.code, "description": code.description}
            for code in health_record.medical_codes
        ]

        # Holen aller ReportTemplates aus der Datenbank
        report_templates = ReportTemplate.query.all()

        for template in report_templates:
            # Generiere den Report
            report_content = generate_report(
                template_name=template.template_name,
                output_format=template.output_format,
                example_structure=template.example_structure,
                health_record_custom_instructions=health_record.custom_instructions,
                system_prompt=template.system_prompt,
                prompt=template.prompt,
                health_record_text=health_record.text,
                health_record_token_count=health_record.token_count,
                health_record_begin=health_record.medical_history_begin,
                health_record_end=health_record.medical_history_end,
                use_custom_instructions=template.use_custom_instructions,
                record_id=health_record_id,
                medical_codes_text=medical_codes_list,
                system_pdf_filename=template.system_pdf_filename
            )

            # Erstelle einen neuen Report
            report = Report(
                report_template_id=template.id,
                health_record_id=health_record.id,
                content=report_content,
                report_type=template.template_name,
                generation_status='completed',  # Setze Status auf completed
                generation_completed_at=datetime.utcnow()  # Setze Zeitstempel
            )

            # Speichere den Report in der Datenbank
            db.session.add(report)
            db.session.commit()

        # Log the upload information
        end_time = datetime.utcnow()
        duration = end_time - start_time
        task_monitor = create_task_monitor(health_record_id)
        update_task_monitor(task_monitor.id, start_date=start_time, end_date=end_time, token_count=health_record.token_count)

        # Setze Status auf "completed" nach erfolgreicher Report-Erstellung
        set_record_processing_status(health_record_id, 'completed')
        
        # Log Task Success
        log_task_success(health_record_id, task_name, task_id, task_start_time, {
            'reports_created': len(report_templates),
            'duration_seconds': duration.total_seconds(),
            'token_count': health_record.token_count
        })

        return f"Reports für HealthRecord {health_record_id} wurden erstellt."

    except Exception as e:
        # Setze Status auf "failed" bei Fehler
        set_record_processing_status(health_record_id, 'failed', str(e))
        
        # Log Task Failure
        log_task_failure(health_record_id, task_name, task_id, e, task_start_time, {
            'attempted_templates': len(report_templates) if 'report_templates' in locals() else 0
        })
        
        # Wenn ein Fehler auftritt, mache alle Änderungen rückgängig
        db.session.rollback()
        logger.exception(f"Fehler beim Erstellen der Reports für HealthRecord {health_record_id}: {str(e)}")
        
        # Senden eines Ereignisses bei Fehler
        # socketio.emit  # WebSockets entfernt('task_status', {'status': 'error', 'message': str(e)}, namespace='/tasks')
        
        return f"Fehler beim Erstellen der Reports: {str(e)}"

    finally:
        # socketio.emit  # WebSockets entfernt('task_status', {'status': 'create_report_completed'}, namespace='/tasks')
        db.session.close()

@celery.task(bind=True)
def generate_single_report(self, record_id, template_id, report_id=None):
    logger.info(f"Generiere Bericht für Datensatz-ID {record_id} und Template-ID {template_id}")
    start_time = datetime.utcnow()
    
    try:
        record = HealthRecord.query.get(record_id)
        template = ReportTemplate.query.get(template_id)
        
        if not record or not template:
            logger.error("Datensatz oder Template nicht gefunden.")
            return f"Fehler: Datensatz oder Template nicht gefunden."
        
        # Wenn report_id übergeben wurde, verwende den existierenden Report
        if report_id:
            report = Report.query.get(report_id)
            if not report:
                logger.error(f"Report mit ID {report_id} nicht gefunden.")
                return f"Fehler: Report mit ID {report_id} nicht gefunden."
            logger.info(f"Using existing report {report_id} with status 'generating'")
        else:
            # Fallback: Erstelle neuen Report (für Abwärtskompatibilität)
            new_report = Report(
                health_record_id=record_id,
                report_template_id=template_id,
                report_type=template.template_name,
                generation_status='generating',
                generation_started_at=start_time
            )
            db.session.add(new_report)
            db.session.commit()
            report_id = new_report.id
            report = new_report
            logger.info(f"Created new report {report_id} with status 'generating'")
        
        medical_codes_list = [
             {"code": code.code, "description": code.description}
             for code in record.medical_codes
         ]

        # Generieren des Berichts
        report_content = generate_report(
            template_name=template.template_name,
            output_format=template.output_format,
            example_structure=template.example_structure,
            system_prompt=template.system_prompt,
            prompt=template.prompt,
            health_record_text=record.text,
            health_record_token_count=record.token_count,
            health_record_begin=record.medical_history_begin,
            health_record_end=record.medical_history_end,
            health_record_custom_instructions=record.custom_instructions,
            use_custom_instructions=template.use_custom_instructions,
            record_id=record_id,
            medical_codes_text=medical_codes_list,
            system_pdf_filename=template.system_pdf_filename
        )    

        if report_content:
            # Report erfolgreich generiert
            report.content = report_content
            report.generation_status = 'completed'
            report.generation_completed_at = datetime.utcnow()
            db.session.commit()
            
            logger.info(f"Report {report_id} für Datensatz-ID {record_id} und Template-ID {template_id} erfolgreich generiert.")
            return f"Report wurde erfolgreich generiert."
        else:
            # Report-Generierung fehlgeschlagen
            report.generation_status = 'failed'
            report.generation_completed_at = datetime.utcnow()
            report.generation_error_message = "Report-Generierung lieferte leeren Inhalt"
            db.session.commit()
            
            logger.error(f"Report {report_id} Generierung fehlgeschlagen: Leerer Inhalt")
            return f"Fehler beim Generieren des Berichts: Leerer Inhalt"
            
    except Exception as e:
        # Bei Fehler: Report als fehlgeschlagen markieren
        try:
            if 'report_id' in locals() and report_id:
                report = Report.query.get(report_id)
                if report:
                    report.generation_status = 'failed'
                    report.generation_completed_at = datetime.utcnow()
                    report.generation_error_message = str(e)[:1000]  # Begrenzen auf 1000 Zeichen
                    db.session.commit()
                    logger.info(f"Marked report {report_id} as failed")
        except Exception as db_exc:
            logger.error(f"Failed to update report status after error: {db_exc}")
        
        logger.exception(f"Fehler beim Generieren des Berichts für Record {record_id}, Template {template_id}: {e}")
        return f"Fehler beim Generieren des Berichts: {str(e)}"

@celery.task(bind=True)
def send_notifications_task(self):
    with current_app.app_context():

        current_app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
        current_app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT'))
        current_app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
        current_app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
        current_app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS') == 'True'
        current_app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL') == 'True'
        # Initialisierung von Mail
        mail = Mail(current_app)

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
            msg = Message(subject=subject, sender=current_app.config['MAIL_USERNAME'], recipients=[user.email])
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

@celery.task(bind=True)
def extract_medical_codes(self, text):
    """
    Extrahiert medizinische Codes (ICD-10, ICD-11, OPS) aus einem Text.
    """
    logger.info("Starting medical code extraction")
    try:
        extractor = CodeExtractor()
        result = extractor.extract(text)
        logger.info("Medical code extraction completed")
        return result
    except Exception as exc:
        logger.exception("Error in medical code extraction")
        return {
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }


@celery.task(bind=True)
def save_medical_codes(self, extraction_result, record_id):
    """
    Speichert extrahierte medizinische Codes für einen Health Record.
    Bestehende Codes werden gelöscht und durch die neuen ersetzt.
    
    :param extraction_result: Liste von extrahierten Codes mit ihren Typen
    :param record_id: ID des Health Records
    :return: Status-Dictionary
    """
    if not extraction_result or not record_id:
        logger.warning(f"Keine Daten oder Record-ID für record_id: {record_id}")
        return {"status": "error", "message": "Keine Daten oder Record-ID"}
    
    try:
        # Lösche alle bestehenden Codes für diesen Record
        deleted_count = MedicalCode.query.filter_by(health_record_id=record_id).delete()
        logger.info(f"Gelöschte Codes für Record {record_id}: {deleted_count}")
        
        # Füge die neuen Codes hinzu
        codes_added = 0
        for item in extraction_result:
            try:
                new_code = MedicalCode(
                    health_record_id=record_id,
                    code=item['code'],
                    code_type=item['type'],
                    description=None  # Wird später durch API-Abfrage gefüllt
                )
                db.session.add(new_code)
                codes_added += 1
                logger.info(f"Code {item['code']} ({item['type']}) für Record {record_id} hinzugefügt")
            except Exception as e:
                logger.exception(f"Fehler beim Hinzufügen des Codes {item['code']}: {str(e)}")
        
        db.session.commit()
        logger.info(f"Erfolgreich {codes_added} neue Codes für Record {record_id} gespeichert")
        
        return {
            "status": "success", 
            "deleted": deleted_count,
            "added": codes_added
        }
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Fehler beim Speichern der medizinischen Codes für Record {record_id}: {str(e)}")
        return {"status": "error", "message": str(e)}

def process_codes_for_record(record_id, record_text):
    """
    Hilfsfunktion zur Extraktion und Speicherung von Codes für einen HealthRecord
    """
    logger.info(f"Processing codes for record {record_id}")
    try:
        # Code-Extraktion durchführen und auf Ergebnis warten
        extraction_result = extract_medical_codes.apply_async((record_text,)).get()
        logger.info(f"Got extraction result for record {record_id}")
        
        # Ergebnis speichern
        if not isinstance(extraction_result, dict) or 'exc_type' not in extraction_result:
            # Parse XML extraction result into list of dictionaries
            parsed_result = parse_medical_codes_xml(extraction_result)
            if parsed_result:
                save_result = save_medical_codes.apply_async((parsed_result, record_id)).get()
                logger.info(f"Saved medical codes for record {record_id}: {save_result}")
                
                # Starte die Beschreibungs-Aktualisierung und WARTE auf das Ergebnis
                if save_result.get('status') == 'success':
                    # Änderung hier: .get() statt .delay()
                    update_result = update_medical_codes_descriptions.apply_async((record_id,)).get()
                    logger.info(f"Completed description update for record {record_id}: {update_result}")
                
                return True
            else:
                logger.error(f"Failed to parse extraction result for record {record_id}")
                return False
        else:
            logger.error(f"Extraction failed for record {record_id}: {extraction_result}")
            return False
    except Exception as exc:
        logger.exception(f"Error processing codes for record {record_id}")
        return False

def parse_medical_codes_xml(xml_string):
    """
    Parst das XML-Ergebnis der Code-Extraktion in eine Liste von Dictionaries
    
    :param xml_string: XML-String mit extrahierten Codes
    :return: Liste von Dictionaries mit 'code' und 'type' Keys
    """
    try:
        logger.info("Parsing medical codes XML")
        result = []
        root = ET.fromstring(xml_string)
        
        # ICD-10 Codes
        icd10_elem = root.find("icd10_codes")
        if icd10_elem is not None:
            for code_elem in icd10_elem.findall("code"):
                if code_elem.text:
                    result.append({
                        'code': code_elem.text,
                        'type': 'ICD10'
                    })
        
        # ICD-11 Codes
        icd11_elem = root.find("icd11_codes")
        if icd11_elem is not None:
            for code_elem in icd11_elem.findall("code"):
                if code_elem.text:
                    result.append({
                        'code': code_elem.text,
                        'type': 'ICD11'
                    })
        
        # OPS Codes
        ops_elem = root.find("ops_codes")
        if ops_elem is not None:
            for code_elem in ops_elem.findall("code"):
                if code_elem.text:
                    result.append({
                        'code': code_elem.text,
                        'type': 'OPS'
                    })
        
        logger.info(f"Parsed {len(result)} medical codes from XML")
        return result
    except Exception as e:
        logger.exception(f"Error parsing medical codes XML: {e}")
        return None


@celery.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 90})
@validate_inputs(health_record_id=lambda x: isinstance(x, int) and x > 0)
@safe_db_operation
def update_medical_codes_descriptions(self, health_record_id):
    """
    Aktualisiert die Beschreibungen aller Medical Codes eines Health Records
    Mit optimierter Batch-Verarbeitung für parallele API-Calls
    
    :param health_record_id: ID des Health Records
    """
    try:
        # Validiere, dass der Health Record existiert
        health_record = HealthRecord.query.get(health_record_id)
        if not health_record:
            raise ValueError(f"Health record {health_record_id} not found")
        
        medical_codes = MedicalCode.query.filter_by(health_record_id=health_record_id).all()
        logger.info(f"Aktualisiere Beschreibungen für {len(medical_codes)} medizinische Codes für Record {health_record_id}")
        
        # Filtere Codes ohne Beschreibung
        codes_to_update = [code for code in medical_codes if not code.description]
        codes_skipped = len(medical_codes) - len(codes_to_update)
        
        if codes_skipped > 0:
            logger.info(f"{codes_skipped} Codes haben bereits Beschreibungen - übersprungen")
        
        if not codes_to_update:
            logger.info("Alle Codes haben bereits Beschreibungen")
            return True
        
        # Nutze ThreadPoolExecutor für parallele API-Calls
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        codes_updated = 0
        codes_failed = 0
        
        # Batch-Verarbeitung mit max 5 parallelen Requests
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_code = {
                executor.submit(update_medical_code_description, code): code 
                for code in codes_to_update
            }
            
            for future in as_completed(future_to_code):
                code = future_to_code[future]
                try:
                    if future.result():
                        codes_updated += 1
                        logger.info(f"Beschreibung für Code {code.code} ({code.code_type}) erfolgreich aktualisiert")
                    else:
                        codes_failed += 1
                        logger.error(f"Konnte Beschreibung für Code {code.code} ({code.code_type}) nicht aktualisieren")
                except Exception as e:
                    codes_failed += 1
                    logger.error(f"Fehler bei Code {code.code}: {str(e)}")
        
        # Commit alle Änderungen auf einmal mit besserer Fehlerbehandlung
        try:
            db.session.commit()
            logger.info(f"Successfully committed medical code description updates for record {health_record_id}")
        except Exception as commit_exc:
            logger.error(f"Failed to commit medical code updates: {commit_exc}")
            db.session.rollback()
            raise commit_exc
        
        logger.info(f"Beschreibungsaktualisierung abgeschlossen für Record {health_record_id}: "
                   f"{codes_updated} aktualisiert, {codes_skipped} übersprungen, {codes_failed} fehlgeschlagen")
        return True
    except Exception as e:
        logger.exception(f"Fehler bei der Aktualisierung der Medical Codes für Record {health_record_id}: {str(e)}")
        return False