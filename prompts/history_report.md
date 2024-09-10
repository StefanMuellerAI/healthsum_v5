JSON
Erstelle bitte einen Bericht, der alle wichtigen Ereignisse und Entwicklungen in der Geschichte des Patienten zusammenfasst als Json-Objekt. Bitte gib nur das Json-Objekt zurück und nichts anderes. Das Json-Objekt muss fehlerfrei sein. Das Json-Objekt soll die folgenden Schlüssel enthalten:
- Datum (YYYY-MM-DD)
- Code (ICD-10/11 Code)
- Diagnose (Beschreibung des Codes)
- Beschreibungen (Beschreibung der Diagnose)
- Arzt (Name des Arztes)

Bitte denke dir keine Informationen aus, die nicht im Datensatz enthalten sind.

Beispiel: 

{
  "Bericht": [
    {
        "Datum": "2021-03-15",
        "Code": "I10",
        "Diagnose: "Essentielle (primäre) Hypertonie",
        "Beschreibung": "Verschreibung von blutdrucksenkenden Medikamenten und Beratung zu Ernährungsumstellung und Bewegung.",
        "Arzt": "Dr. Johannes Meier"
    }
  ]
}
