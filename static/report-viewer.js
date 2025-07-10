$(document).ready(function() {
    console.log("Report-viewer.js loaded");
    
    // Lade Report-Daten aus den window-Variablen (viel zuverlässiger)
    var reportData = window.reportData;
    var reportMeta = window.reportMeta;
    
    console.log("Window reportData:", reportData);
    console.log("Window reportMeta:", reportMeta);
    
    // Check if report data exists
    if (!reportData) {
        console.log("No report data available");
        $('#reportTable').html('<p class="text-lg text-yellow-600">Report-Inhalt ist leer. Bitte prüfen Sie, ob der Report korrekt generiert wurde.</p>');
        return;
    }
    
    console.log("Report data type:", typeof reportData);
    console.log("Is array:", Array.isArray(reportData));
    
    // Parse JSON string if needed
    if (typeof reportData === 'string') {
        try {
            reportData = JSON.parse(reportData);
            console.log("Parsed string to object/array:", reportData);
        } catch (e) {
            console.error("Failed to parse reportData string:", e);
            $('#reportTable').html('<p class="text-lg text-red-600">Fehler beim Parsen der Report-Daten.</p>');
            return;
        }
    }
    
    // Handle legacy data structure
    if (!Array.isArray(reportData) && reportData.Behandlungen) {
        reportData = reportData.Behandlungen;
        console.log("Using Behandlungen array:", reportData);
    }
    
    if (!reportData || !Array.isArray(reportData) || reportData.length === 0) {
        console.log("No valid report data found");
        $('#reportTable').html('<p class="text-lg text-gray-600">Keine Daten verfügbar oder Fehler beim Laden.</p>');
        return;
    }
    
    console.log("Report data entries:", reportData.length);
    console.log("First entry:", reportData[0]);
    
    // **NEUE ANFORDERUNG: Spaltenreihenfolge direkt aus den Daten nehmen**
    // Verwende die Reihenfolge der Eigenschaften aus dem ersten Objekt
    var columnOrder = Object.keys(reportData[0]);
    
    console.log("Column order taken directly from data:", columnOrder);
    
    var columns = columnOrder.map(function(key, index) {
        var columnConfig = {
            title: key,
            data: key
        };
        
        // Spezielle Behandlung für Datum-Spalte: Japanisches Format (YYYY-MM-DD)
        if (key === 'Datum') {
            columnConfig.render = function(data, type, row) {
                if (type === 'sort' || type === 'type') {
                    return data;
                }
                var date = new Date(data);
                if (!isNaN(date.getTime())) {
                    var year = date.getFullYear();
                    var month = (date.getMonth() + 1).toString().padStart(2, '0');
                    var day = date.getDate().toString().padStart(2, '0');
                    return year + '-' + month + '-' + day;  // Japanisches Format: YYYY-MM-DD
                }
                return data;
            };
        }
        
        return columnConfig;
    });

    // Hole Metadaten für Export aus window.reportMeta
    var patientName = (reportMeta && reportMeta.patientName) || 'Unbekannt';
    var createdAt = (reportMeta && reportMeta.createdAt) || new Date().toISOString().split('T')[0];

    console.log("Creating DataTable with", columns.length, "columns");

    try {
        $('#reportTable').DataTable({
            data: reportData,
            columns: columns,
            columnDefs: [
                // Erzwinge die exakte Spalten-Reihenfolge aus den Daten
                {
                    targets: '_all',
                    orderable: true
                }
            ],
            order: [[0, 'desc']],
            paging: false,
            info: false,
            scrollX: true,
            language: {
                url: '//cdn.datatables.net/plug-ins/1.10.24/i18n/German.json'
            },
            dom: 'Bfrt',
            buttons: [
                {
                    extend: 'excel',
                    text: 'Als Excel herunterladen',
                    className: 'bg-green-500 hover:bg-green-700 text-white font-bold py-2 px-4 rounded mt-4',
                    filename: 'Report_' + patientName + '_' + createdAt,
                    title: 'Report für ' + patientName + ' vom ' + createdAt
                }
            ],
            initComplete: function() {
                console.log("DataTable initialized successfully");
                var actualHeaders = $('#reportTable thead th').map(function() { 
                    return $(this).text(); 
                }).get();
                console.log("Actual column headers:", actualHeaders);
            }
        });
    } catch (e) {
        console.error("Error creating DataTable:", e);
        $('#reportTable').html(`
            <div class="bg-red-50 border border-red-200 rounded-lg p-4">
                <p class="text-lg text-red-600 font-bold">Fehler beim Erstellen der Tabelle</p>
                <p class="text-sm text-red-500 mt-2">DataTable Error: ${e.message}</p>
            </div>
        `);
    }
}); 