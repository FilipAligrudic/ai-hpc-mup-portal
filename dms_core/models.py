"""
DMS (Document Management System) modeli za upravljanje zahtjevima
Podržava MUP i turističke zahtjeve sa workflow status tracking
"""

from datetime import datetime
from enum import Enum as PyEnum
import os
from pathlib import Path
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Text, Enum, Float, JSON, Boolean, ForeignKey, Index, text as _sql_text
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# Kreiraj SQLAlchemy engine sa stabilnom putanjom unutar projekta.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTANCE_DIR = PROJECT_ROOT / "instance"
INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
DMS_DB_PATH = INSTANCE_DIR / "dms.db"

engine = create_engine(f"sqlite:///{DMS_DB_PATH.as_posix()}", echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def _ensure_dms_columns() -> None:
    """Sigurna migracija za kolone dodane nakon inicijalnog kreiranja tabele."""
    migrations = [
        "ALTER TABLE dms_requests ADD COLUMN forward_count INTEGER DEFAULT 0",
    ]
    try:
        with engine.connect() as conn:
            for stmt in migrations:
                try:
                    conn.execute(_sql_text(stmt))
                    conn.commit()
                except Exception:
                    pass  # kolona već postoji
    except Exception:
        pass  # tabela još ne postoji (novi install — create_all će je napraviti)

_ensure_dms_columns()


class RequestType(PyEnum):
    """Tipovi zahtjeva u sistemu"""
    # MUP zahtjevi
    LICNA_KARTA = "licna_karta"
    PASOS = "pasos"
    VOZACKA_DOZVOLA = "vozacka_dozvola"
    KRIVICNI_LIST = "krivicni_list"
    VIZA = "viza"
    
    # Turističke registracije
    TURIZAM_REGISTRACIJA = "turizam_registracija"
    TURIZAM_LICENCA = "turizam_licenca"
    TURIZAM_DOZVOLA_GRADNJE = "turizam_dozvola_gradnje"


class RequestStatus(PyEnum):
    """Status zahtjeva kroz workflow"""
    DRAFT = "draft"  # Korisnik samo počeo
    SUBMITTED = "submitted"  # Podnesen zahtjev
    UNDER_REVIEW = "under_review"  # U obradi
    PENDING_USER = "pending_user"  # Čeka korisnika
    APPROVED = "approved"  # Odobren
    REJECTED = "rejected"  # Odbijen
    COMPLETED = "completed"  # Završen


class RequestPriority(PyEnum):
    """Prioritet zahtjeva"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class DmsRequest(Base):
    """Centralni model za sve zahtjeve"""
    __tablename__ = 'dms_requests'
    __table_args__ = (
        Index("idx_dms_requests_user_id", "user_id"),
        Index("idx_dms_requests_status", "status"),
        Index("idx_dms_requests_created_at", "created_at"),
    )
    
    id = Column(Integer, primary_key=True)
    request_type = Column(Enum(RequestType), nullable=False, index=True)
    
    # Korisnik podaci
    user_id = Column(String, nullable=False)
    user_email = Column(String, nullable=False)
    user_city = Column(String, nullable=False)
    
    # Status
    status = Column(Enum(RequestStatus), default=RequestStatus.DRAFT, nullable=False, index=True)
    priority = Column(Enum(RequestPriority), default=RequestPriority.MEDIUM)
    
    # Vremenski žigovi
    created_at = Column(DateTime, default=datetime.now)
    submitted_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    completed_at = Column(DateTime, nullable=True)
    
    # Rok završetka (procjenjeni)
    estimated_completion = Column(DateTime, nullable=True)
    
    # Detalji zahtjeva
    description = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)  # Zašto traži (npr. izgubljena karta, nomadski rad)
    
    # JSON sa specifičnim poljima po tipu zahtjeva
    details = Column(JSON, nullable=True)
    
    # Dokumenta metadata
    documents_metadata = Column(JSON, nullable=True)  # Lista dokumenata sa statusom
    required_documents = Column(JSON, nullable=True)  # Template šta je potrebno
    
    # Dodijeljena osoba (MUP radnik/turizam inspektoa)
    assigned_to = Column(String, nullable=True)
    
    # Napomene
    notes = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)

    # Broj proslijeđivanja (max 2)
    forward_count = Column(Integer, default=0, server_default="0")

    # Plaćanje takse (mock)
    payment_status = Column(String, nullable=True)  # not_required | pending | paid
    payment_reference = Column(String, nullable=True)
    paid_at = Column(DateTime, nullable=True)

    # e-Potpis odluke (PDF rješenje)
    signed_pdf_path = Column(String, nullable=True)
    signature_hash = Column(String, nullable=True)

    # Connections
    status_history = relationship("RequestStatusHistory", back_populates="request", cascade="all, delete-orphan")
    comments = relationship("RequestComment", back_populates="request", cascade="all, delete-orphan")
    
    def __repr__(self):
        return f"<DmsRequest {self.id}: {self.request_type.value} - {self.status.value}>"


class RequestStatusHistory(Base):
    """Istorija promjena statusa zahtjeva sa tamper-evident hash chain."""
    __tablename__ = 'request_status_history'

    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey('dms_requests.id'), nullable=False)

    from_status = Column(Enum(RequestStatus), nullable=False)
    to_status = Column(Enum(RequestStatus), nullable=False)

    changed_by = Column(String, nullable=True)  # Ko je promijenio
    changed_at = Column(DateTime, default=datetime.now)
    reason = Column(Text, nullable=True)

    # Hash chain: entry_hash = sha256(prev_hash || serializovana sadržina)
    prev_hash = Column(String, nullable=True)
    entry_hash = Column(String, nullable=True)

    request = relationship("DmsRequest", back_populates="status_history")

    def __repr__(self):
        return f"<StatusChange {self.from_status.value} → {self.to_status.value}>"


class RequestComment(Base):
    """Komentari na zahtjevu"""
    __tablename__ = 'request_comments'
    
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey('dms_requests.id'), nullable=False)
    
    author = Column(String, nullable=False)  # Ko je istavio komentar
    author_type = Column(String, nullable=False)  # "user" ili "admin"
    
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    
    is_internal = Column(Boolean, default=False)  # Korisnik ne vidi
    
    request = relationship("DmsRequest", back_populates="comments")


class DocumentTemplate(Base):
    """Template za potrebne dokumente po tipu zahtjeva"""
    __tablename__ = 'document_templates'
    
    id = Column(Integer, primary_key=True)
    request_type = Column(String, unique=True, nullable=False)
    
    required_documents = Column(JSON, nullable=False)  # Lista dokumenata
    optional_documents = Column(JSON, nullable=True)   # Opciono
    
    # Vremenska procjena
    estimated_days = Column(Integer, nullable=False)  # Koliko dana traje procedura
    processing_fee_eur = Column(Float, nullable=True)
    
    instructions = Column(Text, nullable=True)  # Upute za korisnika
    ai_keywords = Column(JSON, nullable=True)  # Za AI bot prepoznavanje
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, onupdate=datetime.now)


class TourismProperty(Base):
    """Kuća/Stan registrovan za turizam"""
    __tablename__ = 'tourism_properties'
    
    id = Column(Integer, primary_key=True)
    property_type = Column(String, nullable=False)  # "house", "apartment", "villa"
    
    owner_id = Column(String, nullable=False)
    address = Column(String, nullable=False)
    city = Column(String, nullable=False)
    coordinates = Column(JSON, nullable=True)  # {"lat": x, "lon": y}
    
    # Detalji
    capacity = Column(Integer, nullable=False)  # Broj osoba
    rooms = Column(Integer, nullable=False)
    amenities = Column(JSON, nullable=True)  # ["wifi", "parking", "kitchen"]
    
    # Status registracije
    license_number = Column(String, nullable=True)
    license_valid_from = Column(DateTime, nullable=True)
    license_valid_to = Column(DateTime, nullable=True)
    
    # Kategorija/zvjezdice
    category = Column(String, nullable=True)  # "3*", "4*", itd
    
    # Povezani zahtjev
    registration_request_id = Column(Integer, ForeignKey('dms_requests.id'), nullable=True)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, onupdate=datetime.now)
    
    def __repr__(self):
        return f"<Property {self.address} - Capacity: {self.capacity} osoba>"
