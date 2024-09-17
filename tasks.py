# tasks.py
import os
import logging
from celery import chain, group, shared_task
from celery_config import create_celery_app
from extractors import PDFTextExtractor, OCRExtractor, AzureVisionExtractor, GPT4VisionExtractor
from utils import count_tokens, find_patient_info
from datetime import datetime
import traceback
from models import db, HealthRecord, Report, ReportTemplate
from extractors import openai_client
from reports import generate_report
from flask import current_app
from extensions import socketio
from utils import update_task_monitor, create_task_monitor, mark_notification_sent
# Konfigurieren des Loggings
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
celery = create_celery_app()

@celery.task(bind=True)
def process_pdfs(self, filenames, patient_name, record_id=None, create_reports=False, user_id=None):
    start_time = datetime.utcnow()
    logger.info(f"Starting process_pdfs task for files: {filenames}")
    
    original_task_id = self.request.id

    socketio.emit('task_status', {'status':'process_pdfs_started','create_reports': create_reports}, namespace='/tasks')

    try:
        extraction_tasks = []

        for i, filename in enumerate(filenames):
            socketio.emit('task_status', {'status':'process_pdfs_progress'}, namespace='/tasks')
            file_path = os.path.join('uploads', filename)
            extraction_tasks.extend([
                extract_pdf_text.s(file_path),
                extract_ocr.s(file_path),
                extract_azure_vision.s(file_path),
                extract_gpt4_vision.s(file_path),
            ])

            progress = (i + 1) / len(filenames) * 50
           
            logger.info(f"Prepared extraction tasks for file: {filename}")

        extraction_group = group(extraction_tasks)

        # Updated workflow to include create_patient_summary and pass start_time, original_task_id, and user_id
        workflow_chain = (
            extraction_group |
            combine_extractions.s(filenames, patient_name, record_id, create_reports, start_time, original_task_id, user_id) |
            process_record.s(original_task_id=original_task_id)
        )

        if create_reports:
            workflow_chain |= create_report.s(original_task_id=original_task_id)
        
        logger.info("Starting extraction workflow")
        result = workflow_chain.apply_async()

        logger.info(f"Extraction workflow started with task id: {result.id}")
        return {'status': 'Extraktionsprozess gestartet', 'subtask_id': result.id, 'start_time': start_time}

    except Exception as exc:
        socketio.emit('task_status', {'status': 'error', 'message': str(exc)}, namespace='/tasks')
        logger.exception("Error in process_pdfs task")
        return {
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }
    finally:
        socketio.emit('task_status', {'status':'process_pdfs_completed'}, namespace='/tasks')


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
def combine_extractions(self, extraction_results, filenames, patient_name, record_id=None, create_reports=False, start_time=None, original_task_id=None, user_id=None):
    logger.info(f"Starting combine_extractions task for files: {filenames}")
    logger.info(f"Received extraction results: {extraction_results}")
    try:
        socketio.emit('task_status', {'status':'combine_extractions_started'}, namespace='/tasks')
        task_id_to_emit = original_task_id or self.request.id

        valid_results = []
        errors = []

        for result in extraction_results:
            if isinstance(result, str):
                valid_results.append(result)
            elif isinstance(result, dict) and 'exc_message' in result:
                errors.append(result['exc_message'])
                logger.error(f"Extraction error: {result['exc_message']}")
            else:
                logger.warning(f"Unexpected extraction result: {result}")

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
            record.user_id = user_id
            logger.info(f"Updated existing record {record_id} with new extractions")
        else:
            record = HealthRecord(
                text=combined_extractions,
                filenames=",".join(filenames),
                token_count=token_count,
                patient_name=patient_name,
                medical_history_begin=None,
                medical_history_end=None,
                create_reports=create_reports,
                user_id=user_id
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

        # Senden von Informationen über Fehler in der Extraktion, falls vorhanden
        if errors:
            logger.error(f"Errors occurred during extraction: {errors}")

        with current_app.app_context():
            logger.info(f"Sende 'combine_extractions_completed' Event für task_id: {task_id_to_emit}")
            socketio.emit('task_status', {'status': 'combine_extractions_completed', 'task_id': task_id_to_emit}, namespace='/tasks', room=task_id_to_emit)

        return {
            'status': 'Verarbeitung abgeschlossen',
            'token_count': record.token_count,
            'record_id': record_id,
            'start_time': start_time,
            'errors': errors  # Fügen Sie Fehlerinformationen hinzu, falls gewünscht
        }
    except Exception as exc:
        logger.exception(f"Error in combine_extractions task for files: {filenames}")
        socketio.emit('task_status', {'status': 'error', 'message': str(exc)}, namespace='/tasks')
        return {
            'status': 'error',
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }
    finally:
        socketio.emit('task_status', {'status': 'combine_extractions_completed'}, namespace='/tasks')

@celery.task(bind=True)
def process_record(self, data, original_task_id=None):
    logger.info("Starting process_record task")
     
    try:
        # Verwenden der original_task_id
        task_id_to_emit = original_task_id or self.request.id
        socketio.emit('task_status', {'status': 'process_record_started'}, namespace='/tasks')

        if 'status' in data and data['status'] == 'error':
            logger.error(f"Previous task failed: {data['exc_message']}")
            return data

        record_id = data.get('record_id')
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

        if not record.create_reports:
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
        socketio.emit('task_status', {'status': 'error', 'message': str(exc)}, namespace='/tasks')
        logger.exception("Error in process_record task")
        return {
            'status': 'error',
            'exc_type': type(exc).__name__,
            'exc_message': str(exc),
            'traceback': traceback.format_exc()
        }
    finally:
        socketio.emit('task_status', {'status': 'process_record_completed'}, namespace='/tasks')


@celery.task(bind=True)
def regenerate_report_task(self, report_id):
    try:
        report = Report.query.get(report_id)
        if not report:
            logger.error(f"Report mit ID {report_id} nicht gefunden.")
            return f"Report mit ID {report_id} nicht gefunden."

        # Holen des zugehörigen HealthRecord und ReportTemplate
        health_record = report.health_record
        template = report.report_template

        # Generieren des Berichts unter Verwendung des aktuellen Templates
        report_content = generate_report(
            template_name=template.template_name,
            output_format=template.output_format,
            example_structure=template.example_structure,
            system_prompt=template.system_prompt,
            prompt=template.prompt,
            health_record_text=health_record.text,
            health_record_token_count=health_record.token_count,
            health_record_begin=health_record.medical_history_begin,
            health_record_end=health_record.medical_history_end
        )

        # Aktualisieren des Berichts
        report.content = report_content
        report.created_at = datetime.utcnow()

        db.session.commit()
        logger.info(f"Report {report_id} wurde erfolgreich neu generiert.")

        return f"Report {report_id} wurde erfolgreich neu generiert."

    except Exception as e:
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

    try:
        task_id_to_emit = original_task_id or self.request.id
        socketio.emit('task_status', {'status': 'create_report_started'}, namespace='/tasks')

        # Lade den HealthRecord
        health_record = HealthRecord.query.get(health_record_id)
        if not health_record:
            return f"HealthRecord mit ID {health_record_id} nicht gefunden."

        # Holen aller ReportTemplates aus der Datenbank
        report_templates = ReportTemplate.query.all()

        for template in report_templates:
            # Generiere den Report
            report_content = generate_report(
                template_name=template.template_name,
                output_format=template.output_format,
                example_structure=template.example_structure,
                system_prompt=template.system_prompt,
                prompt=template.prompt,
                health_record_text=health_record.text,
                health_record_token_count=health_record.token_count,
                health_record_begin=health_record.medical_history_begin,
                health_record_end=health_record.medical_history_end
            )

            # Erstelle einen neuen Report
            report = Report(
                report_template_id=template.id,
                health_record_id=health_record.id,
                content=report_content,
                report_type=template.template_name
            )

            # Speichere den Report in der Datenbank
            db.session.add(report)
            db.session.commit()

        # Log the upload information
        end_time = datetime.utcnow()
        duration = end_time - start_time
        task_monitor = create_task_monitor(health_record_id)
        update_task_monitor(task_monitor.id, start_date=start_time, end_date=end_time, token_count=health_record.token_count)

        return f"Reports für HealthRecord {health_record_id} wurden erstellt."

    except Exception as e:
        
        # Wenn ein Fehler auftritt, mache alle Änderungen rückgängig
        db.session.rollback()
        logger.exception(f"Fehler beim Erstellen der Reports für HealthRecord {health_record_id}: {str(e)}")
        
        # Senden eines Ereignisses bei Fehler
        socketio.emit('task_status', {'status': 'error', 'message': str(e)}, namespace='/tasks')
        
        return f"Fehler beim Erstellen der Reports: {str(e)}"

    finally:
        socketio.emit('task_status', {'status': 'create_report_completed'}, namespace='/tasks')
        db.session.close()

@celery.task(bind=True)
def generate_single_report(self, record_id, template_id):
       logger.info(f"Generiere Bericht für Datensatz-ID {record_id} und Template-ID {template_id}")
       record = HealthRecord.query.get(record_id)
       template = ReportTemplate.query.get(template_id)
   
       if not record or not template:
           logger.error("Datensatz oder Template nicht gefunden.")
           return

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
           health_record_end=record.medical_history_end
       )

       if report_content:
           new_report = Report(
               health_record_id=record_id,
               report_template_id=template_id,
               content=report_content,
               report_type=template.template_name,
               created_at=datetime.utcnow()
           )
           db.session.add(new_report)
           db.session.commit()
           logger.info(f"Bericht für Datensatz-ID {record_id} und Template-ID {template_id} erfolgreich generiert.")
       else:
           logger.error("Fehler beim Generieren des Berichts.")