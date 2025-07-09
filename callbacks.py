# callbacks.py
import os
import logging
import time
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ContextTypes, ConversationHandler
from urllib.parse import quote  # <--- aggiungi questa importazione
from utils import (
    build_keyboard,
    build_dynamic_genre_to_moods,
    parse_genre_label,
    parse_mood_label,
    build_keyboard_with_disabled,
    show_loading,
    validate_url,
    is_user_blocked,
    blockeduser_response,
    create_paypal_order,
    LOADING_KEYBOARD,
)
from db_manager import (
    SessionLocal, Beat, Bundle, BundleOrder, reserve_exclusive_beat, release_beat_reservation, 
    cleanup_expired_reservations, is_beat_available, get_beat_availability_status, get_active_bundles, get_bundle_by_id,
    get_user_active_reservation, reserve_bundle_exclusive_beats, release_bundle_reservations, reserve_bundle_exclusive_beats_with_retry
)
from telegram.ext import JobQueue
from telegram.constants import ParseMode

# Configure logging
logger = logging.getLogger(__name__)

# Usa la configurazione centralizzata
from config import get_r2_config, get_paypal_config

# Configurazione dinamica basata su ambiente
R2_CONFIG = get_r2_config()
PAYPAL_CONFIG = get_paypal_config()

# Variabili R2
R2_ENDPOINT_URL = R2_CONFIG["endpoint_url"]
R2_BUCKET_NAME = R2_CONFIG["bucket_name"]
R2_PUBLIC_BASE = R2_CONFIG["public_base_url"]

# Variabili PayPal
PAYPAL_CLIENT_ID = PAYPAL_CONFIG["client_id"]
PAYPAL_CLIENT_SECRET = PAYPAL_CONFIG["client_secret"]

# Helper functions
def ensure_path(key, kind):
    """Assicura che la chiave abbia il percorso corretto per il tipo di file"""
    if not key:
        return None
    if key.startswith("public/") or key.startswith("private/"):
        return key
    if kind == "preview":
        return f"public/previews/{key}"
    if kind == "image":
        return f"public/images/{key}"
    if kind == "file":
        return f"private/beats/{key}"
    return key

# ⚡ FUNZIONE HELPER PER CLEANUP AUTOMATICO
async def cleanup_user_reservation_and_payment(user_id, context, chat_id, reason=""):
    """
    Funzione helper per rilasciare prenotazioni attive e cancellare messaggi di pagamento.
    Utilizzata in tutti i percorsi di navigazione per garantire UX fluida.
    """
    cleanup_expired_reservations()
    has_reservation, reservation_info, reserved_beat_id = get_user_active_reservation(user_id)
    
    # Rilascia prenotazioni beat singoli
    if has_reservation and reserved_beat_id:
        release_beat_reservation(reserved_beat_id, user_id)
        print(f"DEBUG: Prenotazione beat rilasciata per utente {user_id}, beat {reserved_beat_id} - {reason}")
    
    # Rilascia prenotazioni bundle
    reserved_bundle_id = context.user_data.get("reserved_bundle_id")
    if reserved_bundle_id:
        released_count = release_bundle_reservations(reserved_bundle_id, user_id)
        if released_count > 0:
            print(f"DEBUG: {released_count} prenotazioni bundle rilasciate per utente {user_id}, bundle {reserved_bundle_id} - {reason}")
        context.user_data.pop("reserved_bundle_id", None)
    
    # 🧹 CLEANUP MESSAGGI: Lista di tutti i tipi di messaggi da cancellare
    message_types_to_clean = [
        ("payment_message_id", "pagamento"),
        ("warning_message_id", "avviso"),
        ("reservation_message_id", "prenotazione"),
        ("bundle_payment_message_id", "pagamento bundle")
    ]
    
    # Cancella TUTTI i messaggi di avviso se sono una lista (più warning per utente)
    warning_ids = context.user_data.get("warning_message_id")
    if isinstance(warning_ids, list):
        for wid in warning_ids:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=wid
                )
                print(f"DEBUG: Messaggio avviso {wid} cancellato (lista) - {reason}")
            except Exception as e:
                print(f"DEBUG: Errore cancellazione messaggio avviso (lista): {e}")
        context.user_data["warning_message_id"] = []
    elif warning_ids:
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=warning_ids
            )
            print(f"DEBUG: Messaggio avviso {warning_ids} cancellato - {reason}")
        except Exception as e:
            print(f"DEBUG: Errore cancellazione messaggio avviso: {e}")
        context.user_data.pop("warning_message_id", None)

    # Cancella altri tipi di messaggi
    for message_key, message_type in message_types_to_clean:
        if message_key == "warning_message_id":
            continue  # Già gestito sopra
        message_id = context.user_data.get(message_key)
        if message_id:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=message_id
                )
                print(f"DEBUG: Messaggio {message_type} {message_id} cancellato - {reason}")
            except Exception as e:
                print(f"DEBUG: Errore cancellazione messaggio {message_type}: {e}")
            context.user_data.pop(message_key, None)
    
    # Pulisci anche altri dati temporanei del context
    temp_keys_to_clean = [
        "reserved_beat_id",
        "checkout_timestamp",
        "temp_bundle_data"
    ]
    
    for key in temp_keys_to_clean:
        context.user_data.pop(key, None)

def build_beat_urls(beat):
    """Costruisce gli URL per preview, file e immagine di un beat"""
    preview_key = ensure_path(beat.preview_key, "preview")
    file_key = ensure_path(beat.file_key, "file")
    image_key = ensure_path(beat.image_key, "image")
    
    return {
        "preview_url": f"{R2_PUBLIC_BASE}/{quote(preview_key)}" if preview_key else None,
        "file_url": f"{R2_PUBLIC_BASE}/{quote(file_key)}" if file_key else None,
        "image_url": f"{R2_PUBLIC_BASE}/{quote(image_key)}" if image_key else None,
    }

def create_beat_data(beat):
    """Crea i dati del beat con tutti gli URL necessari"""
    urls = build_beat_urls(beat)
    return {
        "id": beat.id,  # Aggiungi l'ID per la gestione delle prenotazioni
        "title": beat.title,
        "genre": beat.genre,
        "mood": beat.mood,
        "price": beat.price,
        "original_price": beat.original_price,
        "is_discounted": beat.is_discounted,
        "discount_percent": beat.discount_percent,
        "is_exclusive": beat.is_exclusive,
        **urls
    }

async def check_user_blocked(update, context):
    """Controlla se l'utente è bloccato e gestisce la risposta"""
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return True
    return False

# Constants for conversation states
GENRE, MOOD, BEAT_SELECTION = range(3)

# Nuovo stato per la selezione categoria
CATEGORY = 1000  # Usa un valore fuori range per evitare collisioni

# Nuovo stato per i bundle
BUNDLE_SELECTION = 1001

# Rate limit config
MAX_INVALID_MSGS = 10         # Quanti messaggi errati prima del blocco temporaneo
BLOCK_DURATION_SEC = 60    # 1 minuti di blocco (puoi aumentare)

# Messaggio di introduzione/tutorial
INTRO_MESSAGE = """🎵 <b>Benvenuto su Pegasus Beat Store!</b>

<b>🚀 La tua musica inizia qui!</b>

<b>Come funziona:</b>
🎧 <b>Naviga</b> → Usa <i>Avanti</i> e <i>Indietro</i> per esplorare il catalogo
🎼 <b>Ascolta</b> → Clicca <i>Spoiler</i> per l'anteprima gratuita di ogni beat
💰 <b>Acquista</b> → Pagamento sicuro direttamente dal bot
🔍 <b>Filtra</b> → Trova il beat perfetto per genere, mood e prezzo
🏠 <b>Torna al menu</b> → Disponibile sempre per ricominciare
📦 <b>Ricevi subito</b> → Beat in formato WAV di alta qualità su Telegram
📞 <b>Assistenza</b> → Supporto completo tramite <i>Contattaci</i>

<b>🎯 Beat professionali con licenza commerciale inclusa!</b>

Buona musica! 🎶✨"""

# Messaggio di benvenuto per il menu categorie
WELCOME_TEXT = """🎵 <b>Scegli la tua categoria preferita:</b>

💸 <b>Beat scontati</b>  
Offerte speciali e prezzi ridotti!

🎖️ <b>Beat esclusivi</b>  
Pezzi unici, disponibili una sola volta!

🎁 <b>Bundle promozionali</b>  
Pacchetti di beat a prezzi vantaggiosi!

🎶 <b>Beat standard</b>  
Il nostro catalogo completo di qualità.

👇 <b>Inizia la tua ricerca musicale:</b>
"""

async def send_welcome_message(update, context, edit=False):
    if await check_user_blocked(update, context):
        return

    # Rilascia eventuali prenotazioni attive quando si torna al menu principale
    reserved_beat_id = context.user_data.get("reserved_beat_id")
    if reserved_beat_id:
        user_id = update.effective_user.id
        release_beat_reservation(reserved_beat_id, user_id)
        context.user_data.pop("reserved_beat_id", None)

    # Reset filtri utente quando si torna al menu principale
    context.user_data.pop("genre", None)
    context.user_data.pop("mood", None)
    context.user_data.pop("price_range", None)

    # Tastiera con le quattro categorie
    keyboard = [
        ["🎶 Beat standard"],
        ["💸 Beat scontati", "🎖️ Beat esclusivi"],
        ["🎁 Bundle promozionali"]
    ]
    reply_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(text=btn, callback_data=btn) for btn in row]
        for row in keyboard
    ])

    text = WELCOME_TEXT

    chat = update.effective_chat
    if not chat:
        logger.error("Nessuna chat trovata per inviare il messaggio di benvenuto.")
        return

    # Cancella vecchio menu (se esiste)
    old_msg_id = context.user_data.get("last_bot_message_id")
    if old_msg_id:
        try:
            await context.bot.delete_message(chat_id=chat.id, message_id=old_msg_id)
        except Exception as e:
            logger.debug(f"Errore cancellazione vecchio menu: {e}")

    # Invia nuovo menu delle categorie
    sent = await chat.send_message(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    context.user_data["last_bot_message_id"] = sent.message_id
    context.user_data["chat_id"] = chat.id
    context.user_data["warning_shown"] = False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_user_blocked(update, context):
        return context.user_data.get("current_state", CATEGORY)
    
    chat = update.effective_chat
    if not chat:
        logger.error("Nessuna chat trovata per il comando start.")
        return CATEGORY
    
    user_id = update.effective_user.id
    
    # ⚡ CLEANUP: Rilascia prenotazioni e cancella messaggi di pagamento quando si usa /start
    await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "comando /start")
    
    # Invia prima il messaggio di introduzione (che rimane fisso)
    try:
        await chat.send_message(INTRO_MESSAGE, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Errore invio messaggio introduttivo: {e}")
    
    # Poi invia il menu di selezione categoria
    await send_welcome_message(update, context)
    context.user_data["current_state"] = CATEGORY
    return CATEGORY

async def genre_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_user_blocked(update, context):
        return context.user_data.get("current_state", GENRE)
    
    query = update.callback_query
    genre_label = query.data
    user_id = update.effective_user.id

    if genre_label.startswith(("disabled_", "🚫")):
        await query.answer("🚫 Attualmente non ci sono beat disponibili per questa categoria.", show_alert=True)
        context.user_data["current_state"] = GENRE
        return GENRE

    await query.answer()
    
    # ⚡ CLEANUP: Rilascia prenotazioni e cancella messaggi quando si cambia genere
    await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "cambio genere")
    
    genre = parse_genre_label(genre_label)
    context.user_data["genre"] = genre

    if "mood" in context.user_data:
        context.user_data.pop("mood", None)
    if "beats" in context.user_data:
        context.user_data.pop("beats", None)
    if "beat_index" in context.user_data:
        context.user_data.pop("beat_index", None)

    genre_to_moods = build_dynamic_genre_to_moods()
    moods = genre_to_moods.get(genre_label, [])
    keyboard = build_keyboard_with_disabled(moods, back_button=True, context_key=genre)
    
    await query.edit_message_text(
        f"🎶 Hai scelto il genere *{genre}*.\n"
        "Ora seleziona il *mood* che preferisci:\n\n"
        "💡 Scegli il mood che meglio si adatta al tuo progetto!",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    context.user_data["current_state"] = MOOD
    return MOOD

async def mood_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_user_blocked(update, context):
        return context.user_data.get("current_state", GENRE)
    
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id

    if data == "back":
        # ⚡ CLEANUP: Rilascia prenotazioni quando si torna indietro dal mood
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "ritorno da mood a genere")
        
        genre_to_moods = build_dynamic_genre_to_moods()
        keyboard = build_keyboard_with_disabled(list(genre_to_moods.keys()))
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = WELCOME_TEXT
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
        context.user_data["current_state"] = GENRE
        return GENRE

    if data.startswith(("disabled_", "🚫")):
        await query.answer("🚫 Attualmente non ci sono beat disponibili per questa categoria.", show_alert=True)
        context.user_data["current_state"] = MOOD
        return MOOD

    await query.answer()

    # ⚡ CLEANUP: Rilascia prenotazioni e cancella messaggi quando si cambia mood
    await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "cambio mood")

    genre = context.user_data["genre"]
    mood = parse_mood_label(data)
    context.user_data["mood"] = mood

    with SessionLocal() as session:
        beats = session.query(Beat).filter_by(genre=genre, mood=mood).all()

    context.user_data["beats"] = [create_beat_data(beat) for beat in beats]
    context.user_data["beat_index"] = 0
    context.user_data["current_state"] = BEAT_SELECTION
    return await show_beat_catalog(update, context)

async def category_selected(update, context):
    if await check_user_blocked(update, context):
        return CATEGORY

    query = update.callback_query
    data = query.data

    # Gestione ritorno al menu principale
    if data == "menu":
        user_id = update.effective_user.id
        
        # ⚡ CLEANUP: Rilascia prenotazioni e cancella messaggi di pagamento quando si torna al menu
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "ritorno al menu da categoria")
        
        await send_welcome_message(update, context)
        context.user_data["current_state"] = CATEGORY
        return CATEGORY

    # Quando si cambia categoria, resetta i filtri secondari
    context.user_data.pop("genre", None)
    context.user_data.pop("mood", None)
    context.user_data.pop("price_range", None)

    # Salva la categoria scelta
    if data == "🎶 Beat standard":
        context.user_data["catalog_category"] = "standard"
    elif data == "💸 Beat scontati":
        context.user_data["catalog_category"] = "discount"
    elif data == "🎖️ Beat esclusivi":
        context.user_data["catalog_category"] = "exclusive"
    elif data == "🎁 Bundle promozionali":
        context.user_data["catalog_category"] = "bundles"
        await query.answer()
        return await show_bundles_catalog(update, context)
    else:
        await query.answer("Categoria non valida.", show_alert=True)
        return CATEGORY

    await query.answer()
    # Mostra subito il catalogo filtrato (shuffle)
    return await show_filtered_catalog(update, context)

async def show_filtered_catalog(update, context):
    """Mostra il catalogo filtrato in base alla categoria scelta, con UI a scorrimento e filtri"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    
    user_id = update.effective_user.id
    
    # ⚡ CLEANUP AUTOMATICO: Rilascia prenotazioni e cancella messaggi quando si visualizza il catalogo filtrato
    await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "visualizzazione catalogo filtrato")

    category = context.user_data.get("catalog_category")
    genre_filter = context.user_data.get("genre")
    mood_filter = context.user_data.get("mood")
    price_range = context.user_data.get("price_range")

    with SessionLocal() as session:
        query = session.query(Beat)
        if category == "exclusive":
            query = query.filter(Beat.is_exclusive == 1)
        elif category == "discount":
            query = query.filter(Beat.is_discounted == 1)
        else:  # standard
            # Mostra tutti i beat NON esclusivi (sia scontati che non scontati)
            query = query.filter(Beat.is_exclusive == 0)

        # Applica filtri indipendenti
        if genre_filter:
            query = query.filter(Beat.genre == genre_filter)
        if mood_filter:
            query = query.filter(Beat.mood == mood_filter)
        if price_range and price_range != "Tutti":
            if price_range == "0-10€":
                query = query.filter(Beat.price >= 0, Beat.price <= 10)
            elif price_range == "10-20€":
                query = query.filter(Beat.price > 10, Beat.price <= 20)
            elif price_range == "20-30€":
                query = query.filter(Beat.price > 20, Beat.price <= 30)
            elif price_range == "30€+":
                query = query.filter(Beat.price > 30)
        beats = query.all()

    # Shuffle i beat
    import random
    beats = list(beats)
    random.shuffle(beats)

    # Salva i beat filtrati in user_data per la navigazione
    context.user_data["beats"] = [create_beat_data(beat) for beat in beats]

    query = update.callback_query
    if not context.user_data["beats"]:
        # Aggiorna il messaggio con testo e bottone per tornare al menu
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Torna al menu", callback_data="menu")]
        ])
        try:
            await query.edit_message_text(
                "❌ Nessun beat disponibile per questa categoria.",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except Exception:
            pass
        context.user_data["current_state"] = CATEGORY
        return CATEGORY

    context.user_data["beat_index"] = 0
    context.user_data["current_state"] = BEAT_SELECTION
    return await show_beat_catalog(update, context)

def build_beat_caption(beat, idx, filtri_str):
    """
    Restituisce la caption HTML per un beat, mostrando badge e messaggi per sconto/esclusività.
    """
    title = beat.get("title")
    genre = beat.get("genre")
    mood = beat.get("mood")
    price = beat.get("price", 19.99)
    original_price = beat.get("original_price", None)
    is_discounted = int(beat.get("is_discounted", 0))
    discount_percent = int(beat.get("discount_percent", 0))
    is_exclusive = int(beat.get("is_exclusive", 0))

    lines = []

    # Esclusivo: messaggio in alto
    if is_exclusive == 1:
        lines.append("<b>🔒 <u>DISPONIBILITÀ LIMITATA</u> 🔒</b>")
        lines.append("<b>Questo beat è <u>unico</u> e acquistabile una sola volta!</b>")
        lines.append("")  # Riga vuota per separazione

    # Titolo beat
    lines.append(f"🎵 <u>#{idx+1}</u> • <b>{title}</b>")

    # Sconto
    if (
        is_discounted == 1
        and discount_percent > 0
        and original_price is not None
        and price is not None
        and float(price) < float(original_price)
    ):
        lines.append("")
        lines.append("<b>🔥 <u>OFFERTA LIMITATA!</u> 🔥</b>")
        lines.append(f"<b>Prezzo: <s>{original_price:.2f}€</s> → <u>{price:.2f}€</u></b>")
        lines.append(f"<b>Sconto del {discount_percent}% 💸</b>")
    else:
        lines.append("")
        lines.append(f"<b>💰 <u>PREZZO: {price:.2f}€</u></b>")

    # Info generali
    lines.append("")
    lines.append(f"Genere: <b>{genre}</b>")
    lines.append(f"Mood: <b>{mood}</b>")

    caption = f"{filtri_str}" + "\n".join(lines)
    return caption

async def show_beat_catalog(update, context):
    if await check_user_blocked(update, context):
        return context.user_data.get("current_state", GENRE)
    
    query = update.callback_query
    user_id = update.effective_user.id
    
    # ⚡ CLEANUP AUTOMATICO: Se l'utente naviga, cancella prenotazioni precedenti
    # Questo permette navigazione libera ma evita prenotazioni multiple
    cleanup_expired_reservations()
    has_reservation, reservation_info, reserved_beat_id = get_user_active_reservation(user_id)
    
    if has_reservation:
        beats = context.user_data.get("beats", [])
        idx = context.user_data.get("beat_index", 0)
        
        if beats and idx < len(beats):
            current_beat = beats[idx]
            current_beat_id = current_beat.get("id")
            
            # Se l'utente sta navigando verso un beat diverso, cancella la prenotazione precedente
            if reserved_beat_id != current_beat_id:
                # Rilascia la prenotazione precedente
                release_beat_reservation(reserved_beat_id, user_id)
                
                # Cancella il messaggio di pagamento precedente se esiste
                previous_payment_msg_id = context.user_data.get("payment_message_id")
                if previous_payment_msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=update.effective_chat.id,
                            message_id=previous_payment_msg_id
                        )
                    except Exception as e:
                        pass  # Il messaggio potrebbe essere già stato cancellato
                    
                    context.user_data.pop("payment_message_id", None)
                
                print(f"DEBUG: Prenotazione precedente cancellata - utente naviga da beat {reserved_beat_id} a beat {current_beat_id}")
    
    await query.answer()
    await show_loading(query)

    beats = context.user_data["beats"]
    idx = context.user_data["beat_index"]
    beat = beats[idx]

    # Ricava filtri attivi (escludi la categoria di base)
    filtri = []
    
    # Aggiungi solo filtri specifici, non la categoria base
    genre = context.user_data.get("genre")
    if genre:
        filtri.append(f"Genere: {genre}")
    
    mood = context.user_data.get("mood")
    if mood:
        filtri.append(f"Mood: {mood}")
    
    price_range = context.user_data.get("price_range")
    if price_range and price_range != "Tutti":
        filtri.append(f"Prezzo: {price_range}")

    # Mostra "Filtri di ricerca" solo se ci sono filtri specifici
    filtri_str = ""
    if filtri:
        filtri_str = f"<i>Filtri di ricerca: {' | '.join(filtri)}</i>\n\n"

    # Usa la funzione riutilizzabile per la caption
    caption = build_beat_caption(beat, idx, filtri_str)

    # Costruisci la tastiera
    keyboard = build_navigation_keyboard(beats)
    
    # Aggiungi il pulsante "Rimuovi filtri" se ci sono filtri attivi
    has_active_filters = any([genre, mood, price_range and price_range != "Tutti"])
    if has_active_filters:
        # Inserisci il pulsante "Rimuovi filtri" prima del "Torna al menu"
        keyboard.insert(-1, [InlineKeyboardButton("🗑️ Rimuovi filtri di ricerca", callback_data="remove_all_filters")])

    try:
        await update_message_with_beat(query, beat, caption, keyboard)
    except Exception as e:
        logger.error(f"Errore generale show_beat_catalog: {e}")
    
    # Reset preview tracking per ogni cambio beat
    context.user_data["last_preview_idx"] = None
    context.user_data["current_state"] = BEAT_SELECTION
    return BEAT_SELECTION

def build_navigation_keyboard(beats):
    """Costruisce la tastiera di navigazione per i beat"""
    # Gestione tasti avanti/indietro disabilitati se c'è solo un beat
    if len(beats) == 1:
        nav_row = [
            InlineKeyboardButton("🚫 Indietro", callback_data="disabled_prev"),
            InlineKeyboardButton("🚫 Avanti", callback_data="disabled_next")
        ]
    else:
        nav_row = [
            InlineKeyboardButton("⬅️ Indietro", callback_data="prev"),
            InlineKeyboardButton("➡️ Avanti", callback_data="next")
        ]

    filter_row = [
        InlineKeyboardButton("📞 Contattaci", url="https://linktr.ee/ProdByPegasus"),
        InlineKeyboardButton("🔎 Filtri di ricerca", callback_data="change_filters")
    ]

    keyboard = [
        nav_row,
        [InlineKeyboardButton("🎧 Spoiler", callback_data="preview")],
        [InlineKeyboardButton("💸 Acquista", callback_data="buy")],
        filter_row,
        [InlineKeyboardButton("🔙 Torna al menu", callback_data="menu")]
    ]
    
    return keyboard

async def update_message_with_beat(query, beat, caption, keyboard):
    """Aggiorna il messaggio con l'immagine e i dettagli del beat"""
    image_url = beat.get("image_url")
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if image_url and validate_url(image_url):
        from random import randint
        sep = '&' if '?' in image_url else '?'
        image_url_refresh = f"{image_url}{sep}_={randint(1, 999999)}"
        
        try:
            await query.edit_message_media(
                media=InputMediaPhoto(image_url_refresh, caption=caption, parse_mode='HTML'),
                reply_markup=reply_markup
            )
        except Exception:
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(image_url, caption=caption, parse_mode='HTML'),
                    reply_markup=reply_markup
                )
            except Exception:
                # Fallback: elimina e ricrea il messaggio
                try:
                    await query.message.delete()
                except Exception as ex:
                    logger.debug(f"Errore eliminazione messaggio: {ex}")
                await query.message.chat.send_photo(
                    photo=image_url,
                    caption=caption,
                    parse_mode='HTML',
                    reply_markup=reply_markup
                )
    else:
        try:
            await query.edit_message_text(
                caption, reply_markup=reply_markup, parse_mode='HTML'
            )
        except Exception:
            try:
                await query.message.delete()
            except Exception as ex:
                logger.debug(f"Errore eliminazione messaggio: {ex}")
            await query.message.chat.send_message(
                caption, reply_markup=reply_markup, parse_mode='HTML'
            )

async def handle_beat_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_user_blocked(update, context):
        return context.user_data.get("current_state", GENRE)
    
    query = update.callback_query
    data = query.data
    beats = context.user_data["beats"]
    idx = context.user_data["beat_index"]
    user_id = update.effective_user.id

    # Mostra pop-up se si clicca su un tasto disabilitato
    if data in ("disabled_prev", "disabled_next"):
        await query.answer("🚫 Attualmente c'è solo un beat disponibile in questa categoria.", show_alert=True)
        context.user_data["current_state"] = BEAT_SELECTION
        return BEAT_SELECTION

    # Gestione spoiler: NON chiamare query.answer() qui, lasciato a send_beat_preview
    if data == "preview":
        context.user_data["current_state"] = BEAT_SELECTION
        return await send_beat_preview(update, context)

    if data == "menu":
        await delete_last_preview(context)
        
        # ⚡ CLEANUP: Rilascia prenotazioni e cancella messaggi di pagamento quando si torna al menu
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "ritorno al menu")
        
        await send_welcome_message(update, context)
        context.user_data["current_state"] = CATEGORY
        return CATEGORY

    if data == "change_filters":
        # ⚡ CLEANUP: Rilascia prenotazioni quando si accede ai filtri
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "accesso filtri")
        
        await show_filters_keyboard(update, context)
        return BEAT_SELECTION

    if data == "remove_all_filters":
        # ⚡ CLEANUP: Rilascia prenotazioni quando si rimuovono tutti i filtri
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "rimozione tutti i filtri")
        
        # Rimuovi tutti i filtri attivi
        context.user_data.pop("genre", None)
        context.user_data.pop("mood", None)
        context.user_data.pop("price_range", None)
        # Ricarica il catalogo con tutti i beat della categoria corrente
        await show_filtered_catalog(update, context)
        return BEAT_SELECTION

    await query.answer()

    # Se si sta navigando a un altro beat, rilascia eventuali prenotazioni attive
    if data in ("prev", "next"):
        user_id = update.effective_user.id
        
        # ⚡ CLEANUP COMPLETO: Rilascia prenotazioni E cancella messaggi di pagamento durante navigazione
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "navigazione tra beat")

    if data == "prev":
        context.user_data["beat_index"] = (idx - 1) % len(beats)
        await delete_last_preview(context)
    elif data == "next":
        context.user_data["beat_index"] = (idx + 1) % len(beats)
        await delete_last_preview(context)
    elif data == "buy":
        context.user_data["current_state"] = BEAT_SELECTION
        return await handle_payment(update, context)
    
    # ⚡ FIX: Solo mostra il catalogo se NON è un acquisto
    context.user_data["current_state"] = BEAT_SELECTION
    return await show_beat_catalog(update, context)

async def send_beat_preview(update, context):
    """Send audio preview of current beat, with anti-abuse logic"""
    if await check_user_blocked(update, context):
        return context.user_data.get("current_state", GENRE)
    
    from telegram import InputFile

    query = update.callback_query
    idx = context.user_data["beat_index"]
    beat = context.user_data["beats"][idx]

    # Se la preview per questo beat è già stata inviata, mostra pop-up e non reinvia
    if context.user_data.get("last_preview_idx") == idx and context.user_data.get("last_preview_message_id"):
        await query.answer("Hai già ricevuto la preview di questo beat.", show_alert=True)
        return BEAT_SELECTION

    await query.answer()  # Solo per la prima preview

    # Cancella la preview precedente se esiste (per altri beat)
    await delete_last_preview(context)

    try:
        sent = await query.message.reply_audio(
            audio=beat["preview_url"],
            caption=f"🎧 Preview di *{beat['title']}*",
            parse_mode='Markdown',
        )
        context.user_data["last_preview_message_id"] = sent.message_id
        context.user_data["last_preview_idx"] = idx
    except Exception as e:
        logger.error(f"Error sending preview: {e}")
        await query.message.reply_text("❌ Errore nel caricamento dell'anteprima")

    context.user_data["current_state"] = BEAT_SELECTION
    return BEAT_SELECTION

async def delete_last_preview(context):
    """Delete the last preview message if present"""
    bot = context.bot
    chat_id = None
    message_id = context.user_data.get("last_preview_message_id")
    if message_id is not None:
        # Ricava chat_id da context (può essere salvato in user_data o preso da update)
        # Qui assumiamo che context._chat_id sia disponibile (Telegram PTB >= 20)
        chat_id = context._chat_id if hasattr(context, "_chat_id") else None
        # Fallback: cerca in context.user_data
        if not chat_id:
            chat_id = context.user_data.get("chat_id")
        try:
            if chat_id:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            logger.debug(f"Errore cancellazione preview: {e}")
        # Rimuovi tracking
        context.user_data["last_preview_message_id"] = None
        context.user_data["last_preview_idx"] = None

async def handle_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_user_blocked(update, context):
        return context.user_data.get("current_state", GENRE)
    
    query = update.callback_query
    await query.answer()
    
    idx = context.user_data["beat_index"]
    beat = context.user_data["beats"][idx]
    user_id = update.effective_user.id

    # ⚡ CONTROLLO PREVENTIVO FORTE: Verifica SUBITO se l'utente ha prenotazioni attive
    # Questo previene qualsiasi race condition o problema di cache
    cleanup_expired_reservations()  # Pulisci prima di controllare
    has_reservation, reservation_info, reserved_beat_id = get_user_active_reservation(user_id)
    if has_reservation:
        beat_id = beat.get("id")
        if reserved_beat_id != beat_id:
            # L'utente ha una prenotazione per un beat diverso - blocca immediatamente
            await query.message.reply_text(
                "⏰ <b>Hai già una prenotazione attiva!</b>\n\n"
                f"📋 {reservation_info}\n\n"
                "💡 <b>Per prenotare un nuovo beat devi:</b>\n"
                "• Completare l'acquisto del beat già prenotato, oppure\n"
                "• Aspettare che scada la prenotazione attuale\n\n"
                "🚫 <i>Puoi prenotare solo 1 beat esclusivo alla volta per evitare abusi.</i>",
                parse_mode='HTML'
            )
            context.user_data["current_state"] = BEAT_SELECTION
            return BEAT_SELECTION

    # Controllo prezzo
    if not beat.get("price") or beat["price"] <= 0:
        await query.message.reply_text(
            "❌ Questo beat non è acquistabile perché non ha un prezzo impostato. Contatta l'amministratore."
        )
        context.user_data["current_state"] = BEAT_SELECTION
        return BEAT_SELECTION

    # Se il beat è esclusivo, gestisci la prenotazione
    if beat.get("is_exclusive") == 1:
        beat_id = beat.get("id")
        if not beat_id:
            await query.message.reply_text(
                "❌ Errore interno: ID beat non trovato. Contatta l'amministratore."
            )
            context.user_data["current_state"] = BEAT_SELECTION
            return BEAT_SELECTION

        # Il controllo prenotazione attiva è già stato fatto all'inizio della funzione

        # Verifica se il beat è disponibile con motivo dettagliato
        is_available, reason = get_beat_availability_status(beat_id)
        if not is_available:
            # Personalizza il messaggio in base al motivo
            if "bundle" in reason.lower():
                message = (
                    "❌ <b>Beat non disponibile!</b>\n\n"
                    f"🎁 {reason}.\n\n"
                    "💡 <b>Suggerimento:</b> Questo beat è incluso in un bundle che un altro utente sta acquistando. "
                    "Prova a:\n"
                    "• Aspettare qualche minuto e riprovare\n"
                    "• Scegliere un altro beat dal catalogo\n"
                    "• Controllare la sezione Bundle per offerte esclusive"
                )
            elif "prenotato" in reason.lower():
                message = (
                    "❌ <b>Beat temporaneamente prenotato!</b>\n\n"
                    f"⏰ {reason}.\n\n"
                    "💡 <b>Cosa fare:</b>\n"
                    "• Aspetta che scada la prenotazione\n"
                    "• Scegli un altro beat dal catalogo\n"
                    "• Torna più tardi per riprovare"
                )
            elif "venduto" in reason.lower():
                message = (
                    "❌ <b>Beat non più disponibile!</b>\n\n"
                    "🔥 Questo beat esclusivo è già stato venduto ad un altro cliente.\n\n"
                    "💡 <b>Suggerimento:</b> Esplora il nostro catalogo per trovare altri beat esclusivi fantastici!"
                )
            else:
                message = (
                    "❌ <b>Beat non disponibile!</b>\n\n"
                    f"ℹ️ {reason}\n\n"
                    "Ti consigliamo di scegliere un altro beat dal catalogo."
                )
            
            warning_msg = await query.message.reply_text(message, parse_mode='HTML')
            context.user_data.setdefault("warning_message_id", [])
            context.user_data["warning_message_id"].append(warning_msg.message_id)
            context.user_data["current_state"] = BEAT_SELECTION
            return BEAT_SELECTION

        # Tenta di prenotare il beat (prenotazione di 10 minuti)
        if not reserve_exclusive_beat(beat_id, user_id, reservation_minutes=10):
            # La prenotazione è fallita - potrebbe essere per vari motivi
            # Ricontrolla lo stato per fornire feedback preciso
            has_reservation_now, reservation_info_now, _ = get_user_active_reservation(user_id)
            if has_reservation_now:
                # L'utente ha già una prenotazione (race condition)
                warning_msg = await query.message.reply_text(
                    "⏰ <b>Prenotazione non possibile!</b>\n\n"
                    f"📋 {reservation_info_now}\n\n"
                    "🚫 <i>Puoi prenotare solo 1 beat esclusivo alla volta.</i>",
                    parse_mode='HTML'
                )
                context.user_data.setdefault("warning_message_id", [])
                context.user_data["warning_message_id"].append(warning_msg.message_id)
            else:
                # Il beat è stato prenotato da qualcun altro nel frattempo
                _, reason = get_beat_availability_status(beat_id)
                warning_msg = await query.message.reply_text(
                    "❌ <b>Prenotazione fallita!</b>\n\n"
                    f"⚡ Un altro utente ha appena prenotato questo beat mentre stavi per acquistarlo.\n\n"
                    f"📊 <b>Stato attuale:</b> {reason}\n\n"
                    "💡 <b>Cosa fare:</b>\n"
                    "• Aspetta qualche minuto e riprova\n"
                    "• Scegli un altro beat dal catalogo\n"
                    "• Controlla la sezione Bundle per offerte alternative",
                    parse_mode='HTML'
                )
                context.user_data.setdefault("warning_message_id", [])
                context.user_data["warning_message_id"].append(warning_msg.message_id)
            context.user_data["current_state"] = BEAT_SELECTION
            return BEAT_SELECTION

        # Prenotazione riuscita - salva l'ID del beat prenotato
        context.user_data["reserved_beat_id"] = beat_id

        # Mostra messaggio di prenotazione
        reservation_msg = (
            "🔒 <b>Beat prenotato!</b>\n\n"
            "Hai 10 minuti per completare l'acquisto.\n"
            "La prenotazione verrà rilasciata automaticamente se non completi il pagamento entro questo tempo.\n\n"
        )
    else:
        reservation_msg = ""

    # Costruisci il link alla pagina di checkout con token di validazione
    import hashlib
    import time
    
    # Genera un token di validazione basato su user_id, beat_id e timestamp
    timestamp = int(time.time())
    token_data = f"{user_id}_{beat['id']}_{timestamp}"
    validation_token = hashlib.md5(token_data.encode()).hexdigest()[:16]
    
    checkout_url = (
        f"https://prodbypegasus.pages.dev/checkout"
        f"?user_id={user_id}"
        f"&beat={quote(beat['title'])}"
        f"&beat_id={beat['id']}"
        f"&price={beat['price']:.2f}"
        f"&token={validation_token}"
        f"&timestamp={timestamp}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Procedi al pagamento", url=checkout_url)],
        [InlineKeyboardButton("📞 Contattaci", url="https://linktr.ee/ProdByPegasus")]
    ])

    payment_message = await query.message.reply_text(
        f"{reservation_msg}🎉 Per acquistare <b>{beat['title']}</b>, clicca sul pulsante qui sotto e completa il pagamento.\n\n"
        "Ti invierò il beat appena ricevo la conferma del pagamento.\n\n"
        "📞 Per assistenza utilizza il pulsante \"Contattaci\".",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

    # Salva l'ID del messaggio di pagamento per poterlo cancellare se l'utente naviga
    context.user_data["payment_message_id"] = payment_message.message_id

    context.user_data["current_state"] = BEAT_SELECTION
    return BEAT_SELECTION


# Rate limit config
MAX_INVALID_MSGS = 10         # Quanti messaggi errati prima del blocco temporaneo
BLOCK_DURATION_SEC = 60    # 1 minuti di blocco (puoi aumentare)

async def handle_wrong_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce input non validi: rate-limiting anti-spam, ignora richieste durante il blocco"""
    import time
    user_id = update.effective_user.id if update.effective_user else None
    now = time.time()

    # Inizializza strutture di rate-limiting
    if "invalid_msg_count" not in context.user_data:
        context.user_data["invalid_msg_count"] = 0
    if "blocked_until" not in context.user_data:
        context.user_data["blocked_until"] = 0

    # Se utente è bloccato, ignora completamente (non cancella, non risponde, non aggiorna contatore)
    if context.user_data["blocked_until"] > now:
        return context.user_data.get("current_state", GENRE)

    # Cancella il messaggio dell'utente se non è /start
    if update.message and update.message.text != "/start":
        try:
            await update.message.delete()
        except Exception as e:
            logger.debug(f"Errore cancellazione messaggio non valido: {e}")

    # Incrementa il contatore di messaggi errati
    context.user_data["invalid_msg_count"] += 1

    # Se supera la soglia, blocca temporaneamente l'utente e avvisa una sola volta
    if context.user_data["invalid_msg_count"] >= MAX_INVALID_MSGS:
        context.user_data["blocked_until"] = now + BLOCK_DURATION_SEC
        context.user_data["invalid_msg_count"] = 0
        chat = update.effective_chat
        if chat:
            try:
                mins = int(BLOCK_DURATION_SEC // 60)
                await chat.send_message(
                    f"🚫 Hai inviato troppi messaggi non validi. Non risponderò più alle tue richieste per {mins} minuti."
                )
            except Exception as e:
                logger.debug(f"Errore invio messaggio blocco: {e}")
        return context.user_data.get("current_state", GENRE)

    # Mantieni lo stato corrente
    return context.user_data.get("current_state", GENRE)

# Funzione duplicata rimossa - viene utilizzata quella originale sopra

async def show_filters_keyboard(update, context):
    """Mostra il pannello filtri unificato con selezioni temporanee"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # ⚡ CLEANUP AUTOMATICO: Se l'utente accede ai filtri, cancella prenotazioni precedenti
    cleanup_expired_reservations()
    has_reservation, reservation_info, reserved_beat_id = get_user_active_reservation(user_id)
    
    if has_reservation:
        # Rilascia automaticamente la prenotazione quando si accede ai filtri
        release_beat_reservation(reserved_beat_id, user_id)
        print(f"DEBUG: Prenotazione {reserved_beat_id} cancellata durante accesso filtri")
        
        # Cancella il messaggio di pagamento se esiste
        previous_payment_msg_id = context.user_data.get("payment_message_id")
        if previous_payment_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=previous_payment_msg_id
                )
            except Exception as e:
                pass  # Il messaggio potrebbe essere già stato cancellato
            
            context.user_data.pop("payment_message_id", None)
    
    # Inizializza filtri temporanei se non esistono
    if "temp_filters" not in context.user_data:
        context.user_data["temp_filters"] = {
            "genre": context.user_data.get("genre"),
            "mood": context.user_data.get("mood"),
            "price_range": context.user_data.get("price_range")
        }
    
    # Non fare query.answer() qui perché viene già fatto in handle_beat_navigation
    await show_main_filter_panel(query, context)

async def handle_filter_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce tutte le interazioni del pannello filtri unificato"""
    query = update.callback_query
    data = query.data

    # --- GESTIONE TASTI DISABILITATI ---
    if data.startswith("disabled_"):
        await query.answer("🚫 Al momento non ci sono beat disponibili per questa categoria.", show_alert=True)
        return BEAT_SELECTION

    await query.answer()

    # --- NAVIGAZIONE PANNELLI FILTRI ---
    if data == "filter_genre":
        await show_genre_selection(query, context)
        return BEAT_SELECTION
    
    if data == "filter_mood":
        await show_mood_selection(query, context)
        return BEAT_SELECTION
    
    if data == "filter_price":
        await show_price_selection(query, context)
        return BEAT_SELECTION
    
    if data == "back_to_filters":
        # ⚡ CLEANUP: Rilascia prenotazioni quando si naviga tra i pannelli filtri
        user_id = update.effective_user.id
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "navigazione pannelli filtri")
        
        await show_main_filter_panel(query, context)
        return BEAT_SELECTION

    # --- SELEZIONE GENERI ---
    if data.startswith("select_genre_"):
        genre = data.replace("select_genre_", "")
        context.user_data["temp_filters"]["genre"] = genre
        await show_main_filter_panel(query, context)
        return BEAT_SELECTION
    
    if data == "remove_genre":
        context.user_data["temp_filters"]["genre"] = None
        await show_main_filter_panel(query, context)
        return BEAT_SELECTION

    # --- SELEZIONE MOOD ---
    if data.startswith("select_mood_"):
        mood = data.replace("select_mood_", "")
        context.user_data["temp_filters"]["mood"] = mood
        await show_main_filter_panel(query, context)
        return BEAT_SELECTION
    
    if data == "remove_mood":
        context.user_data["temp_filters"]["mood"] = None
        await show_main_filter_panel(query, context)
        return BEAT_SELECTION

    # --- SELEZIONE PREZZO ---
    if data.startswith("select_price_"):
        price = data.replace("select_price_", "")
        context.user_data["temp_filters"]["price_range"] = price
        await show_main_filter_panel(query, context)
        return BEAT_SELECTION
    
    if data == "remove_price":
        context.user_data["temp_filters"]["price_range"] = None
        await show_main_filter_panel(query, context)
        return BEAT_SELECTION

    # --- APPLICAZIONE E CANCELLAZIONE FILTRI ---
    if data == "apply_filters":
        # ⚡ CLEANUP: Rilascia prenotazioni quando si applicano nuovi filtri
        user_id = update.effective_user.id
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "applicazione filtri")
        
        # Applica i filtri temporanei ai filtri effettivi
        temp_filters = context.user_data.get("temp_filters", {})
        
        context.user_data["genre"] = temp_filters.get("genre")
        context.user_data["mood"] = temp_filters.get("mood")
        context.user_data["price_range"] = temp_filters.get("price_range")
        
        # Pulisci i filtri temporanei
        context.user_data.pop("temp_filters", None)
        
        # Mostra il catalogo filtrato
        await show_filtered_catalog(update, context)
        return BEAT_SELECTION
    
    if data == "cancel_filters":
        # ⚡ CLEANUP: Rilascia prenotazioni quando si cancellano i filtri
        user_id = update.effective_user.id
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "cancellazione filtri")
        
        # Cancella i filtri temporanei e torna al catalogo
        context.user_data.pop("temp_filters", None)
        await show_beat_catalog(update, context)
        return BEAT_SELECTION

    return BEAT_SELECTION

async def show_main_filter_panel(query, context):
    """Mostra il pannello principale dei filtri con le selezioni correnti"""
    temp_filters = context.user_data.get("temp_filters", {})
    
    # Costruisci il messaggio con i filtri selezionati
    selected_filters = []
    if temp_filters.get("genre"):
        selected_filters.append(f"Genere: {temp_filters['genre']}")
    if temp_filters.get("mood"):
        selected_filters.append(f"Mood: {temp_filters['mood']}")
    if temp_filters.get("price_range") and temp_filters["price_range"] != "Tutti":
        selected_filters.append(f"Prezzo: {temp_filters['price_range']}")
    
    # Header del messaggio
    header = "🎧 <b>Trova il beat perfetto</b>\n\n"
    
    # Filtri selezionati (se presenti)
    if selected_filters:
        header += f"<i>Filtri selezionati: {' | '.join(selected_filters)}</i>\n\n"
    
    # Descrizione
    description = (
        "<b>Personalizza la tua ricerca scegliendo:</b>\n"
        "• <b>Genere:</b> Tipo di sonorità\n"
        "• <b>Mood:</b> Atmosfera che vuoi evocare\n"
        "• <b>Prezzo:</b> Imposta il tuo budget"
    )
    
    message_text = header + description
    
    # Controlla se almeno un filtro è selezionato per abilitare "Applica filtri"
    has_filters = any(temp_filters.get(k) for k in ["genre", "mood", "price_range"] if temp_filters.get(k) != "Tutti")
    
    keyboard = [
        [
            InlineKeyboardButton("🎼 Genere", callback_data="filter_genre"),
            InlineKeyboardButton("🎚️ Mood", callback_data="filter_mood")
        ],
        [InlineKeyboardButton("💰 Prezzo", callback_data="filter_price")],
    ]
    
    # Aggiungi bottone "Applica filtri" solo se ci sono filtri selezionati
    if has_filters:
        keyboard.append([InlineKeyboardButton("✅ Applica filtri", callback_data="apply_filters")])
    
    keyboard.append([InlineKeyboardButton("❌ Annulla", callback_data="cancel_filters")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        # Se il messaggio ha una foto, usa edit_message_media per sostituirla con testo
        if query.message.photo:
            # Cancella il messaggio con foto e invia un nuovo messaggio di testo
            await query.message.delete()
            sent = await query.message.chat.send_message(
                message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            # Aggiorna l'ID del messaggio nel context per future modifiche
            context.user_data["last_bot_message_id"] = sent.message_id
        else:
            # Se è già un messaggio di testo, editalo normalmente
            await query.edit_message_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
    except Exception as e:
        logger.error(f"Errore aggiornamento pannello filtri: {e}")
        # Fallback: cancella e ricrea il messaggio
        try:
            await query.message.delete()
        except Exception:
            pass
        try:
            sent = await query.message.chat.send_message(
                message_text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
            context.user_data["last_bot_message_id"] = sent.message_id
        except Exception as fallback_error:
            logger.error(f"Errore fallback pannello filtri: {fallback_error}")

async def show_genre_selection(query, context):
    """Mostra la selezione dei generi disponibili"""
    category = context.user_data.get("catalog_category", "standard")
    temp_filters = context.user_data.get("temp_filters", {})
    
    # Calcola generi disponibili considerando i filtri già selezionati
    with SessionLocal() as session:
        q = session.query(Beat.genre).distinct()
        
        # Applica filtro categoria
        if category == "exclusive":
            q = q.filter(Beat.is_exclusive == 1)
        elif category == "discount":
            q = q.filter(Beat.is_discounted == 1)
        else:
            q = q.filter(Beat.is_exclusive == 0)
        
        # Applica filtri esistenti
        if temp_filters.get("mood"):
            q = q.filter(Beat.mood == temp_filters["mood"])
        if temp_filters.get("price_range") and temp_filters["price_range"] != "Tutti":
            price_range = temp_filters["price_range"]
            if price_range == "0-10€":
                q = q.filter(Beat.price >= 0, Beat.price <= 10)
            elif price_range == "10-20€":
                q = q.filter(Beat.price > 10, Beat.price <= 20)
            elif price_range == "20-30€":
                q = q.filter(Beat.price > 20, Beat.price <= 30)
            elif price_range == "30€+":
                q = q.filter(Beat.price > 30)
        
        available_genres = set(g for (g,) in q)

    # Lista dei generi da mostrare
    genres = [
        ("Trap", "Hip-Hop"),
        ("Drill", "R&B"),
        ("Raggeton", "Brazilian Funk"),
    ]
    
    keyboard = []
    for g1, g2 in genres:
        row = []
        for g in (g1, g2):
            if g in available_genres:
                # Segna il genere come selezionato se è quello corrente
                if temp_filters.get("genre") == g:
                    row.append(InlineKeyboardButton(f"✅ {g}", callback_data=f"select_genre_{g}"))
                else:
                    row.append(InlineKeyboardButton(g, callback_data=f"select_genre_{g}"))
            else:
                row.append(InlineKeyboardButton(f"🚫 {g}", callback_data=f"disabled_genre_{g}"))
        keyboard.append(row)
    
    # Opzione per rimuovere il filtro genere
    if temp_filters.get("genre"):
        keyboard.append([InlineKeyboardButton("🗑️ Rimuovi filtro genere", callback_data="remove_genre")])
    
    keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="back_to_filters")])
    
    message_text = "🎼 <b>Seleziona un genere:</b>\n\nScegli il tipo di sonorità che preferisci per il tuo beat."
    
    try:
        await query.edit_message_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Errore selezione genere: {e}")
        # Fallback: ricrea il messaggio
        try:
            await query.message.delete()
            sent = await query.message.chat.send_message(
                message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            context.user_data["last_bot_message_id"] = sent.message_id
        except Exception as fallback_error:
            logger.error(f"Errore fallback selezione genere: {fallback_error}")

async def show_mood_selection(query, context):
    """Mostra la selezione dei mood disponibili"""
    category = context.user_data.get("catalog_category", "standard")
    temp_filters = context.user_data.get("temp_filters", {})
    
    # Calcola mood disponibili considerando i filtri già selezionati
    with SessionLocal() as session:
        q = session.query(Beat.mood).distinct()
        
        # Applica filtro categoria
        if category == "exclusive":
            q = q.filter(Beat.is_exclusive == 1)
        elif category == "discount":
            q = q.filter(Beat.is_discounted == 1)
        else:
            q = q.filter(Beat.is_exclusive == 0)
        
        # Applica filtri esistenti
        if temp_filters.get("genre"):
            q = q.filter(Beat.genre == temp_filters["genre"])
        if temp_filters.get("price_range") and temp_filters["price_range"] != "Tutti":
            price_range = temp_filters["price_range"]
            if price_range == "0-10€":
                q = q.filter(Beat.price >= 0, Beat.price <= 10)
            elif price_range == "10-20€":
                q = q.filter(Beat.price > 10, Beat.price <= 20)
            elif price_range == "20-30€":
                q = q.filter(Beat.price > 20, Beat.price <= 30)
            elif price_range == "30€+":
                q = q.filter(Beat.price > 30)
        
        available_moods = set(m for (m,) in q)

    # Lista dei mood da mostrare
    moods = [
        ("Love", "Sad"),
        ("Hard", "Dark"),
        ("Chill", "Epic"),
        ("Happy", "Hype"),
        ("Emotional",),
    ]
    
    keyboard = []
    for row in moods:
        mood_row = []
        for m in row:
            if m in available_moods:
                # Segna il mood come selezionato se è quello corrente
                if temp_filters.get("mood") == m:
                    mood_row.append(InlineKeyboardButton(f"✅ {m}", callback_data=f"select_mood_{m}"))
                else:
                    mood_row.append(InlineKeyboardButton(m, callback_data=f"select_mood_{m}"))
            else:
                mood_row.append(InlineKeyboardButton(f"🚫 {m}", callback_data=f"disabled_mood_{m}"))
        keyboard.append(mood_row)
    
    # Opzione per rimuovere il filtro mood
    if temp_filters.get("mood"):
        keyboard.append([InlineKeyboardButton("🗑️ Rimuovi filtro mood", callback_data="remove_mood")])
    
    keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="back_to_filters")])
    
    message_text = "🎚️ <b>Seleziona un mood:</b>\n\nScegli l'atmosfera che vuoi evocare con il tuo beat."
    
    try:
        await query.edit_message_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Errore selezione mood: {e}")
        # Fallback: ricrea il messaggio
        try:
            await query.message.delete()
            sent = await query.message.chat.send_message(
                message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            context.user_data["last_bot_message_id"] = sent.message_id
        except Exception as fallback_error:
            logger.error(f"Errore fallback selezione mood: {fallback_error}")

async def show_price_selection(query, context):
    """Mostra la selezione delle fasce di prezzo disponibili"""
    category = context.user_data.get("catalog_category", "standard")
    temp_filters = context.user_data.get("temp_filters", {})
    
    # Calcola fasce di prezzo disponibili considerando i filtri già selezionati
    with SessionLocal() as session:
        def price_count(cond):
            q = session.query(Beat)
            
            # Applica filtro categoria
            if category == "exclusive":
                q = q.filter(Beat.is_exclusive == 1)
            elif category == "discount":
                q = q.filter(Beat.is_discounted == 1)
            else:
                q = q.filter(Beat.is_exclusive == 0)
            
            # Applica filtri esistenti
            if temp_filters.get("genre"):
                q = q.filter(Beat.genre == temp_filters["genre"])
            if temp_filters.get("mood"):
                q = q.filter(Beat.mood == temp_filters["mood"])
                
            return q.filter(*cond).count() > 0

        prices_available = {
            "0-10€": price_count([Beat.price >= 0, Beat.price <= 10]),
            "10-20€": price_count([Beat.price > 10, Beat.price <= 20]),
            "20-30€": price_count([Beat.price > 20, Beat.price <= 30]),
            "30€+": price_count([Beat.price > 30]),
            "Tutti": price_count([]),
        }
    
    price_rows = [
        ["0-10€", "10-20€"],
        ["20-30€", "30€+"],
        ["Tutti"]
    ]
    
    keyboard = []
    for row in price_rows:
        btn_row = []
        for p in row:
            if prices_available.get(p, False):
                # Segna il prezzo come selezionato se è quello corrente
                if temp_filters.get("price_range") == p:
                    btn_row.append(InlineKeyboardButton(f"✅ {p}", callback_data=f"select_price_{p}"))
                else:
                    btn_row.append(InlineKeyboardButton(p, callback_data=f"select_price_{p}"))
            else:
                btn_row.append(InlineKeyboardButton(f"🚫 {p}", callback_data=f"disabled_price_{p}"))
        keyboard.append(btn_row)
    
    # Opzione per rimuovere il filtro prezzo
    if temp_filters.get("price_range") and temp_filters["price_range"] != "Tutti":
        keyboard.append([InlineKeyboardButton("🗑️ Rimuovi filtro prezzo", callback_data="remove_price")])
    
    keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="back_to_filters")])
    
    message_text = "💰 <b>Seleziona una fascia di prezzo:</b>\n\nImposta il tuo budget per trovare i beat più adatti."
    
    try:
        await query.edit_message_text(
            message_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Errore selezione prezzo: {e}")
        # Fallback: ricrea il messaggio
        try:
            await query.message.delete()
            sent = await query.message.chat.send_message(
                message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='HTML'
            )
            context.user_data["last_bot_message_id"] = sent.message_id
        except Exception as fallback_error:
            logger.error(f"Errore fallback selezione prezzo: {fallback_error}")

# ====== FUNZIONI PER GESTIONE BUNDLE ======

async def show_bundles_catalog(update, context):
    """Mostra il catalogo dei bundle promozionali"""
    query = update.callback_query
    user_id = update.effective_user.id
    
    # ⚡ CLEANUP: Rilascia eventuali prenotazioni attive quando si va nei bundle
    await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "accesso sezione bundle")
    
    # Recupera i bundle attivi
    bundles = get_active_bundles()
    
    if not bundles:
        # Nessun bundle disponibile
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Torna al menu", callback_data="menu")]
        ])
        try:
            await query.edit_message_text(
                "❌ <b>Nessun bundle disponibile al momento!</b>\n\n"
                "I bundle promozionali non sono attualmente attivi. "
                "Torna a controllare più tardi per offerte speciali!",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except Exception:
            pass
        context.user_data["current_state"] = CATEGORY
        return CATEGORY
    
    # Salva i bundle in user_data per la navigazione
    context.user_data["bundles"] = bundles
    context.user_data["bundle_index"] = 0
    context.user_data["current_state"] = BUNDLE_SELECTION
    
    return await show_bundle_details(update, context)

async def show_bundle_details(update, context):
    """Mostra i dettagli di un bundle specifico"""
    query = update.callback_query
    await query.answer()
    
    bundles = context.user_data["bundles"]
    idx = context.user_data["bundle_index"]
    bundle = bundles[idx]
    
    # Costruisci la caption del bundle
    caption = build_bundle_caption(bundle, idx, len(bundles))
    
    # Costruisci la tastiera di navigazione
    keyboard = build_bundle_navigation_keyboard(bundles)
    
    # Mostra il bundle
    try:
        await update_message_with_bundle(query, bundle, caption, keyboard)
    except Exception as e:
        logger.error(f"Errore visualizzazione bundle: {e}")
    
    context.user_data["current_state"] = BUNDLE_SELECTION
    return BUNDLE_SELECTION

def build_bundle_caption(bundle, idx, total_bundles):
    """Costruisce la caption per un bundle con gestione avanzata beat esclusivi"""
    lines = []
    
    # Header del bundle
    lines.append(f"🎁 <b>BUNDLE #{idx+1}/{total_bundles}</b>")
    lines.append(f"<b>{bundle['name']}</b>")
    lines.append("")
    
    # Descrizione se presente
    if bundle.get('description'):
        lines.append(f"<i>{bundle['description']}</i>")
        lines.append("")
    
    # Informazioni sui prezzi
    individual_price = bundle['individual_price']
    bundle_price = bundle['bundle_price']
    discount_percent = bundle['discount_percent']
    savings = individual_price - bundle_price
    
    lines.append("<b>💰 PREZZI:</b>")
    lines.append(f"Prezzo singoli beat: <s>{individual_price:.2f}€</s>")
    lines.append(f"<b>Prezzo bundle: {bundle_price:.2f}€</b>")
    lines.append(f"<b>🔥 Risparmi: {savings:.2f}€ ({discount_percent}%)</b>")
    lines.append("")
    
    # Lista dei beat inclusi
    lines.append(f"<b>🎵 BEAT INCLUSI ({len(bundle['beats'])}):</b>")
    exclusive_count = 0
    total_beats = len(bundle['beats'])
    
    for i, beat in enumerate(bundle['beats'], 1):
        # Marcatore per beat esclusivi
        exclusive_marker = ""
        if beat.get('is_exclusive', False):
            exclusive_marker = " 🔒"
            exclusive_count += 1
        
        lines.append(f"{i}. <b>{beat['title']}</b> ({beat['genre']} - {beat['mood']}){exclusive_marker}")
    
    # Avvertenze intelligenti per beat esclusivi
    if exclusive_count > 0:
        lines.append("")
        
        if exclusive_count == total_beats:
            # Bundle contiene SOLO beat esclusivi
            lines.append("🔒 <b>BUNDLE ESCLUSIVO LIMITATO!</b>")
            if exclusive_count == 1:
                lines.append("⚡ <i>Questo beat è disponibile per una sola persona!</i>")
            else:
                lines.append(f"⚡ <i>Questi {exclusive_count} beat sono disponibili per una sola persona!</i>")
            lines.append("🏃‍♂️ <i>Solo il primo acquirente potrà riceverli - dopo l'acquisto il bundle sarà eliminato!</i>")
        else:
            # Bundle misto (esclusivi + regolari)
            regular_count = total_beats - exclusive_count
            lines.append("🔒 <b>ATTENZIONE - CONTENUTO VARIABILE:</b>")
            if exclusive_count == 1:
                lines.append(f"⚡ <i>1 beat è esclusivo (🔒) - solo il primo acquirente lo riceverà!</i>")
            else:
                lines.append(f"⚡ <i>{exclusive_count} beat sono esclusivi (🔒) - solo il primo acquirente li riceverà!</i>")
            
            if regular_count == 1:
                lines.append(f"📦 <i>Il beat rimanente sarà sempre disponibile negli acquisti successivi.</i>")
            else:
                lines.append(f"📦 <i>I {regular_count} beat rimanenti saranno sempre disponibili negli acquisti successivi.</i>")
            lines.append("💰 <i>Il prezzo del bundle verrà ricalcolato automaticamente dopo il primo acquisto.</i>")
        
        lines.append("")
        lines.append("⏰ <b>AFFRETTATI!</b> <i>La disponibilità può cambiare in qualsiasi momento!</i>")
    
    return "\n".join(lines)

def build_bundle_navigation_keyboard(bundles):
    """Costruisce la tastiera di navigazione per i bundle"""
    keyboard = []
    
    # Riga di navigazione (se ci sono più bundle)
    if len(bundles) > 1:
        nav_row = [
            InlineKeyboardButton("⬅️ Bundle precedente", callback_data="bundle_prev"),
            InlineKeyboardButton("Bundle successivo ➡️", callback_data="bundle_next")
        ]
        keyboard.append(nav_row)
    
    # Riga delle azioni principali
    keyboard.append([
        InlineKeyboardButton("🎧 Ascolta preview", callback_data="bundle_preview"),
        InlineKeyboardButton("💸 Acquista bundle", callback_data="bundle_buy")
    ])
    
    # Riga di supporto e menu
    keyboard.append([
        InlineKeyboardButton("📞 Contattaci", url="https://linktr.ee/ProdByPegasus"),
        InlineKeyboardButton("🔙 Torna al menu", callback_data="menu")
    ])
    
    return keyboard

async def update_message_with_bundle(query, bundle, caption, keyboard):
    """Aggiorna il messaggio con l'immagine e i dettagli del bundle"""
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Usa l'immagine del bundle se disponibile, altrimenti testo semplice
    image_key = bundle.get("image_key")
    if image_key:
        # Usa la configurazione centralizzata R2
        image_url = f"{R2_PUBLIC_BASE}/{image_key}"
        
        if validate_url(image_url):
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(image_url, caption=caption, parse_mode='HTML'),
                    reply_markup=reply_markup
                )
                return
            except Exception as e:
                logger.debug(f"Errore caricamento immagine bundle: {e}")
    
    # Fallback: messaggio di testo
    try:
        await query.edit_message_text(
            caption, reply_markup=reply_markup, parse_mode='HTML'
        )
    except Exception:
        try:
            await query.message.delete()
        except Exception as ex:
            logger.debug(f"Errore eliminazione messaggio: {ex}")
        await query.message.chat.send_message(
            caption, reply_markup=reply_markup, parse_mode='HTML'
        )

async def handle_bundle_navigation(update, context):
    """Gestisce la navigazione dei bundle"""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    
    await query.answer()
    
    
    if data == "menu":
        # ⚡ CLEANUP: Rilascia prenotazioni bundle quando si torna al menu
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "ritorno al menu dai bundle")
        
        await send_welcome_message(update, context)
        context.user_data["current_state"] = CATEGORY
        return CATEGORY
    
    bundles = context.user_data["bundles"]
    idx = context.user_data["bundle_index"]
    
    if data == "bundle_prev":
        # ⚡ CLEANUP: Rilascia prenotazioni bundle quando si naviga tra bundle
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "navigazione tra bundle (precedente)")
        
        context.user_data["bundle_index"] = (idx - 1) % len(bundles)
        return await show_bundle_details(update, context)
    
    elif data == "bundle_next":
        # ⚡ CLEANUP: Rilascia prenotazioni bundle quando si naviga tra bundle
        await cleanup_user_reservation_and_payment(user_id, context, update.effective_chat.id, "navigazione tra bundle (successivo)")
        
        context.user_data["bundle_index"] = (idx + 1) % len(bundles)
        return await show_bundle_details(update, context)
    
    elif data == "bundle_preview":
        return await send_bundle_preview(update, context)
    
    elif data == "bundle_buy":
        return await handle_bundle_payment(update, context)
    
    context.user_data["current_state"] = BUNDLE_SELECTION
    return BUNDLE_SELECTION

async def send_bundle_preview(update, context):
    """Invia le preview di tutti i beat del bundle"""
    query = update.callback_query
    bundles = context.user_data["bundles"]
    idx = context.user_data["bundle_index"]
    bundle = bundles[idx]
    
    # Invia un messaggio con le preview di tutti i beat del bundle
    preview_text = f"🎧 <b>Preview del bundle: {bundle['name']}</b>\n\n"
    preview_text += "Ecco le anteprime di tutti i beat inclusi nel bundle:\n\n"
    
    await query.message.reply_text(preview_text, parse_mode='HTML')
    
    # Invia ogni preview
    for i, beat in enumerate(bundle['beats'], 1):
        preview_key = beat.get('preview_key')
        if preview_key:
            # Usa la configurazione centralizzata R2
            preview_url = f"{R2_PUBLIC_BASE}/{preview_key}"
            
            try:
                await query.message.reply_audio(
                    audio=preview_url,
                    caption=f"🎵 {i}. <b>{beat['title']}</b>\n{beat['genre']} - {beat['mood']}",
                    parse_mode='HTML'
                )
            except Exception as e:
                logger.error(f"Errore invio preview beat {beat['title']}: {e}")
                await query.message.reply_text(
                    f"❌ Errore nel caricamento della preview di {beat['title']}"
                )
    
    context.user_data["current_state"] = BUNDLE_SELECTION
    return BUNDLE_SELECTION

async def handle_bundle_payment(update, context):
    """Gestisce il pagamento di un bundle"""
    query = update.callback_query
    await query.answer()
    
    bundles = context.user_data["bundles"]
    idx = context.user_data["bundle_index"]
    bundle = bundles[idx]
    user_id = update.effective_user.id

    # Log dettagliato per tracciare le race conditions
    logger.info(f"🔍 BUNDLE PAYMENT START - User: {user_id}, Bundle: {bundle['id']} '{bundle['name']}'")
    
    # ⚡ CONTROLLO PRENOTAZIONE UTENTE ATTIVA
    # Prima di tutto, verifica se l'utente ha già una prenotazione attiva
    has_reservation, reservation_info, _ = get_user_active_reservation(user_id)
    if has_reservation:
        logger.info(f"❌ User {user_id} already has active reservation: {reservation_info}")
        await query.message.reply_text(
            "⏰ <b>Acquisto bundle non possibile!</b>\n\n"
            f"📋 {reservation_info}\n\n"
            "💡 <b>Per acquistare un bundle devi:</b>\n"
            "• Completare l'acquisto del beat già prenotato, oppure\n"
            "• Aspettare che scada la prenotazione attuale\n\n"
            "🚫 <i>Non puoi acquistare bundle mentre hai prenotazioni attive.</i>",
            parse_mode='HTML'
        )
        context.user_data["current_state"] = BUNDLE_SELECTION
        return BUNDLE_SELECTION
    
    # ⚡ CONTROLLO DISPONIBILITÀ BEAT NEL BUNDLE CON PRENOTAZIONE ATOMICA
    # Prima controlla se ci sono beat esclusivi nel bundle
    exclusive_beats_in_bundle = [beat for beat in bundle['beats'] if beat.get('is_exclusive') == 1]
    logger.info(f"🔒 Bundle {bundle['id']} has {len(exclusive_beats_in_bundle)} exclusive beats")
    
    if exclusive_beats_in_bundle:
        # Se ci sono beat esclusivi, prova a prenotare tutto il bundle atomicamente CON RETRY
        logger.info(f"⚡ ATTEMPTING ATOMIC RESERVATION WITH RETRY - User: {user_id}, Bundle: {bundle['id']}")
        success, message = reserve_bundle_exclusive_beats_with_retry(bundle['id'], user_id, reservation_minutes=10, max_retries=3)
        logger.info(f"⚡ RESERVATION RESULT - User: {user_id}, Bundle: {bundle['id']}, Success: {success}, Message: {message}")
        
        if not success:
            logger.warning(f"❌ RESERVATION FAILED - User: {user_id}, Bundle: {bundle['id']}, Reason: {message}")
            warning_msg = await query.message.reply_text(
                f"❌ <b>Bundle non disponibile!</b>\n\n"
                f"📋 <b>Motivo:</b> {message}\n\n"
                "💡 <b>Cosa fare:</b>\n"
                "• Aspetta qualche minuto e riprova\n"
                "• Acquista i singoli beat disponibili\n"
                "• Controlla altri bundle nel catalogo",
                parse_mode='HTML'
            )
            context.user_data.setdefault("warning_message_id", [])
            context.user_data["warning_message_id"].append(warning_msg.message_id)
            context.user_data["current_state"] = BUNDLE_SELECTION
            return BUNDLE_SELECTION
        
        # Prenotazione riuscita - salva gli ID per cleanup
        context.user_data["reserved_bundle_id"] = bundle['id']
        logger.info(f"✅ RESERVATION SUCCESS - User: {user_id}, Bundle: {bundle['id']}, Reserved for cleanup")
        
        # Mostra messaggio di prenotazione
        reservation_msg = (
            f"🔒 <b>Bundle prenotato!</b>\n\n"
            f"📦 Bundle '{bundle['name']}' con {len(exclusive_beats_in_bundle)} beat esclusivi prenotato per 10 minuti.\n"
            f"La prenotazione verrà rilasciata automaticamente se non completi il pagamento entro questo tempo.\n\n"
        )
    else:
        # Nessun beat esclusivo, nessuna prenotazione necessaria
        logger.info(f"ℹ️ Bundle {bundle['id']} has no exclusive beats, no reservation needed")
        reservation_msg = ""
    
    # Se tutti i beat sono disponibili, procedi con l'acquisto
    logger.info(f"💸 GENERATING PAYMENT LINK - User: {user_id}, Bundle: {bundle['id']}")
    
    # Costruisci il link alla pagina di checkout per bundle
    checkout_url = (
        f"https://prodbypegasus.pages.dev/checkout"
        f"?user_id={user_id}"
        f"&bundle_id={bundle['id']}"
        f"&bundle_name={quote(bundle['name'])}"
        f"&price={bundle['bundle_price']:.2f}"
        f"&type=bundle"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Procedi al pagamento", url=checkout_url)],
        [InlineKeyboardButton("📞 Contattaci", url="https://linktr.ee/ProdByPegasus")]
    ])
    
    logger.info(f"✅ PAYMENT LINK SENT - User: {user_id}, Bundle: {bundle['id']}, URL: {checkout_url}")
    
    payment_message = await query.message.reply_text(
        f"{reservation_msg}"
        f"🎁 <b>Acquista il bundle: {bundle['name']}</b>\n\n"
        f"💰 Prezzo: <b>{bundle['bundle_price']:.2f}€</b>\n"
        f"🔥 Risparmi: <b>{bundle['individual_price'] - bundle['bundle_price']:.2f}€</b>\n\n"
        f"🎵 Riceverai <b>{len(bundle['beats'])} beat</b> in formato WAV!\n\n"
        "Clicca sul pulsante qui sotto per completare l'acquisto.\n"
        "Ti invierò tutti i beat appena ricevo la conferma del pagamento.\n\n"
        "📞 Per assistenza utilizza il pulsante \"Contattaci\".",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

    # ⚡ TRACCIA: Salva l'ID del messaggio di pagamento bundle per cleanup automatico
    context.user_data["payment_message_id"] = payment_message.message_id
    
    context.user_data["current_state"] = BUNDLE_SELECTION
    return BUNDLE_SELECTION
