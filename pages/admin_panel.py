from datetime import datetime
import csv
import io
import json
import logging
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from database.database import validate_user_session
from database.database import get_staff_usernames
from dms_core import DmsManager, RequestStatus, RequestType, RequestPriority
from dms_core.models import DmsRequest, SessionLocal
from pages.dms_requests import render_status_stepper
from permissions import Role, get_effective_role, has_admin_access


logger = logging.getLogger("dms_portal.admin")
BASE_DIR = Path(__file__).resolve().parent.parent
_MAX_FORWARDS = 2


def _get_all_staff() -> list:
    """Staff iz DB + env fallback kad je baza prazna (demo)."""
    staff = get_staff_usernames()
    if not staff:
        env_raw = os.getenv("APP_ADMIN_USERS", "admin,rapoz")
        staff = [u.strip() for u in env_raw.split(",") if u.strip()]
    return staff


def _render_forward_section(request, dms: DmsManager) -> None:
    """UI sekcija za proslijeđivanje zahtjeva drugom službeniku."""
    active_for_forward = {RequestStatus.UNDER_REVIEW, RequestStatus.SUBMITTED, RequestStatus.PENDING_USER}
    if request.status not in active_for_forward:
        return

    forward_count = int(getattr(request, "forward_count", 0) or 0)
    label = f"Proslijedi zahtjev  ({forward_count}/{_MAX_FORWARDS})"

    with st.expander(label, expanded=False):
        if forward_count >= _MAX_FORWARDS:
            st.warning(
                f"Zahtjev je već proslijeđen {_MAX_FORWARDS} puta. "
                "Morate ga sami obraditi."
            )
            return

        all_staff = _get_all_staff()
        candidates = [s for s in all_staff if s != (request.assigned_to or "")]
        if not candidates:
            candidates = all_staff
        if not candidates:
            st.info("Nema dostupnih službenika za proslijeđivanje.")
            return

        to_user = st.selectbox(
            "Proslijedi službeniku",
            options=candidates,
            key=f"forward_to_{request.id}",
        )
        forward_reason = st.text_input(
            "Razlog proslijeđivanja (obavezno)",
            placeholder="npr. Godišnji odmor, nije u nadležnosti, preopterećenost...",
            key=f"forward_reason_{request.id}",
        )

        if st.button("Potvrdi proslijeđivanje", key=f"forward_submit_{request.id}"):
            if not forward_reason.strip():
                st.error("Razlog proslijeđivanja je obavezan.")
                return
            try:
                dms.forward_request(
                    request_id=request.id,
                    from_user=st.session_state.user,
                    to_user=to_user,
                    reason=forward_reason.strip(),
                )
                st.success(f"Zahtjev proslijeđen službeniku **{to_user}**.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
            except Exception:
                logger.exception("Forward failed request_id=%s", request.id)
                st.error("Greška pri proslijeđivanju. Pokušajte ponovo.")


SESSION_FILE = BASE_DIR / "data" / "session.json"


def _restore_admin_session_if_possible() -> None:
    if st.session_state.get("user") and st.session_state.get("is_admin"):
        return
    if not SESSION_FILE.exists():
        return

    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        username = data.get("user")
        token = data.get("token")
        if not username or not token:
            return
        if not validate_user_session(username, token):
            return

        st.session_state.user = username
        st.session_state.user_role = get_effective_role(username).value
        st.session_state.is_admin = has_admin_access(username)
    except Exception:
        logger.exception("Admin page session restore failed")


def _seed_demo_requests(dms: DmsManager, officer_username: str) -> dict:
    """Create a small realistic dataset for defense/demo sessions."""
    if dms.db.query(DmsRequest).filter(DmsRequest.user_id.like("demo_%")).count() > 0:
        return {"created": 0, "skipped": True}

    scenarios = [
        {
            "request_type": "PASOS",
            "user_id": "demo_gradjanin_1",
            "user_email": "demo1@example.com",
            "user_city": "Podgorica",
            "description": "Produzenje putne isprave prije putovanja.",
            "final_status": RequestStatus.SUBMITTED,
        },
        {
            "request_type": "LICNA_KARTA",
            "user_id": "demo_gradjanin_2",
            "user_email": "demo2@example.com",
            "user_city": "Andrijevica",
            "description": "Izrada nove licne karte zbog isteka.",
            "final_status": RequestStatus.UNDER_REVIEW,
        },
        {
            "request_type": "VOZACKA_DOZVOLA",
            "user_id": "demo_gradjanin_3",
            "user_email": "demo3@example.com",
            "user_city": "Bar",
            "description": "Obnova vozacke dozvole.",
            "final_status": RequestStatus.PENDING_USER,
            "reason": "Molimo dopunite ljekarsko uvjerenje koje je isteklo.",
        },
        {
            "request_type": "TURIZAM_REGISTRACIJA",
            "user_id": "demo_izdavalac_1",
            "user_email": "rent1@example.com",
            "user_city": "Budva",
            "description": "Registracija apartmana za ljetnju sezonu.",
            "final_status": RequestStatus.COMPLETED,
        },
        {
            "request_type": "TURIZAM_LICENCA",
            "user_id": "demo_izdavalac_2",
            "user_email": "rent2@example.com",
            "user_city": "Kotor",
            "description": "Produzenje turisticke licence.",
            "final_status": RequestStatus.REJECTED,
            "reason": "Nedostaje validna potvrda o vlasnistvu.",
        },
    ]

    created = 0
    for item in scenarios:
        request_type = getattr(RequestType, item["request_type"])
        req = dms.create_request(
            request_type=request_type,
            user_id=item["user_id"],
            user_email=item["user_email"],
            user_city=item["user_city"],
            description=item.get("description"),
        )
        dms.submit_request(req.id, changed_by=item["user_id"])

        final_status = item["final_status"]
        if final_status == RequestStatus.SUBMITTED:
            created += 1
            continue

        dms.transition_request(
            req.id,
            RequestStatus.UNDER_REVIEW,
            changed_by=officer_username,
            actor_role="admin",
            reason="Predmet preuzet u obradu.",
        )

        if final_status == RequestStatus.UNDER_REVIEW:
            created += 1
            continue

        if final_status == RequestStatus.PENDING_USER:
            reason = item.get("reason") or "Potrebna dopuna dokumentacije."
            dms.transition_request(
                req.id,
                RequestStatus.PENDING_USER,
                changed_by=officer_username,
                actor_role="admin",
                reason=reason,
            )
            dms.add_comment(
                req.id,
                author=officer_username,
                content=reason,
                author_type="admin",
                is_internal=False,
            )
            created += 1
            continue

        if final_status in {RequestStatus.COMPLETED, RequestStatus.REJECTED}:
            if final_status == RequestStatus.COMPLETED:
                dms.transition_request(
                    req.id,
                    RequestStatus.APPROVED,
                    changed_by=officer_username,
                    actor_role="admin",
                    reason="Dokumentacija uredna.",
                )
                dms.transition_request(
                    req.id,
                    RequestStatus.COMPLETED,
                    changed_by=officer_username,
                    actor_role="admin",
                    reason="Predmet zavrsen i arhiviran.",
                )
            else:
                dms.transition_request(
                    req.id,
                    RequestStatus.REJECTED,
                    changed_by=officer_username,
                    actor_role="admin",
                    reason=item.get("reason") or "Predmet odbijen.",
                )
            created += 1

    return {"created": created}


def _status_label(status: RequestStatus) -> str:
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


def _build_audit_csv(audit_payload: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["section", "timestamp", "actor", "action", "details"])

    req = audit_payload.get("request", {})
    writer.writerow(
        [
            "request",
            req.get("created_at") or "",
            req.get("user_id") or "",
            f"{req.get('request_type', '')}:{req.get('status', '')}",
            req.get("reason") or "",
        ]
    )

    for row in audit_payload.get("status_history", []):
        writer.writerow(
            [
                "status_history",
                row.get("changed_at") or "",
                row.get("changed_by") or "",
                f"{row.get('from_status', '')}->{row.get('to_status', '')}",
                row.get("reason") or "",
            ]
        )

    for row in audit_payload.get("comments", []):
        writer.writerow(
            [
                "comment",
                row.get("created_at") or "",
                row.get("author") or "",
                row.get("author_type") or "",
                row.get("content") or "",
            ]
        )

    return output.getvalue()


def _request_option_label(req: DmsRequest) -> str:
    return f"#{req.id} | {req.request_type.value} | {req.user_id} | {req.status.value}"


def _render_officer_view() -> None:
    """Pojednostavljen panel za officers — vide samo svoje dodijeljene predmete."""
    st.title("Officer panel")
    st.caption(f"Prijavljeni kao: {st.session_state.user}")

    db = SessionLocal()
    dms = DmsManager(db)
    try:
        tab_mine, tab_all = st.tabs(["Moji predmeti", "Svi aktivni"])

        with tab_mine:
            mine = dms.get_assigned_requests(st.session_state.user)
            if not mine:
                st.info("Nemate aktivnih dodijeljenih predmeta.")
            else:
                st.caption(f"Dodijeljeno vam: {len(mine)} predmeta")
                chosen = st.selectbox(
                    "Odaberite predmet",
                    options=mine,
                    format_func=_request_option_label,
                    key="officer_mine_select",
                )
                _render_request_detail(chosen, dms)

        with tab_all:
            active = dms.get_active_requests()
            if not active:
                st.info("Nema aktivnih zahtjeva u sistemu.")
            else:
                for req in active:
                    assigned = req.assigned_to or "—"
                    col1, col2, col3 = st.columns([3, 2, 2])
                    col1.write(f"#{req.id} — {req.request_type.value}")
                    col2.write(_status_label(req.status))
                    col3.write(f"Dodijeljeno: {assigned}")
    finally:
        db.close()


def admin_dashboard() -> None:
    _restore_admin_session_if_possible()

    if not st.session_state.get("is_admin"):
        st.error("Nemate pristup ovoj sekciji.")
        return

    effective_role = get_effective_role(st.session_state.user)
    is_superadmin = effective_role == Role.ADMIN

    if not is_superadmin:
        _render_officer_view()
        return

    st.title("Admin panel")

    db = SessionLocal()
    dms = DmsManager(db)

    try:
        tab_dashboard, tab_queue, tab_mine, tab_archive = st.tabs(
            ["Dashboard", "Aktivni zahtjevi", "Moji predmeti", "Arhiva"]
        )

        with tab_dashboard:
            st.markdown("### KPI pregled")
            action_col1, action_col2 = st.columns(2)
            with action_col1:
                if st.button("Primijeni SLA eskalaciju", key="sla_escalation_btn"):
                    result = dms.apply_sla_escalation()
                    if result["escalated"]:
                        st.warning(f"Eskalirano predmeta: {result['escalated']}")
                    else:
                        st.info("Nema novih SLA eskalacija.")
            with action_col2:
                if st.button("Generisi demo predmete", key="seed_demo_requests_dashboard"):
                    result = _seed_demo_requests(dms, st.session_state.user)
                    if result.get("skipped"):
                        st.warning("Demo podaci već postoje u bazi.")
                    else:
                        st.success(f"Generisano demo predmeta: {result['created']}")
                        st.rerun()

            kpis = dms.get_kpi_metrics()
            kc1, kc2, kc3, kc4 = st.columns(4)
            kc1.metric("Ukupno zahtjeva", kpis["total_requests"])
            kc2.metric("Prosjek prve obrade (h)", kpis["avg_first_review_hours"])
            kc3.metric("Stopa zavrsetka", f"{kpis['completion_rate_percent']}%")
            kc4.metric("Stopa vracanja", f"{kpis['correction_rate_percent']}%")

            st.markdown("### KPI trendovi (nedeljno)")
            trends = dms.get_weekly_trends(weeks=8)
            trends_df = pd.DataFrame(trends)
            if not trends_df.empty:
                st.dataframe(trends_df, use_container_width=True)
                chart_df = trends_df.set_index("week")[["created", "submitted", "completed"]]
                st.line_chart(chart_df)

            stats = dms.get_statistics()
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Ukupno", stats["total_requests"])
            c2.metric("Podneseni", stats["by_status"].get("submitted", 0))
            c3.metric("U obradi", stats["by_status"].get("under_review", 0))
            c4.metric("Prekoračen rok", stats.get("overdue_count", 0))

            st.markdown("### Kritični slučajevi")
            overdue = dms.get_overdue_requests()
            if overdue:
                for req in overdue:
                    late_days = (datetime.now() - req.estimated_completion).days
                    st.warning(
                        f"Zahtjev #{req.id} ({req.request_type.value}) kasni {late_days} dana."
                    )
            else:
                st.success("Nema prekoračenih rokova.")

            st.markdown("### Prioritetni inbox")
            inbox = dms.get_priority_inbox(pending_user_days=3)
            f1, f2, f3 = st.columns(3)
            with f1:
                only_overdue = st.checkbox("Samo kasni", key="inbox_only_overdue")
            with f2:
                only_pending_user = st.checkbox("Samo 'Čeka korisnika' >3 dana", key="inbox_only_pending")
            with f3:
                only_unassigned = st.checkbox("Samo bez dodjele", key="inbox_only_unassigned")

            filtered = []
            for row in inbox:
                if only_overdue and not row["is_overdue"]:
                    continue
                if only_pending_user and not row["is_pending_user_long"]:
                    continue
                if only_unassigned and not row["is_unassigned"]:
                    continue
                filtered.append(row)

            if not filtered:
                st.caption("Nema predmeta za izabrane filtere.")
            else:
                inbox_df = pd.DataFrame(
                    [
                        {
                            "id": row["request"].id,
                            "tip": row["request"].request_type.value,
                            "korisnik": row["request"].user_id,
                            "status": row["request"].status.value,
                            "prioritet": row["request"].priority.value,
                            "dodijeljen": row["request"].assigned_to or "-",
                            "signal": "; ".join(row["flags"]) if row["flags"] else "normal",
                            "score": row["score"],
                        }
                        for row in filtered[:25]
                    ]
                )
                st.dataframe(inbox_df, use_container_width=True)

            st.markdown("### Workload po službeniku")
            workload = dms.get_officer_workload(days=30)
            if not workload:
                st.caption("Još nema dodijeljenih predmeta po službeniku.")
            else:
                workload_df = pd.DataFrame(workload)
                st.dataframe(workload_df, use_container_width=True)
                chart_df = workload_df.set_index("officer")[["active", "overdue_active", "completed_last_30_days"]]
                st.bar_chart(chart_df)

        with tab_queue:
            active = dms.get_active_requests()
            if not active:
                st.info("Nema aktivnih zahtjeva.")
                if st.button("Generisi demo predmete", key="seed_demo_requests_queue"):
                    result = _seed_demo_requests(dms, st.session_state.user)
                    if result.get("skipped"):
                        st.warning("Demo podaci već postoje u bazi.")
                    else:
                        st.success(f"Generisano demo predmeta: {result['created']}")
                        st.rerun()
            else:
                st.markdown("### Bulk akcije")
                staff_options = get_staff_usernames()
                assign_options = ["(bez promjene)"] + staff_options

                bulk_selection = st.multiselect(
                    "Odaberite više zahtjeva",
                    options=active,
                    format_func=_request_option_label,
                    key="bulk_request_selection",
                )

                b1, b2, b3 = st.columns(3)
                with b1:
                    assign_to_choice = st.selectbox(
                        "Dodijeli službeniku",
                        options=assign_options,
                        key="bulk_assign_to_choice",
                    )
                with b2:
                    priority_choice = st.selectbox(
                        "Novi prioritet",
                        options=["(bez promjene)"] + [p.value for p in RequestPriority],
                        key="bulk_priority_choice",
                    )
                with b3:
                    status_choice = st.selectbox(
                        "Novi status",
                        options=["(bez promjene)"] + [s.value for s in RequestStatus],
                        key="bulk_status_choice",
                    )

                bulk_reason = st.text_area("Napomena za bulk akciju", key="bulk_reason")
                if st.button("Primijeni bulk akciju", key="bulk_apply_btn", type="primary"):
                    if not bulk_selection:
                        st.warning("Odaberite barem jedan zahtjev.")
                    elif status_choice == RequestStatus.PENDING_USER.value and not bulk_reason.strip():
                        st.warning("Za status 'Čeka korisnika' unesite poruku za korisnika.")
                    else:
                        priority = (
                            RequestPriority(priority_choice)
                            if priority_choice != "(bez promjene)"
                            else None
                        )
                        status = (
                            RequestStatus(status_choice)
                            if status_choice != "(bez promjene)"
                            else None
                        )
                        assign_to = assign_to_choice if assign_to_choice != "(bez promjene)" else None
                        result = dms.bulk_manage_requests(
                            request_ids=[req.id for req in bulk_selection],
                            changed_by=st.session_state.user,
                            assign_to=assign_to,
                            new_priority=priority,
                            new_status=status,
                            reason=bulk_reason.strip() or None,
                        )
                        st.success(
                            f"Bulk završeno: obrađeno={result['processed']}, ažurirano={result['updated']}, "
                            f"tranzicija={result['transitioned']}, grešaka={len(result['failed'])}."
                        )
                        if result["failed"]:
                            st.warning("Neki predmeti nijesu ažurirani:")
                            st.json(result["failed"])
                        st.rerun()

                chosen = st.selectbox(
                    "Odaberite zahtjev",
                    options=active,
                    format_func=lambda req: f"#{req.id} | {req.request_type.value} | {req.user_id} | {req.status.value}",
                )
                _render_request_detail(chosen, dms)

        with tab_mine:
            mine = dms.get_assigned_requests(st.session_state.user)
            if not mine:
                st.info("Nemate aktivnih dodijeljenih predmeta.")
            else:
                st.caption(f"Dodijeljeno vam: {len(mine)} predmeta")
                chosen_mine = st.selectbox(
                    "Odaberite predmet",
                    options=mine,
                    format_func=_request_option_label,
                    key="admin_mine_select",
                )
                _render_request_detail(chosen_mine, dms)

        with tab_archive:
            completed = db.query(DmsRequest).filter(
                DmsRequest.status.in_([RequestStatus.COMPLETED, RequestStatus.REJECTED])
            ).order_by(DmsRequest.updated_at.desc()).limit(50).all()

            if not completed:
                st.caption("Arhiva je prazna.")
            else:
                for req in completed:
                    st.write(
                        f"#{req.id} | {req.request_type.value} | {_status_label(req.status)} | "
                        f"{req.updated_at.strftime('%d.%m.%Y %H:%M')}"
                    )

    finally:
        db.close()


def _render_detail_info(request: DmsRequest) -> None:
    render_status_stepper(
        request.status,
        assigned_to=request.assigned_to,
        forward_count=getattr(request, "forward_count", 0) or 0,
    )
    c1, c2, c3 = st.columns(3)
    c1.write(f"Korisnik: {request.user_id}")
    c1.write(f"Email: {request.user_email}")
    c2.write(f"Status: {_status_label(request.status)}")
    c2.write(f"Prioritet: {request.priority.value}")
    c3.write(f"Opština: {request.user_city}")
    c3.write(f"Rok: {request.estimated_completion.strftime('%d.%m.%Y') if request.estimated_completion else '-'}")

    st.markdown("#### Opis i detalji")
    st.write(request.description or "-")
    if request.details:
        st.json(request.details)

    st.markdown("#### Dokumenta")
    if request.documents_metadata:
        for doc in request.documents_metadata:
            st.write(f"- {doc.get('name')} ({doc.get('status', 'pending_review')})")
    else:
        st.caption("Nema uploadovanih dokumenata.")

    if request.payment_status:
        _PAYMENT_LABELS = {"not_required": "Nije potrebno", "pending": "Neplaćeno", "paid": "Plaćeno"}
        st.caption(
            f"Plaćanje: {_PAYMENT_LABELS.get(request.payment_status, request.payment_status)}"
            + (f" • Ref: {request.payment_reference}" if request.payment_reference else "")
        )
    if request.signed_pdf_path and request.signature_hash:
        st.caption(
            f"E-potpis rješenja: hash {request.signature_hash[:16]}… "
            f"({Path(request.signed_pdf_path).name})"
        )


def _render_detail_audit(request: DmsRequest, dms: DmsManager) -> None:
    st.markdown("#### Audit export")
    audit_payload = dms.build_audit_pack(request.id)
    chain_info = audit_payload.get("audit_chain", {})
    if chain_info.get("legacy"):
        st.info(
            f"ℹ️ Audit log iz pre-hash ere ({chain_info.get('total_entries', 0)} unosa). "
            "Nove tranzicije će biti zaštićene hash chain-om."
        )
    elif chain_info.get("valid"):
        st.success(
            f"✅ Hash chain validan ({chain_info.get('total_entries', 0)} unosa). "
            "Audit log nije modifikovan."
        )
    else:
        st.error(f"⚠️ Hash chain nije validan — prekid kod unosa #{chain_info.get('broken_at')}.")

    st.download_button(
        "Preuzmi audit JSON",
        data=json.dumps(audit_payload, ensure_ascii=False, indent=2),
        file_name=f"audit_request_{request.id}.json",
        mime="application/json",
        key=f"audit_export_{request.id}",
    )
    st.download_button(
        "Preuzmi audit CSV",
        data=_build_audit_csv(audit_payload),
        file_name=f"audit_request_{request.id}.csv",
        mime="text/csv",
        key=f"audit_export_csv_{request.id}",
    )


def _render_detail_comments(request: DmsRequest, dms: DmsManager) -> None:
    st.markdown("#### Komunikacija")
    comments = dms.get_visible_comments(request.id, for_user=False)
    if comments:
        for comment in comments:
            created = comment.created_at.strftime("%d.%m.%Y %H:%M") if comment.created_at else ""
            visibility = "interno" if comment.is_internal else "vidljivo korisniku"
            st.write(f"{comment.author} ({created}, {visibility}): {comment.content}")
    else:
        st.caption("Nema komentara.")

    officer_comment = st.text_area("Komentar službenika", key=f"officer_comment_{request.id}")
    internal_only = st.checkbox("Interni komentar", key=f"internal_comment_{request.id}")
    if st.button("Sačuvaj komentar", key=f"save_comment_{request.id}"):
        if officer_comment.strip():
            dms.add_comment(
                request.id,
                author=st.session_state.user,
                content=officer_comment.strip(),
                author_type="admin",
                is_internal=internal_only,
            )
            st.success("Komentar je sačuvan.")
            st.rerun()


def _render_detail_actions(request: DmsRequest, dms: DmsManager) -> None:
    _render_forward_section(request, dms)

    st.markdown("#### Promjena statusa")
    available = dms.get_available_transitions(request.id)
    if not available:
        st.caption("Za trenutni status nema dostupnih tranzicija.")
        return

    _QA_CONFIG = {
        RequestStatus.UNDER_REVIEW: ("🔍 U obradu",      "secondary"),
        RequestStatus.PENDING_USER: ("⏳ Zatraži dopunu", "secondary"),
        RequestStatus.APPROVED:     ("✅ Odobri",         "primary"),
        RequestStatus.REJECTED:     ("❌ Odbij",          "secondary"),
        RequestStatus.COMPLETED:    ("📦 Završi",         "primary"),
    }
    qa_cols = st.columns(max(len(available), 1))
    for col, avail_s in zip(qa_cols, available):
        label, btn_type = _QA_CONFIG.get(avail_s, (avail_s.value, "secondary"))
        with col:
            if st.button(label, key=f"qa_{request.id}_{avail_s.value}", type=btn_type):
                st.session_state[f"qa_sel_{request.id}"] = avail_s

    qa_sel = st.session_state.get(f"qa_sel_{request.id}")
    preselect_idx = available.index(qa_sel) if qa_sel and qa_sel in available else 0

    new_status = st.selectbox(
        "Novi status",
        options=available,
        index=preselect_idx,
        format_func=_status_label,
        key=f"status_change_{request.id}",
    )
    reason = st.text_area("Napomena za promjenu", key=f"status_reason_{request.id}")

    if st.button("Potvrdi promjenu", key=f"status_submit_{request.id}", type="primary"):
        try:
            if new_status == RequestStatus.UNDER_REVIEW and not request.assigned_to:
                request.assigned_to = st.session_state.user

            if new_status == RequestStatus.PENDING_USER and not reason.strip():
                st.error("Unesite poruku za korisnika kada status prelazi na 'Čeka korisnika'.")
                return

            dms.transition_request(
                request.id,
                new_status,
                changed_by=st.session_state.user,
                actor_role="admin",
                reason=reason.strip() or None,
            )

            if new_status == RequestStatus.PENDING_USER and reason.strip():
                dms.add_comment(
                    request.id,
                    author=st.session_state.user,
                    content=reason.strip(),
                    author_type="admin",
                    is_internal=False,
                )

            if new_status == RequestStatus.REJECTED and reason.strip():
                request.rejection_reason = reason.strip()
                dms.db.commit()

            st.session_state.pop(f"qa_sel_{request.id}", None)
            st.success("Status je ažuriran.")
            if request.user_email:
                st.info(f"📧 Notifikacija poslana korisniku: {request.user_email} — novi status: {_status_label(new_status)}")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except PermissionError as exc:
            st.error(str(exc))
        except Exception:
            logger.exception("Unexpected admin status update failure request_id=%s", request.id)
            st.error("Doslo je do greske pri promjeni statusa. Pokusajte ponovo.")


def _render_request_detail(request: DmsRequest, dms: DmsManager) -> None:
    st.markdown(f"### Zahtjev #{request.id}")
    _render_detail_info(request)
    _render_detail_audit(request, dms)
    _render_detail_comments(request, dms)
    _render_detail_actions(request, dms)


if __name__ == "__main__":
    admin_dashboard()
