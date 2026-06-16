from datetime import datetime
import logging
from pathlib import Path
import re
from typing import Optional

import streamlit as st

from database.database import save_request_submission
from dms_core import DmsManager, RequestStatus, RequestType
from dms_core.models import DocumentTemplate, SessionLocal
from municipality_utils import get_all_municipalities, validate_municipality


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS_DIR = PROJECT_ROOT / "documents"
DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("dms_portal.requests")

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


SEMI_DIGITAL_TYPES = {
    RequestType.LICNA_KARTA,
    RequestType.PASOS,
    RequestType.VOZACKA_DOZVOLA,
}


def _sanitize_filename(name: str) -> str:
    cleaned = Path(name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    return cleaned[:120] or "document.bin"


def _is_allowed_signature(file_name: str, content: bytes) -> bool:
    ext = Path(file_name).suffix.lower()
    if ext == ".pdf":
        return content.startswith(b"%PDF")
    if ext in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if ext == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    return False


def _validate_upload(uploaded_file) -> Optional[str]:
    ext = Path(uploaded_file.name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"Datoteka '{uploaded_file.name}' nije dozvoljenog tipa."

    size = int(uploaded_file.size or 0)
    if size <= 0:
        return f"Datoteka '{uploaded_file.name}' je prazna."
    if size > MAX_UPLOAD_BYTES:
        return f"Datoteka '{uploaded_file.name}' prelazi limit od 5 MB."

    content = bytes(uploaded_file.getbuffer())
    if not _is_allowed_signature(uploaded_file.name, content):
        return f"Datoteka '{uploaded_file.name}' nema validan format sadrzaja."
    return None


def render_status_stepper(status: RequestStatus, assigned_to: str = None, forward_count: int = 0) -> None:
    """4-korački HTML/CSS vizuelni tracker napretka predmeta."""
    if status == RequestStatus.DRAFT:
        return

    _STEPS = [
        (0, "Podnesen"),
        (1, "U obradi"),
        (2, "Odluka"),
        (3, "Završen"),
    ]

    _STATUS_TO_STEP = {
        RequestStatus.SUBMITTED:     0,
        RequestStatus.UNDER_REVIEW:  1,
        RequestStatus.PENDING_USER:  1,
        RequestStatus.APPROVED:      2,
        RequestStatus.REJECTED:      2,
        RequestStatus.COMPLETED:     3,
    }

    is_rejected = status == RequestStatus.REJECTED
    is_pending  = status == RequestStatus.PENDING_USER
    cur_step    = _STATUS_TO_STEP.get(status, -1)

    parts = []
    for i, (step_idx, step_label) in enumerate(_STEPS):
        if step_idx == 2:
            if is_rejected:
                step_label = "Odbijen"
            elif cur_step >= 2:
                step_label = "Odobren"

        if cur_step > step_idx:
            cls, dot = "done", "✓"
        elif cur_step == step_idx:
            if is_rejected:
                cls, dot = "rejected", "✗"
            elif is_pending:
                cls, dot = "pending", str(i + 1)
            else:
                cls, dot = "active", str(i + 1)
        else:
            cls, dot = "future", str(i + 1)

        parts.append(
            f'<div class="dms-step">'
            f'<div class="dms-dot {cls}">{dot}</div>'
            f'<div class="dms-lbl {cls}">{step_label}</div>'
            f'</div>'
        )
        if i < len(_STEPS) - 1:
            conn = "done" if cls == "done" else ""
            parts.append(f'<div class="dms-conn {conn}"></div>')

    extras = ""
    if is_pending:
        extras += '<div class="dms-sub">⏳ Čeka vašu dopunu dokumentacije</div>'
    if assigned_to:
        extras += f'<div class="dms-sub">👤 Obrađuje: <b>{assigned_to}</b></div>'
    if forward_count and forward_count > 0:
        extras += f'<div class="dms-sub">↪ Predmet proslijeđen {forward_count}× — u obradi</div>'

    html = (
        "<style>"
        ".dms-stepper{display:flex;align-items:flex-start;padding:12px 0 2px;gap:0}"
        ".dms-step{display:flex;flex-direction:column;align-items:center;min-width:80px}"
        ".dms-dot{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;"
        "justify-content:center;font-weight:700;font-size:.82rem;"
        "border:2px solid #dee2e6;background:#f8f9fa;color:#adb5bd}"
        ".dms-dot.done{background:#198754;border-color:#198754;color:#fff}"
        ".dms-dot.active{background:#0d6efd;border-color:#0d6efd;color:#fff;"
        "box-shadow:0 0 0 3px rgba(13,110,253,.2)}"
        ".dms-dot.pending{background:#6f42c1;border-color:#6f42c1;color:#fff;"
        "box-shadow:0 0 0 3px rgba(111,66,193,.2)}"
        ".dms-dot.rejected{background:#dc3545;border-color:#dc3545;color:#fff;"
        "box-shadow:0 0 0 3px rgba(220,53,69,.2)}"
        ".dms-lbl{margin-top:5px;font-size:.7rem;text-align:center;"
        "color:#adb5bd;max-width:80px;line-height:1.2}"
        ".dms-lbl.done{color:#198754;font-weight:500}"
        ".dms-lbl.active{color:#0d6efd;font-weight:700}"
        ".dms-lbl.pending{color:#6f42c1;font-weight:700}"
        ".dms-lbl.rejected{color:#dc3545;font-weight:700}"
        ".dms-conn{flex:1;height:2px;background:#dee2e6;margin-top:15px;min-width:16px}"
        ".dms-conn.done{background:#198754}"
        ".dms-sub{font-size:.76rem;color:#6c757d;margin-top:4px}"
        "</style>"
        '<div class="dms-stepper">'
        + "".join(parts)
        + "</div>"
        + extras
    )
    st.markdown(html, unsafe_allow_html=True)


def _service_catalog() -> dict:
    return {
        "MUP (polu-digitalno)": [
            {
                "label": "Lična karta",
                "type": RequestType.LICNA_KARTA,
                "note": "Biometrija i preuzimanje dokumenta se obavljaju fizički.",
            },
            {
                "label": "Pasoš",
                "type": RequestType.PASOS,
                "note": "Biometrija i preuzimanje dokumenta se obavljaju fizički.",
            },
            {
                "label": "Vozačka dozvola",
                "type": RequestType.VOZACKA_DOZVOLA,
                "note": "Preuzimanje dozvole je fizičko u MUP centru.",
            },
        ],
        "Turizam (potpuno digitalno)": [
            {
                "label": "Registracija nekretnine",
                "type": RequestType.TURIZAM_REGISTRACIJA,
                "note": "Kompletan proces je digitalan bez obaveznog dolaska.",
            },
            {
                "label": "Turistička licenca",
                "type": RequestType.TURIZAM_LICENCA,
                "note": "Kompletan proces je digitalan bez obaveznog dolaska.",
            },
            {
                "label": "Dozvola za adaptaciju",
                "type": RequestType.TURIZAM_DOZVOLA_GRADNJE,
                "note": "Kompletan proces je digitalan bez obaveznog dolaska.",
            },
        ],
    }


def _format_status(status: RequestStatus) -> str:
    labels = {
        RequestStatus.DRAFT: "Nacrt",
        RequestStatus.SUBMITTED: "Podnesen",
        RequestStatus.UNDER_REVIEW: "U obradi",
        RequestStatus.PENDING_USER: "Čeka korisnika",
        RequestStatus.APPROVED: "Odobren",
        RequestStatus.REJECTED: "Odbijen",
        RequestStatus.COMPLETED: "Završen",
    }
    return labels.get(status, status.value)


def _status_badge(status: RequestStatus) -> str:
    palette = {
        RequestStatus.DRAFT:        ("#6c757d", "#fff"),
        RequestStatus.SUBMITTED:    ("#0d6efd", "#fff"),
        RequestStatus.UNDER_REVIEW: ("#fd7e14", "#fff"),
        RequestStatus.PENDING_USER: ("#6f42c1", "#fff"),
        RequestStatus.APPROVED:     ("#198754", "#fff"),
        RequestStatus.REJECTED:     ("#dc3545", "#fff"),
        RequestStatus.COMPLETED:    ("#0a7275", "#fff"),
    }
    label = _format_status(status)
    bg, fg = palette.get(status, ("#6c757d", "#fff"))
    return (
        f'<span class="status-badge" '
        f'style="background:{bg};color:{fg};">{label}</span>'
    )


def _render_template_info(db, request_type: RequestType) -> Optional[DocumentTemplate]:
    template = db.query(DocumentTemplate).filter_by(request_type=request_type.value).first()
    if not template:
        st.warning("Nisu pronađeni šabloni dokumenata za ovu uslugu.")
        return None

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Procijenjeni rok", f"{template.estimated_days} dana")
    with c2:
        fee_text = f"{template.processing_fee_eur:.2f} EUR" if template.processing_fee_eur else "Bez takse"
        st.metric("Taksa", fee_text)
    with c3:
        st.metric("Broj dokumenata", len(template.required_documents or []))

    st.markdown("#### Potrebna dokumenta")
    for idx, doc in enumerate(template.required_documents or [], start=1):
        doc_name = doc.get("naziv", f"Dokument {idx}") if isinstance(doc, dict) else str(doc)
        doc_desc = doc.get("opis", "") if isinstance(doc, dict) else ""
        st.markdown(f"{idx}. {doc_name}")
        if doc_desc:
            st.caption(doc_desc)

    if template.instructions:
        st.info(template.instructions)

    return template


def dms_request_page() -> None:
    st.title("Podnesi zahtjev")
    st.caption("Jedinstven portal za MUP i turističke usluge")

    if not st.session_state.get("user"):
        st.warning("Morate biti prijavljeni.")
        return

    db = SessionLocal()
    dms = DmsManager(db)

    try:
        catalog = _service_catalog()
        services = []
        for group_name, group_services in catalog.items():
            for service_item in group_services:
                item = dict(service_item)
                item["group"] = group_name
                services.append(item)

        prefill_request_type = st.session_state.get("prefill_request_type")
        selected_index = 0
        if prefill_request_type:
            for idx, service_item in enumerate(services):
                if service_item["type"].value == prefill_request_type:
                    selected_index = idx
                    break

        selected = st.selectbox(
            "Odaberite uslugu",
            options=services,
            format_func=lambda item: f"{item['label']} ({item['group']})",
            index=selected_index,
        )

        # Primijeni prefill samo jednom nakon otvaranja iz dashboard-a.
        st.session_state.prefill_group = None
        st.session_state.prefill_request_type = None

        request_type = selected["type"]
        is_semi_digital = request_type in SEMI_DIGITAL_TYPES

        auto_mode = "Polu-digitalno" if is_semi_digital else "Potpuno digitalno"
        st.caption(f"Automatski odabrani tok: {auto_mode}")

        if is_semi_digital:
            st.warning(selected["note"])
        else:
            st.success(selected["note"])

        template = _render_template_info(db, request_type)

        with st.form("submit_request_form"):
            st.markdown("### Podaci zahtjeva")
            reason = st.text_input("Razlog zahtjeva")
            description = st.text_area("Dodatni opis", height=120)

            details = {
                "service_group": "semi_digital" if is_semi_digital else "full_digital",
                "physical_presence_required": is_semi_digital,
                "requested_at": datetime.now().isoformat(),
            }

            if not is_semi_digital:
                st.markdown("### Podaci o nekretnini")
                property_name = st.text_input("Naziv objekta")
                property_address = st.text_input("Adresa objekta")
                property_city = st.selectbox("Opština objekta", get_all_municipalities())
                capacity = st.number_input("Kapacitet (broj osoba)", min_value=1, max_value=200, value=2)
                rooms = st.number_input("Broj soba", min_value=1, max_value=50, value=1)

                details.update(
                    {
                        "property_name": property_name,
                        "property_address": property_address,
                        "property_city": property_city,
                        "capacity": int(capacity),
                        "rooms": int(rooms),
                    }
                )

            uploaded_files = st.file_uploader(
                "Dodajte dokumenta",
                accept_multiple_files=True,
                type=["pdf", "jpg", "jpeg", "png"],
                help="Dozvoljeni su PDF/JPG/JPEG/PNG, maksimalno 5 MB po datoteci.",
            )

            confirm = st.checkbox("Potvrđujem da su podaci tačni.")
            submit = st.form_submit_button("Podnesi zahtjev", type="primary")

            if submit:
                validation_errors = []

                if not confirm:
                    validation_errors.append("Morate potvrditi tačnost podataka.")
                if len(reason.strip()) < 3:
                    validation_errors.append("Razlog zahtjeva mora imati najmanje 3 karaktera.")
                if not st.session_state.get("user_city"):
                    validation_errors.append("Korisnik nema postavljenu opštinu prebivališta.")

                if not is_semi_digital:
                    if not details.get("property_name", "").strip():
                        validation_errors.append("Naziv objekta je obavezan za digitalne turisticke usluge.")
                    if not details.get("property_address", "").strip():
                        validation_errors.append("Adresa objekta je obavezna za digitalne turisticke usluge.")
                    if not validate_municipality(details.get("property_city", "")):
                        validation_errors.append("Opština objekta nije validna.")

                for file in uploaded_files or []:
                    error = _validate_upload(file)
                    if error:
                        validation_errors.append(error)

                if validation_errors:
                    for error in validation_errors:
                        st.error(error)
                    return

                try:
                    request = dms.create_request(
                        request_type=request_type,
                        user_id=st.session_state.user,
                        user_email=st.session_state.get("user_email") or "",
                        user_city=st.session_state.user_city,
                        details=details,
                        description=description,
                        reason=reason,
                    )
                    dms.submit_request(request.id, changed_by=st.session_state.user)

                    request_dir = DOCUMENTS_DIR / str(request.id)
                    request_dir.mkdir(exist_ok=True)

                    for file in uploaded_files or []:
                        safe_name = _sanitize_filename(file.name)
                        file_path = request_dir / safe_name
                        with open(file_path, "wb") as out:
                            out.write(file.getbuffer())
                        dms.upload_document(request.id, doc_name=safe_name, file_path=str(file_path))

                    dms.add_comment(
                        request_id=request.id,
                        author=st.session_state.user,
                        content="Zahtjev je podnesen preko građanskog portala.",
                        author_type="user",
                    )

                    save_request_submission(
                        username=st.session_state.user,
                        request_id=request.id,
                        request_type=request.request_type.value,
                        status=request.status.value,
                    )

                    st.success(f"Zahtjev #{request.id} je uspješno podnesen i automatski dodijeljen službeniku na obradu.")
                    user_email = st.session_state.get("user_email") or ""
                    if user_email:
                        st.info(f"📧 Potvrda zahtjeva poslana na: {user_email}")
                    st.info("Status i komunikaciju pratite u sekciji 'Moji zahtjevi'.")
                    st.session_state.current_view = "requests"
                    st.rerun()
                except Exception:
                    logger.exception("Request submission failed for user=%s", st.session_state.user)
                    st.error("Doslo je do greske pri podnosenju zahtjeva. Pokusajte ponovo.")

    finally:
        db.close()


def _render_payment_widget(request, dms) -> None:
    """Mock plaćanje takse — prikazuje se samo ako je status 'pending'."""
    status = request.payment_status or ""
    if status == "not_required" or not status:
        return

    fee = dms.get_processing_fee(request.id)
    if status == "paid":
        ref = request.payment_reference or "-"
        paid_at = request.paid_at.strftime("%d.%m.%Y %H:%M") if request.paid_at else "-"
        st.success(f"Taksa plaćena ({fee:.2f} EUR). Referenca: {ref} • {paid_at}")
        return

    st.warning(f"Taksa nije plaćena. Iznos: {fee:.2f} EUR")
    if st.button("Plati taksu (demo)", key=f"pay_btn_{request.id}"):
        ref = dms.mark_payment_paid(request.id, paid_by=st.session_state.user)
        if ref:
            st.success(f"Uplata zabilježena. Referenca: {ref}")
            st.rerun()
        else:
            st.info("Plaćanje nije bilo potrebno za ovaj predmet.")


def _render_signed_decision(request) -> None:
    """Ako predmet ima generisano potpisano rješenje, ponudi preuzimanje."""
    if not request.signed_pdf_path:
        return

    path = Path(request.signed_pdf_path)
    if not path.exists():
        return

    suffix = path.suffix.lower()
    mime = "application/pdf" if suffix == ".pdf" else "text/html"
    file_label = "PDF rješenje" if suffix == ".pdf" else "HTML rješenje"

    with open(path, "rb") as fh:
        data = fh.read()

    sig_hash_short = (request.signature_hash or "")[:16]
    st.info(
        f"📜 Digitalno potpisano {file_label} dostupno. "
        f"Hash dokumenta: {sig_hash_short}…"
    )
    st.download_button(
        f"Preuzmi {file_label}",
        data=data,
        file_name=path.name,
        mime=mime,
        key=f"download_decision_{request.id}",
    )


def my_requests_page() -> None:
    st.title("Moji zahtjevi")

    if not st.session_state.get("user"):
        st.warning("Morate biti prijavljeni.")
        return

    db = SessionLocal()
    dms = DmsManager(db)

    try:
        requests = dms.get_user_requests(st.session_state.user)
        if not requests:
            st.info("Nemate podnesenih zahtjeva.")
            return

        selected_status = st.selectbox(
            "Filter po statusu",
            ["Svi"] + [status.value for status in RequestStatus],
            format_func=lambda value: "Svi" if value == "Svi" else _format_status(RequestStatus(value)),
        )

        if selected_status != "Svi":
            requests = [req for req in requests if req.status.value == selected_status]

        for request in requests:
            mode = request.details.get("service_group") if request.details else "semi_digital"
            mode_label = "Polu-digitalno" if mode == "semi_digital" else "Potpuno digitalno"

            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 2, 2])
                with c1:
                    st.markdown(f"### #{request.id} - {request.request_type.value.replace('_', ' ').title()}")
                    st.caption(f"Podnesen: {request.created_at.strftime('%d.%m.%Y %H:%M')}")
                with c2:
                    st.markdown(
                        f"Status: {_status_badge(request.status)}",
                        unsafe_allow_html=True,
                    )
                with c3:
                    st.write(f"Tip toka: {mode_label}")

                render_status_stepper(
                    request.status,
                    assigned_to=request.assigned_to,
                    forward_count=getattr(request, "forward_count", 0) or 0,
                )

                if request.details and request.details.get("physical_presence_required"):
                    st.warning("Za ovu uslugu je potreban fizički dolazak u završnoj fazi.")
                else:
                    st.success("Za ovu uslugu proces se vodi digitalno do kraja.")

                _render_payment_widget(request, dms)
                _render_signed_decision(request)

                with st.expander("Detalji zahtjeva"):
                    st.write(f"Razlog: {request.reason or '-'}")
                    st.write(f"Opis: {request.description or '-'}")

                    st.markdown("#### Dokumenta")
                    if request.documents_metadata:
                        for doc in request.documents_metadata:
                            uploaded_at = doc.get("uploaded_at", "")[:16].replace("T", " ")
                            st.write(f"- {doc.get('name')} ({doc.get('status', 'pending')}) {uploaded_at}")
                    else:
                        st.caption("Nema uploadovanih dokumenata.")

                    st.markdown("#### Komentari")
                    for comment in dms.get_visible_comments(request.id, for_user=True):
                        created = comment.created_at.strftime("%d.%m.%Y %H:%M") if comment.created_at else ""
                        st.write(f"{comment.author} ({created}): {comment.content}")

                    if request.status not in [RequestStatus.COMPLETED, RequestStatus.REJECTED]:
                        reply = st.text_area("Dodajte komentar", key=f"comment_{request.id}")
                        if st.button("Pošalji komentar", key=f"send_{request.id}"):
                            if reply.strip():
                                dms.add_comment(
                                    request.id,
                                    author=st.session_state.user,
                                    content=reply.strip(),
                                    author_type="user",
                                )
                                st.success("Komentar je sačuvan.")
                                st.rerun()

                    if request.status == RequestStatus.PENDING_USER:
                        if st.button("Ponovo podnesi zahtjev", key=f"resubmit_{request.id}"):
                            try:
                                dms.transition_request(
                                    request.id,
                                    RequestStatus.SUBMITTED,
                                    changed_by=st.session_state.user,
                                    actor_role="user",
                                    reason="Korisnik je dopunio podatke i ponovo podnio zahtjev.",
                                )
                                st.success("Zahtjev je ponovo podnesen.")
                                st.rerun()
                            except Exception:
                                logger.exception("Resubmission failed request_id=%s", request.id)
                                st.error("Ponovno podnosenje nije uspjelo. Pokusajte ponovo.")

                    st.markdown("#### Audit trail")
                    if request.status_history:
                        for entry in sorted(request.status_history, key=lambda item: item.changed_at):
                            changed = entry.changed_at.strftime("%d.%m.%Y %H:%M") if entry.changed_at else ""
                            st.write(
                                f"- {changed}: {entry.from_status.value} -> {entry.to_status.value} ({entry.changed_by or 'sistem'})"
                            )
                    else:
                        st.caption("Nema zabilježenih promjena statusa.")

    finally:
        db.close()


if __name__ == "__main__":
    submit_tab, my_tab = st.tabs(["Podnesi zahtjev", "Moji zahtjevi"])
    with submit_tab:
        dms_request_page()
    with my_tab:
        my_requests_page()
