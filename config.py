"""
Zentrale Konfiguration mit Azure Key Vault Integration

Lädt alle Konfigurationswerte aus Azure Key Vault basierend auf der 
Umgebung (test oder prod), die in der .env Datei festgelegt ist.
"""
import os
import json
import logging
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from dotenv import load_dotenv

# Lade .env nur für ENVIRONMENT
load_dotenv()

logger = logging.getLogger(__name__)

class Config:
    """Singleton für zentrale Konfiguration mit Azure Key Vault"""
    
    _instance = None
    _config = {}
    _loaded = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(Config, cls).__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not self._loaded:
            self._load_config()
            self._loaded = True
    
    def _load_config(self):
        """Lädt Konfiguration aus Azure Key Vault"""
        try:
            # Umgebung bestimmen (test oder prod)
            environment = os.getenv('ENVIRONMENT', 'test').lower()
            logger.info(f"🔧 Loading configuration for environment: {environment}")
            
            # Key Vault Setup
            vault_url = "https://healthsum-vault.vault.azure.net/"
            secret_name = f"healthsum-{environment}"
            
            # DefaultAzureCredential versucht automatisch:
            # 1. Umgebungsvariablen (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)
            # 2. Managed Identity (auf Azure VM)
            # 3. Azure CLI (lokal)
            # 4. Visual Studio Code
            credential = DefaultAzureCredential()
            client = SecretClient(vault_url=vault_url, credential=credential)
            
            # Lade Secret
            logger.info(f"🔐 Fetching secret '{secret_name}' from Key Vault...")
            secret = client.get_secret(secret_name)
            
            # Parse JSON
            self._config = json.loads(secret.value)
            logger.info(f"✅ Successfully loaded {len(self._config)} configuration values from Key Vault")
            
            # Validiere kritische Werte
            required_keys = [
                'SECRET_KEY', 
                'MAIL_SERVER', 
                'OPENAI_API_KEY', 
                'AZURE_KEY_CREDENTIALS'
            ]
            missing_keys = [key for key in required_keys if key not in self._config]
            if missing_keys:
                raise ValueError(f"Missing required configuration keys: {missing_keys}")
            
            # Log geladene Keys (ohne Werte aus Sicherheitsgründen)
            logger.info(f"📋 Loaded configuration keys: {', '.join(self._config.keys())}")
            
        except Exception as e:
            logger.error(f"❌ Failed to load configuration from Key Vault: {e}")
            logger.warning("⚠️  Falling back to environment variables")
            # Fallback auf Umgebungsvariablen
            self._load_from_env()
    
    def _load_from_env(self):
        """Fallback: Lädt Konfiguration aus Umgebungsvariablen"""
        # Alle relevanten Keys
        keys = [
            'SECRET_KEY', 'MAIL_SERVER', 'MAIL_PORT', 'MAIL_USERNAME',
            'MAIL_PASSWORD', 'MAIL_USE_TLS', 'MAIL_USE_SSL',
            'OPENAI_API_KEY', 'OPENAI_MODEL',
            'AZURE_KEY_CREDENTIALS', 'GEMINI_API_KEY', 'GEMINI_MODEL'
        ]
        
        for key in keys:
            value = os.getenv(key)
            if value:
                self._config[key] = value
        
        logger.info(f"📋 Loaded {len(self._config)} values from environment variables")
    
    def get(self, key, default=None):
        """
        Holt einen Konfigurationswert
        
        Args:
            key: Der Schlüssel der Konfiguration
            default: Standardwert falls Key nicht existiert
            
        Returns:
            Der Konfigurationswert oder default
        """
        return self._config.get(key, default)
    
    def get_all(self):
        """Gibt alle Konfigurationswerte zurück (Kopie)"""
        return self._config.copy()
    
    def __getitem__(self, key):
        """Ermöglicht dict-ähnlichen Zugriff: config['KEY']"""
        if key not in self._config:
            raise KeyError(f"Configuration key '{key}' not found")
        return self._config[key]
    
    def __contains__(self, key):
        """Ermöglicht 'in' Operator: 'KEY' in config"""
        return key in self._config


# Singleton-Instanz
config = Config()

# Hilfsfunktionen für einfachen Zugriff
def get_config(key, default=None):
    """
    Holt einen Konfigurationswert aus Azure Key Vault
    
    Args:
        key: Der Schlüssel der Konfiguration
        default: Standardwert falls Key nicht existiert
        
    Returns:
        Der Konfigurationswert oder default
    """
    return config.get(key, default)

def get_all_config():
    """Gibt alle Konfigurationswerte zurück"""
    return config.get_all()

