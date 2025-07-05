from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy.pool import QueuePool
import os

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
    
    orders = relationship("Order", back_populates="beat")

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    transaction_id = Column(String(255), unique=True, nullable=False)
    telegram_user_id = Column(Integer, nullable=False)
    beat_title = Column(String(255), nullable=False)
    payer_email = Column(String(255), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False)
    token = Column(String(255), nullable=True)

    beat_id = Column(Integer, ForeignKey("beats.id"), nullable=True)  # Chiave esterna opzionale

    beat = relationship("Beat", back_populates="orders")

def get_session():
    """Restituisce una sessione per interagire con il database"""
    return