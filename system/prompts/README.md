# System Prompts Ordner

Dieser Ordner enthält PDF-Dateien, die als Kontext für die AI-Report-Generierung verwendet werden.

## Verwendung

Die PDF-Dateien in diesem Ordner können in den Report-Templates als `system_pdf_filename` hinterlegt werden. 
Das AI-Modell (Gemini) erhält dann sowohl den konfigurierten Prompt als auch den Inhalt der PDF-Datei als Kontext.

## Beispiele für PDF-Dateien

- `medical_guidelines_2024.pdf` - Medizinische Leitlinien
- `icd_classification.pdf` - ICD-Klassifikation
- `diagnosis_standards.pdf` - Diagnose-Standards

## Hinweise

- PDF-Dateien sollten strukturiert und maschinenlesbar sein
- Maximale Dateigröße beachten (Gemini-Limits)
- Dateinamen ohne Leerzeichen verwenden
- Nur vertrauenswürdige Quellen verwenden 