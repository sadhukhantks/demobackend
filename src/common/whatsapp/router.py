"""WhatsApp test endpoints. Mounted at /api/whatsapp in main.py.

POST /send_test_text      {to, message}
POST /send_party_details  {party_id, to?}            to defaults to the party's phone_no
POST /send_report_now     {report_code, co_id, branch_id, to, report_date?,
                           template_name?, template_language?}

Every attempt is written to whatsapp_message_log. Responses carry Meta's message
id. Missing credentials -> 503, a Meta-side rejection -> 502.
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.config.db import get_tenant_db

from . import client
from . import query as q
from .daily_reports import build_report_pdf, default_report_date, report_caption
from .party_pdf import PartyNotFoundError, fetch_party, party_pdf_filename, render_party_pdf

logger = logging.getLogger(__name__)

router = APIRouter()


class SendTextBody(BaseModel):
    to: str
    message: str


class SendPartyDetailsBody(BaseModel):
    party_id: int
    to: str | None = None


class SendReportNowBody(BaseModel):
    report_code: str
    co_id: int
    branch_id: int
    to: str
    report_date: date | None = None
    template_name: str | None = None
    template_language: str | None = None


def _phone(raw) -> str:
    try:
        return client.normalize_phone(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _log(db: Session, **kwargs) -> None:
    """Logging must never turn a delivered message into an error response."""
    try:
        q.log_message(db, **kwargs)
        db.commit()
    except Exception:
        logger.exception("whatsapp: could not write whatsapp_message_log")
        db.rollback()


def _send(db: Session, send_fn, *, phone_no: str, message_type: str,
          file_name: str | None = None, report_code: str | None = None) -> dict:
    """Run one send, log it, map client errors to HTTP errors."""
    try:
        resp = send_fn()
    except client.WhatsAppConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except client.WhatsAppApiError as exc:
        _log(db, phone_no=phone_no, message_type=message_type, status="FAILED",
             file_name=file_name, report_code=report_code, error_text=str(exc))
        raise HTTPException(status_code=502, detail=str(exc))
    wa_id = client.message_id(resp)
    _log(db, phone_no=phone_no, message_type=message_type, status="SENT",
         file_name=file_name, report_code=report_code, wa_message_id=wa_id)
    return {"data": {"wa_message_id": wa_id, "to": phone_no, "message_type": message_type,
                     "file_name": file_name}}


@router.post("/send_test_text")
def send_test_text(
    body: SendTextBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        if not body.message.strip():
            raise HTTPException(status_code=400, detail="message is required")
        to = _phone(body.to)
        return _send(db, lambda: client.send_text(to, body.message, db=db), phone_no=to, message_type="text")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send_party_details")
def send_party_details(
    body: SendPartyDetailsBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        try:
            party, branches = fetch_party(db, body.party_id)
        except PartyNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        pdf_bytes, filename = render_party_pdf(party, branches), party_pdf_filename(party)
        raw_to = body.to or party.get("phone_no")
        if not raw_to:
            raise HTTPException(status_code=400, detail="Party has no phone_no; pass 'to'")
        to = _phone(raw_to)
        return _send(
            db, lambda: client.send_document(to, pdf_bytes, filename, caption="Party Details", db=db),
            phone_no=to, message_type="document", file_name=filename,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/send_report_now")
def send_report_now(
    body: SendReportNowBody,
    db: Session = Depends(get_tenant_db),
    token_data: dict = Depends(get_current_user_with_refresh),
):
    try:
        code = body.report_code.strip().upper()
        to = _phone(body.to)
        report_d = body.report_date or default_report_date(code)
        try:
            pdf_bytes, filename = build_report_pdf(db, code, body.co_id, body.branch_id, report_d)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        branch_name = db.execute(q.get_branch_name(), {"branch_id": body.branch_id}).scalar()
        caption = report_caption(code, branch_name, report_d)
        if body.template_name:
            return _send(
                db,
                lambda: client.send_template(to, body.template_name, body.template_language or "en",
                                             document=(pdf_bytes, filename), db=db),
                phone_no=to, message_type="template", file_name=filename, report_code=code,
            )
        return _send(
            db, lambda: client.send_document(to, pdf_bytes, filename, caption=caption, db=db),
            phone_no=to, message_type="document", file_name=filename, report_code=code,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
