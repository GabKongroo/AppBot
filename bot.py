#bot.py
import os
import asyncio
import threading
import uvicorn
import aiohttp
import tempfile
import boto3
import time
import requests
from urllib.parse import urlparse
from botocore.config import Config

from telegram.ext import ApplicationBuilder
from fastapi import FastAPI, Request

from handlers import conversation_handler
from db_manager import SessionLocal, Beat, Bundle, BundleBeat, release_beat_reservation, cleanup_expired_reservations
from config import get_telegram_config, get_r2_config, get_internal_config, print_config_summary

# Configurazione dinamica basata su ambiente
TELEGRAM_CONFIG = get_telegram_config()
R2_CONFIG = get_r2_config()
INTERNAL_CONFIG = get_internal_config()

TOKEN = TELEGRAM_CONFIG["token"]
INTERNAL_TOKEN = INTERNAL_CONFIG["token"]

app_fastapi = FastAPI()

# Stampa configurazione al startup
print_config_summary()

# Health check endpoint per Railway
@app_fastapi.get("/health")
async def health_check():
    return {"status": "healthy", "service": "pegasus-bot"}

def generate_r2_signed_url(key: str, expires_in: int = 3600) -> str:
    """
    Genera un URL firmato per accedere a un file in R2.
    
    Args:
        key: Chiave del file in R2
        expires_in: Tempo di scadenza in secondi
        
    Returns:
        URL firmato
        
    Raises:
        ValueError: Se la configurazione R2 è incompleta
        Exception: Altri errori durante la generazione
    """
    # Configurazioni R2 dinamiche
    R2_ACCESS_KEY_ID = R2_CONFIG["access_key_id"]
    R2_SECRET_ACCESS_KEY = R2_CONFIG["secret_access_key"]
    R2_ENDPOINT_URL = R2_CONFIG["endpoint_url"]
    R2_BUCKET_NAME = R2_CONFIG["bucket_name"]
    R2_PUBLIC_BASE_URL = R2_CONFIG["public_base_url"]
    
    # Verifica configurazione R2
    missing_configs = []
    if not R2_ACCESS_KEY_ID:
        missing_configs.append("access_key_id")
    if not R2_SECRET_ACCESS_KEY:
        missing_configs.append("secret_access_key")
    if not R2_ENDPOINT_URL:
        missing_configs.append("endpoint_url")
    if not R2_BUCKET_NAME:
        missing_configs.append("bucket_name")
    if not R2_PUBLIC_BASE_URL:
        missing_configs.append("public_base_url")
        
    if missing_configs:
        raise ValueError(f"Configurazione R2 incompleta. Mancano: {', '.join(missing_configs)}")
    
    if not key:
        raise ValueError("File key non può essere vuoto o None")
    
    try:
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
        parsed = urlparse(url)
        path_parts = parsed.path.split('/', 2)
        if len(path_parts) == 3:
            key_path = path_parts[2]
        else:
            key_path = parsed.path.lstrip('/')
        signed_url = f"{R2_PUBLIC_BASE_URL}/{key_path}?{parsed.query}"
        return signed_url
    except Exception as e:
        raise Exception(f"Errore generazione URL firmato R2 per '{key}': {e}")

@app_fastapi.post("/internal/send_waiting_message")
async def send_waiting_message_endpoint(request: Request):
    # Sicurezza: verifica internal token usando configurazione dinamica
    internal_config = get_internal_config()
    internal_token = request.headers.get("X-Internal-Token")
    if internal_config["bot_url"]:
        if internal_config["token"] and (internal_token != internal_config["token"]):
            return {"status": "error", "message": "Unauthorized"}, 401

    data = await request.json()
    user_id = data.get("user_id")
    beat_title = data.get("beat_title")
    bundle_id = data.get("bundle_id")
    order_type = data.get("order_type", "beat")
    
    # CACHE PER MESSAGGI DI ATTESA: Evita duplicati basandosi su user_id + beat_title
    import time
    cache_key = f"waiting_msg_{user_id}_{beat_title}_{bundle_id or 'none'}"
    processing_key = f"processing_{user_id}_{beat_title}_{bundle_id or 'none'}"
    
    if not hasattr(app_fastapi, '_waiting_messages_cache'):
        app_fastapi._waiting_messages_cache = {}
    if not hasattr(app_fastapi, '_currently_processing'):
        app_fastapi._currently_processing = {}
    
    current_time = time.time()
    
    # Pulisci cache entries vecchi (>10 minuti)
    app_fastapi._waiting_messages_cache = {
        k: v for k, v in app_fastapi._waiting_messages_cache.items()
        if current_time - v < 600  # 10 minuti
    }
    
    # Controlla se stiamo già processando questo ordine (arrivo tardivo del messaggio di attesa)
    if processing_key in app_fastapi._currently_processing:
        elapsed = current_time - app_fastapi._currently_processing[processing_key]
        if elapsed < 300:  # Se stiamo processando da meno di 5 minuti
            print(f"[INFO] Ordine {beat_title} già in elaborazione da {elapsed:.1f}s - skip messaggio attesa tardivo")
            return {"status": "ok", "message": "Order already being processed - late waiting message ignored"}
    
    # **NUOVO: Controlla se l'ordine è già stato consegnato**
    delivery_cache_key = f"delivered_{user_id}_{beat_title}"
    if not hasattr(app_fastapi, '_delivered_orders_cache'):
        app_fastapi._delivered_orders_cache = {}
    
    # Pulisci cache consegne vecchie (>30 minuti)
    app_fastapi._delivered_orders_cache = {
        k: v for k, v in app_fastapi._delivered_orders_cache.items()
        if current_time - v < 1800
    }
    
    if delivery_cache_key in app_fastapi._delivered_orders_cache:
        elapsed = current_time - app_fastapi._delivered_orders_cache[delivery_cache_key]
        print(f"[INFO] Beat {beat_title} già consegnato {elapsed:.1f}s fa - skip messaggio attesa tardivo")
        return {"status": "ok", "message": "Beat already delivered - waiting message skipped"}
    
    # Controlla se già inviato
    if cache_key in app_fastapi._waiting_messages_cache:
        print(f"[INFO] Messaggio di attesa già inviato per {beat_title} a user {user_id} - skip")
        return {"status": "ok", "message": "Waiting message already sent"}
    
    # Segna come inviato
    app_fastapi._waiting_messages_cache[cache_key] = current_time
    
    # Determina se è un beat singolo o un bundle
    is_bundle = order_type == "bundle" and bundle_id is not None

    # Invia messaggio di attesa (più breve e immediato)
    try:
        if is_bundle:
            message = (
                "⏳ <b>Ordine confermato!</b>\n"
                f"📦 Bundle: <b>{beat_title}</b>\n\n"
                "💳 Pagamento verificato con successo!\n"
                "🎵 Preparazione dei file in corso...\n\n"
                "📞 <i>Per assistenza utilizza il pulsante \"Contattaci\" o scrivici su Instagram</i>"
            )
        else:
            message = (
                "⏳ <b>Ordine confermato!</b>\n"
                f"🎵 Beat: <b>{beat_title}</b>\n\n"
                "💳 Pagamento verificato con successo!\n"
                "📁 Preparazione del file in corso...\n\n"
                "📞 <i>Per assistenza utilizza il pulsante \"Contattaci\" o scrivici su Instagram</i>"
            )
        
        await app_fastapi.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode="HTML"
        )
        
        print(f"[INFO] Messaggio attesa inviato a user {user_id}")
        return {"status": "ok", "message": "Waiting message sent"}
        
    except Exception as e:
        print(f"[ERROR] Errore invio messaggio attesa: {e}")
        return {"status": "error", "message": str(e)}

@app_fastapi.post("/internal/send_message")
async def send_message_endpoint(request: Request):
    try:
        # Sicurezza: verifica internal token usando configurazione dinamica
        internal_config = get_internal_config()
        internal_token = request.headers.get("X-Internal-Token")
        if internal_config["bot_url"]:
            if internal_config["token"] and (internal_token != internal_config["token"]):
                return {"status": "error", "message": "Unauthorized"}, 401

        data = await request.json()
        user_id = data.get("user_id")
        beat_title = data.get("beat_title")
        bundle_id = data.get("bundle_id")
        order_type = data.get("order_type", "beat")
        transaction_id = data.get("transaction_id")
        
        # IDEMPOTENZA: Controlla se abbiamo già processato questa transazione
        if transaction_id:
            # Usa una cache in memoria per transazioni recenti (ultimi 30 minuti)
            import time
            cache_key = f"processed_txn_{transaction_id}"
            
            # Cache semplice in memoria (potrebbe essere migliorata con Redis in produzione)
            if not hasattr(app_fastapi, '_processed_transactions_cache'):
                app_fastapi._processed_transactions_cache = {}
            
            current_time = time.time()
            
            # Pulisci cache entries vecchi (>30 minuti)
            app_fastapi._processed_transactions_cache = {
                k: v for k, v in app_fastapi._processed_transactions_cache.items()
                if current_time - v < 1800  # 30 minuti
            }
            
            # Controlla se già processato
            if cache_key in app_fastapi._processed_transactions_cache:
                print(f"[WARNING] Transazione {transaction_id} già processata - rifiuto duplicato")
                return {"status": "ok", "message": "Transaction already processed (idempotent)"}
            
            # MARCA CHE STIAMO PROCESSANDO per evitare messaggi di attesa in ritardo
            processing_key = f"processing_{user_id}_{beat_title}_{bundle_id or 'none'}"
            if not hasattr(app_fastapi, '_currently_processing'):
                app_fastapi._currently_processing = {}
            app_fastapi._currently_processing[processing_key] = current_time
            
            # Segna come processato
            app_fastapi._processed_transactions_cache[cache_key] = current_time
            print(f"[INFO] Elaborazione transazione {transaction_id} - prima volta")
        
        # Chiama la funzione principale e restituisce il risultato
        return await send_beat_to_user(user_id, beat_title, bundle_id, order_type, transaction_id)
        
    except Exception as critical_error:
        # Gestione errori critici - assicuriamoci di restituire sempre un errore chiaro
        print(f"[CRITICAL ERROR] Errore critico in send_message_endpoint: {critical_error}")
        import traceback
        traceback.print_exc()
        
        # Tentativo di notificare l'utente dell'errore se possibile
        try:
            if 'user_id' in locals():
                await app_fastapi.bot.send_message(
                    chat_id=user_id,
                    text=(
                        "❌ Si è verificato un errore durante l'elaborazione del tuo ordine.\n"
                        "Il nostro team è stato notificato e risolverà il problema al più presto.\n\n"
                        "📞 Instagram: https://linktr.ee/ProdByPegasus"
                    )
                )
        except:
            pass  # Se non riusciamo a inviare il messaggio, ignora
            
        return {"status": "error", "message": f"Critical error: {str(critical_error)}"}

async def send_beat_to_user(user_id, beat_title, bundle_id=None, order_type="beat", transaction_id=None):
    """
    Funzione principale per inviare beat/bundle all'utente.
    Restituisce sempre uno status corretto.
    """
    import time
    
    # Determina se è un beat singolo o un bundle
    is_bundle = order_type == "bundle" and bundle_id is not None

    # **NUOVO: Controlla se abbiamo già inviato un messaggio di attesa**
    waiting_cache_key = f"waiting_msg_{user_id}_{beat_title}_{bundle_id or 'none'}"
    has_waiting_message = False
    
    if hasattr(app_fastapi, '_waiting_messages_cache'):
        current_time = time.time()
        if waiting_cache_key in app_fastapi._waiting_messages_cache:
            elapsed = current_time - app_fastapi._waiting_messages_cache[waiting_cache_key]
            if elapsed < 600:  # Se il messaggio di attesa è stato inviato negli ultimi 10 minuti
                has_waiting_message = True
                print(f"[INFO] Messaggio di attesa già inviato {elapsed:.1f}s fa - skip secondo messaggio")

    # Invia messaggio di elaborazione SOLO se non c'è già un messaggio di attesa
    if not has_waiting_message:
        if is_bundle:
            await app_fastapi.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ Pagamento ricevuto!\n"
                    f"🆔 ID transazione: <code>{transaction_id}</code>\n"
                    f"📦 Bundle: <b>{beat_title}</b>\n"
                    "Sto preparando tutti i beat del bundle in formato WAV, riceverai i file tra qualche secondo/minuto.\n\n"
                    "Per assistenza scrivici su Instagram tramite il pulsante \"Contattaci\"."
                ),
                parse_mode="HTML"
            )
        else:
            await app_fastapi.bot.send_message(
                chat_id=user_id,
                text=(
                    "✅ Pagamento ricevuto!\n"
                    f"🆔 ID transazione: <code>{transaction_id}</code>\n"
                    "Sto preparando il tuo beat in formato WAV, riceverai il file tra qualche secondo/minuto.\n\n"
                    "Per assistenza scrivici su Instagram tramite il pulsante \"Contattaci\"."
                ),
                parse_mode="HTML"
            )
    else:
        print(f"[INFO] Skip messaggio elaborazione - utente {user_id} ha già ricevuto messaggio di attesa")

    # Recupera i beat dal DB
    with SessionLocal() as db:
        if is_bundle:
            # Per i bundle, recupera tutti i beat contenuti
            bundle = db.query(Bundle).filter(Bundle.id == bundle_id).first()
            if not bundle:
                await app_fastapi.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Errore: Bundle non trovato. Contatta l'assistenza."
                )
                return {"status": "error", "message": "Bundle not found"}
            
            # Recupera tutti i beat del bundle
            beats = db.query(Beat).join(BundleBeat).filter(
                BundleBeat.bundle_id == bundle_id
            ).all()
            
            if not beats:
                await app_fastapi.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Errore: Nessun beat trovato nel bundle. Contatta l'assistenza."
                )
                return {"status": "error", "message": "No beats found in bundle"}
        else:
            # Per beat singoli, recupera il beat specifico
            beat = db.query(Beat).filter(Beat.title == beat_title).first()
            if not beat:
                await app_fastapi.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Errore: Beat '{beat_title}' non trovato. Contatta l'assistenza."
                )
                return {"status": "error", "message": "Beat not found"}
            beats = [beat]  # Lista con un solo beat per uniformità

    # Scarica e invia i beat con gestione timeout migliorata
    failed_beats = []
    success_count = 0
    
    try:
        for idx, beat in enumerate(beats):
            try:
                file_key = beat.file_key
                if not file_key:
                    error_msg = "❌ Errore: file_key mancante nel database"
                    print(f"[ERROR] {error_msg} per beat '{beat.title}'")
                    failed_beats.append((beat.title, error_msg))
                    continue
                    
                if not file_key.startswith("private/"):
                    file_key = f"private/beats/{file_key.lstrip('/')}"
                is_exclusive = getattr(beat, "is_exclusive", 0) == 1

                print(f"[INFO] Elaborazione beat {idx + 1}/{len(beats)}: {beat.title}")
                
                # Timeout più lungo per il download
                signed_url = generate_r2_signed_url(file_key, expires_in=3600)
                print(f"[DEBUG] Signed URL generato: {signed_url}")

                if is_bundle:
                    caption = (
                        f"📦 Bundle: <b>{beat_title}</b>\n"
                        f"🎵 Beat {idx + 1}/{len(beats)}: <b>{beat.title}</b>\n"
                        f"🆔 ID transazione: <code>{transaction_id or 'N/A'}</code>\n\n"
                        "✅ Pagamento verificato.\n\n"
                    )
                else:
                    caption = (
                        f"Ecco il tuo beat <b>{beat.title}</b> in formato WAV!\n"
                        f"🆔 ID transazione: <code>{transaction_id or 'N/A'}</code>\n\n"
                        "✅ Pagamento verificato.\n\n"
                    )
                
                if is_exclusive:
                    caption += (
                        "<b>🔒 Questo beat è esclusivo e sarà disponibile solo per te!</b>\n"
                        "<i>Sei l'unico che potrà utilizzarlo liberamente per il tuo progetto.</i>\n\n"
                    )
                
                if is_bundle and idx == len(beats) - 1:
                    # Ultimo beat del bundle
                    caption += (
                        "📦 <b>Questo completa il tuo bundle!</b>\n"
                        "Consigliamo di salvare tutti i beat nei messaggi salvati di Telegram\n"
                        "oppure scaricarli sul tuo dispositivo.\n"
                        "Se dovessi perdere i file, <u>non assumiamo responsabilità.</u>"
                    )
                elif not is_bundle:
                    caption += (
                        "Consigliamo di salvare il beat nei messaggi salvati di Telegram\n"
                        "oppure scaricarlo sul tuo dispositivo.\n"
                        "Se dovessi perdere il file, <u>non assumiamo responsabilità.</u>\n\n"
                        "🔄 <b>Per tornare al catalogo digita /start</b>"
                    )

                # Scarica il file con timeout aumentato
                download_timeout = aiohttp.ClientTimeout(total=60)  # 60s per download
                async with aiohttp.ClientSession(timeout=download_timeout) as session:
                    async with session.get(signed_url) as resp:
                        if resp.status != 200:
                            error_msg = f"❌ Errore download: HTTP {resp.status}"
                            print(f"[ERROR] {error_msg} per beat '{beat.title}'")
                            failed_beats.append((beat.title, error_msg))
                            continue
                            
                        with tempfile.NamedTemporaryFile(delete=False) as tmp:
                            tmp.write(await resp.read())
                            tmp_path = tmp.name

                # Invia il file con timeout aumentato
                print(f"[INFO] Invio beat '{beat.title}' a user {user_id}")
                with open(tmp_path, "rb") as f:
                    await asyncio.wait_for(
                        app_fastapi.bot.send_document(
                            chat_id=user_id,
                            document=f,
                            filename=f"{beat.title}.wav",
                            caption=caption,
                            parse_mode="HTML",
                            read_timeout=120,  # 2 minuti per upload
                            write_timeout=120,
                            connect_timeout=30
                        ),
                        timeout=180  # 3 minuti totali
                    )
                
                import os
                os.remove(tmp_path)
                success_count += 1
                print(f"[SUCCESS] Beat '{beat.title}' inviato con successo ({success_count}/{len(beats)})")
                
                # Rilascia la prenotazione se il beat era esclusivo e prenotato
                if is_exclusive:
                    release_beat_reservation(beat.id, user_id)
                    print(f"[INFO] Prenotazione rilasciata per beat esclusivo '{beat.title}' (user: {user_id})")
                
                # Pausa tra beat per evitare rate limiting e sovraccarico
                if is_bundle and idx < len(beats) - 1:
                    await asyncio.sleep(2)  # Aumentata a 2 secondi
                    
            except asyncio.TimeoutError:
                error_msg = "❌ Timeout durante invio"
                print(f"[ERROR] {error_msg} per beat '{beat.title}'")
                failed_beats.append((beat.title, error_msg))
                continue
            except Exception as beat_error:
                error_msg = f"❌ Errore: {str(beat_error)}"
                print(f"[ERROR] {error_msg} per beat '{beat.title}'")
                failed_beats.append((beat.title, error_msg))
                continue
                
        # Invia resoconto finale se ci sono stati errori
        if failed_beats:
            failed_list = "\n".join([f"• {title}: {error}" for title, error in failed_beats])
            summary_msg = (
                f"📊 <b>Resoconto invio:</b>\n"
                f"✅ Inviati: {success_count}/{len(beats)}\n"
                f"❌ Falliti: {len(failed_beats)}\n\n"
                f"<b>Beat non inviati:</b>\n{failed_list}\n\n"
                "🔄 Riprova a contattare l'assistenza se alcuni beat non sono arrivati.\n"
                "📞 Instagram: https://linktr.ee/ProdByPegasus\n\n"
                "🔄 <b>Per tornare al catalogo digita /start</b>"
            )
            await app_fastapi.bot.send_message(
                chat_id=user_id,
                text=summary_msg,
                parse_mode="HTML"
            )
        elif is_bundle and success_count > 1:
            # Messaggio di successo per bundle completi
            await app_fastapi.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🎉 <b>Bundle completato!</b>\n"
                    f"✅ Tutti i {success_count} beat sono stati inviati con successo!\n\n"
                    "📱 Ricorda di salvare i file nei messaggi salvati di Telegram.\n\n"
                    "🔄 <b>Per tornare al catalogo digita /start</b>"
                ),
                parse_mode="HTML"
            )

    except Exception as e:
        import traceback
        print(f"[ERROR] Errore generale invio beat: {e}")
        traceback.print_exc()
        
        # Invia messaggio di errore generale
        await app_fastapi.bot.send_message(
            chat_id=user_id,
            text=(
                f"❌ <b>Errore durante l'invio dei beat</b>\n\n"
                f"✅ Beat inviati: {success_count}/{len(beats)}\n"
                f"❌ Errore: {str(e)}\n\n"
                "🔄 Contatta l'assistenza per ricevere i beat mancanti.\n"
                "📞 Instagram: https://linktr.ee/ProdByPegasus\n\n"
                "🔄 <b>Per tornare al catalogo digita /start</b>"
            ),
            parse_mode="HTML"
        )
        return {"status": "partial_error", "message": f"Sent {success_count}/{len(beats)} beats", "error": str(e)}

    # PULIZIA: Rimuovi dalla cache di processing
    if transaction_id:
        processing_key = f"processing_{user_id}_{beat_title}_{bundle_id or 'none'}"
        if hasattr(app_fastapi, '_currently_processing') and processing_key in app_fastapi._currently_processing:
            del app_fastapi._currently_processing[processing_key]
            print(f"[INFO] Elaborazione {beat_title} completata - rimosso da cache processing")

    # **NUOVO: Segna come consegnato per evitare messaggi di attesa tardivi**
    if success_count > 0:  # Se almeno un beat è stato inviato con successo
        delivery_cache_key = f"delivered_{user_id}_{beat_title}"
        if not hasattr(app_fastapi, '_delivered_orders_cache'):
            app_fastapi._delivered_orders_cache = {}
        app_fastapi._delivered_orders_cache[delivery_cache_key] = time.time()
        print(f"[INFO] Ordine {beat_title} marcato come consegnato per user {user_id}")

    # **Controllo finale del successo**
    if success_count == 0:
        # Nessun beat inviato con successo
        print(f"[ERROR] Nessun beat inviato con successo per {beat_title}")
        return {"status": "error", "message": f"Failed to send all beats", "sent": 0, "total": len(beats)}
    elif success_count < len(beats):
        # Alcuni beat inviati, altri falliti
        print(f"[WARNING] Invio parziale: {success_count}/{len(beats)} beat inviati")
        return {"status": "partial", "message": f"Sent {success_count}/{len(beats)} beats", "sent": success_count, "total": len(beats)}
    else:
        # Tutti i beat inviati con successo
        print(f"[SUCCESS] Tutti i {success_count} beat inviati con successo")
        return {"status": "ok", "message": f"All {success_count} beats sent successfully", "sent": success_count, "total": len(beats)}

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(conversation_handler)
    app_fastapi.bot = app.bot
    
    # Aggiungi un job per pulire le prenotazioni scadute ogni 5 minuti
    job_queue = app.job_queue
    
    async def cleanup_job(context):
        """Job asincrono per pulire le prenotazioni scadute"""
        cleanup_expired_reservations()
    
    # Controlla se JobQueue è disponibile prima di usarlo
    if job_queue is not None:
        job_queue.run_repeating(
            callback=cleanup_job,
            interval=300,  # 5 minuti
            first=30,      # Avvia dopo 30 secondi
            name="cleanup_reservations"
        )
        print("[INFO] JobQueue configurato per cleanup automatico prenotazioni")
    else:
        print("[WARNING] JobQueue non disponibile - cleanup automatico disabilitato")
    
    # Usa la porta da variabile d'ambiente, default a 8080 (per locale)
    port = int(os.environ.get("PORT", 8080))

    # Avvia FastAPI in un thread separato
    threading.Thread(
        target=lambda: uvicorn.run(app_fastapi, host="0.0.0.0", port=port, reload=False),
        daemon=True
    ).start()

    # Aspetta che il server si avvii e sia pronto
    import time
    import requests
    
    print(f"[INFO] Avvio del server HTTP sulla porta {port}...")
    time.sleep(5)  # Aumentato da 2 a 5 secondi
    
    # Verifica che il server sia pronto
    max_retries = 10
    for i in range(max_retries):
        try:
            response = requests.get(f"http://localhost:{port}/health", timeout=5)
            if response.status_code == 200:
                print(f"[INFO] Server HTTP pronto sulla porta {port}")
                break
        except Exception as e:
            print(f"[INFO] Server non ancora pronto, tentativo {i+1}/{max_retries}")
            time.sleep(2)
    else:
        print(f"[WARNING] Server HTTP potrebbe non essere pronto dopo {max_retries} tentativi")

    # Avvia il bot Telegram con polling (funziona sia locale che su server)
    app.run_polling()

if __name__ == "__main__":
    main()
