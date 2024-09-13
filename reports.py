import os
import json
import pandas as pd
from utils import repair_json
from openai import OpenAI
from datetime import datetime
import google.generativeai as genai
import logging

# Konfiguration des Loggings
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Konfiguration der OpenAI- und Google Gemini-Clients
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
openai_model = os.environ["OPENAI_MODEL"]

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
gemini_model = genai.GenerativeModel(os.environ["GEMINI_MODEL"])

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
def generate_report_gpt4(prompt_template, health_record_text, year):
    """
    Generiert einen Gesundheitsbericht für ein bestimmtes Jahr mit GPT-4.
    """
    logger.info(f"Erstelle Bericht für Jahr {year} mit GPT-4.")
    
    # Extrahiere den Ausgabetyp aus der ersten Zeile des Prompts
    output_type = prompt_template.split('\n')[0].strip().lower()

    # Entferne die erste Zeile aus dem Prompt
    actual_prompt = '\n'.join(prompt_template.split('\n')[1:]).strip()

    # Passe den Gesundheitstext für das Jahr an
    year_focussed_actual_prompt = f"{actual_prompt}\n\nFokussiere dich nur auf das Jahr {year}:"

    # Basis-Parameter für die API-Anfrage
    api_params = {
        "model": openai_model,
        "messages": [
            {"role": "system", "content": year_focussed_actual_prompt},
            {"role": "user", "content": f"Das ist deine Datenbasis: {health_record_text}"}
        ],
        "temperature": 0.7,
        "max_tokens": 16000
    }

    # Generiere den Report mit GPT-4
    try:
        response = openai_client.chat.completions.create(**api_params)
        response_content = response.choices[0].message.content.strip()
        logger.info(f"GPT-4 API-Antwort erhalten: {response_content[:100]}...")  # Nur die ersten 100 Zeichen loggen

        # Bereinige die Antwort von Markdown-Syntax, falls vorhanden
        response_content = clean_json_response(response_content)

        # Parsen des JSON
        try:
            json_data = json.loads(response_content)
            logger.info(f"GPT-4 JSON erfolgreich geparst für Jahr {year}.")
        except json.JSONDecodeError as json_err:
            logger.error(f"Fehler beim Parsen des JSON von GPT-4 für Jahr {year}: {json_err}")
            return None

        # Extrahieren der Behandlungen aus dem 'Bericht' Schlüssel
        behandlungen = json_data.get('Bericht', [])

        # Bereinigung von Whitespaces
        for behandlung in behandlungen:
            for key, value in behandlung.items():
                if isinstance(value, str):
                    behandlung[key] = value.strip()

        # Konvertiere in DataFrame für einfache Sortierung
        df = pd.DataFrame(behandlungen)

        # Sortiere nach Datum der Behandlung
        df['Datum'] = pd.to_datetime(df['Datum'])
        df_sorted = df.sort_values('Datum')

        return df_sorted

    except Exception as e:
        logger.error(f"Fehler bei der Verarbeitung mit GPT-4: {e}")
        return None

# Funktion für die Erstellung des Berichts mit Google Gemini
def generate_report_gemini(prompt_template, health_record_text, year):
    """
    Generiert einen Gesundheitsbericht für ein bestimmtes Jahr mit Google Gemini.
    """
    logger.info(f"Erstelle Bericht für Jahr {year} mit Google Gemini.")
    
    # Bereite den Prompt vor
    year_focussed_actual_prompt = f"{prompt_template}\n\nFokussiere dich nur auf das Jahr {year}:"
    
    try:
        # API-Aufruf mit Google Gemini
        response = gemini_model.generate_content(
            f"Das ist deine Datenbasis: {health_record_text}\n\n{year_focussed_actual_prompt}",
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=8000, 
                response_mime_type="application/json"  # Gemini unterstützt größere Kontextfenster
            )
        )
        response_text = response.text.strip()
        logger.info(f"Google Gemini API-Antwort erhalten: {response_text[:100]}...")  # Nur die ersten 100 Zeichen loggen

        # Versuche, das Ergebnis als JSON zu interpretieren
        try:
            json_data = json.loads(response_text)
            logger.info(f"Google Gemini JSON erfolgreich geparst für Jahr {year}.")
        except json.JSONDecodeError as json_err:
            logger.error(f"Fehler beim Parsen des JSON von Google Gemini für Jahr {year}: {json_err}")
            return None

        # Extrahiere die Behandlungen aus dem JSON-Bericht
        behandlungen = json_data.get('Bericht', [])

        # Bereinigung von Whitespaces
        for behandlung in behandlungen:
            for key, value in behandlung.items():
                if isinstance(value, str):
                    behandlung[key] = value.strip()

        # Konvertiere in DataFrame für einfache Sortierung
        df = pd.DataFrame(behandlungen)

        # Sortiere nach Datum der Behandlung
        df['Datum'] = pd.to_datetime(df['Datum'])
        df_sorted = df.sort_values('Datum')

        return df_sorted

    except Exception as e:
        logger.error(f"Fehler bei der Verarbeitung mit Google Gemini: {e}")
        return None

# Hauptfunktion zur Generierung des Berichts
def generate_report(prompt_template, health_record_text, health_record_token_count, health_record_begin, health_record_end):
    """
    Generiert einen kombinierten Gesundheitsbericht für den angegebenen Zeitraum (mehrere Jahre).
    Nutzt GPT-4 oder Gemini abhängig von der Tokenanzahl.
    """
    logger.info("Starte die Erstellung des kombinierten Gesundheitsberichts.")
    
    all_year_reports = []

    # Entscheide, ob GPT-4 oder Gemini verwendet wird
    use_gemini = health_record_token_count > 16000
    if use_gemini:
        logger.info(f"Verwende Google Gemini aufgrund der Token-Anzahl: {health_record_token_count}")
    else:
        logger.info(f"Verwende GPT-4 aufgrund der Token-Anzahl: {health_record_token_count}")

    # Verwandle health_record_begin und health_record_end in Jahreszahlen
    start_year = health_record_begin.year
    end_year = health_record_end.year

    # Iteriere durch jedes Jahr im angegebenen Bereich
    for year in range(start_year, end_year + 1):
        logger.info(f"Generiere Bericht für das Jahr {year}...")

        try:
            # Verwende GPT-4 oder Gemini basierend auf der Tokenanzahl
            if use_gemini:
                yearly_report = generate_report_gemini(prompt_template, health_record_text, year)
            else:
                yearly_report = generate_report_gpt4(prompt_template, health_record_text, year)

            # Wenn der Bericht erfolgreich ist, füge ihn zur Liste hinzu
            if yearly_report is not None:
                all_year_reports.append(yearly_report)
            else:
                logger.warning(f"Bericht für das Jahr {year} konnte nicht erstellt werden.")

        except Exception as e:
            logger.error(f"Fehler beim Erstellen des Berichts für Jahr {year}: {e}")
            # Fortfahren, auch wenn ein Jahr fehlschlägt

    if all_year_reports:
        # Kombiniere alle jährlichen Berichte in einem DataFrame
        combined_report = pd.concat(all_year_reports)

        # Konvertiere zurück zu JSON
        combined_report_json = combined_report.to_json(orient='records')
        logger.info("Erstellung des kombinierten Berichts abgeschlossen.")
        return combined_report_json
    else:
        logger.warning("Keine Berichte verfügbar.")
        return json.dumps({"error": "Keine Berichte verfügbar."})
