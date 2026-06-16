"""
Document Management System Manager
Upravljanje zahtjevima kroz kompletan workflow
"""

import hashlib
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dms_core.models import (
    DmsRequest, RequestType, RequestStatus, RequestPriority,
    DocumentTemplate, RequestStatusHistory, RequestComment, TourismProperty
)
from municipality_utils import validate_municipality
from database.database import get_staff_usernames
from dms_core.notifications import notify
from dms_core.signature import generate_signed_decision


_STATUS_LABELS_HR = {
    RequestStatus.DRAFT: "Nacrt",
    RequestStatus.SUBMITTED: "Podnesen",
    RequestStatus.UNDER_REVIEW: "U obradi",
    RequestStatus.PENDING_USER: "Čeka korisnika",
    RequestStatus.APPROVED: "Odobren",
    RequestStatus.REJECTED: "Odbijen",
    RequestStatus.COMPLETED: "Završen",
}


def _hash_history_entry(prev_hash: str, request_id: int, from_status: str,
                        to_status: str, changed_by: str, changed_at: str,
                        reason: str) -> str:
    """Deterministički SHA-256 nad serijalizovanom sadržinom + prethodni hash."""
    payload = "|".join([
        prev_hash or "",
        str(request_id),
        from_status or "",
        to_status or "",
        changed_by or "",
        changed_at or "",
        reason or "",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_TOURISM_TYPES = {
    RequestType.TURIZAM_REGISTRACIJA,
    RequestType.TURIZAM_LICENCA,
    RequestType.TURIZAM_DOZVOLA_GRADNJE,
}


class DmsManager:
    """Upravljanje Dokumentima i Zahtjevima"""
    
    def __init__(self, db_session):
        self.db = db_session
        self.logger = logging.getLogger("dms_portal.dms_manager")

    def _get_request(self, request_id: int) -> Optional[DmsRequest]:
        return self.db.get(DmsRequest, request_id)
    
    # ============= KREIRANJE ZAHTJEVA =============
    
    def create_request(
        self,
        request_type: RequestType,
        user_id: str,
        user_email: str,
        user_city: str,
        details: Dict = None,
        description: str = None,
        reason: str = None
    ) -> DmsRequest:
        """Kreiraj novi zahtjev"""

        if user_city and not validate_municipality(user_city):
            raise ValueError("Nevalidna opština za korisnika.")
        
        # Učitaj template sa potrebnim dokumentima
        template = self.db.query(DocumentTemplate).filter(
            DocumentTemplate.request_type == request_type.value
        ).first()

        # Procijeni trajanje iz istorije (po tipu); fallback na template
        template_days = template.estimated_days if template else 15
        estimated_days = self._estimate_days_for_type(request_type, fallback=template_days)

        fee = float(template.processing_fee_eur or 0) if template else 0.0
        payment_status = "pending" if fee > 0 else "not_required"

        request = DmsRequest(
            request_type=request_type,
            user_id=user_id,
            user_email=user_email,
            user_city=user_city,
            details=details or {},
            description=description,
            reason=reason,
            status=RequestStatus.DRAFT,
            priority=RequestPriority.MEDIUM,
            required_documents=template.required_documents if template else [],
            estimated_completion=datetime.now() + timedelta(days=estimated_days),
            payment_status=payment_status,
        )
        
        self.db.add(request)
        self.db.commit()
        
        return request
    
    # ============= WORKFLOW STATUS =============
    
    def submit_request(self, request_id: int, changed_by: str = None) -> bool:
        """Podnesi zahtjev: DRAFT → SUBMITTED → automatski UNDER_REVIEW ako postoji slobodan službenik."""
        request = self._get_request(request_id)
        if not request:
            return False

        request.submitted_at = datetime.now()
        success = self._change_status(
            request_id,
            RequestStatus.SUBMITTED,
            changed_by=changed_by,
            reason="Korisnik je podnio zahtjev",
        )

        if success:
            self._auto_assign(request_id, submitted_by=changed_by)

        return success

    def _auto_assign(self, request_id: int, submitted_by: str = None) -> bool:
        """Pronađi najmanje opterećenog službenika i prebaci zahtjev u UNDER_REVIEW."""
        try:
            staff = get_staff_usernames()
        except Exception:
            self.logger.warning("Auto-assign: nije moguće učitati listu službenika")
            return False

        if not staff:
            # Demo fallback: koristi APP_ADMIN_USERS env listu kao virtualne službenike
            env_raw = os.getenv("APP_ADMIN_USERS", "admin,rapoz")
            env_admins = [u.strip() for u in env_raw.split(",") if u.strip()]
            if env_admins:
                staff = env_admins
                self.logger.info("Auto-assign: nema DB staff, fallback na env admins: %s", staff)
            else:
                self.logger.info("Auto-assign: nema registrovanih službenika, request_id=%s ostaje SUBMITTED", request_id)
                return False

        active_statuses = [RequestStatus.SUBMITTED, RequestStatus.UNDER_REVIEW, RequestStatus.PENDING_USER]

        # Izbjegni dodjelu istom korisniku koji je podnio (npr. admin demo)
        candidates = [o for o in staff if o != submitted_by] or list(staff)

        workload = {}
        for officer in candidates:
            workload[officer] = self.db.query(DmsRequest).filter(
                DmsRequest.assigned_to == officer,
                DmsRequest.status.in_(active_statuses),
            ).count()

        assigned_to = min(workload, key=workload.get)

        request = self._get_request(request_id)
        if not request:
            return False

        request.assigned_to = assigned_to
        self.db.commit()

        self._change_status(
            request_id,
            RequestStatus.UNDER_REVIEW,
            changed_by="sistem",
            reason=f"Automatski dodijeljeno službeniku {assigned_to} (aktivnih predmeta: {workload[assigned_to]})",
        )

        self.logger.info(
            "Auto-assign: request_id=%s → officer=%s (active=%s)",
            request_id, assigned_to, workload[assigned_to],
        )
        return True
    
    _MAX_FORWARDS = 2

    def forward_request(
        self,
        request_id: int,
        from_user: str,
        to_user: str,
        reason: str,
    ) -> bool:
        """Proslijedi zahtjev drugom službeniku (max _MAX_FORWARDS puta)."""
        request = self._get_request(request_id)
        if not request:
            return False

        current = int(request.forward_count or 0)
        if current >= self._MAX_FORWARDS:
            raise ValueError(
                f"Zahtjev je već proslijeđen {self._MAX_FORWARDS} puta i ne može se dalje proslijeđivati."
            )
        if not reason or not reason.strip():
            raise ValueError("Razlog proslijeđivanja je obavezan.")
        if not to_user or not to_user.strip():
            raise ValueError("Odredišni službenik je obavezan.")

        old_assignee = request.assigned_to or "—"
        request.assigned_to = to_user.strip()
        request.forward_count = current + 1
        request.updated_at = datetime.now()
        self.db.commit()

        self.add_comment(
            request_id=request_id,
            author=from_user,
            content=f"Proslijeđeno od {old_assignee} → {to_user}. Razlog: {reason.strip()}",
            author_type="admin",
            is_internal=True,
        )

        self.logger.info(
            "Forward request_id=%s %s→%s forward_count=%s",
            request_id, old_assignee, to_user, request.forward_count,
        )
        return True

    def start_review(self, request_id: int, assigned_to: str) -> bool:
        """Počni pregled (SUBMITTED → UNDER_REVIEW)"""
        request = self._get_request(request_id)
        if not request:
            return False
        
        request.assigned_to = assigned_to
        
        return self._change_status(
            request_id,
            RequestStatus.UNDER_REVIEW,
            changed_by=assigned_to,
            reason=f"Pregled započet od strane {assigned_to}"
        )
    
    def request_user_action(self, request_id: int, action_needed: str, changed_by: str) -> bool:
        """Zahtijevaj od korisnika akciju (→ PENDING_USER)"""
        request = self._get_request(request_id)
        if not request:
            return False
        
        # Dodaj komentar koji vidi korisnik
        self.add_comment(
            request_id,
            author=changed_by,
            content=action_needed,
            author_type="admin",
            is_internal=False
        )
        
        return self._change_status(
            request_id,
            RequestStatus.PENDING_USER,
            changed_by=changed_by,
            reason=action_needed
        )
    
    def approve_request(self, request_id: int, approved_by: str, notes: str = None) -> bool:
        """Odobri zahtjev"""
        request = self._get_request(request_id)
        if not request:
            return False
        
        request.completed_at = datetime.now()
        if notes:
            request.notes = notes
        
        return self._change_status(
            request_id,
            RequestStatus.APPROVED,
            changed_by=approved_by,
            reason=f"Zahtjev odobren. {notes or ''}"
        )
    
    def complete_request(self, request_id: int, completed_by: str) -> bool:
        """Označi zahtjev kao završen"""
        request = self._get_request(request_id)
        if not request:
            return False
        
        request.completed_at = datetime.now()
        
        return self._change_status(
            request_id,
            RequestStatus.COMPLETED,
            changed_by=completed_by,
            reason="Zahtjev je završen i isporučen korisniku"
        )
    
    def reject_request(self, request_id: int, rejection_reason: str, rejected_by: str) -> bool:
        """Odbij zahtjev"""
        request = self._get_request(request_id)
        if not request:
            return False
        
        request.rejection_reason = rejection_reason
        request.completed_at = datetime.now()
        
        # Dodaj javni komentar
        self.add_comment(
            request_id,
            author=rejected_by,
            content=f"Zahtjev odbijen. Razlog: {rejection_reason}",
            author_type="admin",
            is_internal=False
        )
        
        return self._change_status(
            request_id,
            RequestStatus.REJECTED,
            changed_by=rejected_by,
            reason=rejection_reason
        )
    
    def _change_status(
        self,
        request_id: int,
        new_status: RequestStatus,
        changed_by: str = None,
        reason: str = None
    ) -> bool:
        """Promijeni status i spremi u istoriju"""
        request = self._get_request(request_id)
        if not request:
            return False

        if not changed_by:
            raise ValueError("Promjena statusa zahtijeva korisnicki identitet.")
        
        old_status = request.status
        if old_status == new_status:
            return True

        if not self._is_transition_allowed(old_status, new_status):
            raise ValueError(f"Nedozvoljena tranzicija: {old_status.value} -> {new_status.value}")

        request.status = new_status
        request.updated_at = datetime.now()

        # Hash chain: nadovezi se na zadnji red u istoriji
        last_entry = (
            self.db.query(RequestStatusHistory)
            .filter(RequestStatusHistory.request_id == request_id)
            .order_by(RequestStatusHistory.id.desc())
            .first()
        )
        prev_hash = last_entry.entry_hash if last_entry and last_entry.entry_hash else ""
        changed_at = datetime.now()
        entry_hash = _hash_history_entry(
            prev_hash=prev_hash,
            request_id=request_id,
            from_status=old_status.value if old_status else "",
            to_status=new_status.value,
            changed_by=changed_by,
            changed_at=changed_at.isoformat(),
            reason=reason or "",
        )

        history = RequestStatusHistory(
            request_id=request_id,
            from_status=old_status,
            to_status=new_status,
            changed_by=changed_by,
            changed_at=changed_at,
            reason=reason,
            prev_hash=prev_hash or None,
            entry_hash=entry_hash,
        )

        self.db.add(history)
        self.db.commit()
        self.logger.info(
            "Status change request_id=%s from=%s to=%s by=%s",
            request_id,
            old_status.value,
            new_status.value,
            changed_by,
        )

        # Pošalji notifikaciju korisniku ako je promjena javna
        if changed_by != request.user_id:
            self._notify_status_change(request, old_status, new_status, reason)

        return True

    def _notify_status_change(
        self,
        request: DmsRequest,
        old_status: RequestStatus,
        new_status: RequestStatus,
        reason: Optional[str],
    ) -> None:
        new_label = _STATUS_LABELS_HR.get(new_status, new_status.value)
        title = f"Zahtjev #{request.id}: novi status — {new_label}"
        body_lines = [
            f"Tip zahtjeva: {request.request_type.value}",
            f"Prethodni status: {_STATUS_LABELS_HR.get(old_status, old_status.value)}",
            f"Novi status: {new_label}",
        ]
        if reason:
            body_lines.append(f"Napomena: {reason}")
        try:
            notify(
                username=request.user_id,
                title=title,
                body="\n".join(body_lines),
                request_id=request.id,
                email=request.user_email,
            )
        except Exception:
            self.logger.exception("Notifikacija nije poslana request_id=%s", request.id)

    def _is_transition_allowed(self, old_status: RequestStatus, new_status: RequestStatus) -> bool:
        allowed = {
            RequestStatus.DRAFT: {RequestStatus.SUBMITTED},
            RequestStatus.SUBMITTED: {RequestStatus.UNDER_REVIEW, RequestStatus.REJECTED},
            RequestStatus.UNDER_REVIEW: {
                RequestStatus.PENDING_USER,
                RequestStatus.APPROVED,
                RequestStatus.REJECTED,
            },
            RequestStatus.PENDING_USER: {RequestStatus.SUBMITTED, RequestStatus.UNDER_REVIEW},
            RequestStatus.APPROVED: {RequestStatus.COMPLETED},
            RequestStatus.REJECTED: set(),
            RequestStatus.COMPLETED: set(),
        }
        return new_status in allowed.get(old_status, set())

    def get_available_transitions(self, request_id: int) -> List[RequestStatus]:
        request = self._get_request(request_id)
        if not request:
            return []
        candidates = [status for status in RequestStatus if status != request.status]
        return [status for status in candidates if self._is_transition_allowed(request.status, status)]

    def transition_request(
        self,
        request_id: int,
        new_status: RequestStatus,
        changed_by: str,
        actor_role: str = "admin",
        reason: str = None,
    ) -> bool:
        """Javni API za promjenu statusa kroz validirani workflow."""
        request = self._get_request(request_id)
        if not request:
            return False

        role = (actor_role or "").strip().lower()
        if role != "admin":
            # Korisniku je dozvoljen samo povratak iz PENDING_USER u SUBMITTED.
            allowed_user_transition = (
                request.user_id == changed_by
                and request.status == RequestStatus.PENDING_USER
                and new_status == RequestStatus.SUBMITTED
            )
            if not allowed_user_transition:
                raise PermissionError("Nemate dozvolu za ovu promjenu statusa.")

        result = self._change_status(
            request_id=request_id,
            new_status=new_status,
            changed_by=changed_by,
            reason=reason,
        )

        if result and new_status == RequestStatus.APPROVED:
            req = self._get_request(request_id)
            if req:
                self._finalize_tourism_registration(req)
                self._generate_decision_document(req, officer_name=changed_by, reason=reason)

        return result

    def _generate_decision_document(
        self,
        request: DmsRequest,
        officer_name: str,
        reason: Optional[str],
    ) -> None:
        """Pri odobravanju zahtjeva generiši potpisano rješenje i zabilježi hash."""
        try:
            request_type_label = request.request_type.value.replace("_", " ").title()
            decision_lines = [
                f"Zahtjev za uslugu '{request_type_label}' je odobren.",
            ]
            if reason:
                decision_lines.append(f"Obrazloženje: {reason}")
            if request.request_type in _TOURISM_TYPES:
                decision_lines.append(
                    "Korisnik je ovlašćen da otpočne sa pružanjem usluge u skladu sa registracijom."
                )
            else:
                decision_lines.append(
                    "Za preuzimanje fizičkog dokumenta očekujte instrukcije u sekciji 'Moji zahtjevi'."
                )

            file_path, sig_hash = generate_signed_decision(
                request_id=request.id,
                request_type=request_type_label,
                citizen_name=request.user_id,
                officer_name=officer_name or "Sistem",
                decision_text="\n".join(decision_lines),
            )

            request.signed_pdf_path = file_path
            request.signature_hash = sig_hash
            self.db.commit()
            self.logger.info(
                "Decision document generated request_id=%s path=%s hash=%s",
                request.id, file_path, sig_hash[:16],
            )
        except Exception:
            self.logger.exception("Generisanje rješenja neuspjelo request_id=%s", request.id)

    # ============= PLAĆANJE =============

    def mark_payment_paid(
        self,
        request_id: int,
        paid_by: str,
        reference: Optional[str] = None,
    ) -> Optional[str]:
        """Označi taksu kao plaćenu i vrati referencu uplate.

        Mock implementacija — u realnom sistemu bi se zvao payment gateway.
        """
        request = self._get_request(request_id)
        if not request:
            return None

        if request.payment_status == "not_required":
            return None

        if request.payment_status == "paid":
            return request.payment_reference

        ref = reference or f"PAY-{request_id}-{int(datetime.now().timestamp())}"
        request.payment_status = "paid"
        request.payment_reference = ref
        request.paid_at = datetime.now()
        request.updated_at = datetime.now()
        self.db.commit()

        self.add_comment(
            request_id=request_id,
            author=paid_by or request.user_id,
            content=f"Taksa plaćena. Referenca uplate: {ref}",
            author_type="user" if paid_by == request.user_id else "system",
            is_internal=False,
        )
        self.logger.info("Payment marked paid request_id=%s ref=%s by=%s", request_id, ref, paid_by)
        return ref

    def get_processing_fee(self, request_id: int) -> float:
        """Vraća taksu iz templata za zadati predmet."""
        request = self._get_request(request_id)
        if not request:
            return 0.0
        template = self.db.query(DocumentTemplate).filter(
            DocumentTemplate.request_type == request.request_type.value
        ).first()
        if not template:
            return 0.0
        return float(template.processing_fee_eur or 0)

    # ============= DOKUMENTA =============
    
    def upload_document(self, request_id: int, doc_name: str, file_path: str) -> bool:
        """Učitaj dokument na zahtjev"""
        request = self._get_request(request_id)
        if not request:
            return False
        
        current_docs = list(request.documents_metadata or [])
        current_docs.append({
            "name": doc_name,
            "file_path": file_path,
            "uploaded_at": datetime.now().isoformat(),
            "status": "pending_review"
        })
        request.documents_metadata = current_docs
        request.updated_at = datetime.now()
        
        self.db.commit()
        return True
    
    def get_required_documents(self, request_id: int) -> List[str]:
        """Vrni listu potrebnih dokumenata"""
        request = self._get_request(request_id)
        return request.required_documents if request else []
    
    # ============= KOMENTARI =============
    
    def add_comment(
        self,
        request_id: int,
        author: str,
        content: str,
        author_type: str = "user",
        is_internal: bool = False
    ) -> Optional[RequestComment]:
        """Dodaj komentar na zahtjev"""
        comment = RequestComment(
            request_id=request_id,
            author=author,
            content=content,
            author_type=author_type,
            is_internal=is_internal
        )
        
        self.db.add(comment)
        self.db.commit()
        
        return comment
    
    def get_visible_comments(self, request_id: int, for_user: bool = False) -> List[RequestComment]:
        """Vrni vidljive komentare"""
        query = self.db.query(RequestComment).filter(
            RequestComment.request_id == request_id
        )
        
        if for_user:
            query = query.filter(RequestComment.is_internal == False)
        
        return query.order_by(RequestComment.created_at).all()
    
    # ============= TURIZAM =============
    
    def register_tourism_property(
        self,
        request_id: int,
        owner_id: str,
        property_type: str,
        address: str,
        city: str,
        capacity: int,
        rooms: int,
        amenities: List[str] = None,
        coordinates: Dict = None
    ) -> Optional[TourismProperty]:
        """Registruj turističku nekretninu sa zahtjevom"""
        
        property = TourismProperty(
            property_type=property_type,
            owner_id=owner_id,
            address=address,
            city=city,
            capacity=capacity,
            rooms=rooms,
            amenities=amenities or [],
            coordinates=coordinates,
            registration_request_id=request_id
        )
        
        self.db.add(property)
        self.db.commit()

        return property

    def _finalize_tourism_registration(self, request: DmsRequest) -> None:
        """Kreira TourismProperty zapis tek kad je turistički zahtjev odobren (ne na submit)."""
        if request.request_type not in _TOURISM_TYPES:
            return

        details = request.details or {}
        address = details.get("property_address", "")
        city = details.get("property_city", "")
        if not address or not city:
            self.logger.warning(
                "Turizam finalizacija preskočena — nedostaje adresa/grad, request_id=%s", request.id
            )
            return

        existing = self.db.query(TourismProperty).filter_by(
            registration_request_id=request.id
        ).first()
        if existing:
            return

        self.register_tourism_property(
            request_id=request.id,
            owner_id=request.user_id,
            property_type="turizam_objekat",
            address=address,
            city=city,
            capacity=int(details.get("capacity", 1)),
            rooms=int(details.get("rooms", 1)),
            amenities=[],
        )
        self.logger.info("Turizam nekretnina finalizovana za request_id=%s", request.id)

    # ============= PRETRAGA I FILTRIRANJE =============
    
    def get_user_requests(self, user_id: str) -> List[DmsRequest]:
        """Sve zahtjeve korisnika"""
        return self.db.query(DmsRequest).filter(
            DmsRequest.user_id == user_id
        ).order_by(DmsRequest.created_at.desc()).all()
    
    def get_active_requests(self) -> List[DmsRequest]:
        """Sve aktivne zahtjeve (nisu završeni)"""
        active_statuses = [
            RequestStatus.SUBMITTED,
            RequestStatus.UNDER_REVIEW,
            RequestStatus.PENDING_USER
        ]
        
        return self.db.query(DmsRequest).filter(
            DmsRequest.status.in_(active_statuses)
        ).order_by(DmsRequest.priority, DmsRequest.created_at).all()

    def get_priority_inbox(self, pending_user_days: int = 3) -> List[Dict]:
        """Vraća aktivne predmete sa prioritetnim oznakama za admin inbox."""
        now = datetime.now()
        inbox = []

        for req in self.get_active_requests():
            flags = []
            score = 0

            if req.estimated_completion and req.estimated_completion < now:
                late_days = (now - req.estimated_completion).days
                flags.append(f"Kasni {late_days} dana")
                score += 3 + min(4, max(0, late_days))

            if req.status == RequestStatus.PENDING_USER and req.updated_at:
                wait_days = (now - req.updated_at).days
                if wait_days >= pending_user_days:
                    flags.append(f"Čeka korisnika {wait_days} dana")
                    score += 3

            if req.status == RequestStatus.SUBMITTED and not req.assigned_to:
                flags.append("Bez dodijeljenog službenika")
                score += 2

            priority_weight = {
                RequestPriority.URGENT: 3,
                RequestPriority.HIGH: 2,
                RequestPriority.MEDIUM: 1,
                RequestPriority.LOW: 0,
            }.get(req.priority, 0)
            score += priority_weight

            inbox.append(
                {
                    "request": req,
                    "score": score,
                    "flags": flags,
                    "is_overdue": any("Kasni" in flag for flag in flags),
                    "is_pending_user_long": any("Čeka korisnika" in flag for flag in flags),
                    "is_unassigned": "Bez dodijeljenog službenika" in flags,
                }
            )

        inbox.sort(key=lambda item: (item["score"], item["request"].created_at), reverse=True)
        return inbox

    def bulk_manage_requests(
        self,
        request_ids: List[int],
        changed_by: str,
        assign_to: Optional[str] = None,
        new_priority: Optional[RequestPriority] = None,
        new_status: Optional[RequestStatus] = None,
        reason: Optional[str] = None,
    ) -> Dict:
        """Primijeni grupne izmjene nad više predmeta."""
        if not changed_by:
            raise ValueError("Bulk izmjene zahtijevaju korisnicki identitet.")

        updated = 0
        transitioned = 0
        failed: List[Dict] = []
        touched = False

        for request_id in request_ids:
            request = self._get_request(int(request_id))
            if not request:
                failed.append({"request_id": request_id, "error": "Zahtjev nije pronađen."})
                continue

            try:
                if assign_to is not None:
                    normalized_assignee = assign_to.strip() if assign_to else None
                    if request.assigned_to != normalized_assignee:
                        request.assigned_to = normalized_assignee
                        touched = True

                if new_priority and request.priority != new_priority:
                    request.priority = new_priority
                    request.updated_at = datetime.now()
                    touched = True

                if new_status and request.status != new_status:
                    transition_reason = reason.strip() if reason else "Bulk status promjena"
                    self.transition_request(
                        request_id=request.id,
                        new_status=new_status,
                        changed_by=changed_by,
                        actor_role="admin",
                        reason=transition_reason,
                    )
                    transitioned += 1

                    if new_status == RequestStatus.PENDING_USER and transition_reason:
                        self.add_comment(
                            request.id,
                            author=changed_by,
                            content=transition_reason,
                            author_type="admin",
                            is_internal=False,
                        )
                    if new_status == RequestStatus.REJECTED and transition_reason:
                        request.rejection_reason = transition_reason
                        touched = True

                updated += 1
            except Exception as exc:
                failed.append({"request_id": request_id, "error": str(exc)})

        if touched:
            self.db.commit()

        return {
            "processed": len(request_ids),
            "updated": updated,
            "transitioned": transitioned,
            "failed": failed,
        }

    def get_officer_workload(self, days: int = 30) -> List[Dict]:
        """Vraća workload metrike po službeniku."""
        since = datetime.now() - timedelta(days=max(1, days))
        active_statuses = {
            RequestStatus.SUBMITTED,
            RequestStatus.UNDER_REVIEW,
            RequestStatus.PENDING_USER,
        }

        requests = self.db.query(DmsRequest).filter(DmsRequest.assigned_to != None).all()
        officers: Dict[str, Dict] = {}

        for req in requests:
            officer = (req.assigned_to or "").strip()
            if not officer:
                continue

            bucket = officers.setdefault(
                officer,
                {
                    "officer": officer,
                    "active": 0,
                    "overdue_active": 0,
                    "completed_last_30_days": 0,
                    "first_review_samples": 0,
                    "avg_first_review_hours": 0.0,
                },
            )

            if req.status in active_statuses:
                bucket["active"] += 1
                if req.estimated_completion and req.estimated_completion < datetime.now():
                    bucket["overdue_active"] += 1

            if req.status == RequestStatus.COMPLETED and req.completed_at and req.completed_at >= since:
                bucket["completed_last_30_days"] += 1

            first_review = (
                self.db.query(RequestStatusHistory)
                .filter(
                    RequestStatusHistory.request_id == req.id,
                    RequestStatusHistory.to_status == RequestStatus.UNDER_REVIEW,
                    RequestStatusHistory.changed_by == officer,
                )
                .order_by(RequestStatusHistory.changed_at.asc())
                .first()
            )
            if first_review and req.submitted_at and first_review.changed_at:
                delta_hours = (first_review.changed_at - req.submitted_at).total_seconds() / 3600
                if delta_hours >= 0:
                    sample_count = bucket["first_review_samples"]
                    avg = bucket["avg_first_review_hours"]
                    bucket["avg_first_review_hours"] = round(
                        ((avg * sample_count) + delta_hours) / (sample_count + 1),
                        2,
                    )
                    bucket["first_review_samples"] += 1

        result = sorted(officers.values(), key=lambda row: (row["active"], row["overdue_active"]), reverse=True)
        for row in result:
            row.pop("first_review_samples", None)
        return result
    
    def get_overdue_requests(self) -> List[DmsRequest]:
        """Zahtjevi koji su prošli rok"""
        return self.db.query(DmsRequest).filter(
            DmsRequest.estimated_completion < datetime.now(),
            DmsRequest.status != RequestStatus.COMPLETED,
            DmsRequest.status != RequestStatus.REJECTED
        ).all()
    
    def get_requests_by_type(self, request_type: RequestType) -> List[DmsRequest]:
        """Zahtjevi određenog tipa"""
        return self.db.query(DmsRequest).filter(
            DmsRequest.request_type == request_type
        ).all()
    
    def get_assigned_requests(self, worker_id: str) -> List[DmsRequest]:
        """Zahtjevi dodijeljeni radniku"""
        return self.db.query(DmsRequest).filter(
            DmsRequest.assigned_to == worker_id
        ).order_by(
            DmsRequest.priority,
            DmsRequest.estimated_completion
        ).all()
    
    # ============= STATISTIKA =============
    
    def get_statistics(self) -> Dict:
        """Osnovne statistike DMS-a"""
        total = self.db.query(DmsRequest).count()
        
        statuses = {}
        for status in RequestStatus:
            count = self.db.query(DmsRequest).filter(
                DmsRequest.status == status
            ).count()
            statuses[status.value] = count
        
        avg_completion_time = self._calculate_avg_completion_time()
        
        return {
            "total_requests": total,
            "by_status": statuses,
            "avg_completion_days": avg_completion_time,
            "overdue_count": len(self.get_overdue_requests())
        }

    def get_kpi_metrics(self) -> Dict:
        """KPI metrike za dashboard i odbranu projekta."""
        total = self.db.query(DmsRequest).count()
        submitted = self.db.query(DmsRequest).filter(DmsRequest.submitted_at != None).count()
        completed = self.db.query(DmsRequest).filter(DmsRequest.status == RequestStatus.COMPLETED).count()

        pending_user_ids = {
            req_id
            for (req_id,) in self.db.query(RequestStatusHistory.request_id)
            .filter(RequestStatusHistory.to_status == RequestStatus.PENDING_USER)
            .distinct()
            .all()
        }

        first_review_hours = []
        submitted_requests = self.db.query(DmsRequest).filter(DmsRequest.submitted_at != None).all()
        for req in submitted_requests:
            first_review = (
                self.db.query(RequestStatusHistory)
                .filter(
                    RequestStatusHistory.request_id == req.id,
                    RequestStatusHistory.to_status == RequestStatus.UNDER_REVIEW,
                )
                .order_by(RequestStatusHistory.changed_at.asc())
                .first()
            )
            if first_review and req.submitted_at and first_review.changed_at:
                delta_hours = (first_review.changed_at - req.submitted_at).total_seconds() / 3600
                first_review_hours.append(max(delta_hours, 0))

        avg_first_review_hours = (
            round(sum(first_review_hours) / len(first_review_hours), 2) if first_review_hours else 0
        )

        completion_rate = round((completed / submitted) * 100, 2) if submitted else 0
        correction_rate = round((len(pending_user_ids) / submitted) * 100, 2) if submitted else 0

        return {
            "total_requests": total,
            "submitted_requests": submitted,
            "completed_requests": completed,
            "overdue_requests": len(self.get_overdue_requests()),
            "avg_first_review_hours": avg_first_review_hours,
            "completion_rate_percent": completion_rate,
            "correction_rate_percent": correction_rate,
        }

    def get_weekly_trends(self, weeks: int = 8) -> List[Dict]:
        """Vraća nedeljne KPI trendove za broj kreiranih, podnesenih i završenih zahtjeva."""
        weeks = max(2, min(weeks, 26))
        now = datetime.now()
        current_week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        first_week_start = current_week_start - timedelta(days=7 * (weeks - 1))

        buckets = {}
        for idx in range(weeks):
            week_start = first_week_start + timedelta(days=7 * idx)
            label = week_start.strftime("%Y-%m-%d")
            buckets[label] = {
                "week": label,
                "created": 0,
                "submitted": 0,
                "completed": 0,
            }

        requests = self.db.query(DmsRequest).filter(DmsRequest.created_at >= first_week_start).all()

        def add(metric_name: str, timestamp):
            if not timestamp or timestamp < first_week_start:
                return
            week_start = (timestamp - timedelta(days=timestamp.weekday())).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            label = week_start.strftime("%Y-%m-%d")
            if label in buckets:
                buckets[label][metric_name] += 1

        for req in requests:
            add("created", req.created_at)
            add("submitted", req.submitted_at)
            add("completed", req.completed_at)

        return [buckets[key] for key in sorted(buckets.keys())]

    def apply_sla_escalation(self) -> Dict:
        """Eskalira prioritet aktivnih predmeta koji kasne preko procijenjenog roka."""
        now = datetime.now()
        active_statuses = [
            RequestStatus.SUBMITTED,
            RequestStatus.UNDER_REVIEW,
            RequestStatus.PENDING_USER,
        ]
        active = self.db.query(DmsRequest).filter(DmsRequest.status.in_(active_statuses)).all()

        escalated = 0
        checked = 0
        for req in active:
            if not req.estimated_completion or req.estimated_completion >= now:
                continue

            checked += 1
            late_days = (now - req.estimated_completion).days
            target_priority = RequestPriority.URGENT if late_days >= 7 else RequestPriority.HIGH

            if req.priority != target_priority:
                req.priority = target_priority
                req.updated_at = now
                escalated += 1

        if escalated:
            self.db.commit()

        return {
            "checked_overdue": checked,
            "escalated": escalated,
        }

    def verify_audit_chain(self, request_id: int) -> Dict:
        """Provjeri integritet hash chain-a u istoriji statusa.

        Vraća dict sa poljima:
          - valid (bool)        : True ako su svi hashevi konzistentni
          - total_entries (int) : ukupno redova provjereno
          - broken_at (int|None): id reda gdje je lanac prekinut, ako postoji
          - legacy (bool)       : True ako su svi unosi pre-hash-chain (bez hash polja)
        """
        history = (
            self.db.query(RequestStatusHistory)
            .filter(RequestStatusHistory.request_id == request_id)
            .order_by(RequestStatusHistory.id.asc())
            .all()
        )

        if not history:
            return {"valid": True, "total_entries": 0, "broken_at": None, "legacy": False}

        # Ako nijedan unos nema hash, predmet je iz ere prije hash chain-a
        if all(row.entry_hash is None for row in history):
            return {"valid": True, "total_entries": len(history), "broken_at": None, "legacy": True}

        expected_prev = ""
        for row in history:
            # Preskoči legacy redove koji nemaju entry_hash, ali ne resetuj lanac
            if row.entry_hash is None:
                continue

            stored_prev = row.prev_hash or ""
            if stored_prev != expected_prev:
                return {
                    "valid": False, "total_entries": len(history),
                    "broken_at": row.id, "legacy": False,
                }

            recomputed = _hash_history_entry(
                prev_hash=stored_prev,
                request_id=row.request_id,
                from_status=row.from_status.value if row.from_status else "",
                to_status=row.to_status.value if row.to_status else "",
                changed_by=row.changed_by or "",
                changed_at=row.changed_at.isoformat() if row.changed_at else "",
                reason=row.reason or "",
            )
            if recomputed != row.entry_hash:
                return {
                    "valid": False, "total_entries": len(history),
                    "broken_at": row.id, "legacy": False,
                }

            expected_prev = row.entry_hash

        return {"valid": True, "total_entries": len(history), "broken_at": None, "legacy": False}

    def build_audit_pack(self, request_id: int) -> Dict:
        """Generiše audit izvještaj za pojedinačni predmet."""
        request = self._get_request(request_id)
        if not request:
            raise ValueError("Zahtjev nije pronađen.")

        history_rows = (
            self.db.query(RequestStatusHistory)
            .filter(RequestStatusHistory.request_id == request_id)
            .order_by(RequestStatusHistory.changed_at.asc())
            .all()
        )
        comment_rows = (
            self.db.query(RequestComment)
            .filter(RequestComment.request_id == request_id)
            .order_by(RequestComment.created_at.asc())
            .all()
        )

        return {
            "generated_at": datetime.now().isoformat(),
            "request": {
                "id": request.id,
                "request_type": request.request_type.value if request.request_type else None,
                "status": request.status.value if request.status else None,
                "priority": request.priority.value if request.priority else None,
                "user_id": request.user_id,
                "user_email": request.user_email,
                "user_city": request.user_city,
                "description": request.description,
                "reason": request.reason,
                "details": request.details or {},
                "documents_metadata": request.documents_metadata or [],
                "required_documents": request.required_documents or [],
                "created_at": request.created_at.isoformat() if request.created_at else None,
                "submitted_at": request.submitted_at.isoformat() if request.submitted_at else None,
                "updated_at": request.updated_at.isoformat() if request.updated_at else None,
                "completed_at": request.completed_at.isoformat() if request.completed_at else None,
                "assigned_to": request.assigned_to,
                "notes": request.notes,
                "rejection_reason": request.rejection_reason,
            },
            "status_history": [
                {
                    "from_status": row.from_status.value if row.from_status else None,
                    "to_status": row.to_status.value if row.to_status else None,
                    "changed_by": row.changed_by,
                    "changed_at": row.changed_at.isoformat() if row.changed_at else None,
                    "reason": row.reason,
                    "prev_hash": row.prev_hash,
                    "entry_hash": row.entry_hash,
                }
                for row in history_rows
            ],
            "audit_chain": self.verify_audit_chain(request_id),
            "comments": [
                {
                    "author": row.author,
                    "author_type": row.author_type,
                    "is_internal": row.is_internal,
                    "content": row.content,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in comment_rows
            ],
        }
    
    def _estimate_days_for_type(
        self,
        request_type: RequestType,
        fallback: int,
        sample_size: int = 20,
    ) -> int:
        """Procijeni dane na osnovu prosjeka zadnjih završenih predmeta istog tipa.

        Vraća prosjek ako postoji najmanje 3 uzorka; inače fallback na template.
        """
        completed = (
            self.db.query(DmsRequest)
            .filter(
                DmsRequest.request_type == request_type,
                DmsRequest.status == RequestStatus.COMPLETED,
                DmsRequest.completed_at != None,
                DmsRequest.submitted_at != None,
            )
            .order_by(DmsRequest.completed_at.desc())
            .limit(sample_size)
            .all()
        )

        if len(completed) < 3:
            return max(1, int(fallback))

        deltas = [(req.completed_at - req.submitted_at).days for req in completed]
        deltas = [d for d in deltas if d >= 0]
        if not deltas:
            return max(1, int(fallback))

        avg = sum(deltas) / len(deltas)
        # Dodaj 20% margine kao buffer, zaokruži na cijeli dan
        estimated = max(1, int(round(avg * 1.2)))
        return estimated

    def _calculate_avg_completion_time(self) -> float:
        """Prosječne dane za završetak zahtjeva"""
        completed = self.db.query(DmsRequest).filter(
            DmsRequest.status == RequestStatus.COMPLETED,
            DmsRequest.completed_at != None,
            DmsRequest.submitted_at != None
        ).all()
        
        if not completed:
            return 0
        
        total_days = 0
        for req in completed:
            delta = (req.completed_at - req.submitted_at).days
            total_days += delta
        
        return total_days / len(completed)
