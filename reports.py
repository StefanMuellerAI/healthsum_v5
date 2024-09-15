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
def generate_report_gpt4(template_name, output_format, example_structure, system_prompt, prompt, health_record_text, year):
    """
    Generiert einen Gesundheitsbericht für ein bestimmtes Jahr mit GPT-4.
    """
    logger.info(f"Erstelle Bericht für Jahr {year} mit GPT-4.")
    
    year_focussed_actual_prompt = (
        f"System Prompt: {system_prompt}\n\n"
        f"Prompt: {prompt}\n\n"
        f"Output Format: {output_format}\n\n"
        f"Beispielstruktur: {example_structure}\n"
        f"Fokussiere dich nur auf das Jahr {year}:"
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

    # Füge response_format hinzu, wenn das Ausgabeformat JSON ist
    if output_format.lower() == "json":
        api_params["response_format"] = {"type": "json_object"}

    # Generiere den Report mit GPT-4
    try:
        response = openai_client.chat.completions.create(**api_params)
        response_content = response.choices[0].message.content.strip()
        logger.info(f"GPT-4 API-Antwort erhalten: {response_content[:100]}...")

        if output_format.lower() == "json":
            # Bereinige die Antwort von Markdown-Syntax, falls vorhanden
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
def generate_report_gemini(template_name, output_format, example_structure, system_prompt, prompt, health_record_text, year):
    """
    Generiert einen Gesundheitsbericht für ein bestimmtes Jahr mit Google Gemini.
    """
    logger.info(f"Erstelle Bericht für Jahr {year} mit Google Gemini.")
    
    year_focussed_actual_prompt = (
        f"System Prompt: {system_prompt}\n\n"
        f"Prompt: {prompt}\n\n"
        f"Output Format: {output_format}\n\n"
        f"Beispielstruktur: {example_structure}\n"
        f"Fokussiere dich nur auf das Jahr {year}:"
    )
    
    try:
        # Generationskonfiguration
        generation_config = genai.types.GenerationConfig(
            max_output_tokens=8000,
        )

        # Füge response_mime_type hinzu, wenn das Ausgabeformat JSON ist
        if output_format.lower() == "json":
            generation_config.response_mime_type = "application/json"

        # API-Aufruf mit Google Gemini
        response = gemini_model.generate_content(
            f"Das ist deine Datenbasis: {health_record_text}\n\n{year_focussed_actual_prompt}",
            generation_config=generation_config
        )
        response_text = response.text.strip()
        logger.info(f"Google Gemini API-Antwort erhalten: {response_text[:100]}...")

        if output_format.lower() == "json":
            # Extrahiere den ersten Schlüssel aus der Beispielstruktur
            example_structure_json = json.loads(example_structure)
            first_key = next(iter(example_structure_json.keys()))

            # Versuche, das Ergebnis als JSON zu interpretieren
            try:
                json_data = json.loads(response_text)
                logger.info(f"Google Gemini JSON erfolgreich geparst für Jahr {year}.")
            except json.JSONDecodeError as json_err:
                logger.error(f"Fehler beim Parsen des JSON von Google Gemini für Jahr {year}: {json_err}")
                return None

            # Extrahiere die Daten aus dem ersten Schlüssel
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
            return response_text

    except Exception as e:
        logger.error(f"Fehler bei der Verarbeitung mit Google Gemini: {e}")
        return None

# Hauptfunktion zur Generierung des Berichts
def generate_report(template_name, output_format, example_structure, system_prompt, prompt, health_record_text, health_record_token_count, health_record_begin, health_record_end):
    """
    Generiert einen kombinierten Gesundheitsbericht für den angegebenen Zeitraum (mehrere Jahre).
    Nutzt GPT-4 oder Gemini abhängig von der Tokenanzahl.
    """
    logger.info("Starte die Erstellung des kombinierten Gesundheitsberichts.")
    
    all_year_reports = []
    text_reports = []

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
                yearly_report = generate_report_gemini(
                    template_name, output_format, example_structure, system_prompt, prompt, health_record_text, year
                )
            else:
                yearly_report = generate_report_gpt4(
                    template_name, output_format, example_structure, system_prompt, prompt, health_record_text, year
                )

            # Wenn der Bericht erfolgreich ist, füge ihn zur entsprechenden Liste hinzu
            if yearly_report is not None:
                if output_format.lower() == "json":
                    all_year_reports.append(yearly_report)
                else:
                    # Für Textberichte
                    text_reports.append(f"Bericht für Jahr {year}:\n{yearly_report}\n")
            else:
                logger.warning(f"Bericht für das Jahr {year} konnte nicht erstellt werden.")

        except Exception as e:
            logger.error(f"Fehler beim Erstellen des Berichts für Jahr {year}: {e}")
            # Fortfahren, auch wenn ein Jahr fehlschlägt

    if output_format.lower() == "json" and all_year_reports:
        # Kombiniere alle jährlichen Berichte in einem DataFrame
        combined_report = pd.concat(all_year_reports, ignore_index=True)

        # Konvertiere zurück zu JSON
        combined_report_json = combined_report.to_json(orient='records', force_ascii=False)
        logger.info("Erstellung des kombinierten Berichts abgeschlossen.")
        return combined_report_json

    elif output_format.lower() != "json" and text_reports:
        # Kombiniere alle Textberichte
        combined_text_report = "\n".join(text_reports)
        logger.info("Erstellung des kombinierten Textberichts abgeschlossen.")

        # Führe zusätzliche Verarbeitung des kombinierten Berichts durch
        logger.info("Starte zusätzliche Verarbeitung des kombinierten Textberichts.")

        try:
            if use_gemini:
                final_report = process_combined_text_gemini(
                    template_name, output_format, example_structure, system_prompt, prompt, combined_text_report
                )
            else:
                final_report = process_combined_text_gpt4(
                    template_name, output_format, example_structure, system_prompt, prompt, combined_text_report
                )
            logger.info("Zusätzliche Verarbeitung des kombinierten Textberichts abgeschlossen.")
            return final_report
        except Exception as e:
            logger.error(f"Fehler bei der zusätzlichen Verarbeitung des kombinierten Textberichts: {e}")
            return combined_text_report

    else:
        logger.warning("Keine Berichte verfügbar.")
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
        response = openai_client.chat.completions.create(**api_params)
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
