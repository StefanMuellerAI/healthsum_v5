# Migration Guide für HealthSum v5

## Übersicht

Diese Anleitung beschreibt die Migration von älteren HealthSum-Versionen auf v5 mit den neuen Status-Tracking-Features.

## Neue Features in v5

### 1. Status-Tracking für HealthRecords
- `processing_status`: 'pending', 'processing', 'completed', 'failed'
- `processing_completed_at`: Zeitstempel wann die Verarbeitung abgeschlossen wurde
- `processing_error_message`: Fehlermeldung bei fehlgeschlagener Verarbeitung

### 2. Status-Tracking für Reports
- `generation_status`: 'pending', 'generating', 'completed', 'failed'
- `generation_started_at`: Zeitstempel wann die Generierung gestartet wurde
- `generation_completed_at`: Zeitstempel wann die Generierung abgeschlossen wurde
- `generation_error_message`: Fehlermeldung bei fehlgeschlagener Generierung
- `unique_identifier`: Eindeutige ID im Format "UserID-HealthRecordID-TemplateID-YYYYMMDD-ReportID"

### 3. Task Logging
- Detailliertes Logging aller Celery-Tasks
- Fehlertracking mit Retry-Count
- Performance-Metriken (Dauer, Start/Ende)

## Migration durchführen

### Option 1: Vollständige Migration (empfohlen)

```bash
# 1. Backup der alten Datenbank erstellen
cp instance/health_records.db instance/health_records_old.db

# 2. Neue Datenbank-Struktur erstellen
python
>>> from app import app, db
>>> with app.app_context():
...     db.create_all()

# 3. Daten migrieren
python migrate_db.py
```

### Option 2: Status-Felder korrigieren (für teilweise migrierte DBs)

Falls die Datenbank bereits teilweise migriert wurde und nur die Status-Felder korrigiert werden müssen:

```bash
python migrate_fix_status_fields.py
```

## Wichtige Hinweise

### Migrierte Records/Reports

- Alle migrierten HealthRecords erhalten `processing_status = 'completed'`
- Alle migrierten Reports erhalten `generation_status = 'completed'`
- Eindeutige IDs für migrierte Reports: "MIGRATED-{report_id}"
- Die Timestamps werden vom ursprünglichen Erstellungsdatum übernommen

### Frontend-Kompatibilität

Das Frontend ist vollständig kompatibel mit migrierten Daten:
- Fehlende Status-Felder werden automatisch als 'completed' interpretiert
- Keine Ausrufezeichen oder Fehlerindikatoren für erfolgreich migrierte Daten
- Alle Features funktionieren wie gewohnt

### Neue Features nach Migration

Nach der Migration stehen folgende neue Features zur Verfügung:

1. **Echtzeit-Status-Tracking**
   - Live-Updates während der Verarbeitung
   - Detaillierte Fortschrittsanzeigen

2. **Fehler-Transparenz**
   - Rote Ausrufezeichen bei Fehlern
   - Detaillierte Task-Logs mit Fehlermeldungen
   - Klickbare Fehlerindikatoren

3. **Performance-Monitoring**
   - Task-Dauer und Retry-Versuche
   - Erfolgs-/Fehlerstatistiken

## Troubleshooting

### Problem: "processing_status" ist NULL

**Lösung:** Führen Sie `migrate_fix_status_fields.py` aus

### Problem: Reports haben keine unique_identifier

**Lösung:** Die Migration setzt automatisch "MIGRATED-{id}" als Identifier

### Problem: Frontend zeigt keine Status-Badges

**Ursache:** Browser-Cache
**Lösung:** Browser-Cache leeren (Strg+F5)

## Rollback

Falls nötig, können Sie zur alten Version zurückkehren:

```bash
# Backup der neuen DB
cp instance/health_records.db instance/health_records_v5_backup.db

# Alte DB wiederherstellen
cp instance/health_records_old.db instance/health_records.db
```

## Support

Bei Fragen oder Problemen wenden Sie sich bitte an das Entwicklungsteam.
