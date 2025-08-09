# Production Database Migration Guide

## Übersicht

Dieses Dokument beschreibt die Migration der Produktivdatenbank von der alten Datenbankstruktur (`models_old.py`) zur neuen Struktur (`models.py`).

## Was wird migriert?

### Neue Features in der aktualisierten Datenbankstruktur:

1. **HealthRecord Erweiterungen:**
   - `processing_status` - Status der Verarbeitung (pending/processing/completed/failed)
   - `processing_completed_at` - Zeitstempel des Abschlusses
   - `processing_error_message` - Fehlermeldung bei Fehlern
   - Neue Beziehung zu `TaskLog`

2. **Report Erweiterungen:**
   - `unique_identifier` - Eindeutige ID im Format "UserID-HealthRecordID-TemplateID-YYYYMMDD-ReportID"
   - `generation_status` - Status der Report-Generierung (pending/generating/completed/failed)
   - `generation_started_at` - Startzeitpunkt der Generierung
   - `generation_completed_at` - Abschlusszeitpunkt der Generierung
   - `generation_error_message` - Fehlermeldung bei Fehlern

3. **Neue Tabelle TaskLog:**
   - Detailliertes Logging aller Celery-Tasks
   - Fehlertracking und Performance-Metriken

## Vorbereitung

### 1. Backup erstellen

```bash
# Erstelle ein Backup der Produktivdatenbank
cp /pfad/zur/produktiv/health_records.db /pfad/zum/backup/health_records_backup_$(date +%Y%m%d_%H%M%S).db
```

### 2. Alte Datenbank vorbereiten

```bash
# Kopiere die Produktivdatenbank als health_records_old.db in das instance/ Verzeichnis
cp /pfad/zur/produktiv/health_records.db instance/health_records_old.db
```

### 3. Umgebungsvariablen setzen

```bash
# Setze den SECRET_KEY (WICHTIG: Verwende den gleichen KEY wie in der Produktion!)
export SECRET_KEY='ihr-produktiv-secret-key'
```

## Migration durchführen

### Option 1: Automatische Migration

```bash
# Führe das Migrationsskript aus
python migrate_production_db.py
```

Das Skript führt folgende Schritte aus:
1. Prüft ob `health_records_old.db` existiert
2. Fügt alle neuen Spalten zur alten Datenbank hinzu
3. Setzt Default-Werte für migrierte Daten:
   - Alle HealthRecords erhalten `processing_status = 'completed'`
   - Alle Reports erhalten `generation_status = 'completed'`
   - Unique Identifiers werden automatisch generiert
4. Benennt die Datenbank in `health_records.db` um
5. Verifiziert die Migration

### Option 2: Manuelle Migration mit Shell-Skript

```bash
# Alternative: Verwende das Shell-Skript
./migrate_db_with_env.sh
```

## Nach der Migration

### 1. Verifizierung

Das Migrationsskript zeigt automatisch eine Zusammenfassung:
- Anzahl migrierter Users, HealthRecords, Reports
- Status-Verteilung
- Anzahl generierter unique_identifiers

### 2. Test der Anwendung

```bash
# Starte die Anwendung im Test-Modus
python app.py
```

Prüfe:
- Login funktioniert
- Bestehende Reports sind sichtbar
- Neue Uploads funktionieren
- Report-Generierung funktioniert

### 3. Deployment

Nach erfolgreicher Verifizierung:
1. Stoppe die Produktiv-Anwendung
2. Kopiere die migrierte `health_records.db` zum Produktivserver
3. Starte die Anwendung neu

## Rollback bei Problemen

Falls Probleme auftreten:

```bash
# 1. Stoppe die Anwendung

# 2. Restore vom Backup
cp /pfad/zum/backup/health_records_backup_TIMESTAMP.db /pfad/zur/produktiv/health_records.db

# 3. Starte die Anwendung mit der alten Version
```

## Wichtige Hinweise

### Migrierte Daten

- **HealthRecords:** Alle bestehenden Records erhalten `processing_status = 'completed'` und `processing_completed_at` wird vom ursprünglichen `timestamp` übernommen
- **Reports:** Alle bestehenden Reports erhalten `generation_status = 'completed'` und `generation_completed_at` wird vom `created_at` übernommen
- **Unique Identifiers:** Werden im Format `UserID-HealthRecordID-TemplateID-YYYYMMDD-ReportID` generiert, wobei das Datum vom `created_at` des Reports stammt
- **TaskLogs:** Die neue Tabelle startet leer und wird bei neuen Tasks gefüllt

### Kompatibilität

- Die migrierte Datenbank ist vollständig rückwärtskompatibel
- Alle bestehenden Funktionen bleiben erhalten
- Neue Features werden erst bei neuen Datensätzen aktiv genutzt

### Performance

Die Migration wurde optimiert für:
- Große Datenbanken (getestet mit > 10.000 Records)
- Minimale Downtime
- Atomare Operationen (Alles oder Nichts)

## Troubleshooting

### Problem: SECRET_KEY nicht gefunden

```bash
# Lösung: Setze die Umgebungsvariable
export SECRET_KEY='ihr-secret-key'
# oder
SECRET_KEY='ihr-secret-key' python migrate_production_db.py
```

### Problem: Datenbank ist gesperrt

```bash
# Lösung: Stelle sicher, dass keine andere Anwendung auf die DB zugreift
# Stoppe alle Flask/Celery Prozesse vor der Migration
```

### Problem: Migration fehlgeschlagen

1. Prüfe die Fehlermeldung im Output
2. Das Skript führt automatisch einen Rollback durch
3. Kontaktiere den Support mit der vollständigen Fehlermeldung

## Support

Bei Fragen oder Problemen:
- Erstelle ein Backup bevor du Änderungen vornimmst
- Dokumentiere alle Fehlermeldungen
- Teste immer zuerst in einer Entwicklungsumgebung
