# callbacks.py
import os
import logging
import time
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
from db_manager import SessionLocal, Beat
from telegram.ext import JobQueue
from telegram.constants import ParseMode

# Configure logging
logger = logging.getLogger(__name__)

# Gestione variabili ambiente: locale (.env) o produzione (Render.com)
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    from dotenv import load_dotenv
    load_dotenv(env_path)

# Get Cloudflare R2 public URL from .env
R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_BASE = os.environ.get("R2_PUBLIC_BASE_URL")  # Use a direct public URL
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")

# Constants for conversation states
GENRE, MOOD, BEAT_SELECTION = range(3)

# Nuovo stato per la selezione categoria
CATEGORY = 1000  # Usa un valore fuori range per evitare collisioni

# Messaggio di benvenuto centralizzato (ora in HTML)
WELCOME_TEXT = """🎵 <b>Benvenuto nel catalogo dei beat ProdByPeagsus!</b>

Ascolta in anteprima e acquista beat originali, tutti con licenza commerciale.

Scegli come iniziare:

💸 <b>Beat scontati</b>  
Offerte attive su beat selezionati.

🎖️ <b>Beat esclusivi</b>  
Disponibili in copia unica. Dopo l'acquisto non saranno più rivenduti.

🎶 <b>Beat standard</b>  
Tutti gli altri beat disponibili nel catalogo.

👇 Seleziona una categoria per iniziare:
"""

async def send_welcome_message(update, context, edit=False):
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return

    # Reset filtri utente quando si torna al menu principale
    context.user_data.pop("genre", None)
    context.user_data.pop("mood", None)
    context.user_data.pop("price_range", None)

    # Nuova tastiera con le tre categorie richieste
    keyboard = [
        ["🎶 Beat standard"],
        ["💸 Beat scontati", "🎖️ Beat esclusivi"]
    ]
    # Trasforma in InlineKeyboardButton
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

    # Invia nuovo menu
    sent = await chat.send_message(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    context.user_data["last_bot_message_id"] = sent.message_id
    context.user_data["chat_id"] = chat.id
    context.user_data["warning_shown"] = False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return context.user_data.get("current_state", CATEGORY)
    await send_welcome_message(update, context)
    context.user_data["current_state"] = CATEGORY
    return CATEGORY

async def genre_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return context.user_data.get("current_state", GENRE)
    query = update.callback_query
    genre_label = query.data

    if genre_label.startswith(("disabled_", "🚫")):
        await query.answer("🚫 Attualmente non ci sono beat disponibili per questa categoria.", show_alert=True)
        context.user_data["current_state"] = GENRE
        return GENRE

    await query.answer()
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
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return context.user_data.get("current_state", GENRE)
    query = update.callback_query
    data = query.data

    if data == "back":
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

    genre = context.user_data["genre"]
    mood = parse_mood_label(data)
    context.user_data["mood"] = mood

    with SessionLocal() as session:
        beats = session.query(Beat).filter_by(genre=genre, mood=mood).all()

    context.user_data["beats"] = []
    for b in beats:
        # Se le chiavi NON contengono già il path, aggiungilo qui:
        def ensure_path(key, kind):
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

        preview_key = ensure_path(b.preview_key, "preview")
        file_key = ensure_path(b.file_key, "file")
        image_key = ensure_path(b.image_key, "image")

        preview_url = f"{R2_PUBLIC_BASE}/{quote(preview_key)}" if preview_key else None
        file_url = f"{R2_PUBLIC_BASE}/{quote(file_key)}" if file_key else None
        image_url = f"{R2_PUBLIC_BASE}/{quote(image_key)}" if image_key else None

        beat_data = {
            "title": b.title,
            "genre": b.genre,
            "mood": b.mood,
            "preview_url": preview_url,
            "file_url": file_url,
            "image_url": image_url,
            "price": b.price,
            "original_price": b.original_price,
            "is_discounted": b.is_discounted,
            "discount_percent": b.discount_percent,
            "is_exclusive": b.is_exclusive,  # <-- assicurati che venga passato!
        }
        context.user_data["beats"].append(beat_data)

    context.user_data["beat_index"] = 0
    context.user_data["current_state"] = BEAT_SELECTION
    return await show_beat_catalog(update, context)

async def category_selected(update, context):
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return CATEGORY

    query = update.callback_query
    data = query.data

    # Gestione ritorno al menu principale
    if data == "menu":
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
    else:
        await query.answer("Categoria non valida.", show_alert=True)
        return CATEGORY

    await query.answer()
    # Mostra subito il catalogo filtrato (shuffle)
    return await show_filtered_catalog(update, context)

async def show_filtered_catalog(update, context):
    """Mostra il catalogo filtrato in base alla categoria scelta, con UI a scorrimento e filtri"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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
    context.user_data["beats"] = []
    for b in beats:
        def ensure_path(key, kind):
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

        preview_key = ensure_path(b.preview_key, "preview")
        file_key = ensure_path(b.file_key, "file")
        image_key = ensure_path(b.image_key, "image")

        preview_url = f"{R2_PUBLIC_BASE}/{quote(preview_key)}" if preview_key else None
        file_url = f"{R2_PUBLIC_BASE}/{quote(file_key)}" if file_key else None
        image_url = f"{R2_PUBLIC_BASE}/{quote(image_key)}" if image_key else None
        # DEBUG: stampa i valori estratti dal DB
        print(f"[DEBUG] DB BEAT id={b.id} title={b.title} is_exclusive={getattr(b, 'is_exclusive', None)} is_discounted={getattr(b, 'discount_percent', None)} original_price={getattr(b, 'original_price', None)} price={getattr(b, 'price', None)}")
        beat_data = {
            "title": b.title,
            "genre": b.genre,
            "mood": b.mood,
            "preview_url": preview_url,
            "file_url": file_url,
            "image_url": image_url,
            "price": b.price,
            "original_price": b.original_price,
            "is_discounted": b.is_discounted,
            "discount_percent": b.discount_percent,
            "is_exclusive": b.is_exclusive,  # <-- assicurati che venga passato!
        }
        context.user_data["beats"].append(beat_data)

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

    # DEBUG: stampa i valori per capire cosa arriva
    print(f"[DEBUG] build_beat_caption idx={idx} title={title} is_exclusive={is_exclusive} is_discounted={is_discounted} discount_percent={discount_percent} original_price={original_price} price={price}")

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
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return context.user_data.get("current_state", GENRE)
    query = update.callback_query
    await query.answer()
    await show_loading(query)

    beats = context.user_data["beats"]
    idx = context.user_data["beat_index"]
    beat = beats[idx]

    # Ricava filtri attivi
    filtri = []
    cat = context.user_data.get("catalog_category")
    if cat == "standard":
        filtri.append("Standard")
    elif cat == "discount":
        filtri.append("Scontati")
    elif cat == "exclusive":
        filtri.append("Esclusivi")
    genre = context.user_data.get("genre")
    if genre:
        filtri.append(f"Genere: {genre}")
    mood = context.user_data.get("mood")
    if mood:
        filtri.append(f"Mood: {mood}")

    filtri_str = " | ".join(filtri)
    if filtri_str:
        filtri_str = f"<i>Filtri attivi: {filtri_str}</i>\n\n"

    # Usa la funzione riutilizzabile per la caption
    caption = build_beat_caption(beat, idx, filtri_str)

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

    # Un solo pulsante per i filtri
    filter_row = [
        InlineKeyboardButton("📞 Contattaci", url="https://linktr.ee/ProdByPegasus"),
        InlineKeyboardButton("🔎 Cambia filtri", callback_data="change_filters")
        
    ]

    keyboard = [
        nav_row,
        [InlineKeyboardButton("🎧 Spoiler", callback_data="preview")],  # Spoiler su tutta la riga
        [InlineKeyboardButton("💸 Acquista", callback_data="buy")],
        filter_row,
        [InlineKeyboardButton("🔙 Torna al menu", callback_data="menu")]
    ]

    try:
        image_url = beat.get("image_url")
        print(f"[DEBUG] show_beat_catalog idx={idx} title={beat['title']} image_url={image_url}")
        if image_url and validate_url(image_url):
            from random import randint
            sep = '&' if '?' in image_url else '?'
            image_url_refresh = f"{image_url}{sep}_={randint(1, 999999)}"
            try:
                await query.edit_message_media(
                    media=InputMediaPhoto(image_url_refresh, caption=caption, parse_mode='HTML'),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                print(f"[DEBUG] edit_message_media con dummy param fallita: {e}")
                try:
                    await query.edit_message_media(
                        media=InputMediaPhoto(image_url, caption=caption, parse_mode='HTML'),
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except Exception as e2:
                    print(f"[DEBUG] edit_message_media senza dummy param fallita: {e2}")
                    try:
                        await query.message.delete()
                    except Exception as ex:
                        print(f"[LOG] Errore eliminazione messaggio: {ex}")
                    await query.message.chat.send_photo(
                        photo=image_url,
                        caption=caption,
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
        else:
            print(f"[DEBUG] Immagine non valida o mancante: {image_url}")
            try:
                await query.edit_message_text(
                    caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                )
            except Exception as e:
                try:
                    await query.message.delete()
                except Exception as ex:
                    print(f"[LOG] Errore eliminazione messaggio: {ex}")
                await query.message.chat.send_message(
                    caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
                )
    except Exception as e:
        print(f"[LOG] Errore generale show_beat_catalog: {e}")
    # Prima di mostrare il catalogo, resetta il tracking della preview per il beat corrente
    idx = context.user_data["beat_index"]
    context.user_data["last_preview_idx"] = None  # Reset preview tracking per ogni cambio beat
    context.user_data["current_state"] = BEAT_SELECTION
    return BEAT_SELECTION

async def handle_beat_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return context.user_data.get("current_state", GENRE)
    query = update.callback_query
    data = query.data
    beats = context.user_data["beats"]
    idx = context.user_data["beat_index"]

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
        # Mostra il menu principale invece della scelta del genere
        await delete_last_preview(context)
        await send_welcome_message(update, context)
        context.user_data["current_state"] = CATEGORY
        return CATEGORY

    if data == "change_filters":
        await show_filters_keyboard(update, context)
        return BEAT_SELECTION

    await query.answer()

    if data == "prev":
        context.user_data["beat_index"] = (idx - 1) % len(beats)
        await delete_last_preview(context)
    elif data == "next":
        context.user_data["beat_index"] = (idx + 1) % len(beats)
        await delete_last_preview(context)
    elif data == "buy":
        context.user_data["current_state"] = BEAT_SELECTION
        return await handle_payment(update, context)

    context.user_data["current_state"] = BEAT_SELECTION
    return await show_beat_catalog(update, context)

async def send_beat_preview(update, context):
    """Send audio preview of current beat, with anti-abuse logic"""
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return context.user_data.get("current_state", GENRE)
    if is_user_blocked(context):
        await blockeduser_response(update, context)
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
        # Usa direttamente l'URL come audio, ma aggiungi filename solo se Telegram lo supporta (potrebbe essere ignorato per URL remoti)
        filename = f"{beat['title'].upper()} {beat['genre'].upper()} {beat['mood'].upper()} SPOILER.mp3"
        sent = await query.message.reply_audio(
            audio=beat["preview_url"],
            caption=f"🎧 Preview di *{beat['title']}*",
            parse_mode='Markdown',
            # filename=filename  # Telegram ignora filename per URL remoti, ma lasciato come commento per chiarezza
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
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return context.user_data.get("current_state", GENRE)
    
    query = update.callback_query
    await query.answer()
    
    idx = context.user_data["beat_index"]
    beat = context.user_data["beats"][idx]
    user_id = update.effective_user.id

    # --- CONTROLLO PREZZO ---
    if not beat.get("price") or beat["price"] <= 0:
        await query.message.reply_text(
            "❌ Questo beat non è acquistabile perché non ha un prezzo impostato. Contatta l'amministratore."
        )
        context.user_data["current_state"] = BEAT_SELECTION
        return BEAT_SELECTION

    # Costruisci il link alla tua pagina di checkout
    checkout_url = (
        f"https://prodbypegasus.pages.dev/checkout"
        f"?user_id={user_id}"
        f"&beat={quote(beat['title'])}"
        f"&price={beat['price']:.2f}"
    )

    # Crea pulsante inline
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Procedi al pagamento", url=checkout_url)]
    ])

    await query.message.reply_text(
        f"🎉 Per acquistare <b>{beat['title']}</b>, clicca sul pulsante qui sotto e completa il pagamento.\n\n"
        "Ti invierò il beat appena ricevo la conferma del pagamento.\n"
        "Per qualsiasi problema, utilizza il pulsante \"Contattaci\" -> instagram.",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

async def handle_beat_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_user_blocked(context):
        await blockeduser_response(update, context)
        return context.user_data.get("current_state", GENRE)
    query = update.callback_query
    data = query.data
    beats = context.user_data["beats"]
    idx = context.user_data["beat_index"]

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
        # Mostra il menu principale invece della scelta del genere
        await delete_last_preview(context)
        await send_welcome_message(update, context)
        context.user_data["current_state"] = CATEGORY
        return CATEGORY

    if data == "change_filters":
        await show_filters_keyboard(update, context)
        return BEAT_SELECTION

    await query.answer()

    if data == "prev":
        context.user_data["beat_index"] = (idx - 1) % len(beats)
        await delete_last_preview(context)
    elif data == "next":
        context.user_data["beat_index"] = (idx + 1) % len(beats)
        await delete_last_preview(context)
    elif data == "buy":
        context.user_data["current_state"] = BEAT_SELECTION
        return await handle_payment(update, context)

    context.user_data["current_state"] = BEAT_SELECTION
    return await show_beat_catalog(update, context)

async def show_filters_keyboard(update, context):
    """Mostra la tastiera per la scelta dei filtri, solo con opzioni effettivamente disponibili per la categoria corrente."""
    query = update.callback_query
    category = context.user_data.get("catalog_category", "standard")

    # --- GENRES DISPONIBILI PER LA CATEGORIA ---
    with SessionLocal() as session:
        if category == "exclusive":
            genre_q = session.query(Beat.genre).filter(Beat.is_exclusive == 1).distinct()
        elif category == "discount":
            genre_q = session.query(Beat.genre).filter(Beat.is_discounted == 1).distinct()
        else:
            genre_q = session.query(Beat.genre).filter(Beat.is_exclusive == 0).distinct()
        available_genres = set(g for (g,) in genre_q)

    genres = [
        ("Trap", "Hip-Hop"),
        ("Drill", "R&B"),
        ("Raggeton", "Brazilian Funk"),
    ]
    genre_keyboard = []
    for g1, g2 in genres:
        row = []
        for g in (g1, g2):
            if g in available_genres:
                row.append(InlineKeyboardButton(g, callback_data=f"set_genre_{g}"))
            else:
                row.append(InlineKeyboardButton(f"🚫 {g}", callback_data=f"disabled_{g}"))
        genre_keyboard.append(row)
    genre_keyboard.append([InlineKeyboardButton("Standard", callback_data="set_genre_NONE")])
    genre_keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="change_filters")])

    # --- MOODS DISPONIBILI PER LA CATEGORIA E GENERE SELEZIONATO ---
    genre = context.user_data.get("genre")
    with SessionLocal() as session:
        if category == "exclusive":
            mood_q = session.query(Beat.mood)
            if genre:
                mood_q = mood_q.filter(Beat.genre == genre)
            mood_q = mood_q.filter(Beat.is_exclusive == 1).distinct()
        elif category == "discount":
            mood_q = session.query(Beat.mood)
            if genre:
                mood_q = mood_q.filter(Beat.genre == genre)
            mood_q = mood_q.filter(Beat.is_discounted == 1).distinct()
        else:
            mood_q = session.query(Beat.mood)
            if genre:
                mood_q = mood_q.filter(Beat.genre == genre)
            mood_q = mood_q.filter(Beat.is_exclusive == 0).distinct()
        available_moods = set(m for (m,) in mood_q)

    moods = [
        ("Love", "Sad"),
        ("Hard", "Dark"),
        ("Chill", "Epic"),
        ("Happy", "Hype"),
        ("Emotional",),
    ]
    mood_keyboard = []
    for row in moods:
        mood_row = []
        for m in row:
            if m in available_moods:
                mood_row.append(InlineKeyboardButton(m, callback_data=f"set_mood_{m}"))
            else:
                mood_row.append(InlineKeyboardButton(f"🚫 {m}", callback_data=f"disabled_{m}"))
        mood_keyboard.append(mood_row)
    mood_keyboard.append([InlineKeyboardButton("Standard", callback_data="set_mood_NONE")])
    mood_keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="change_filters")])

    # --- FASCE DI PREZZO DISPONIBILI PER LA CATEGORIA ---
    with SessionLocal() as session:
        def price_count(cond):
            q = session.query(Beat)
            if category == "exclusive":
                q = q.filter(Beat.is_exclusive == 1)
            elif category == "discount":
                q = q.filter(Beat.is_discounted == 1)
            else:
                q = q.filter(Beat.is_exclusive == 0)
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
    price_keyboard = []
    for row in price_rows:
        btn_row = []
        for p in row:
            if prices_available.get(p, False):
                btn_row.append(InlineKeyboardButton(p, callback_data=f"set_price_{p}"))
            else:
                btn_row.append(InlineKeyboardButton(f"🚫 {p}", callback_data=f"disabled_{p}"))
        price_keyboard.append(btn_row)
    price_keyboard.append([InlineKeyboardButton("⬅️ Indietro", callback_data="change_filters")])

    # Tastiera principale filtri
    filter_keyboard = [
        [
            InlineKeyboardButton("🎼 Genere", callback_data="filter_select_genre"),
            InlineKeyboardButton("🎚️ Mood", callback_data="filter_select_mood"),
        ],
        [
            InlineKeyboardButton("💰 Prezzo", callback_data="filter_select_price"),
        ],
        [InlineKeyboardButton("⬅️ Indietro", callback_data="filter_back")]
    ]

    # Mostra la tastiera principale filtri
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(filter_keyboard))

    # Salva le tastiere dinamiche in context per uso rapido nei callback
    context.user_data["filter_keyboards"] = {
        "genre": genre_keyboard,
        "mood": mood_keyboard,
        "price": price_keyboard,
    }

# Sostituisci la gestione dei filtri in handle_filter_selection:
async def handle_filter_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # --- AGGIUNTA GESTIONE TASTI DISABILITATI ---
    if data.startswith("disabled_"):
        await query.answer("🚫 Al momento non ci sono beat disponibili per questa categoria.", show_alert=True)
        return BEAT_SELECTION

    # --- GESTIONE FILTRI ---
    if data == "filter_back":
        await show_beat_catalog(update, context)
        return BEAT_SELECTION

    if data == "filter_select_genre":
        kb = context.user_data.get("filter_keyboards", {}).get("genre")
        if kb:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.answer("🚫 Nessun genere disponibile per questa categoria.", show_alert=True)
        return BEAT_SELECTION

    if data == "filter_select_mood":
        kb = context.user_data.get("filter_keyboards", {}).get("mood")
        if kb:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.answer("🚫 Nessun mood disponibile per questa categoria.", show_alert=True)
        return BEAT_SELECTION

    if data == "filter_select_price":
        kb = context.user_data.get("filter_keyboards", {}).get("price")
        if kb:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
        else:
            await query.answer("🚫 Nessuna fascia di prezzo disponibile per questa categoria.", show_alert=True)
        return BEAT_SELECTION

    if data.startswith("set_genre_"):
        genre = data.replace("set_genre_", "")
        if genre == "NONE":
            context.user_data.pop("genre", None)
        else:
            context.user_data["genre"] = genre
        await show_filtered_catalog(update, context)
        return BEAT_SELECTION

    if data.startswith("set_mood_"):
        mood = data.replace("set_mood_", "")
        if mood == "NONE":
            context.user_data.pop("mood", None)
        else:
            context.user_data["mood"] = mood
        await show_filtered_catalog(update, context)
        return BEAT_SELECTION

    if data.startswith("set_price_"):
        price = data.replace("set_price_", "")
        context.user_data["price_range"] = price
        await show_filtered_catalog(update, context)
        return BEAT_SELECTION
    if data.startswith("set_mood_"):
        mood = data.replace("set_mood_", "")
        if mood == "NONE":
            context.user_data.pop("mood", None)
        else:
            context.user_data["mood"] = mood
        await show_beat_catalog(update, context)
        return BEAT_SELECTION

    if data.startswith("set_price_"):
        price = data.replace("set_price_", "")
        context.user_data["price_range"] = price
        await show_beat_catalog(update, context)
        return BEAT_SELECTION
        await show_beat_catalog(update, context)
        return BEAT_SELECTION
    if data.startswith("set_price_"):
        price = data.replace("set_price_", "")
        context.user_data["price_range"] = price
        await show_beat_catalog(update, context)
        return BEAT_SELECTION
        await show_beat_catalog(update, context)
        return BEAT_SELECTION
