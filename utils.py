#utils.py
import os
import time
import requests
import httpx
import boto3
from botocore.client import Config
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from db_manager import SessionLocal, Beat
from config import (
    get_paypal_config, 
    get_r2_config, 
    get_env_var,
    get_environment,
    print_config_summary
)

# Configurazione dinamica basata su ambiente
PAYPAL_CONFIG = get_paypal_config()
R2_CONFIG = get_r2_config()
CURRENT_ENV = get_environment()

def get_internal_token():
    """
    Restituisce l'INTERNAL_TOKEN usando configurazione dinamica.
    """
    from config import get_internal_config
    internal_config = get_internal_config()
    if internal_config["bot_url"] and not internal_config["token"]:
        raise RuntimeError("INTERNAL_TOKEN non impostato nelle variabili di ambiente!")
    return internal_config["token"]

def get_bot_internal_url():
    """
    Restituisce l'URL interno del bot usando configurazione dinamica.
    """
    from config import get_internal_config
    internal_config = get_internal_config()
    if internal_config["bot_url"]:
        return internal_config["bot_url"]
    return "http://localhost:8000"

# Configurazioni PayPal dinamiche
PAYPAL_CLIENT_ID = PAYPAL_CONFIG["client_id"]
PAYPAL_CLIENT_SECRET = PAYPAL_CONFIG["client_secret"]
PAYPAL_API_BASE_URL = PAYPAL_CONFIG["api_base"]
PAYPAL_OAUTH_URL = f"{PAYPAL_API_BASE_URL}/v1/oauth2/token"
PAYPAL_ORDER_URL = f"{PAYPAL_API_BASE_URL}/v2/checkout/orders"

def validate_url(url):
    """Check if URL is accessible and returns image"""
    if not url:
        return False
        
    try:
        response = requests.head(url, timeout=5)
        content_type = response.headers.get('Content-Type', '')
        return response.status_code == 200 and 'image' in content_type
    except Exception:
        return False

def build_keyboard(items, back_button=False):
    keyboard = []

    count = len(items)

    if count == 1:
        # Un solo bottone in una riga
        keyboard.append([InlineKeyboardButton(text=items[0], callback_data=items[0])])
    else:
        # Righe da 2 elementi
        for i in range(0, count - 1, 2):
            row = [
                InlineKeyboardButton(text=items[i], callback_data=items[i]),
                InlineKeyboardButton(text=items[i + 1], callback_data=items[i + 1])
            ]
            keyboard.append(row)

        # Se c'è un elemento dispari finale
        if count % 2 != 0:
            keyboard.append([InlineKeyboardButton(text=items[-1], callback_data=items[-1])])

    # Bottone "Torna indietro"
    if back_button:
        keyboard.append([InlineKeyboardButton("🔙 Torna indietro", callback_data="back")])

    return keyboard


def get_beat_counts():
    session = SessionLocal()
    counts = {}
    for beat in session.query(Beat).all():
        key = (beat.genre, beat.mood)
        counts[key] = counts.get(key, 0) + 1
    genre_counts = {}
    for beat in session.query(Beat).all():
        genre_counts[beat.genre] = genre_counts.get(beat.genre, 0) + 1
    session.close()
    return counts, genre_counts


def build_dynamic_genre_to_moods():
    base = {
        "Trap":           ["Hard", "Love", "Sad", "Dark"],
        "Hip-Hop":        ["Hard", "Love", "Chill", "Epic"],
        "Drill":          ["Hard", "Love", "Sad", "Epic"],
        "R&B":            ["Happy", "Love", "Chill", "Sad"],
        "Raggeton":       ["Happy", "Love", "Chill", "Sad"],
        "Brazilian Funk": ["Hype", "Chill", "Love", "Emotional"]
    }
    counts, genre_counts = get_beat_counts()
    result = {}
    for genre, moods in base.items():
        genre_available = genre_counts.get(genre, 0) > 0
        genre_label = genre if genre_available else f"🚫 {genre}"
        mood_labels = []
        for mood in moods:
            available = counts.get((genre, mood), 0) > 0
            mood_labels.append(mood if available else f"🚫 {mood}")
        result[genre_label] = mood_labels
    return result


def parse_genre_label(label):
    return label.split(" ", 1)[1] if " " in label else label


def parse_mood_label(label):
    return label.split(" ", 1)[1] if " " in label else label


def build_keyboard_with_disabled(items, back_button=False, context_key=None):
    keyboard = []
    count = len(items)
    for i in range(0, count, 2):
        row = []
        for j in range(2):
            if i + j < count:
                item = items[i + j]
                if item.startswith("🚫 "):
                    label = item
                    if context_key:
                        cb_data = f"disabled_{context_key}|{item[2:]}"
                    else:
                        cb_data = f"disabled_{item[2:]}"
                    row.append(InlineKeyboardButton(text=label, callback_data=cb_data))
                else:
                    row.append(InlineKeyboardButton(text=item, callback_data=item))
        keyboard.append(row)
    if back_button:
        keyboard.append([InlineKeyboardButton("🔙 Torna indietro", callback_data="back")])
    return keyboard


LOADING_KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Caricamento...", callback_data="loading")]])


async def show_loading(query):
    try:
        await query.edit_message_reply_markup(reply_markup=LOADING_KEYBOARD)
    except Exception:
        pass  # Ignora errori se il markup non può essere editato


def is_user_blocked(context):
    """Restituisce True se l'utente è bloccato, False altrimenti."""
    now = time.time()
    return context.user_data.get("blocked_until", 0) > now

async def blockeduser_response(update, context):
    """Risponde all'utente bloccato in modo appropriato (messaggio/chat/alert)."""
    now = time.time()
    mins = int((context.user_data["blocked_until"] - now) // 60) + 1
    chat = update.effective_chat
    if hasattr(update, "callback_query") and update.callback_query:
        try:
            await update.callback_query.answer(
                f"🚫 Sei temporaneamente bloccato per spam. Riprova tra {mins} minuti.",
                show_alert=True
            )
        except Exception as e:
            # logging non disponibile qui, fallback print
            print(f"Errore invio alert blocco: {e}")
    elif chat:
        try:
            await chat.send_message(
                f"🚫 Sei temporaneamente bloccato per spam. Riprova tra {mins} minuti."
            )
        except Exception as e:
            print(f"Errore invio messaggio blocco: {e}")

#paypal utils


async def get_paypal_access_token() -> str:
    url = PAYPAL_OAUTH_URL
    auth = (PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    headers = {"Accept": "application/json", "Accept-Language": "en_US"}
    data = {"grant_type": "client_credentials"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, auth=auth, data=data, headers=headers)
        if resp.status_code != 200:
            print("PayPal error:", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp.json().get("access_token")

async def create_paypal_order(custom_id: str, amount: float, description: str, currency="EUR") -> str:
    access_token = await get_paypal_access_token()
    url = PAYPAL_ORDER_URL

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }

    data = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": custom_id,
                "custom_id": custom_id,
                "amount": {
                    "currency_code": currency,
                    "value": f"{amount:.2f}"
                },
                "description": description
            }
        ],
        "application_context": {
            "brand_name": "ProdByPegasus",
            "landing_page": "NO_PREFERENCE",
            "user_action": "PAY_NOW",
            "return_url": "https://prodbypegasus.pages.dev/success",  # <--- canonical path for Cloudflare Pages
            "cancel_url": "https://prodbypegasus.pages.dev/checkout"  # <--- canonical path for Cloudflare Pages
        }
    }

    print("PayPal order request JSON:", data)  # <--- DEBUG
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=data, headers=headers)
        if resp.status_code != 201:
            print("PayPal order error:", resp.status_code, resp.text)  # <--- DEBUG
        resp.raise_for_status()
        order = resp.json()

        for link in order.get("links", []):
            if link.get("rel") == "approve":
                return link.get("href")

    raise Exception("No approval URL found in PayPal order response")

def generate_r2_signed_url(key: str, expires_in: int = 3600) -> str:
    # Configurazioni R2 dinamiche
    R2_ACCESS_KEY_ID = R2_CONFIG["access_key_id"]
    R2_SECRET_ACCESS_KEY = R2_CONFIG["secret_access_key"]
    R2_ENDPOINT_URL = R2_CONFIG["endpoint_url"]
    R2_BUCKET_NAME = R2_CONFIG["bucket_name"]
    R2_PUBLIC_BASE_URL = R2_CONFIG["public_base_url"]
    
    session = boto3.session.Session()
    s3 = session.client(
        service_name="s3",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        endpoint_url=R2_ENDPOINT_URL,
        config=Config(signature_version="s3v4"),
        region_name="auto"
    )
    url = s3.generate_presigned_url(
        ClientMethod='get_object',
        Params={'Bucket': R2_BUCKET_NAME, 'Key': key},
        ExpiresIn=expires_in
    )
    from urllib.parse import urlparse
    parsed = urlparse(url)
    path_parts = parsed.path.split('/', 2)
    if len(path_parts) == 3:
        key_path = path_parts[2]
    else:
        key_path = parsed.path.lstrip('/')
    signed_url = f"{R2_PUBLIC_BASE_URL}/{key_path}?{parsed.query}"
    return signed_url

