import os
import json
import pandas as pd
import base64
from utils import repair_json
from openai import OpenAI
from datetime import datetime
import google.generativeai as genai
from google.ai.generativelanguage_v1beta.types import content
from google.generativeai.types import content_types
import logging
import time
from tenacity import retry, stop_after_attempt, wait_exponential
import httpx
from config import get_config

# Konfiguration des Loggings
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),  # Ausgabe in die Konsole
        logging.FileHandler('openai_api.log')  # Ausgabe in eine Datei
    ]
)
logger = logging.getLogger(__name__)

# Konfiguration der OpenAI- und Google Gemini-Clients mit Azure Key Vault
openai_client = OpenAI(
    api_key=get_config("OPENAI_API_KEY"),
    timeout=500,
    max_retries=10,
    http_client=httpx.Client(
        timeout=httpx.Timeout(
            connect=60.0,    # Timeout für den Verbindungsaufbau
            read=120.0,      # Timeout für das Lesen der Antwort
            write=60.0,      # Timeout für das Schreiben der Anfrage
            pool=60.0        # Timeout für Connection-Pool
        )
    )
)
openai_model = get_config("OPENAI_MODEL")
token_threshold = int(get_config("TOKEN_THRESHOLD", "100000"))
logger.info(f"📊 TOKEN_THRESHOLD aus Key Vault geladen: {token_threshold}")

genai.configure(api_key=get_config("GEMINI_API_KEY"))
gemini_model = genai.GenerativeModel(model_name=get_config("GEMINI_MODEL"))


# Retry-Decorator für OpenAI API Calls
@retry(
    stop=stop_after_attempt(5),    # Erhöht auf 5 Versuche
    wait=wait_exponential(multiplier=2, min=4, max=60),  # Längeres maximales Warten
    reraise=True
)
def make_openai_request(api_params):
    """
    Führt eine OpenAI API-Anfrage mit Retry-Logik aus
    """
    try:
        logger.info("Sende Anfrage an OpenAI API...")
        logger.debug(f"Request Parameter: {api_params}")
        
        start_time = time.time()
        response = openai_client.chat.completions.create(**api_params)
        end_time = time.time()
        
        duration = round(end_time - start_time, 2)
        logger.info(f"Antwort von OpenAI erhalten nach {duration} Sekunden")
        
        # Log der Antwort-Details
        if hasattr(response, 'usage'):
            logger.info(f"Token Usage - Prompt: {response.usage.prompt_tokens}, "
                       f"Completion: {response.usage.completion_tokens}, "
                       f"Total: {response.usage.total_tokens}")
        
        # Prüfe ob eine Antwort vorhanden ist
        if hasattr(response, 'choices') and response.choices:
            content_preview = response.choices[0].message.content[:200]  # Erste 200 Zeichen
            logger.info(f"Antwort Preview: {content_preview}...")
        else:
            logger.warning("Keine Antwort im Response-Objekt gefunden!")
            
        return response
        
    except httpx.ReadTimeout as e:
        logger.error(f"Timeout beim Lesen der OpenAI-Antwort nach {time.time() - start_time} Sekunden: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Fehler bei OpenAI Anfrage: {str(e)}")
        if hasattr(e, 'response'):
            logger.error(f"API Response Status: {e.response.status_code}")
            logger.error(f"API Response Body: {e.response.text}")
        raise

# Funktion zur Bereinigung der JSON-Antwort von GPT-4
def clean_json_response(response_content):
    """
    Entfernt die Markdown-Syntax aus der GPT-4 API-Antwort, falls vorhanden.
    """
    if response_content.startswith("```json"):
        # Entferne die umschließenden ```json und ```
        response_content = response_content[7:-3].strip()
    return response_content

# Neue Hilfsfunktion zum Abrufen und Formatieren der medizinischen Codes
def get_formatted_medical_codes(record_id):
    """
    Ruft die medizinischen Codes für einen Health Record ab und formatiert sie
    für die Verwendung in Prompts
    
    :param record_id: ID des Health Records
    :return: Formatierter String mit Codes und Beschreibungen
    """
    try:
        from models import MedicalCode  # Import hier, um zirkuläre Importe zu vermeiden
        
        codes = MedicalCode.query.filter_by(health_record_id=record_id).all()
        if not codes:
            logger.info(f"Keine medizinischen Codes gefunden für Record {record_id}")
            return ""
        
        formatted_codes = "Medizinische Codes:\n"
        
        # Gruppieren nach Code-Typ
        icd10_codes = [c for c in codes if c.code_type == 'ICD10']
        icd11_codes = [c for c in codes if c.code_type == 'ICD11']
        ops_codes = [c for c in codes if c.code_type == 'OPS']
        
        # ICD-10 Codes formatieren
        if icd10_codes:
            formatted_codes += "\nICD-10 Codes:\n"
            for code in icd10_codes:
                desc = f" - {code.description}" if code.description else ""
                formatted_codes += f"- {code.code}{desc}\n"
        
        # ICD-11 Codes formatieren
        if icd11_codes:
            formatted_codes += "\nICD-11 Codes:\n"
            for code in icd11_codes:
                desc = f" - {code.description}" if code.description else ""
                formatted_codes += f"- {code.code}{desc}\n"
        
        # OPS Codes formatieren
        if ops_codes:
            formatted_codes += "\nOPS Codes:\n"
            for code in ops_codes:
                desc = f" - {code.description}" if code.description else ""
                formatted_codes += f"- {code.code}{desc}\n"
        
        return formatted_codes
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der medizinischen Codes: {e}")
        return ""

def generate_gemini_schema_from_example(example_structure):
    """
    Generiert ein Gemini JSON Schema aus einer example_structure
    
    Args:
        example_structure (str): JSON-String der Beispielstruktur
        
    Returns:
        content.Schema: Gemini Schema-Objekt
    """
    try:
        if not example_structure or not example_structure.strip():
            logger.warning("Keine example_structure vorhanden, verwende Standard-Schema")
            return get_default_gemini_schema()
            
        # Parse example structure
        example_json = json.loads(example_structure)
        logger.info(f"Generiere Schema aus example_structure: {list(example_json.keys())}")
        
        return build_schema_recursive(example_json)
        
    except Exception as e:
        logger.error(f"Fehler beim Generieren des Schemas aus example_structure: {e}")
        logger.info("Verwende Standard-Schema als Fallback")
        return get_default_gemini_schema()

def build_schema_recursive(obj, is_root=True):
    """
    Baut rekursiv ein Gemini Schema aus einem JSON-Objekt
    """
    if isinstance(obj, dict):
        properties = {}
        required_keys = []
        
        for key, value in obj.items():
            properties[key] = build_schema_recursive(value, False)
            required_keys.append(key)
        
        return content.Schema(
            type=content.Type.OBJECT,
            required=required_keys,
            properties=properties
        )
    
    elif isinstance(obj, list):
        if len(obj) > 0:
            # Verwende das erste Element als Template für das Array
            item_schema = build_schema_recursive(obj[0], False)
            return content.Schema(
                type=content.Type.ARRAY,
                items=item_schema
            )
        else:
            # Leeres Array - verwende generisches Object
            return content.Schema(
                type=content.Type.ARRAY,
                items=content.Schema(type=content.Type.OBJECT)
            )
    
    elif isinstance(obj, str):
        return content.Schema(type=content.Type.STRING)
    
    elif isinstance(obj, int):
        return content.Schema(type=content.Type.INTEGER)
    
    elif isinstance(obj, float):
        return content.Schema(type=content.Type.NUMBER)
    
    elif isinstance(obj, bool):
        return content.Schema(type=content.Type.BOOLEAN)
    
    else:
        # Fallback für unbekannte Typen
        return content.Schema(type=content.Type.STRING)

def get_default_gemini_schema():
    """
    Gibt das Standard-Schema zurück (das bisherige hart codierte)
    """
    return content.Schema(
        type=content.Type.OBJECT,
        enum=[],
        required=["Bericht"],
        properties={
            "Bericht": content.Schema(
                type=content.Type.ARRAY,
                items=content.Schema(
                    type=content.Type.OBJECT,
                    enum=[],
                    required=["Datum", "Code", "Diagnose", "Beschreibung", "Arzt"],
                    properties={
                        "Datum": content.Schema(type=content.Type.STRING),
                        "Code": content.Schema(type=content.Type.STRING),
                        "Diagnose": content.Schema(type=content.Type.STRING),
                        "Beschreibung": content.Schema(type=content.Type.STRING),
                        "Arzt": content.Schema(type=content.Type.STRING),
                    },
                ),
            ),
        },
    )

# Funktion für die Erstellung des Berichts mit GPT-4
def generate_report_gpt5(output_format, example_structure, system_prompt, prompt, health_record_text, year, health_record_custom_instructions, use_custom_instructions, record_id=None, medical_codes_text=None):
    """
    Generiert einen Bericht für ein spezifisches Jahr mit GPT-5
    """
    try:
        logger.info(f"Starte GPT-5 Bericht für Jahr {year}")
        
        if not use_custom_instructions:
            year_focussed_actual_prompt = (
            f"Follow this role: {system_prompt}\n\n"
            f"Follow this task: {prompt}\n\n"
            f"Use this medical codes: {medical_codes_text}\n\n"
            f"You give your output in this format: {output_format}\n\n"
            f"Extremely important: Create a report for and only contain data for the year {year}.\n\n"
        )
        else:
            year_focussed_actual_prompt = (
            f"Follow this role: {system_prompt}\n\n"
            f"Follow this task: {prompt}\n\n"
            f"This medical codes are already extracted: {medical_codes_text}\n\n"
            f"You give your output in this format: {output_format}\n\n"
            f"Extremely important: Create a report for and only contain data for the year {year}.\n\n"
            f"Consider the following additional important information for the analysis of the dataset: {health_record_custom_instructions}\n\n"
        )

        

        # Basis-Parameter für die API-Anfrage
        api_params = {
            "model": openai_model,
            "messages": [
                {"role": "system", "content": year_focussed_actual_prompt},
                {"role": "user", "content": f"Das ist deine Datenbasis: {health_record_text}"}
            ],
            "temperature": 0.7,
            "max_completion_tokens": 32000,
        }

        if output_format.lower() == "json":
            api_params["response_format"] = {"type": "json_object"}

        try:
            # Verwende die neue Retry-Funktion
            response = make_openai_request(api_params)
            response_content = response.choices[0].message.content.strip()
            logger.info(f"GPT-4 API-Antwort erhalten: {response_content[:100]}...")

            if output_format.lower() == "json":
                response_content = clean_json_response(response_content)
                
                # Extrahiere den ersten Schlüssel aus der Beispielstruktur
                example_structure_json = json.loads(example_structure)
                first_key = next(iter(example_structure_json.keys()))

                # Parsen des JSON
                try:
                    json_data = json.loads(response_content)
                    logger.info(f"GPT-4 JSON erfolgreich geparst für Jahr {year}.")
                except json.JSONDecodeError as json_err:
                    logger.error(f"Fehler beim Parsen des JSON von GPT-4 für Jahr {year}: {json_err}")
                    return None

                # Extrahieren der Daten aus dem ersten Schlüssel
                data = json_data.get(first_key, [])

                # Bereinigung von Whitespaces
                for item in data:
                    for key, value in item.items():
                        if isinstance(value, str):
                            item[key] = value.strip()

                # Konvertiere in DataFrame für einfache Sortierung, falls möglich
                df = pd.DataFrame(data)

                if 'Datum' in df.columns:
                    df['Datum'] = pd.to_datetime(df['Datum'])
                    df_sorted = df.sort_values('Datum')
                else:
                    df_sorted = df

                return df_sorted

            else:
                # Wenn das Ausgabeformat Text ist, gib den Text zurück
                logger.info(f"Textbericht für Jahr {year} erhalten.")
                return response_content

        except Exception as e:
            logger.error(f"Fehler bei der Verarbeitung mit GPT-4: {e}")
            return None

    except Exception as e:
        logger.error(f"Fehler bei der Verarbeitung mit GPT-4: {e}")
        return None

# Hilfsfunktion zum Laden von System-PDFs
def get_system_pdf_path(filename):
    """Gibt den vollständigen Pfad zur System-PDF zurück"""
    if not filename:
        return None
    system_prompts_dir = os.path.join(os.path.dirname(__file__), 'system', 'prompts')
    return os.path.join(system_prompts_dir, filename)

def load_system_pdf_for_gemini(pdf_filename):
    """Lädt eine System-PDF für Gemini - als Inline-Daten statt Upload"""
    if not pdf_filename:
        return None
    
    pdf_path = get_system_pdf_path(pdf_filename)
    if not pdf_path or not os.path.exists(pdf_path):
        logger.warning(f"System-PDF nicht gefunden: {pdf_path}")
        return None
    
    try:
        logger.info(f"Lade System-PDF für Gemini: {pdf_path}")
        
        # Für Gemini 2.5: PDF als Inline-Daten laden statt Upload
        # Lese die PDF-Datei als Bytes
        with open(pdf_path, 'rb') as f:
            pdf_data = f.read()
        
        # Erstelle ein Inline-Datenobjekt für Gemini
        pdf_base64 = base64.b64encode(pdf_data).decode('utf-8')
        
        # Erstelle das Part-Objekt für Gemini
        pdf_part = content_types.to_part({
            "inline_data": {
                "mime_type": "application/pdf",
                "data": pdf_base64
            }
        })
        
        logger.info(f"System-PDF erfolgreich als Inline-Daten geladen: {pdf_filename} ({len(pdf_data)} bytes)")
        return pdf_part
        
    except Exception as e:
        logger.error(f"Fehler beim Laden der System-PDF: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

# Funktion für die Erstellung des Berichts mit Google Gemini
def generate_report_gemini(output_format, example_structure, system_prompt, prompt, health_record_text, year, health_record_custom_instructions, use_custom_instructions, record_id=None, medical_codes_text=None, system_pdf_filename=None):
    """
    Generiert einen Bericht für ein spezifisches Jahr mit Google Gemini
    Unterstützt optional eine System-PDF als zusätzlichen Kontext
    """
    try:
        logger.info(f"Starte Gemini Bericht für Jahr {year}")
        
        # System-PDF laden falls vorhanden
        if system_pdf_filename:
            logger.info(f"🔍 Versuche System-PDF zu laden: {system_pdf_filename}")
            system_pdf_file = load_system_pdf_for_gemini(system_pdf_filename)
            if system_pdf_file:
                logger.info(f"✅ System-PDF erfolgreich geladen: {system_pdf_filename}")
            else:
                logger.warning(f"⚠️ System-PDF konnte nicht geladen werden: {system_pdf_filename}")
        else:
            system_pdf_file = None
            logger.info("ℹ️ Kein system_pdf_filename angegeben - kein System-PDF wird verwendet")
        
        if not use_custom_instructions:
            year_focussed_actual_prompt = (
            f"Follow this role: {system_prompt}\n\n"
            f"Follow this task: {prompt}\n\n"
            f"Use this medical codes: {medical_codes_text}\n\n"
            f"You give your output in this format: {output_format}\n\n"
            f"Extremely important: Create a report for and only contain data for the year {year}.\n\n"
        )
        else:
            year_focussed_actual_prompt = (
            f"Follow this role: {system_prompt}\n\n"
            f"Follow this task: {prompt}\n\n"
            f"This medical codes are already extracted: {medical_codes_text}\n\n"
            f"You give your output in this format: {output_format}\n\n"
            f"Extremely important: Create a report for and only contain data for the year {year}.\n\n"
            f"Consider the following additional important information for the analysis of the dataset: {health_record_custom_instructions}\n\n"
        )

        logger.info(f"Gemini Prompt: {year_focussed_actual_prompt}")
        logger.info(f"Gemini Model: {get_config('GEMINI_MODEL')}")

        try:
            if output_format.lower() == "json":
                # Generiere Schema aus example_structure
                response_schema = generate_gemini_schema_from_example(example_structure)
                logger.info("Verwende dynamisch generiertes Schema basierend auf example_structure")
                
                generation_config = {
                    "temperature": 0.2,
                    "max_output_tokens": 32000,  # Erhöht von 8192 auf 32000
                    "response_schema": response_schema,
                    "response_mime_type": "application/json",
                }
            else:
                # Einfache Konfiguration für Text-Output
                generation_config = {
                    "temperature": 1
                }

            # Safety Settings für medizinische Daten
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]
            
            gemini_model = genai.GenerativeModel(
                model_name=get_config("GEMINI_MODEL"),
                generation_config=generation_config,
                safety_settings=safety_settings
            )

            chat_session = gemini_model.start_chat(
                history=[]
            )

            # Erstelle die Nachricht mit oder ohne PDF
            if system_pdf_file:
                # Mit System-PDF (als Inline-Daten)
                message_content = [
                    system_pdf_file,
                    f"{year_focussed_actual_prompt}\n\nDas ist deine Datenbasis: {health_record_text}"
                ]
                logger.info(f"📤 Sende Nachricht MIT System-PDF an Gemini: {system_pdf_filename}")
                logger.info(f"   PDF als Inline-Daten geladen")
                logger.info(f"   Prompt-Länge: {len(year_focussed_actual_prompt)} chars")
                logger.info(f"   Daten-Länge: {len(health_record_text)} chars")
            else:
                # Ohne System-PDF (Standard)
                message_content = f"{year_focussed_actual_prompt}\n\nDas ist deine Datenbasis: {health_record_text}"
                logger.info("📤 Sende Nachricht OHNE System-PDF an Gemini")
                logger.info(f"   Nachricht-Länge: {len(message_content)} chars")

            response = chat_session.send_message(message_content)
            logger.info("✅ Antwort von Gemini erhalten")
            
            # Bessere Gemini Response-Behandlung
            try:
                response_text = response.text.strip()
                logger.info(f"Google Gemini API-Antwort erhalten: {response_text[:100]}...")
            except ValueError as response_error:
                logger.error(f"Gemini Response Error: {response_error}")
                logger.error(f"Response candidates: {response.candidates}")
                
                # Prüfe finish_reason
                if response.candidates:
                    candidate = response.candidates[0]
                    finish_reason = candidate.finish_reason if hasattr(candidate, 'finish_reason') else None
                    logger.error(f"Finish reason: {finish_reason}")
                    
                    # Finish reasons: 1=STOP, 2=MAX_TOKENS, 3=SAFETY, 4=RECITATION, 5=OTHER
                    if finish_reason == 2:  # MAX_TOKENS
                        logger.error(f"Gemini hat das Token-Limit erreicht für Jahr {year}")
                        # Versuche es nochmal mit reduziertem Text oder nutze Fallback
                        return f"[MAX_TOKENS] Gemini hat das Token-Limit für {year} erreicht. Bitte reduzieren Sie die Datenmenge."
                    elif finish_reason == 3:  # SAFETY
                        logger.error(f"Content wurde von Gemini Safety-Filtern blockiert für Jahr {year}")
                        return f"[SAFETY BLOCKED] Gemini hat den Content für {year} aus Sicherheitsgründen blockiert."
                    elif finish_reason == 4:  # RECITATION
                        logger.error(f"Content wurde wegen Recitation blockiert für Jahr {year}")
                        return f"[RECITATION BLOCKED] Gemini hat den Content für {year} wegen Urheberrechtsproblemen blockiert."
                    else:
                        logger.error(f"Unbekannter finish_reason: {finish_reason} für Jahr {year}")
                        return f"[ERROR] Gemini Response-Fehler für {year}: {response_error} (finish_reason: {finish_reason})"
                else:
                    logger.error(f"Keine Candidates in Gemini Response für Jahr {year}")
                    return f"[ERROR] Keine Response-Candidates von Gemini für {year}"

            if output_format.lower() == "json":
                try:
                    json_data = json.loads(response_text)
                    logger.info(f"Google Gemini JSON erfolgreich geparst für Jahr {year}.")
                    
                    # Extrahiere den ersten Schlüssel aus der Beispielstruktur
                    example_structure_json = json.loads(example_structure)
                    first_key = next(iter(example_structure_json.keys()))
                    
                    # Extrahiere die Daten aus dem ersten Schlüssel
                    data = json_data.get(first_key, [])
                    
                    # Bereinigung von Whitespaces
                    for item in data:
                        if isinstance(item, dict):
                            for key, value in item.items():
                                if isinstance(value, str):
                                    item[key] = value.strip()

                    # Konvertiere in DataFrame
                    df = pd.DataFrame(data)
                    
                    if 'Datum' in df.columns:
                        df['Datum'] = pd.to_datetime(df['Datum'])
                        df_sorted = df.sort_values('Datum')
                    else:
                        df_sorted = df

                    return df_sorted

                except json.JSONDecodeError as json_err:
                    logger.error(f"Fehler beim Parsen des JSON von Google Gemini für Jahr {year}: {json_err}")
                    return None

            else:
                logger.info(f"Textbericht für Jahr {year} erhalten.")
                return response_text

        except Exception as e:
            logger.error(f"Fehler bei der Verarbeitung mit Google Gemini: {e}")
            return None

    except Exception as e:
        logger.error(f"Fehler bei der Verarbeitung mit Google Gemini: {e}")
        return None

# Hauptfunktion zur Generierung des Berichts
def generate_report(template_name, output_format, example_structure, system_prompt, prompt, health_record_text, health_record_token_count, health_record_begin, health_record_end, health_record_custom_instructions, use_custom_instructions, record_id=None, medical_codes_text=None, system_pdf_filename=None):
    """
    Generiert einen kombinierten Gesundheitsbericht für den angegebenen Zeitraum (mehrere Jahre).
    """
    # Sichere Fallback-Prüfung für None-Datumswerte
    if health_record_begin is None or health_record_end is None:
        current_year = datetime.now().year
        fallback_start_year = current_year - 20
        fallback_end_year = current_year
        
        if health_record_begin is None:
            health_record_begin = datetime(fallback_start_year, 1, 1)
            logger.warning(f"health_record_begin war None, verwende Fallback: {fallback_start_year}")
        
        if health_record_end is None:
            health_record_end = datetime(fallback_end_year, 12, 31)
            logger.warning(f"health_record_end war None, verwende Fallback: {fallback_end_year}")
    
    logger.info(f"Starte Berichterstellung für Zeitraum {health_record_begin.year}-{health_record_end.year}")
    
    all_year_reports = []
    text_reports = []

    # Entscheide, ob GPT-4 oder Gemini verwendet wird basierend auf Token-Threshold
    use_gemini = health_record_token_count > token_threshold
    if use_gemini:
        logger.info(f"Verwende Google Gemini (Token-Count: {health_record_token_count} > Threshold: {token_threshold})")
    else:
        logger.info(f"Verwende GPT-4 (Token-Count: {health_record_token_count} <= Threshold: {token_threshold})")

    # Verwandle health_record_begin und health_record_end in Jahreszahlen
    # (Diese sind jetzt garantiert nicht None aufgrund der obigen Prüfung)
    start_year = health_record_begin.year
    end_year = health_record_end.year
    logger.info(f"Verarbeite Jahre von {start_year} bis {end_year}")

    for year in range(start_year, end_year + 1):
        logger.info(f"Generiere Bericht für das Jahr {year}...")

        try:
            if use_gemini:
                yearly_report = generate_report_gemini(
                    output_format, example_structure, system_prompt, 
                    prompt, health_record_text, year, health_record_custom_instructions, 
                    use_custom_instructions, record_id, medical_codes_text, system_pdf_filename
                )
            else:
                yearly_report = generate_report_gpt5(
                    output_format, example_structure, system_prompt,
                    prompt, health_record_text, year, health_record_custom_instructions,
                    use_custom_instructions, record_id, medical_codes_text
                )

            if yearly_report is not None:
                if output_format.lower() == "json":
                    # Prüfe ob yearly_report ein DataFrame oder ein String (Fehler) ist
                    if isinstance(yearly_report, pd.DataFrame):
                        logger.info(f"Jahr {year}: JSON-Report erfolgreich generiert")
                        logger.debug(f"Jahr {year} Report Inhalt: {yearly_report.to_dict('records')[:2] if len(yearly_report) >= 2 else yearly_report.to_dict('records')}...")  # Zeige die ersten 2 Einträge
                        all_year_reports.append(yearly_report)
                    elif isinstance(yearly_report, str):
                        # Bei Fehlern (z.B. SAFETY BLOCKED) ist yearly_report ein String
                        logger.warning(f"Jahr {year}: Report als String erhalten (möglicherweise Fehler): {yearly_report[:100]}...")
                        # Überspringe dieses Jahr
                        continue
                    else:
                        logger.warning(f"Jahr {year}: Unerwarteter Report-Typ: {type(yearly_report)}")
                        continue
                else:
                    logger.info(f"Jahr {year}: Text-Report erfolgreich generiert")
                    logger.debug(f"Jahr {year} Report Länge: {len(yearly_report)} Zeichen")
                    text_reports.append(f"Bericht für Jahr {year}:\n{yearly_report}\n")
            else:
                logger.warning(f"Jahr {year}: Kein Report generiert")

        except Exception as e:
            logger.error(f"Fehler beim Erstellen des Berichts für Jahr {year}: {str(e)}")
            continue

    logger.info(f"Alle Jahresberichte generiert. JSON Reports: {len(all_year_reports)}, Text Reports: {len(text_reports)}")

    if output_format.lower() == "json" and all_year_reports:
        try:
            logger.info("Beginne Zusammenführung der JSON-Reports...")
            logger.info(f"Anzahl der zu kombinierenden DataFrames: {len(all_year_reports)}")
            
            # Log die Struktur jedes DataFrames
            for i, df in enumerate(all_year_reports):
                logger.info(f"DataFrame {i} Struktur: {df.shape}, Spalten: {df.columns.tolist()}")
            
            # Kombiniere alle jährlichen Berichte
            combined_report = pd.concat(all_year_reports, ignore_index=True)
            logger.info(f"Kombinierter DataFrame erstellt. Größe: {combined_report.shape}")
            
            # Konvertiere Datum zu String im YYYY-MM-DD Format vor der JSON-Konvertierung
            if 'Datum' in combined_report.columns:
                combined_report['Datum'] = combined_report['Datum'].dt.strftime('%Y-%m-%d')
            
            # Konvertiere zu JSON mit angepassten Parametern
            combined_report_json = combined_report.to_json(
                orient='records',
                force_ascii=False,
                date_format='iso'  # ISO Format für Datums-Strings
            )
            
            logger.info(f"JSON-Konvertierung abgeschlossen. Länge des JSON-Strings: {len(combined_report_json)}")
            logger.debug(f"Preview des kombinierten JSON: {combined_report_json[:500]}...")
            
            return combined_report_json

        except Exception as e:
            logger.error(f"Fehler beim Zusammenführen der JSON-Reports: {str(e)}")
            return None

    elif output_format.lower() != "json" and text_reports:
        try:
            logger.info("Beginne Zusammenführung der Text-Reports...")
            logger.info(f"Anzahl der Text-Reports: {len(text_reports)}")
            
            # Kombiniere alle Textberichte
            combined_text_report = "\n".join(text_reports)
            logger.info(f"Textberichte zusammengeführt. Gesamtlänge: {len(combined_text_report)} Zeichen")
            
            # Führe zusätzliche Verarbeitung durch
            logger.info("Starte zusätzliche Verarbeitung des kombinierten Textberichts...")
            
            try:
                if use_gemini:
                    final_report = process_combined_text_gemini(
                        template_name, output_format, example_structure, system_prompt, prompt, combined_text_report
                    )
                else:
                    final_report = process_combined_text_gpt5(
                        template_name, output_format, example_structure, system_prompt, prompt, combined_text_report
                    )
                logger.info(f"Zusätzliche Verarbeitung abgeschlossen. Finale Berichtslänge: {len(final_report)} Zeichen")
                return final_report
            except Exception as e:
                logger.error(f"Fehler bei der zusätzlichen Verarbeitung: {str(e)}")
                logger.info("Gebe unverarbeiteten kombinierten Textbericht zurück")
                return combined_text_report

        except Exception as e:
            logger.error(f"Fehler beim Zusammenführen der Text-Reports: {str(e)}")
            return None

    else:
        logger.warning("Keine Berichte zum Zusammenführen verfügbar")
        return "Keine Berichte verfügbar."

def process_combined_text_gpt5(template_name, output_format, example_structure, system_prompt, prompt, combined_text_report):
    """
    Verarbeitet den kombinierten Textbericht mit GPT-5, um eine Gesamtauswertung zu erhalten.
    """
    logger.info("Verarbeite den kombinierten Textbericht mit GPT-5.")

    final_system_prompt = system_prompt
    final_prompt = (
        f"{prompt}\n\n"
        f"Bitte fasse die folgenden Jahresberichte zu einem Gesamtbericht zusammen und beantworte dabei die Fragestellung über alle Jahre hinweg.\n\n"
        f"{combined_text_report}"
    )

    api_params = {
        "model": openai_model,
        "messages": [
            {"role": "system", "content": final_system_prompt},
            {"role": "user", "content": final_prompt}
        ],
        "temperature": 0.7,
        "max_completion_tokens": 32000,
    }

    try:
        response = make_openai_request(api_params)
        response_content = response.choices[0].message.content.strip()
        logger.info("Verarbeitung mit GPT-4 abgeschlossen.")
        return response_content
    except Exception as e:
        logger.error(f"Fehler bei der Verarbeitung mit GPT-4: {e}")
        return combined_text_report

def process_combined_text_gemini(template_name, output_format, example_structure, system_prompt, prompt, combined_text_report):
    """
    Verarbeitet den kombinierten Textbericht mit Google Gemini, um eine Gesamtauswertung zu erhalten.
    """
    logger.info("Verarbeite den kombinierten Textbericht mit Google Gemini.")

    final_system_prompt = system_prompt
    final_prompt = (
        f"{prompt}\n\n"
        f"Bitte fasse die folgenden Jahresberichte zu einem Gesamtbericht zusammen und beantworte dabei die Fragestellung über alle Jahre hinweg.\n\n"
        f"{combined_text_report}"
    )

    try:
        # Generationskonfiguration
        generation_config = genai.types.GenerationConfig(
            max_output_tokens=32000,  # Erhöht von 8000 auf 32000
            temperature=0.7
        )

        # Konfiguriere minimale Safety Settings für medizinische Daten
        safety_settings = [
            {
                "category": "HARM_CATEGORY_HARASSMENT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_HATE_SPEECH",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                "threshold": "BLOCK_NONE"
            },
            {
                "category": "HARM_CATEGORY_CIVIC_INTEGRITY",
                "threshold": "BLOCK_NONE"
            }
        ]

        # Erstelle neue Model-Instanz mit lockeren Safety Settings
        safe_gemini_model = genai.GenerativeModel(
            model_name=get_config("GEMINI_MODEL"),
            generation_config=generation_config,
            safety_settings=safety_settings
        )

        response = safe_gemini_model.generate_content(
            f"{final_system_prompt}\n\n{final_prompt}"
        )
        
        # Bessere Gemini Response-Behandlung
        try:
            response_text = response.text.strip()
            logger.info("Verarbeitung mit Google Gemini abgeschlossen.")
            return response_text
        except ValueError as response_error:
            logger.error(f"Gemini Combined Text Response Error: {response_error}")
            logger.error(f"Response candidates: {response.candidates}")
            
            # Prüfe finish_reason
            if response.candidates:
                candidate = response.candidates[0]
                finish_reason = candidate.finish_reason if hasattr(candidate, 'finish_reason') else None
                logger.error(f"Combined text finish reason: {finish_reason}")
                
                # Finish reasons: 1=STOP, 2=MAX_TOKENS, 3=SAFETY, 4=RECITATION, 5=OTHER
                if finish_reason == 2:  # MAX_TOKENS
                    logger.error("Combined text hat Gemini Token-Limit erreicht")
                    return f"[MAX_TOKENS] Gemini hat das Token-Limit für die Zusammenfassung erreicht.\n\nOriginal combined text:\n{combined_text_report}"
                elif finish_reason == 3:  # SAFETY
                    logger.error("Combined text wurde von Gemini Safety-Filtern blockiert")
                    return f"[SAFETY BLOCKED] Gemini hat die Zusammenfassung aus Sicherheitsgründen blockiert.\n\nOriginal combined text:\n{combined_text_report}"
                else:
                    logger.error(f"Combined text Gemini-Fehler: {response_error}")
                    return f"[ERROR] Gemini Combined Text Fehler: {response_error} (finish_reason: {finish_reason})\n\nOriginal combined text:\n{combined_text_report}"
            else:
                logger.error("Keine Candidates in Combined Text Gemini Response")
                return f"[ERROR] Keine Response von Gemini\n\nOriginal combined text:\n{combined_text_report}"
    except Exception as e:
        logger.error(f"Fehler bei der Verarbeitung mit Google Gemini: {e}")
        return combined_text_report
