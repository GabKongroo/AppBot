from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, text, BigInteger
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.pool import QueuePool
from datetime import datetime, timedelta, timezone
import logging
import os
import os
import logging
import time
import random
from sqlalchemy.exc import OperationalError
from contextlib import contextmanager

# Configurazione logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Carica le variabili d'ambiente dal file .env nella stessa cartella
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(env_path)

DATABASE_URL = os.getenv("DATABASE_URL")

# Configurazione del pool di connessioni
engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=10,
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=300
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()

class Beat(Base):
    __tablename__ = "beats"
    
    id = Column(Integer, primary_key=True)
    genre = Column(String(50), nullable=False)
    mood = Column(String(50), nullable=False)
    folder = Column(String(50), nullable=False)
    title = Column(String(100), nullable=False)
    preview_key = Column(String(255), nullable=False)
    file_key = Column(String(255), nullable=False)
    image_key = Column(String(255), nullable=False)
    price = Column(Float, nullable=False, default=19.99)
    original_price = Column(Float, nullable=True)
    is_exclusive = Column(Integer, nullable=False, default=0)   # 0 = False, 1 = True
    is_discounted = Column(Integer, nullable=False, default=0)  # 0 = False, 1 = True
    discount_percent = Column(Integer, nullable=False, default=0)
    available = Column(Integer, nullable=False, default=1)      # 0 = False, 1 = True
    
    # Campi per prenotazione temporanea beat esclusivi
    reserved_by_user_id = Column(BigInteger, nullable=True)  # ID utente che ha prenotato (BigInteger per Telegram IDs)
    reserved_at = Column(DateTime, nullable=True)  # Timestamp prenotazione
    reservation_expires_at = Column(DateTime, nullable=True)  # Scadenza prenotazione
    
    orders = relationship("Order", back_populates="beat")
    bundle_beats = relationship("BundleBeat", back_populates="beat")

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    transaction_id = Column(String(255), unique=True, nullable=False)
    telegram_user_id = Column(BigInteger, nullable=False)  # BigInteger per Telegram IDs
    beat_title = Column(String(255), nullable=False)
    payer_email = Column(String(255), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False)
    token = Column(String(255), nullable=True)
    beat_id = Column(Integer, ForeignKey("beats.id"), nullable=True)  # Chiave esterna opzionale
    bundle_id = Column(Integer, ForeignKey("bundles.id"), nullable=True)  # Supporto per bundle
    order_type = Column(String(20), nullable=False, default="beat")  # "beat" o "bundle"
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))  # Campo aggiunto

    beat = relationship("Beat", back_populates="orders")
    bundle = relationship("Bundle", back_populates="orders")

class Bundle(Base):
    """Tabella per i bundle di beat promozionali"""
    __tablename__ = "bundles"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)  # Nome del bundle
    description = Column(String(500), nullable=True)  # Descrizione del bundle
    individual_price = Column(Float, nullable=False)  # Prezzo totale se comprati singolarmente
    bundle_price = Column(Float, nullable=False)  # Prezzo scontato del bundle
    discount_percent = Column(Integer, nullable=False, default=0)  # Percentuale di sconto
    is_active = Column(Integer, nullable=False, default=1)  # Bundle attivo/disattivo
    created_at = Column(DateTime, nullable=True)
    image_key = Column(String(255), nullable=True)  # Immagine promozionale del bundle
    
    # Relazioni
    bundle_beats = relationship("BundleBeat", back_populates="bundle")
    orders = relationship("Order", back_populates="bundle")

class BundleBeat(Base):
    """Tabella di associazione tra bundle e beat"""
    __tablename__ = "bundle_beats"
    
    id = Column(Integer, primary_key=True)
    bundle_id = Column(Integer, ForeignKey("bundles.id"), nullable=False)
    beat_id = Column(Integer, ForeignKey("beats.id"), nullable=False)
    
    # Relazioni
    bundle = relationship("Bundle", back_populates="bundle_beats")
    beat = relationship("Beat", back_populates="bundle_beats")

class BundleOrder(Base):
    """Tabella per gli ordini dei bundle"""
    __tablename__ = "bundle_orders"
    
    id = Column(Integer, primary_key=True)
    bundle_id = Column(Integer, ForeignKey("bundles.id"), nullable=False)
    user_id = Column(BigInteger, nullable=False)  # BigInteger per Telegram IDs
    total_price = Column(Float, nullable=False)
    payment_status = Column(String(50), nullable=False, default="pending")
    transaction_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=True)
    
    # Relazioni
    # bundle = relationship("Bundle", back_populates="bundle_orders")  # Disabilitato per approccio unificato

def get_session():
    """Restituisce una sessione per interagire con il database"""
    return SessionLocal()

def reserve_exclusive_beat(beat_id: int, user_id: int, reservation_minutes: int = 10) -> bool:
    """
    Prenota temporaneamente un beat esclusivo per l'utente specificato.
    LIMITAZIONE: Un utente può prenotare solo 1 beat esclusivo alla volta.
    Ritorna True se la prenotazione è riuscita, False se il beat è già prenotato.
    """
    
    with SessionLocal() as session:
        # Trova il beat
        beat = session.query(Beat).filter(Beat.id == beat_id).first()
        if not beat or beat.is_exclusive != 1:
            return False
        
        now = datetime.now()  # Uso datetime naive per consistenza
        
        # ⚡ NUOVO: Controlla se l'utente ha già una prenotazione attiva
        # Controllo sicuro che gestisce sia datetime naive che aware
        try:
            existing_reservation = session.query(Beat).filter(
                Beat.reserved_by_user_id == user_id,
                Beat.reservation_expires_at.isnot(None)
            ).first()
            
            if existing_reservation:
                # Verifica se la prenotazione è ancora valida
                expires_at = existing_reservation.reservation_expires_at
                
                # Confronto diretto tra datetime naive
                if expires_at > now:
                    # Se l'utente sta tentando di prenotare lo stesso beat, permetti di continuare
                    if existing_reservation.id == beat_id:
                        logger.info(f"Utente {user_id} rinnova prenotazione per beat {beat_id}: {existing_reservation.title}")
                        # Resetta la prenotazione a 10 minuti esatti dal momento attuale
                        existing_reservation.reserved_at = now
                        existing_reservation.reservation_expires_at = now + timedelta(minutes=reservation_minutes)
                        session.commit()
                        return True
                    else:
                        # L'utente ha già una prenotazione attiva per un beat diverso, non può prenotarne altre
                        logger.info(f"Utente {user_id} ha già prenotato beat {existing_reservation.id}: {existing_reservation.title}, non può prenotare beat {beat_id}")
                        return False
                else:
                    # Prenotazione scaduta, pulisci automaticamente
                    existing_reservation.reserved_by_user_id = None
                    existing_reservation.reserved_at = None
                    existing_reservation.reservation_expires_at = None
                    session.flush()  # Applica le modifiche nella stessa transazione
        except Exception as e:
            logger.error(f"Errore controllo prenotazione esistente per utente {user_id}: {e}")
            # In caso di errore, procedi cautamente assumendo che non ci siano prenotazioni
        
        # Controlla se il beat è già prenotato e la prenotazione non è scaduta
        if (beat.reserved_by_user_id is not None and 
            beat.reservation_expires_at is not None):
            
            expires_at = beat.reservation_expires_at
            # Confronto diretto tra datetime naive
            if expires_at > now:
                # Beat già prenotato da qualcun altro
                if beat.reserved_by_user_id != user_id:
                    logger.info(f"Beat {beat_id} già prenotato da utente {beat.reserved_by_user_id}")
                    return False
            else:
                # Prenotazione scaduta, pulisci automaticamente
                beat.reserved_by_user_id = None
                beat.reserved_at = None
                beat.reservation_expires_at = None
                session.flush()
        
        # Prenota il beat
        beat.reserved_by_user_id = user_id
        beat.reserved_at = now
        beat.reservation_expires_at = now + timedelta(minutes=reservation_minutes)
        
        session.commit()
        return True

def release_beat_reservation(beat_id: int, user_id: int = None) -> bool:
    """
    Rilascia la prenotazione di un beat esclusivo.
    Se user_id è specificato, rilascia solo se il beat è prenotato da quell'utente.
    """
    with SessionLocal() as session:
        beat = session.query(Beat).filter(Beat.id == beat_id).first()
        if not beat:
            return False
        
        # Se user_id è specificato, controlla che sia lo stesso utente
        if user_id is not None and beat.reserved_by_user_id != user_id:
            return False
        
        # Rilascia la prenotazione
        beat.reserved_by_user_id = None
        beat.reserved_at = None
        beat.reservation_expires_at = None
        
        session.commit()
        return True

def cleanup_expired_reservations():
    """
    Pulisce automaticamente le prenotazioni scadute.
    Da chiamare periodicamente o prima di ogni operazione critica.
    """
    
    with SessionLocal() as session:
        now = datetime.now()  # Uso datetime naive per consistenza
        
        # Trova tutte le prenotazioni che potrebbero essere scadute
        potentially_expired = session.query(Beat).filter(
            Beat.reserved_by_user_id.isnot(None),
            Beat.reservation_expires_at.isnot(None)
        ).all()
        
        expired_count = 0
        
        # Controlla ogni prenotazione (ora tutto naive)
        for beat in potentially_expired:
            expires_at = beat.reservation_expires_at
            
            # Confronto diretto tra datetime naive
            if expires_at < now:
                # Prenotazione scaduta
                beat.reserved_by_user_id = None
                beat.reserved_at = None
                beat.reservation_expires_at = None
                expired_count += 1
        
        session.commit()
        return expired_count

def is_beat_available(beat_id: int) -> bool:
    """
    Controlla se un beat esclusivo è disponibile per l'acquisto.
    Considera prenotazioni individuali e beat inclusi in bundle con ordini recenti.
    """
    
    with SessionLocal() as session:
        beat = session.query(Beat).filter(Beat.id == beat_id).first()
        if not beat:
            return False
        
        # Se non è esclusivo, è sempre disponibile
        if beat.is_exclusive != 1:
            return True
        
        now = datetime.now()  # Uso datetime naive per consistenza
        
        # 1. Controlla se è prenotato individualmente e la prenotazione non è scaduta
        if (beat.reserved_by_user_id is not None and 
            beat.reservation_expires_at is not None):
            
            expires_at = beat.reservation_expires_at
            # Confronto diretto tra datetime naive
            if expires_at > now:
                return False
        
        # 2. Controlla se il beat è già stato venduto (ha ordini completati)
        completed_orders = session.query(Order).filter(
            Order.beat_title == beat.title,
            Order.order_type == "beat"
        ).first()
        if completed_orders:
            return False
        
        # 3. Controlla se il beat è parte di un bundle con ordini recenti (ultimi 15 minuti)
        # Questo previene race condition durante acquisti bundle
        recent_threshold = now - timedelta(minutes=15)
        
        # Trova tutti i bundle che contengono questo beat
        bundles_with_beat = session.query(Bundle).join(BundleBeat).filter(
            BundleBeat.beat_id == beat_id,
            Bundle.is_active == 1
        ).all()
        
        for bundle in bundles_with_beat:
            # Controlla se ci sono ordini recenti per questo bundle
            recent_bundle_orders = session.query(Order).filter(
                Order.bundle_id == bundle.id,
                Order.order_type == "bundle",
                Order.created_at >= recent_threshold
            ).first()
            
            if recent_bundle_orders:
                # Il bundle è stato acquistato di recente, il beat non è più disponibile
                return False
        
        return True

def get_active_bundles():
    """Recupera tutti i bundle attivi con i loro beat"""
    with SessionLocal() as session:
        bundles = session.query(Bundle).filter(Bundle.is_active == 1).all()
        result = []
        
        for bundle in bundles:
            bundle_data = {
                "id": bundle.id,
                "name": bundle.name,
                "description": bundle.description,
                "individual_price": bundle.individual_price,
                "bundle_price": bundle.bundle_price,
                "discount_percent": bundle.discount_percent,
                "image_key": bundle.image_key,
                "beats": []
            }
            
            # Recupera i beat del bundle
            for bundle_beat in bundle.bundle_beats:
                beat = bundle_beat.beat
                beat_data = {
                    "id": beat.id,
                    "title": beat.title,
                    "genre": beat.genre,
                    "mood": beat.mood,
                    "price": beat.price,
                    "preview_key": beat.preview_key,
                    "image_key": beat.image_key,
                    "is_exclusive": beat.is_exclusive  # Aggiungi info esclusività
                }
                bundle_data["beats"].append(beat_data)
            
            result.append(bundle_data)
        
        return result

def get_bundle_by_id(bundle_id: int):
    """Recupera un bundle specifico con tutti i suoi beat"""
    with SessionLocal() as session:
        bundle = session.query(Bundle).filter(Bundle.id == bundle_id).first()
        if not bundle:
            return None
        
        bundle_data = {
            "id": bundle.id,
            "name": bundle.name,
            "description": bundle.description,
            "individual_price": bundle.individual_price,
            "bundle_price": bundle.bundle_price,
            "discount_percent": bundle.discount_percent,
            "image_key": bundle.image_key,
            "beats": []
        }
        
        # Recupera i beat del bundle
        for bundle_beat in bundle.bundle_beats:
            beat = bundle_beat.beat
            beat_data = {
                "id": beat.id,
                "title": beat.title,
                "genre": beat.genre,
                "mood": beat.mood,
                "price": beat.price,
                "preview_key": beat.preview_key,
                "image_key": beat.image_key,
                "file_key": beat.file_key,
                "is_exclusive": beat.is_exclusive  # Aggiungi info esclusività
            }
            bundle_data["beats"].append(beat_data)
        
        return bundle_data

def create_bundle_order(bundle_id: int, user_id: int, total_price: float) -> int:
    """Crea un nuovo ordine per un bundle"""
    from datetime import datetime
    
    with SessionLocal() as session:
        order = BundleOrder(
            bundle_id=bundle_id,
            user_id=user_id,
            total_price=total_price,
            payment_status="pending",
            created_at=datetime.now(timezone.utc)
        )
        session.add(order)
        session.commit()
        return order.id

def initialize_database():
    """
    Inizializza il database creando tutte le tabelle necessarie.
    Questa funzione deve essere chiamata per configurare un nuovo database.
    
    Le tabelle create sono:
    - beats: tabella principale dei beat (vuota, da riempire manualmente)
    - orders: tabella degli ordini (si popolerà con gli acquisti)
    - bundles: tabella dei bundle promozionali (si gestisce tramite web admin)
    - bundle_beats: tabella di associazione bundle-beat
    - bundle_orders: tabella degli ordini bundle (deprecata, usa orders)
    
    Returns:
        bool: True se l'inizializzazione è riuscita, False altrimenti
    """
    try:
        logger.info("Inizializzazione database in corso...")
        
        # Crea tutte le tabelle definite nei modelli
        Base.metadata.create_all(bind=engine)
        
        logger.info("✅ Tabelle create con successo:")
        logger.info("  - beats (vuota - da riempire manualmente)")
        logger.info("  - orders (si popolerà con gli acquisti)")
        logger.info("  - bundles (gestita tramite web admin)")
        logger.info("  - bundle_beats (associazioni bundle-beat)")
        logger.info("  - bundle_orders (tabella legacy)")
        
        # Verifica che le tabelle siano state create
        with SessionLocal() as session:
            # Test di connessione e verifica struttura tabelle
            result = session.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_type = 'BASE TABLE'
                ORDER BY table_name;
            """))
            
            tables = [row[0] for row in result]
            logger.info(f"Tabelle presenti nel database: {', '.join(tables)}")
            
            # Verifica che le tabelle principali esistano
            required_tables = ['beats', 'orders', 'bundles', 'bundle_beats']
            missing_tables = [table for table in required_tables if table not in tables]
            
            if missing_tables:
                logger.error(f"❌ Tabelle mancanti: {', '.join(missing_tables)}")
                return False
            
            logger.info("✅ Tutte le tabelle richieste sono presenti")
            
            # Verifica count delle tabelle principali
            beats_count = session.query(Beat).count()
            bundles_count = session.query(Bundle).count()
            orders_count = session.query(Order).count()
            
            logger.info(f"Stato attuale del database:")
            logger.info(f"  - Beats: {beats_count} record")
            logger.info(f"  - Bundles: {bundles_count} record")
            logger.info(f"  - Orders: {orders_count} record")
            
        logger.info("🎉 Inizializzazione database completata con successo!")
        logger.info("💡 Prossimi passi:")
        logger.info("  1. Popolare manualmente la tabella 'beats' con i tuoi beat")
        logger.info("  2. Utilizzare la web admin per creare bundle promozionali")
        logger.info("  3. Gli ordini si creeranno automaticamente con gli acquisti")
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Errore durante l'inizializzazione del database: {str(e)}")
        return False

def check_database_status():
    """
    Verifica lo stato del database e delle tabelle.
    Utile per diagnosticare problemi o verificare la configurazione.
    
    Returns:
        dict: Informazioni sullo stato del database
    """
    try:
        status = {
            "connected": False,
            "tables": [],
            "counts": {},
            "errors": []
        }
        
        with SessionLocal() as session:
            status["connected"] = True
            
            # Lista delle tabelle
            result = session.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_type = 'BASE TABLE'
                ORDER BY table_name;
            """))
            status["tables"] = [row[0] for row in result]
            
            # Count dei record per tabella principale
            if 'beats' in status["tables"]:
                status["counts"]["beats"] = session.query(Beat).count()
            if 'bundles' in status["tables"]:
                status["counts"]["bundles"] = session.query(Bundle).count()
            if 'orders' in status["tables"]:
                status["counts"]["orders"] = session.query(Order).count()
            if 'bundle_beats' in status["tables"]:
                status["counts"]["bundle_beats"] = session.query(BundleBeat).count()
                
        return status
        
    except Exception as e:
        return {
            "connected": False,
            "tables": [],
            "counts": {},
            "errors": [str(e)]
        }

def reset_database(confirm_reset=False):
    """
    ATTENZIONE: Elimina tutte le tabelle e le ricrea vuote.
    Utilizzare solo per reset completo del database.
    
    Args:
        confirm_reset (bool): Deve essere True per confermare l'operazione
        
    Returns:
        bool: True se il reset è riuscito, False altrimenti
    """
    if not confirm_reset:
        logger.warning("⚠️  Reset database non confermato. Passa confirm_reset=True per procedere.")
        return False
    
    try:
        logger.warning("🚨 RESET DATABASE IN CORSO - TUTTI I DATI VERRANNO ELIMINATI!")
        
        # Elimina tutte le tabelle
        Base.metadata.drop_all(bind=engine)
        logger.info("✅ Tabelle eliminate")
        
        # Ricrea le tabelle vuote
        result = initialize_database()
        
        if result:
            logger.info("✅ Database resettato e reinizializzato con successo")
        else:
            logger.error("❌ Errore durante la reinizializzazione dopo il reset")
            
        return result
        
    except Exception as e:
        logger.error(f"❌ Errore durante il reset del database: {str(e)}")
        return False

def get_beat_availability_status(beat_id: int) -> tuple[bool, str]:
    """
    Controlla la disponibilità di un beat esclusivo e restituisce il motivo specifico.
    
    Returns:
        tuple[bool, str]: (is_available, reason_message)
    """
    
    with SessionLocal() as session:
        beat = session.query(Beat).filter(Beat.id == beat_id).first()
        if not beat:
            return False, "Beat non trovato"
        
        # Se non è esclusivo, è sempre disponibile
        if beat.is_exclusive != 1:
            return True, "Disponibile"
        
        now = datetime.now()  # Uso datetime naive per consistenza
        
        # 1. Controlla se è prenotato individualmente
        if (beat.reserved_by_user_id is not None and 
            beat.reservation_expires_at is not None):
            
            expires_at = beat.reservation_expires_at
            # Confronto diretto tra datetime naive
            if expires_at > now:
                minutes_left = int((expires_at - now).total_seconds() / 60)
                return False, f"Prenotato da un altro utente (scade tra {minutes_left} minuti)"
        
        # 2. Controlla se il beat è già stato venduto
        completed_orders = session.query(Order).filter(
            Order.beat_title == beat.title,
            Order.order_type == "beat"
        ).first()
        if completed_orders:
            return False, "Beat già venduto"
        
        # 3. Controlla se è parte di un bundle in acquisto
        recent_threshold = now - timedelta(minutes=15)
        
        bundles_with_beat = session.query(Bundle).join(BundleBeat).filter(
            BundleBeat.beat_id == beat_id,
            Bundle.is_active == 1
        ).all()
        
        for bundle in bundles_with_beat:
            recent_bundle_orders = session.query(Order).filter(
                Order.bundle_id == bundle.id,
                Order.order_type == "bundle",
                Order.created_at >= recent_threshold
            ).first()
            
            if recent_bundle_orders:
                return False, f"Incluso nel bundle '{bundle.name}' attualmente in acquisto"
        
        return True, "Disponibile"

def reset_all_reservations():
    """
    UTILITY: Resetta tutte le prenotazioni beat esistenti.
    Utile per testing e debug senza toccare il resto del database.
    
    Returns:
        int: Numero di prenotazioni resettate
    """
    with SessionLocal() as session:
        # Trova tutti i beat con prenotazioni attive
        reserved_beats = session.query(Beat).filter(
            Beat.reserved_by_user_id.isnot(None)
        ).all()
        
        count = len(reserved_beats)
        
        # Resetta tutte le prenotazioni
        for beat in reserved_beats:
            beat.reserved_by_user_id = None
            beat.reserved_at = None
            beat.reservation_expires_at = None
        
        session.commit()
        
        print(f"🔄 Reset completato: {count} prenotazioni eliminate")
        if count > 0:
            print("✅ Tutti i beat esclusivi sono ora disponibili per il test")
        else:
            print("ℹ️  Nessuna prenotazione attiva trovata")
            
        return count

def get_user_active_reservation(user_id: int) -> tuple[bool, str, int]:
    """
    Controlla se l'utente ha già una prenotazione attiva.
    
    Returns:
        tuple[bool, str, int]: (has_reservation, beat_title_or_message, beat_id)
    """
    
    with SessionLocal() as session:
        now = datetime.now()  # Uso datetime naive per consistenza
        
        # Cerca prenotazioni attive dell'utente
        active_reservation = session.query(Beat).filter(
            Beat.reserved_by_user_id == user_id,
            Beat.reservation_expires_at.isnot(None)
        ).first()
        
        if active_reservation:
            expires_at = active_reservation.reservation_expires_at
            
            # Ora tutto è naive, confronto diretto
            if expires_at > now:
                minutes_left = int((expires_at - now).total_seconds() / 60)
                return True, f"Hai già prenotato '{active_reservation.title}' (scade tra {minutes_left} minuti)", active_reservation.id
            else:
                # Prenotazione scaduta, pulisci
                active_reservation.reserved_by_user_id = None
                active_reservation.reserved_at = None
                active_reservation.reservation_expires_at = None
                session.commit()
        
        return False, "Nessuna prenotazione attiva", None

def validate_checkout_token(user_id: int, beat_id: int, token: str, timestamp: int) -> bool:
    """
    Valida un token di checkout per evitare abusi di link salvati.
    
    Args:
        user_id: ID utente Telegram
        beat_id: ID del beat
        token: Token di validazione dal link
        timestamp: Timestamp di creazione del link
        
    Returns:
        bool: True se il token è valido e l'utente ha prenotazione attiva per quel beat
    """
    import hashlib
    import time
    
    # Verifica che il token non sia troppo vecchio (massimo 15 minuti)
    current_time = int(time.time())
    if current_time - timestamp > 900:  # 15 minuti
        logger.info(f"Token scaduto per utente {user_id}, beat {beat_id}")
        return False
    
    # Ricostruisci il token atteso
    token_data = f"{user_id}_{beat_id}_{timestamp}"
    expected_token = hashlib.md5(token_data.encode()).hexdigest()[:16]
    
    if token != expected_token:
        logger.info(f"Token non valido per utente {user_id}, beat {beat_id}")
        return False
    
    # Verifica che l'utente abbia effettivamente prenotato questo beat
    cleanup_expired_reservations()
    has_reservation, _, reserved_beat_id = get_user_active_reservation(user_id)
    
    if not has_reservation or reserved_beat_id != beat_id:
        logger.info(f"Utente {user_id} non ha prenotazione attiva per beat {beat_id}")
        return False
    
    logger.info(f"Token valido per utente {user_id}, beat {beat_id}")
    return True

def reserve_bundle_exclusive_beats(bundle_id: int, user_id: int, reservation_minutes: int = 10) -> tuple[bool, str]:
    """
    Prenota temporaneamente tutti i beat esclusivi in un bundle per l'utente specificato.
    LIMITAZIONE: Un utente può prenotare solo 1 beat esclusivo alla volta.
    Ritorna True se la prenotazione è riuscita, False se il beat è già prenotato.
    """
    
    with SessionLocal() as session:
        # Trova il bundle
        bundle = session.query(Bundle).filter(Bundle.id == bundle_id).first()
        if not bundle:
            return False, "Bundle non trovato"
        
        # Trova tutti i beat esclusivi nel bundle
        exclusive_beats = session.query(Beat).join(BundleBeat).filter(
            BundleBeat.bundle_id == bundle_id,
            Beat.is_exclusive == 1
        ).all()
        
        if not exclusive_beats:
            return True, "Nessun beat esclusivo nel bundle"
        
        now = datetime.now()  # Uso datetime naive per consistenza
        
        # Controlla se l'utente ha già una prenotazione attiva (anche su beat singoli)
        existing_reservation = session.query(Beat).filter(
            Beat.reserved_by_user_id == user_id,
            Beat.reservation_expires_at.isnot(None)
        ).first()
        
        if existing_reservation:
            # Controlla se la prenotazione è scaduta
            try:
                if existing_reservation.reservation_expires_at > now:
                    return False, f"Hai già una prenotazione attiva per '{existing_reservation.title}'"
            except Exception:
                pass  # Se c'è un errore di confronto datetime, continua
        
        # Controlla che tutti i beat esclusivi siano disponibili
        unavailable_beats = []
        for beat in exclusive_beats:
            # Controlla se è già prenotato da qualcun altro
            if (beat.reserved_by_user_id is not None and 
                beat.reserved_by_user_id != user_id and
                beat.reservation_expires_at is not None):
                
                try:
                    if beat.reservation_expires_at > now:
                        unavailable_beats.append(beat.title)
                except Exception:
                    pass  # Se c'è un errore di confronto datetime, assume non prenotato
        
        if unavailable_beats:
            return False, f"Beat già prenotati: {', '.join(unavailable_beats)}"
        
        # Prenota tutti i beat esclusivi
        reservation_expires = now + timedelta(minutes=reservation_minutes)
        
        for beat in exclusive_beats:
            beat.reserved_by_user_id = user_id
            beat.reserved_at = now
            beat.reservation_expires_at = reservation_expires
        
        session.commit()
        logger.info(f"Bundle {bundle_id} prenotato: {len(exclusive_beats)} beat esclusivi per utente {user_id}")
        return True, f"Bundle prenotato: {len(exclusive_beats)} beat esclusivi per {reservation_minutes} minuti"

def release_bundle_reservations(bundle_id: int, user_id: int = None) -> int:
    """
    Rilascia le prenotazioni di tutti i beat esclusivi in un bundle.
    Se user_id è specificato, rilascia solo se i beat sono prenotati da quell'utente.
    
    Returns:
        int: Numero di prenotazioni rilasciate
    """
    with SessionLocal() as session:
        try:
            # Trova tutti i beat esclusivi nel bundle che sono prenotati
            exclusive_beats = session.query(Beat).join(BundleBeat).filter(
                BundleBeat.bundle_id == bundle_id,
                Beat.is_exclusive == 1,
                Beat.reserved_by_user_id.isnot(None)
            ).all()
            
            released_count = 0
            
            for beat in exclusive_beats:
                # Se è specificato un user_id, controlla che coincida
                if user_id is None or beat.reserved_by_user_id == user_id:
                    beat.reserved_by_user_id = None
                    beat.reserved_at = None
                    beat.reservation_expires_at = None
                    released_count += 1
            
            if released_count > 0:
                session.commit()
                logger.info(f"✅ Rilasciate {released_count} prenotazioni bundle {bundle_id} per utente {user_id}")
            
            return released_count
            
        except Exception as e:
            logger.error(f"❌ Errore rilascio prenotazioni bundle {bundle_id}: {e}")
            session.rollback()
            return 0

@contextmanager
def timeout_session(timeout_seconds=5):
    """Context manager per sessioni con timeout per prevenire deadlock"""
    session = SessionLocal()
    try:
        # Imposta timeout per SQLite
        session.execute(text(f"PRAGMA busy_timeout = {timeout_seconds * 1000}"))
        yield session
    finally:
        session.close()

def reserve_bundle_exclusive_beats_with_retry(bundle_id: int, user_id: int, reservation_minutes: int = 10, max_retries: int = 3) -> tuple[bool, str]:
    """
    Versione con retry della prenotazione bundle per gestire race conditions estreme.
    """
    for attempt in range(max_retries):
        try:
            success, message = reserve_bundle_exclusive_beats(bundle_id, user_id, reservation_minutes)
            if success or "già prenotati" in message or "già una prenotazione" in message:
                # Se successo o se è un errore definitivo (non race condition), ritorna immediatamente
                return success, message
            
            # Se è un errore temporaneo, aspetta un po' prima di riprovare
            if attempt < max_retries - 1:
                wait_time = 0.1 + (attempt * 0.2) + random.uniform(0, 0.1)  # Backoff esponenziale con jitter
                logger.info(f"🔄 Retry {attempt + 1}/{max_retries} for bundle {bundle_id} reservation in {wait_time:.2f}s")
                time.sleep(wait_time)
                
        except OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                wait_time = 0.2 + (attempt * 0.3) + random.uniform(0, 0.1)
                logger.warning(f"⚠️ Database locked during bundle {bundle_id} reservation, retry {attempt + 1}/{max_retries} in {wait_time:.2f}s")
                time.sleep(wait_time)
                continue
            else:
                logger.error(f"❌ Database error during bundle {bundle_id} reservation: {e}")
                return False, "Errore database durante la prenotazione"
        except Exception as e:
            logger.error(f"❌ Unexpected error during bundle {bundle_id} reservation: {e}")
            return False, "Errore imprevisto durante la prenotazione"
    
    # Se tutti i tentativi sono falliti
    logger.error(f"❌ All {max_retries} attempts failed for bundle {bundle_id} reservation")
    return False, "Bundle temporaneamente non disponibile, riprova tra qualche secondo"

def release_bundle_reservations(bundle_id: int, user_id: int = None) -> int:
    """
    Rilascia le prenotazioni di tutti i beat esclusivi in un bundle.
    Se user_id è specificato, rilascia solo se i beat sono prenotati da quell'utente.
    
    Returns:
        int: Numero di prenotazioni rilasciate
    """
    with SessionLocal() as session:
        try:
            # Trova tutti i beat esclusivi nel bundle che sono prenotati
            exclusive_beats = session.query(Beat).join(BundleBeat).filter(
                BundleBeat.bundle_id == bundle_id,
                Beat.is_exclusive == 1,
                Beat.reserved_by_user_id.isnot(None)
            ).all()
            
            released_count = 0
            
            for beat in exclusive_beats:
                # Se è specificato un user_id, controlla che coincida
                if user_id is None or beat.reserved_by_user_id == user_id:
                    beat.reserved_by_user_id = None
                    beat.reserved_at = None
                    beat.reservation_expires_at = None
                    released_count += 1
            
            if released_count > 0:
                session.commit()
                logger.info(f"✅ Rilasciate {released_count} prenotazioni bundle {bundle_id} per utente {user_id}")
            
            return released_count
            
        except Exception as e:
            logger.error(f"❌ Errore rilascio prenotazioni bundle {bundle_id}: {e}")
            session.rollback()
            return 0