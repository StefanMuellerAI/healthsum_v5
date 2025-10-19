# utils.py
import tiktoken
import re
import os
import time
import json
from extractors import openai_client
import google.generativeai as genai
from tenacity import retry, wait_random_exponential, stop_after_attempt
from datetime import datetime
from models import db, TaskMonitor
import requests
from requests.auth import HTTPBasicAuth
import logging
from config import get_config

logger = logging.getLogger(__name__)

# Verwende get_config() statt os.environ für Azure Key Vault Integration
openai_model = get_config("OPENAI_MODEL")
token_threshold = int(get_config("TOKEN_THRESHOLD", "100000"))
gemini_model_name = get_config("GEMINI_MODEL")  # Kein Fallback - muss im Key Vault sein
genai.configure(api_key=get_config("GEMINI_API_KEY"))
gemini_model = genai.GenerativeModel(gemini_model_name)
logger.info(f"📊 utils.py - TOKEN_THRESHOLD aus Key Vault geladen: {token_threshold}")
logger.info(f"📊 utils.py - GEMINI_MODEL aus Key Vault geladen: {gemini_model_name}")


def count_tokens(text):
    """
    Zählt die Anzahl der Tokens in einem gegebenen Text für das GPT-4 Modell.

    :param text: Der zu zählende Text
    :return: Anzahl der Tokens
    """
    encoding = tiktoken.encoding_for_model("gpt-4")
    return len(encoding.encode(text))

def extract_years(text):
    """
    Extrahiert die niedrigste und höchste Jahreszahl aus einem Text mittels Regex.

    :param text: Der zu durchsuchende Text
    :return: Tuple mit (niedrigste_jahreszahl, höchste_jahreszahl) oder (None, None) wenn keine Jahreszahlen gefunden wurden
    """
    years = re.findall(r'\b(19|20)\d{2}\b', text)
    if years:
        years = [int(year) for year in years]
        return min(years), max(years)
    return None, None




def find_patient_info(input_text, token_count):
    """
    Findet die Start- und Endjahre der Behandlungen sowie den Namen des Patienten im Text mittels GPT-4 oder Gemini.

    :param input_text: Der zu durchsuchende Text
    :param token_count: Anzahl der Tokens im Input, um das Modell zu wählen
    :return: Tuple mit (start_year, end_year, patient_name) oder (aktuelle Jahreszahl, aktuelle Jahreszahl, None) wenn keine Informationen gefunden wurden
    """
    current_year = datetime.now().year
    logger.info("="*80)
    logger.info(f"🔍 STARTING find_patient_info")
    logger.info(f"Input text length: {len(input_text) if input_text else 0} chars")
    logger.info(f"Input text preview: {input_text[:200] if input_text else 'EMPTY'}...")
    logger.info(f"Token count: {token_count}")
    
    try:
        # Verwende den TOKEN_THRESHOLD aus dem Key Vault
        use_gpt4 = token_count <= token_threshold
        model_used = "GPT-4" if use_gpt4 else "Google Gemini"
        logger.info(f"📊 Token count = {token_count}, Threshold = {token_threshold}")
        logger.info(f"🤖 Selected model: {model_used}")
        
        if use_gpt4:
            # GPT-4 wird verwendet
            response = openai_client.chat.completions.create(
                model=openai_model,  # Stellen Sie sicher, dass Sie das korrekte Modell verwenden
                messages=[
                    {"role": "system",
                     "content": "You are a helpful AI assistant specialized in the extraction of unstructured patient medical data. Your result is a valid JSON object."},
                    {"role": "user",
                     "content": f"Take a deep breath now! Concentrate! Find me across the whole medical history and all files the earliest year (start_year) and the latest year (end_year) of treatments, as well as the patient's name in this input. Give it back as a JSON object: {input_text}. It can be that start_year equals end_year because the medical history is just one year long. If you can't find a specific piece of information, use null for that field."},
                ],
                max_completion_tokens=500,
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            response_content = response.choices[0].message.content
        else:
            example_response = {
                "start_year": 2004,
                "end_year": 2022,
                "patient_name": "John Doe"
            }

            # Google Gemini wird verwendet (mit 1M Token Context-Fenster)
            prompt = f"""Extract from the medical text below:
1. start_year: earliest year mentioned (4-digit number)
2. end_year: latest year mentioned (4-digit number)
3. patient_name: full name of the patient

Return ONLY this JSON (no explanation, no additional text):
{json.dumps(example_response)}

Medical text:
{input_text}
"""
            
            # Safety Settings für medizinische Daten
            safety_settings = [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
            ]
            
            response = gemini_model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=8192,  # Erhöht, um genug Platz zu haben
                    temperature=0.0,  # Deterministisch für konsistente Ergebnisse
                    response_mime_type="application/json"
                ),
                safety_settings=safety_settings
            )
            
            # Prüfe auf Probleme BEVOR wir response.text aufrufen
            if not response.candidates or not response.candidates[0].content.parts:
                logger.error(f"❌ Gemini did not return a valid response!")
                logger.error(f"Candidates: {response.candidates}")
                if response.candidates:
                    logger.error(f"Safety ratings: {response.candidates[0].safety_ratings}")
                    logger.error(f"Finish reason: {response.candidates[0].finish_reason}")
                    finish_reason = response.candidates[0].finish_reason
                    
                    # finish_reason: 1=STOP (normal), 2=MAX_TOKENS, 3=SAFETY, 4=RECITATION, 5=OTHER
                    if finish_reason == 2:  # MAX_TOKENS
                        logger.error(f"❌ Gemini hit MAX_TOKENS limit - prompt or response too long")
                    elif finish_reason == 3:  # SAFETY
                        logger.error(f"❌ Gemini blocked due to SAFETY")
                    else:
                        logger.error(f"❌ Gemini stopped with finish_reason: {finish_reason}")
                        
                raise ValueError(f"Gemini API did not return valid content (finish_reason={finish_reason if 'finish_reason' in locals() else 'unknown'})")
            
            response_content = response.text.strip()
            logger.info(f"✅ Gemini response received: {len(response_content)} chars")
            logger.debug(f"Gemini response preview: {response_content[:200]}...")

        logger.info(f"Model used: {model_used}")
        logger.info(f"Raw response: {response_content[:200]}...")

        # Parsen der JSON-Antwort
        response_dict = json.loads(response_content)
        start_year_raw = response_dict.get('start_year')
        end_year_raw = response_dict.get('end_year')
        patient_name = response_dict.get('patient_name')

        logger.info(f"Parsed values from API: start_year={start_year_raw}, end_year={end_year_raw}, patient_name={patient_name}")

        # Sicherstellen, dass wir gültige Jahreszahlen haben
        # Konvertiere zu int, handle None, null, und String-Werte
        start_year = None
        end_year = None
        
        if start_year_raw is not None and str(start_year_raw).strip().lower() != 'null':
            try:
                start_year = int(start_year_raw)
                if start_year < 1900 or start_year > current_year + 1:
                    logger.warning(f"Invalid start_year {start_year}, using current_year")
                    start_year = current_year
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse start_year '{start_year_raw}': {e}, using current_year")
                start_year = current_year
        else:
            logger.warning(f"No start_year found in response, using current_year")
            start_year = current_year
            
        if end_year_raw is not None and str(end_year_raw).strip().lower() != 'null':
            try:
                end_year = int(end_year_raw)
                if end_year < 1900 or end_year > current_year + 1:
                    logger.warning(f"Invalid end_year {end_year}, using current_year")
                    end_year = current_year
            except (ValueError, TypeError) as e:
                logger.warning(f"Could not parse end_year '{end_year_raw}': {e}, using current_year")
                end_year = current_year
        else:
            logger.warning(f"No end_year found in response, using current_year")
            end_year = current_year

        # Sicherstellen, dass patient_name ein String oder None ist
        if patient_name and str(patient_name).strip().lower() not in ['null', 'none']:
            patient_name = str(patient_name).strip()
        else:
            patient_name = None

        logger.info(f"✅ find_patient_info final results: start_year={start_year}, end_year={end_year}, patient={patient_name}")
        
        return start_year, end_year, patient_name

    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON decode error in find_patient_info: {e}")
        try:
            logger.error(f"Problematic JSON string: {response_content}")
        except:
            logger.error("Response content not available")
        logger.error(f"❌ Returning fallback: ({current_year}, {current_year}, None)")
        return current_year, current_year, None
    except Exception as e:
        logger.error(f"❌ Unexpected error in find_patient_info: {e}", exc_info=True)
        logger.error(f"❌ Returning fallback: ({current_year}, {current_year}, None)")
        return current_year, current_year, None

def repair_json(response_text):
    """
    Repariert potenziell defektes JSON durch Bereinigung offensichtlicher Formatierungsfehler
    und Rückgabe eines validen JSON-Objekts.
    
    :param response_text: Der Text, der repariert werden soll.
    :return: Ein repariertes JSON-Objekt, wenn erfolgreich; ansonsten None.
    """
    # Entferne unerwünschte Formatierungs-Tags wie ```json oder ähnliches
    response_text = re.sub(r"^```json", "", response_text.strip(), flags=re.IGNORECASE).strip()
    response_text = re.sub(r"```$", "", response_text.strip(), flags=re.IGNORECASE).strip()

    # Versuche, das JSON sofort zu parsen
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        print(f"Erster JSON-Fehler: {e}")
        print(f"Response-Text (für Debugging): {response_text}")

    # Entferne nicht-JSON-Inhalte vor und nach dem JSON-Objekt
    json_like_text = re.search(r'(\{.*\})', response_text, re.DOTALL)
    if json_like_text:
        response_text = json_like_text.group(1)

    # Beispiel: Fehlende Anführungszeichen in Schlüsseln reparieren
    response_text = re.sub(r"(\w+):", r'"\1":', response_text)

    # Versuche erneut, das JSON zu parsen
    try:
        repaired_json = json.loads(response_text)
        print("JSON wurde repariert.")
        return repaired_json
    except json.JSONDecodeError as e:
        print(f"Fehler nach Reparaturversuch: {e}")
        print(f"Fehlerhafter Text: {response_text}")
        return None

def create_task_monitor(health_record_id):
    task_monitor = TaskMonitor(
        health_record_id=health_record_id,
        created_at=datetime.utcnow(),
        notification_sent=False  # Standardmäßig auf False gesetzt
    )
    db.session.add(task_monitor)
    db.session.commit()
    return task_monitor

def update_task_monitor(task_monitor_id, start_date=None, end_date=None, token_count=None, notification_sent=None):
    task_monitor = TaskMonitor.query.get(task_monitor_id)
    if not task_monitor:
        return None

    if start_date is not None:
        task_monitor.start_date = start_date
    if end_date is not None:
        task_monitor.end_date = end_date
    if token_count is not None:
        task_monitor.health_record_token_count = token_count
    if notification_sent is not None:
        task_monitor.notification_sent = notification_sent
    
    db.session.commit()
    return task_monitor

def mark_notification_sent(task_monitor_id):
    return update_task_monitor(task_monitor_id, notification_sent=True)

def get_icd_access_token():
    """
    Holt einen Access Token von der WHO ICD API
    """
    token_endpoint = "https://icdaccessmanagement.who.int/connect/token"
    client_id = get_config("ICD_API_CLIENT_ID", os.getenv("ICD_API_CLIENT_ID"))
    client_secret = get_config("ICD_API_CLIENT_SECRET", os.getenv("ICD_API_CLIENT_SECRET"))
    scope = "icdapi_access"
    
    try:
        response = requests.post(
            token_endpoint,
            data={"grant_type": "client_credentials", "scope": scope},
            auth=HTTPBasicAuth(client_id, client_secret)
        )
        
        if response.status_code != 200:
            print(f"Fehler beim Abrufen des Tokens: {response.status_code}")
            return None
        
        return response.json().get("access_token")
    except Exception as e:
        print(f"Fehler beim Token-Abruf: {str(e)}")
        return None

@retry(wait=wait_random_exponential(min=1, max=40), stop=stop_after_attempt(3))
def get_icd10_description(code):
    """
    Ruft die Beschreibung für einen ICD-10 Code ab
    
    :param code: Der ICD-10 Code (z.B. 'J20')
    :return: Die Beschreibung des Codes oder None im Fehlerfall
    """
    token = get_icd_access_token()
    if not token:
        return None
    
    api_url = f"https://id.who.int/icd/release/10/2016/{code}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "en",
        "API-Version": "v2"
    }
    
    try:
        response = requests.get(api_url, headers=headers)
        
        if response.status_code != 200:
            print(f"Fehler beim API-Aufruf für Code {code}: {response.status_code}")
            return None
        
        data = response.json()
        # Extrahiere den Titel (Hauptbeschreibung) aus der API-Antwort
        if "title" in data and "@value" in data["title"]:
            return data["title"]["@value"]
        
        return None
    
    except Exception as e:
        print(f"Fehler beim Abruf der Beschreibung für Code {code}: {str(e)}")
        return None

@retry(wait=wait_random_exponential(min=1, max=40), stop=stop_after_attempt(3))
def get_icd11_description(code):
    """
    Ruft die Beschreibung für einen ICD-11 Code ab
    
    :param code: Der ICD-11 Code (z.B. 'MG22')
    :return: Die Beschreibung des Codes oder None im Fehlerfall
    """
    token = get_icd_access_token()
    if not token:
        return None
    
    api_url = f"https://id.who.int/icd/release/11/2025-01/mms/describe"
    params = {
        'code': code,
        'simplify': 'false',
        'flexiblemode': 'false',
        'convertToTerminalCodes': 'false'
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "en",
        "API-Version": "v2"
    }
    
    try:
        response = requests.get(api_url, headers=headers, params=params)
        
        if response.status_code != 200:
            print(f"Fehler beim API-Aufruf für Code {code}: {response.status_code}")
            return None
        
        data = response.json()
        # Extrahiere das Label (Hauptbeschreibung) aus der API-Antwort
        if "label" in data:
            return data["label"]
        
        return None
    
    except Exception as e:
        print(f"Fehler beim Abruf der Beschreibung für Code {code}: {str(e)}")
        return None

def update_medical_code_description(medical_code):
    """
    Aktualisiert die Beschreibung eines Medical Code Eintrags
    
    :param medical_code: MedicalCode Objekt
    :return: True wenn erfolgreich, False sonst
    """
    try:
        if medical_code.code_type == 'ICD10':
            description = get_icd10_description(medical_code.code)
        elif medical_code.code_type == 'ICD11':
            description = get_icd11_description(medical_code.code)
        else:
            # Für andere Code-Typen (z.B. OPS)
            logger.info(f"Code-Typ {medical_code.code_type} wird nicht unterstützt für Beschreibungsabfrage")
            return False
            
        if description:
            # App-Context für Celery-Tasks erstellen
            try:
                from app import app
                with app.app_context():
                    medical_code.description = description
                    db.session.commit()
                    return True
            except Exception as app_ctx_exc:
                logger.error(f"App context error for code {medical_code.code}: {app_ctx_exc}")
                # Fallback: Versuche ohne App-Context (falls im Flask-Request)
                medical_code.description = description
                db.session.commit()
                return True
        
        logger.warning(f"Keine Beschreibung gefunden für Code {medical_code.code} ({medical_code.code_type})")
        return False
    except Exception as e:
        logger.exception(f"Fehler beim Update der Beschreibung für Code {medical_code.code}: {str(e)}")
        try:
            from app import app
            with app.app_context():
                db.session.rollback()
        except:
            db.session.rollback()
        return False