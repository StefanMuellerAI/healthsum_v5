"""
Test-Script für Azure Key Vault Integration

Prüft ob alle Konfigurationswerte korrekt aus dem Key Vault geladen werden.
"""
import os
import sys

def test_config():
    """Testet die Key Vault Konfiguration"""
    print("=" * 60)
    print("Azure Key Vault Konfigurations-Test")
    print("=" * 60)
    
    # Prüfe ENVIRONMENT
    environment = os.getenv('ENVIRONMENT', 'nicht gesetzt')
    print(f"\n1. Umgebung: {environment}")
    
    if environment not in ['test', 'prod']:
        print("   ⚠️  WARNUNG: ENVIRONMENT sollte 'test' oder 'prod' sein")
        print("   💡 Setzen Sie: export ENVIRONMENT=test")
    else:
        print(f"   ✅ Umgebung ist korrekt auf '{environment}' gesetzt")
    
    # Versuche Config zu laden
    print("\n2. Lade Konfiguration aus Key Vault...")
    try:
        from config import config, get_config
        print("   ✅ Config-Modul erfolgreich importiert")
    except Exception as e:
        print(f"   ❌ Fehler beim Import: {e}")
        return False
    
    # Prüfe kritische Keys
    print("\n3. Prüfe kritische Konfigurationswerte...")
    critical_keys = [
        'SECRET_KEY',
        'MAIL_SERVER',
        'MAIL_USERNAME',
        'OPENAI_API_KEY',
        'AZURE_KEY_CREDENTIALS',
        'GEMINI_API_KEY'
    ]
    
    all_ok = True
    for key in critical_keys:
        value = get_config(key)
        if value:
            # Zeige nur die ersten und letzten 4 Zeichen
            if len(value) > 8:
                masked = f"{value[:4]}...{value[-4:]}"
            else:
                masked = "***"
            print(f"   ✅ {key}: {masked}")
        else:
            print(f"   ❌ {key}: NICHT GEFUNDEN")
            all_ok = False
    
    # Zeige alle geladenen Keys
    print("\n4. Alle geladenen Konfigurationswerte:")
    all_config = config.get_all()
    for key in sorted(all_config.keys()):
        print(f"   - {key}")
    
    print(f"\n   📊 Gesamt: {len(all_config)} Werte geladen")
    
    # Prüfe Azure Authentifizierung
    print("\n5. Azure Authentifizierung:")
    azure_client_id = os.getenv('AZURE_CLIENT_ID')
    azure_tenant_id = os.getenv('AZURE_TENANT_ID')
    
    if azure_client_id and azure_tenant_id:
        print("   ✅ Service Principal Credentials gefunden")
        print(f"      Client ID: {azure_client_id[:8]}...")
        print(f"      Tenant ID: {azure_tenant_id[:8]}...")
    else:
        print("   ℹ️  Keine Service Principal Credentials")
        print("   💡 Vermutlich wird Managed Identity verwendet (gut!)")
    
    # Zusammenfassung
    print("\n" + "=" * 60)
    if all_ok:
        print("✅ ERFOLG: Konfiguration wurde erfolgreich geladen!")
        print(f"   Umgebung: {environment}")
        print(f"   Key Vault: https://healthsum-vault.vault.azure.net/")
        print(f"   Secret: healthsum-{environment}")
    else:
        print("❌ FEHLER: Einige Konfigurationswerte fehlen!")
        print("\n💡 Mögliche Lösungen:")
        print("   1. Prüfen Sie das Secret im Azure Portal")
        print("   2. Prüfen Sie die Access Policy im Key Vault")
        print("   3. Für lokale Entwicklung: az login")
    print("=" * 60)
    
    return all_ok

if __name__ == '__main__':
    try:
        success = test_config()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ Unerwarteter Fehler: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

