import os
import json
import pandas as pd
from utils import repair_json
from openai import OpenAI
from datetime import datetime
import google.generativeai as genai
from google.ai.generativelanguage_v1beta.types import content
import logging
import time
from tenacity import retry, stop_after_attempt, wait_exponential
import httpx

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

# Konfiguration der OpenAI- und Google Gemini-Clients
openai_client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
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
openai_model = os.environ["OPENAI_MODEL"]
token_threshold = int(os.environ["TOKEN_THRESHOLD"])

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
gemini_model = genai.GenerativeModel(model_name=os.environ["GEMINI_MODEL"])


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

# Funktion für die Erstellung des Berichts mit GPT-4
def generate_report_gpt4(output_format, example_structure, system_prompt, prompt, health_record_text, year, health_record_custom_instructions, use_custom_instructions):
    """
    Generiert einen Gesundheitsbericht für ein bestimmtes Jahr mit GPT-4.
    """
    logger.info(f"Erstelle Bericht für Jahr {year} mit GPT-4.")
    
    if not use_custom_instructions:
        year_focussed_actual_prompt = (
        f"Follow this role: {system_prompt}\n\n"
        f"Follow this task: {prompt}\n\n"
        f"You give your output in this format: {output_format}\n\n"
        f"Extremely important: Create a report for and only contain data for the year {year}.\n\n"
    )
    else:
        year_focussed_actual_prompt = (
        f"Follow this role: {system_prompt}\n\n"
        f"Follow this task: {prompt}\n\n"
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
        "max_tokens": 16000,
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

# Funktion für die Erstellung des Berichts mit Google Gemini
def generate_report_gemini(output_format, example_structure, system_prompt, prompt, health_record_text, year, health_record_custom_instructions, use_custom_instructions):
    """
    Generiert einen Gesundheitsbericht für ein bestimmtes Jahr mit Google Gemini.
    """
    logger.info(f"Erstelle Bericht für Jahr {year} mit Google Gemini.")
    
    if not use_custom_instructions:
        year_focussed_actual_prompt = (
        f"Follow this role: {system_prompt}\n\n"
        f"Follow this task: {prompt}\n\n"
        f"You give your output in this format: {output_format}\n\n"
        f"Extremely important: Create a report for and only contain data for the year {year}.\n\n"
    )
    else:
        year_focussed_actual_prompt = (
        f"Follow this role: {system_prompt}\n\n"
        f"Follow this task: {prompt}\n\n"
        f"You give your output in this format: {output_format}\n\n"
        f"Extremely important: Create a report for and only contain data for the year {year}.\n\n"
        f"Consider the following additional important information for the analysis of the dataset: {health_record_custom_instructions}\n\n"
    )


    logger.info(f"Gemini Prompt: {year_focussed_actual_prompt}")
    logger.info(f"Gemini Model: {os.environ['GEMINI_MODEL']}")


    
    try:
        if output_format.lower() == "json":
            # Strukturiertes JSON Output Schema
            generation_config = {
                "temperature": 0.2,
                "max_output_tokens": 8192,
                "response_schema": content.Schema(
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
                                    "Datum": content.Schema(
                                        type=content.Type.STRING,
                                    ),
                                    "Code": content.Schema(
                                        type=content.Type.STRING,
                                    ),
                                    "Diagnose": content.Schema(
                                        type=content.Type.STRING,
                                    ),
                                    "Beschreibung": content.Schema(
                                        type=content.Type.STRING,
                                    ),
                                    "Arzt": content.Schema(
                                        type=content.Type.STRING,
                                    ),
                                },
                            ),
                        ),
                    },
                ),
                "response_mime_type": "application/json",
            }
        else:
            # Einfache Konfiguration für Text-Output
            generation_config = {
                "temperature": 1
            }

        gemini_model = genai.GenerativeModel(
            model_name=os.environ["GEMINI_MODEL"],
            generation_config=generation_config
        )

        chat_session = gemini_model.start_chat(
            history=[]
        )

        response = chat_session.send_message(
            f"{year_focussed_actual_prompt}\n\nDas ist deine Datenbasis: {health_record_text}",
        )
        response_text = response.text.strip()
        logger.info(f"Google Gemini API-Antwort erhalten: {response_text[:100]}...")

        if output_format.lower() == "json":
            try:
                json_data = json.loads(response_text)
                logger.info(f"Google Gemini JSON erfolgreich geparst für Jahr {year}.")
                
                # Extrahiere die Daten direkt aus dem "Bericht" Schlüssel
                data = json_data.get("Bericht", [])
                
                # Bereinigung von Whitespaces
                for item in data:
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

# Hauptfunktion zur Generierung des Berichts
def generate_report(template_name, output_format, example_structure, system_prompt, prompt, health_record_text, health_record_token_count, health_record_begin, health_record_end, health_record_custom_instructions, use_custom_instructions):
    """
    Generiert einen kombinierten Gesundheitsbericht für den angegebenen Zeitraum (mehrere Jahre).
    """
    logger.info(f"Starte Berichterstellung für Zeitraum {health_record_begin.year}-{health_record_end.year}")
    
    all_year_reports = []
    text_reports = []

    # Entscheide, ob GPT-4 oder Gemini verwendet wird
    use_gemini = health_record_token_count > token_threshold
    if use_gemini:
        logger.info(f"Verwende Google Gemini aufgrund der Token-Anzahl: {health_record_token_count}")
    else:
        logger.info(f"Verwende GPT-4 aufgrund der Token-Anzahl: {health_record_token_count}")

    # Verwandle health_record_begin und health_record_end in Jahreszahlen
    start_year = health_record_begin.year
    end_year = health_record_end.year
    logger.info(f"Verarbeite Jahre von {start_year} bis {end_year}")

    for year in range(start_year, end_year + 1):
        logger.info(f"Generiere Bericht für das Jahr {year}...")

        try:
            if use_gemini:
                yearly_report = generate_report_gemini(
                    output_format, example_structure, system_prompt, prompt, health_record_text, year, health_record_custom_instructions, use_custom_instructions
                )
            else:
                yearly_report = generate_report_gpt4(
                    output_format, example_structure, system_prompt, prompt, health_record_text, year, health_record_custom_instructions, use_custom_instructions
                )

            if yearly_report is not None:
                if output_format.lower() == "json":
                    logger.info(f"Jahr {year}: JSON-Report erfolgreich generiert")
                    logger.debug(f"Jahr {year} Report Inhalt: {yearly_report.to_dict('records')[:2]}...")  # Zeige die ersten 2 Einträge
                    all_year_reports.append(yearly_report)
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
                    final_report = process_combined_text_gpt4(
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

def process_combined_text_gpt4(template_name, output_format, example_structure, system_prompt, prompt, combined_text_report):
    """
    Verarbeitet den kombinierten Textbericht mit GPT-4, um eine Gesamtauswertung zu erhalten.
    """
    logger.info("Verarbeite den kombinierten Textbericht mit GPT-4.")

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
        "max_tokens": 16000,
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
            max_output_tokens=8000,
        )

        response = gemini_model.generate_content(
            f"{final_system_prompt}\n\n{final_prompt}",
            generation_config=generation_config
        )
        response_text = response.text.strip()
        logger.info("Verarbeitung mit Google Gemini abgeschlossen.")
        return response_text
    except Exception as e:
        logger.error(f"Fehler bei der Verarbeitung mit Google Gemini: {e}")
        return combined_text_report
