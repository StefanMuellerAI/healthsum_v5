# tasks.py

import os
import logging
from celery import chain, group, shared_task
from celery_config import create_celery_app
from extractors import PDFTextExtractor, OCRExtractor, AzureVisionExtractor, GPT4VisionExtractor
from utils import count_tokens, find_patient_info
from datetime import datetime
import traceback
from models import db, HealthRecord, Report
from extractors import openai_client
from reports import generate_report


# Konfigurieren des Loggings
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

celery = create_celery_app()


@celery.task(bind=True)
def process_pdfs(self, filenames, patient_name, record_id=None, create_reports=False):
    logger.info(f"Starting process_pdfs task for files: {filenames}")
    self.update_state(state='STARTED', meta={'status': 'Initialisiere Verarbeitung...'})

    try:
        extraction_tasks = []

        for i, filename in enumerate(filenames):
            file_path = os.path.join('uploads', filename)
            extraction_tasks.extend([
                extract_pdf_text.s(file_path),
                extract_ocr.s(file_path),
                extract_azure_vision.s(file_path),
                extract_gpt4_vision.s(file_path),

            ])

            progress = (i + 1) / len(filenames) * 50
            self.update_state(state='PROGRESS', meta={'status': f'Vorbereitung der Extraktion: {progress:.0f}%'})
            logger.info(f"Prepared extraction tasks for file: {filename}")

        extraction_group = group(extraction_tasks)

        # Updated workflow to include create_patient_summary
        workflow = (extraction_group |
                    combine_extractions.s(filenames, patient_name, record_id, create_reports) |
                    process_record.s() |
                    create_report.s())

        self.update_state(state='PROGRESS', meta={'status': 'Starte Extraktionsprozess...'})
        logger.info("Starting extraction workflow")
        result = workflow.apply_async()

        logger.info(f"Extraction workflow started with task id: {result.id}")
        return {'status': 'Extraktionsprozess gestartet', 'subtask_id': result.id}

    except Exception as exc:
        logger.exception("Error in process_pdfs task")
        return {
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }


@celery.task(bind=True)
def extract_pdf_text(self, file_path):
    logger.info(f"Starting PDF text extraction for file: {file_path}")
    try:
        extractor = PDFTextExtractor()
        result = extractor.extract(file_path)
        logger.info(f"PDF text extraction completed for file: {file_path}")
        return result
    except Exception as exc:
        logger.exception(f"Error in PDF text extraction for file: {file_path}")
        return {
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }


@celery.task(bind=True)
def extract_ocr(self, file_path):
    logger.info(f"Starting OCR extraction for file: {file_path}")
    try:
        extractor = OCRExtractor()
        result = extractor.extract(file_path)
        logger.info(f"OCR extraction completed for file: {file_path}")
        return result
    except Exception as exc:
        logger.exception(f"Error in OCR extraction for file: {file_path}")
        return {
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }


@celery.task(bind=True)
def extract_azure_vision(self, file_path):
    logger.info(f"Starting Azure Vision extraction for file: {file_path}")
    try:
        extractor = AzureVisionExtractor()
        result = extractor.extract(file_path)
        logger.info(f"Azure Vision extraction completed for file: {file_path}")
        return result
    except Exception as exc:
        logger.exception(f"Error in Azure Vision extraction for file: {file_path}")
        return {
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }


@celery.task(bind=True)
def extract_gpt4_vision(self, file_path):
    logger.info(f"Starting GPT-4 Vision extraction for file: {file_path}")
    try:
        extractor = GPT4VisionExtractor()
        result = extractor.extract(file_path)
        logger.info(f"GPT-4 Vision extraction completed for file: {file_path}")
        return result
    except Exception as exc:
        logger.exception(f"Error in GPT-4 Vision extraction for file: {file_path}")
        return {
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }



@celery.task(bind=True)
def combine_extractions(self, extraction_results, filenames, patient_name, record_id=None, create_reports=False):
    logger.info(f"Starting combine_extractions task for files: {filenames}")
    logger.info(f"Received extraction results: {extraction_results}")
    try:
        valid_results = [result for result in extraction_results if result]

        if not valid_results:
            logger.warning("No valid extraction results received")
            return {'status': 'error', 'message': 'No valid extraction results'}

        combined_extractions = "\n".join(valid_results)
        token_count = count_tokens(combined_extractions)

        if record_id:
            record = HealthRecord.query.get(record_id)
            if not record:
                logger.error(f"Record with id {record_id} not found")
                return {'status': 'error', 'message': 'Record not found'}

            record.text += "\n\n" + combined_extractions
            record.filenames += "," + ",".join(filenames)
            record.token_count = count_tokens(record.text)
            record.timestamp = datetime.utcnow()
            record.create_reports = create_reports
            logger.info(f"Updated existing record {record_id} with new extractions")
        else:
            record = HealthRecord(
                text=combined_extractions,
                filenames=",".join(filenames),
                token_count=token_count,
                patient_name=patient_name,
                medical_history_begin=None,
                medical_history_end=None,
                create_reports = create_reports
            )
            db.session.add(record)
            logger.info("Created new record with extractions")

        db.session.commit()

        record_id = record.id

        # Löschen der verarbeiteten PDF-Dateien
        for filename in filenames:
            file_path = os.path.join('uploads', filename)
            try:
                os.remove(file_path)
                logger.info(f"Deleted file: {file_path}")
            except OSError as e:
                logger.warning(f"Error deleting file {file_path}: {e}")

        logger.info(f"Saved combined extraction result to database and deleted PDF files for files: {filenames}")
        logger.info(f"combine_extractions task completed successfully for files: {filenames}")
        return {
            'status': 'Verarbeitung abgeschlossen',
            'token_count': record.token_count,
            'record_id': record_id
        }
    except Exception as exc:
        logger.exception(f"Error in combine_extractions task for files: {filenames}")
        return {
            'status': 'error',
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }


@celery.task(bind=True)
def process_record(self, previous_result):
    logger.info("Starting process_record task")
    try:
        if 'status' in previous_result and previous_result['status'] == 'error':
            logger.error(f"Previous task failed: {previous_result['exc_message']}")
            return previous_result

        record_id = previous_result.get('record_id')
        if not record_id:
            logger.error("No record_id provided in previous result")
            return {'status': 'error', 'message': 'No record_id provided'}

        record = HealthRecord.query.get(record_id)
        if record:
            start_year, end_year, patient_name = find_patient_info(record.text, record.token_count)
            if start_year and end_year and patient_name:
                record.medical_history_begin = datetime(start_year, 1, 1)
                record.medical_history_end = datetime(end_year, 12, 31)
                db.session.commit()
                logger.info(f"Updated record {record_id} with medical history years")
            else:
                logger.warning(f"No years found in record {record_id}")
        else:
            logger.error(f"Record {record_id} not found")

        return {'status': 'Record processing completed', 'record_id': record_id, 'patient_name': record.patient_name}
    except Exception as exc:
        logger.exception("Error in process_record task")
        return {
            'status': 'error',
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }



@celery.task(bind=True)
def create_report(self, data):
    if not isinstance(data, dict):
        return "Ungültige Eingabe: Erwartet ein Dictionary."

    health_record_id = data.get('record_id')
    if health_record_id is None:
        return "Keine 'record_id' im Eingabe-Dictionary gefunden."

    try:
        health_record_id = int(health_record_id)
    except ValueError:
        return f"Ungültige health_record_id: {health_record_id}. Muss eine ganze Zahl sein."

    try:
        # Lade den HealthRecord
        health_record = HealthRecord.query.get(health_record_id)
        if not health_record:
            return f"HealthRecord mit ID {health_record_id} nicht gefunden."

        # Überprüfe, ob Reports erstellt werden sollen
        if not health_record.create_reports:
            logger.info(f"Keine Reports für HealthRecord {health_record_id} erstellt, da create_reports nicht aktiviert ist.")
            return f"Keine Reports für HealthRecord {health_record_id} erstellt, da create_reports nicht aktiviert ist."

        # Verzeichnis mit den Prompt-Dateien
        prompts_dir = 'prompts'

        total_files = len([f for f in os.listdir(prompts_dir) if f.endswith('.md')])
        for i, filename in enumerate(os.listdir(prompts_dir)):
            if filename.endswith('.md'):
                # Aktualisiere den Fortschritt
                self.update_state(state='PROGRESS', meta={'current': i + 1, 'total': total_files})

                # Lese den Inhalt der Prompt-Datei
                with open(os.path.join(prompts_dir, filename), 'r') as file:
                    prompt_template = file.read()

                # Generiere den Report
                report_content = generate_report(prompt_template, health_record.text, health_record.token_count, health_record.medical_history_begin, health_record.medical_history_end)

                # Erstelle einen neuen Report
                report = Report(
                    health_record_id=health_record.id,
                    content=report_content,
                    report_type=os.path.splitext(filename)[0]  # Verwende den Dateinamen ohne Erweiterung als report_type
                )

                # Speichere den Report in der Datenbank
                db.session.add(report)
                db.session.commit()

        return f"Reports für HealthRecord {health_record_id} wurden erstellt."

    except Exception as e:
        # Wenn ein Fehler auftritt, mache alle Änderungen rückgängig
        db.session.rollback()
        logger.exception(f"Fehler beim Erstellen der Reports für HealthRecord {health_record_id}: {str(e)}")
        return f"Fehler beim Erstellen der Reports: {str(e)}"

    finally:
        # Stelle sicher, dass die Datenbankverbindung in jedem Fall geschlossen wird
        db.session.close()