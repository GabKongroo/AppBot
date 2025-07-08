#bot.py
import os
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    from dotenv import load_dotenv
    load_dotenv(env_path)

from telegram.ext import ApplicationBuilder
from handlers import conversation_handler
import os
from fastapi import FastAPI, Request
import asyncio
import threading
import uvicorn
from db_manager import SessionLocal, Beat
import aiohttp
import tempfile
from botocore.config import Config
import boto3
from urllib.parse import urlparse
from utils import get_env_var, get_internal_token

TOKEN = get_env_var("Token_Bot")
INTERNAL_TOKEN = get_internal_token()

app_fastapi = FastAPI()

def generate_r2_signed_url(key: str, expires_in: int = 3600) -> str:
    R2_ACCESS_KEY_ID = get_env_var("R2_ACCESS_KEY_ID")
    R2_SECRET_ACCESS_KEY = get_env_var("R2_SECRET_ACCESS_KEY")
    R2_ENDPOINT_URL = get_env_var("R2_ENDPOINT_URL")
    R2_BUCKET_NAME = get_env_var("R2_BUCKET_NAME")
    R2_PUBLIC_BASE_URL = get_env_var("R2_PUBLIC_BASE_URL")
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

@app_fastapi.post("/internal/send_message")
async def send_message_endpoint(request: Request):
    # Sicurezza: verifica internal token solo se BOT_INTERNAL_URL è impostato (ambiente produzione)
    from utils import get_env_var
    internal_url = get_env_var("BOT_INTERNAL_URL")
    internal_token = request.headers.get("X-Internal-Token")
    if internal_url:
        INTERNAL_TOKEN = get_env_var("INTERNAL_TOKEN")
        if INTERNAL_TOKEN and (internal_token != INTERNAL_TOKEN):
            return {"status": "error", "message": "Unauthorized"}, 401

    data = await request.json()
    user_id = data.get("user_id")
    beat_title = data.get("beat_title")
    transaction_id = data.get("transaction_id")

    # Invia messaggio di attesa
    await app_fastapi.bot.send_message(
        chat_id=user_id,
        text=(
            "✅ Pagamento ricevuto!\n"
            f"🆔 ID transazione: <code>{transaction_id}</code>\n"
            "Sto preparando il tuo beat in formato WAV, riceverai il file tra qualche secondo/minuto.\n\n"
            "Se hai problemi o non ricevi il beat, scrivici su instagram: https://linktr.ee/ProdByPegasus "
        ),
        parse_mode="HTML"
    )

    # Recupera il beat dal DB
    with SessionLocal() as db:
        beat = db.query(Beat).filter(Beat.title == beat_title).first()
        if not beat:
            await app_fastapi.bot.send_message(
                chat_id=user_id,
                text=f"❌ Errore: Beat '{beat_title}' non trovato. Contatta l'assistenza."
            )
            return {"status": "error", "message": "Beat not found"}

        file_key = beat.file_key
        if not file_key.startswith("private/"):
            file_key = f"private/beats/{file_key.lstrip('/')}"
        is_exclusive = getattr(beat, "is_exclusive", 0) == 1

    signed_url = generate_r2_signed_url(file_key, expires_in=3600)

    # DEBUG: stampa il signed URL generato
    print(f"[DEBUG] Signed URL inviato a Telegram: {signed_url}")

    caption = (
        f"Ecco il tuo beat <b>{beat_title}</b> in formato WAV!\n"
        f"🆔 ID transazione: <code>{transaction_id or 'N/A'}</code>\n\n"
        "✅ Pagamento verificato.\n\n"
    )
    if is_exclusive:
        caption += (
            "<b>🔒 Questo beat è esclusivo e sarà disponibile solo per te!</b>\n"
            "<i>Sei l'unico che potrà utilizzarlo liberamente per il tuo progetto.</i>\n\n"
        )
    caption += (
        "Consigliamo di salvare il beat nei messaggi salvati di Telegram.\n"
        "oppure scaricarlo sul tuo dispositivo.\n"
        "se dovessi perdere il file, <u>non assumiamo responsabilità.</u>"
    )

    # Scarica il file temporaneamente e invialo
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(signed_url) as resp:
                if resp.status != 200:
                    await app_fastapi.bot.send_message(
                        chat_id=user_id,
                        text="❌ Errore nel download del beat. Contatta l'assistenza."
                    )
                    return {"status": "error", "message": "Download failed"}
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp.write(await resp.read())
                    tmp_path = tmp.name

        with open(tmp_path, "rb") as f:
            await app_fastapi.bot.send_document(
                chat_id=user_id,
                document=f,
                filename=f"{beat_title}.wav",
                caption=caption,
                parse_mode="HTML"
            )
        import os
        os.remove(tmp_path)
    except Exception as e:
        import traceback
        print(f"[ERROR] Errore invio beat Telegram: {e}")
        traceback.print_exc()
        # Non inviare un secondo messaggio di errore se il file è stato già inviato
        return {"status": "error", "message": str(e)}

    return {"status": "ok"}

def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(conversation_handler)
    app_fastapi.bot = app.bot
    
    # Usa la porta da variabile d'ambiente, default a 8080 (per locale)
    port = int(os.environ.get("PORT", 8080))

    # Avvia FastAPI in un thread separato
    threading.Thread(
        target=lambda: uvicorn.run(app_fastapi, host="0.0.0.0", port=port, reload=False),
        daemon=True
    ).start()

    # Avvia il bot Telegram con polling (funziona sia locale che su server)
    app.run_polling()

if __name__ == "__main__":
    main()
