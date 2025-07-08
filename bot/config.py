#!/usr/bin/env python3
"""
Configurazione environment-based per il Bot Telegram
Gestisce automaticamente sandbox vs produzione
"""

import os
from dotenv import load_dotenv

# Carica .env se esiste
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    load_dotenv(env_path)

def get_environment():
    """Determina l'ambiente di esecuzione"""
    env = os.environ.get("ENVIRONMENT", "development").lower()
    return "production" if env == "production" else "development"

def get_telegram_config():
    """Ottiene configurazione Telegram basata sull'ambiente"""
    env = get_environment()
    
    if env == "production":
        token = os.environ.get("PROD_TOKEN_BOT")
        if not token:
            raise RuntimeError("PROD_TOKEN_BOT non impostato per ambiente di produzione!")
        return {
            "token": token,
            "env_name": "PRODUCTION 🚀"
        }
    else:
        token = os.environ.get("DEV_TOKEN_BOT")
        if not token:
            # Fallback per compatibilità
            token = os.environ.get("Token_Bot")
        if not token:
            raise RuntimeError("DEV_TOKEN_BOT non impostato per ambiente di sviluppo!")
        return {
            "token": token,
            "env_name": "DEVELOPMENT 🧪"
        }

def get_paypal_config():
    """Ottiene configurazione PayPal basata sull'ambiente"""
    env = get_environment()
    
    if env == "production":
        return {
            "client_id": os.environ.get("PROD_PAYPAL_CLIENT_ID"),
            "client_secret": os.environ.get("PROD_PAYPAL_CLIENT_SECRET"),
            "api_base": "https://api-m.paypal.com",
            "env_name": "LIVE 💰"
        }
    else:
        return {
            "client_id": os.environ.get("DEV_PAYPAL_CLIENT_ID"),
            "client_secret": os.environ.get("DEV_PAYPAL_CLIENT_SECRET"),
            "api_base": "https://api-m.sandbox.paypal.com",
            "env_name": "SANDBOX 🧪"
        }

def get_r2_config():
    """Ottiene configurazione R2 basata sull'ambiente"""
    env = get_environment()
    
    if env == "production":
        return {
            "public_base_url": os.environ.get("PROD_R2_PUBLIC_BASE_URL"),
            "access_key_id": os.environ.get("PROD_R2_ACCESS_KEY_ID"),
            "secret_access_key": os.environ.get("PROD_R2_SECRET_ACCESS_KEY"),
            "endpoint_url": os.environ.get("PROD_R2_ENDPOINT_URL"),
            "bucket_name": os.environ.get("PROD_R2_BUCKET_NAME")
        }
    else:
        return {
            "public_base_url": os.environ.get("DEV_R2_PUBLIC_BASE_URL"),
            "access_key_id": os.environ.get("DEV_R2_ACCESS_KEY_ID"),
            "secret_access_key": os.environ.get("DEV_R2_SECRET_ACCESS_KEY"),
            "endpoint_url": os.environ.get("DEV_R2_ENDPOINT_URL"),
            "bucket_name": os.environ.get("DEV_R2_BUCKET_NAME")
        }

def get_database_url():
    """Ottiene URL database basato sull'ambiente"""
    env = get_environment()
    
    if env == "production":
        url = os.environ.get("PROD_DATABASE_URL")
        if not url:
            raise RuntimeError("PROD_DATABASE_URL non impostato per ambiente di produzione!")
        return url
    else:
        url = os.environ.get("DEV_DATABASE_URL")
        if not url:
            # Fallback per compatibilità
            url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DEV_DATABASE_URL non impostato per ambiente di sviluppo!")
        return url

def get_internal_config():
    """Ottiene configurazione comunicazione interna basata sull'ambiente"""
    env = get_environment()
    
    if env == "production":
        return {
            "bot_url": os.environ.get("PROD_BOT_INTERNAL_URL"),
            "token": os.environ.get("PROD_INTERNAL_TOKEN")
        }
    else:
        return {
            "bot_url": os.environ.get("DEV_BOT_INTERNAL_URL"),
            "token": os.environ.get("DEV_INTERNAL_TOKEN")
        }

def get_env_var(key, default=None):
    """Ottiene variabile di ambiente (compatibilità)"""
    return os.environ.get(key, default)

def print_config_summary():
    """Stampa un riassunto della configurazione attuale"""
    env = get_environment()
    telegram_config = get_telegram_config()
    paypal_config = get_paypal_config()
    r2_config = get_r2_config()
    
    print("🤖 BOT TELEGRAM AVVIATO")
    print(f"🌍 Ambiente: {telegram_config['env_name']}")
    print(f"💳 PayPal: {paypal_config['env_name']}")
    print(f"💾 Database: {get_database_url().split('@')[0]}@***")  # Log sicuro
    print(f"☁️  R2 Bucket: {r2_config['bucket_name']}")
    print("=" * 50)

if __name__ == "__main__":
    # Test configurazione
    print_config_summary()
