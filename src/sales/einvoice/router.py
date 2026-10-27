"""
Portal endpoints for the GST e-invoice integration.

Prefix: /api/salesEInvoice (registered in src/main.py). Portal persona —
tenant DB via get_tenant_db. The base sales tables are never written here;
all writes land in the einv_* tables (src/models/einvoice.py).

Sandbox-first: every endpoint takes/defaults env=SANDBOX. Generating against
PRODUCTION additionally requires the invoice to be Approved (status 3).
"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.authorization.utils import get_current_user_with_refresh
from src.common.utils import create_response, parse_json_body
from src.config.db import get_tenant_db
from src.models.einvoice import EinvApiLog, EinvGstinConfig, EinvIrnRegistry
from src.sales.einvoice import constants as C
from src.sales.einvoice.client import EInvoiceClient, EInvoiceError
from src.sales.einvoice.payload_builder import build_invoice_payload
from src.sales.einvoice.query import (
    get_api_logs_for_invoice,
    get_einv_config_for_branch,
    get_invoice_header_for_einvoice,
    get_irn_registry_for_invoice,
)

router = APIRouter()

STATUS_APPROVED = 3

_SECRET_FIELDS = ("client_secret", "api_password")


def _mask_config_row(row: dict) -> dict:
    masked = dict(row)
    for field in _SECRET_FIELDS:
        if masked.get(field):
            masked[field] = "***"
    if masked.get("public_key_pem"):
        masked["public_key_pem"] = "(set)"
    return masked


def _resolve_config(db: Session, co_id: int, branch_id: int | None, env: str) -> dict:
    row = db.execute(
        get_einv_config_for_branch(),
        {"co_id": int(co_id), "branch_id": int(branch_id) if branch_id is not None else None, "env": env},
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No active einv_gstin_config for co_id={co_id} branch_id={branch_id} env={env}. "
                "Save credentials via POST /api/salesEInvoice/config first "
                "(see docs/einvoice/SANDBOX_SETUP_GUIDE.md)."
            ),
        )
    return dict(row._mapping)


def _make_log_sink(db: Session, config_id: int | None, invoice_id: int | None,
                   einv_irn_id: int | None, user_id: int | None):
    def sink(entry: dict):
        db.add(EinvApiLog(
            einv_irn_id=einv_irn_id,
            einv_gstin_config_id=config_id,
            invoice_id=invoice_id,
            api_name=entry["api_name"],
            http_status=entry.get("http_status"),
            success=entry.get("success", 0),
            error_code=entry.get("error_code"),
            error_message=entry.get("error_message"),
            request_json=entry.get("request_json"),
            response_json=entry.get("response_json"),
            duration_ms=entry.get("duration_ms"),
            called_by=user_id,
        ))
        db.flush()
    return sink


def _parse_ack_date(value) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Credential config
# ---------------------------------------------------------------------------
@router.get("/config")
def list_config(request: Request, db: Session = Depends(get_tenant_db),
                token_data: dict = Depends(get_current_user_with_refresh)):
    try:
        co_id = request.query_params.get("co_id")
        if not co_id:
            raise HTTPException(status_code=400, detail="co_id is required")
        rows = (
            db.query(EinvGstinConfig)
            .filter(EinvGstinConfig.co_id == int(co_id), EinvGstinConfig.active == 1)
            .all()
        )
        data = [
            _mask_config_row({
                "einv_gstin_config_id": r.einv_gstin_config_id,
                "co_id": r.co_id,
                "branch_id": r.branch_id,
                "gstin": r.gstin,
                "irp_provider": r.irp_provider,
                "env": r.env,
                "api_base_url": r.api_base_url,
                "auth_path": r.auth_path,
                "generate_irn_path": r.generate_irn_path,
                "cancel_irn_path": r.cancel_irn_path,
                "get_irn_path": r.get_irn_path,
                "encryption_mode": r.encryption_mode,
                "client_id": r.client_id,
                "client_secret": r.client_secret,
                "api_username": r.api_username,
                "api_password": r.api_password,
                "public_key_pem": r.public_key_pem,
            })
            for r in rows
        ]
        return create_response(data=data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config")
def save_config(request: Request, db: Session = Depends(get_tenant_db),
                token_data: dict = Depends(get_current_user_with_refresh)):
    """Create or update the credential set for a (gstin, env, provider)."""
    try:
        body = parse_json_body(request)
        for required in ("co_id", "gstin"):
            if not body.get(required):
                raise HTTPException(status_code=400, detail=f"{required} is required")
        gstin = str(body["gstin"]).strip().upper()
        env = str(body.get("env") or C.ENV_SANDBOX).upper()
        provider = str(body.get("irp_provider") or C.PROVIDER_IRIS).upper()
        if env not in (C.ENV_SANDBOX, C.ENV_PRODUCTION):
            raise HTTPException(status_code=400, detail="env must be SANDBOX or PRODUCTION")

        row = (
            db.query(EinvGstinConfig)
            .filter(
                EinvGstinConfig.gstin == gstin,
                EinvGstinConfig.env == env,
                EinvGstinConfig.irp_provider == provider,
            )
            .first()
        )
        if row is None:
            row = EinvGstinConfig(gstin=gstin, env=env, irp_provider=provider, co_id=int(body["co_id"]))
            db.add(row)

        row.co_id = int(body["co_id"])
        row.branch_id = int(body["branch_id"]) if body.get("branch_id") is not None else None
        for field in ("api_base_url", "auth_path", "generate_irn_path", "cancel_irn_path",
                      "get_irn_path", "client_id", "api_username", "public_key_pem"):
            if field in body:
                setattr(row, field, body.get(field) or None)
        # Secrets: only overwrite when a non-masked value is provided
        for field in _SECRET_FIELDS:
            value = body.get(field)
            if value and value != "***":
                setattr(row, field, value)
        if body.get("encryption_mode"):
            mode = str(body["encryption_mode"]).upper()
            if mode not in (C.ENCRYPTION_NIC, C.ENCRYPTION_PLAIN):
                raise HTTPException(status_code=400, detail="encryption_mode must be NIC or PLAIN")
            row.encryption_mode = mode
        row.active = int(body.get("active", 1))
        row.updated_by = token_data.get("user_id")

        db.commit()
        return create_response(data=[{"einv_gstin_config_id": row.einv_gstin_config_id}])
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------
@router.post("/auth-test")
def auth_test(request: Request, db: Session = Depends(get_tenant_db),
              token_data: dict = Depends(get_current_user_with_refresh)):
    """Force a fresh IRP auth call — first thing to run after saving sandbox creds."""
    try:
        body = parse_json_body(request)
        if not body.get("co_id"):
            raise HTTPException(status_code=400, detail="co_id is required")
        env = str(body.get("env") or C.ENV_SANDBOX).upper()
        config = _resolve_config(db, int(body["co_id"]), body.get("branch_id"), env)
        client = EInvoiceClient(
            config,
            log_sink=_make_log_sink(db, config["einv_gstin_config_id"], None, None, token_data.get("user_id")),
        )
        try:
            client.authenticate(force=True)
            db.commit()
            return create_response(data=[{"authenticated": True, "env": env, "gstin": config["gstin"]}])
        except (EInvoiceError, RuntimeError) as exc:
            db.commit()  # keep the failure log row
            return create_response(
                data=[{"authenticated": False, "env": env, "gstin": config["gstin"], "error": str(exc)}],
                status="error", message=str(exc),
            )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Payload preview (dry run — no IRP call)
# ---------------------------------------------------------------------------
@router.get("/payload-preview/{invoice_id}")
def payload_preview(invoice_id: int, db: Session = Depends(get_tenant_db),
                    token_data: dict = Depends(get_current_user_with_refresh)):
    try:
        payload, errors = build_invoice_payload(db, int(invoice_id))
        return create_response(data=[{"invoice_id": int(invoice_id), "payload": payload, "errors": errors}])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Generate IRN
# ---------------------------------------------------------------------------
@router.post("/generate/{invoice_id}")
def generate_irn(invoice_id: int, request: Request, db: Session = Depends(get_tenant_db),
                 token_data: dict = Depends(get_current_user_with_refresh)):
    try:
        body = parse_json_body(request)
        env = str(body.get("env") or C.ENV_SANDBOX).upper()

        header = db.execute(
            get_invoice_header_for_einvoice(), {"invoice_id": int(invoice_id)}
        ).fetchone()
        if header is None:
            raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
        header = dict(header._mapping)
        if env == C.ENV_PRODUCTION and header.get("status_id") != STATUS_APPROVED:
            raise HTTPException(
                status_code=400,
                detail="Only Approved invoices can be reported to the production IRP",
            )

        existing = db.execute(
            get_irn_registry_for_invoice(), {"invoice_id": int(invoice_id), "env": env}
        ).fetchone()
        if existing is not None and existing._mapping["einv_status"] == C.EINV_STATUS_GENERATED:
            raise HTTPException(
                status_code=400,
                detail=f"IRN already generated for invoice {invoice_id} in {env}: {existing._mapping['irn']}",
            )

        payload, errors = build_invoice_payload(db, int(invoice_id))
        if errors:
            raise HTTPException(status_code=400, detail={"message": "Invoice data incomplete for e-invoice", "errors": errors})

        if header.get("co_id") is None:
            raise HTTPException(
                status_code=400,
                detail="Invoice branch is not linked to a company (branch_mst.co_id) — fix the branch master first",
            )
        config = _resolve_config(db, int(header["co_id"]), header.get("branch_id"), env)

        registry = (
            db.query(EinvIrnRegistry)
            .filter(EinvIrnRegistry.invoice_id == int(invoice_id), EinvIrnRegistry.env == env)
            .first()
        )
        if registry is None:
            registry = EinvIrnRegistry(invoice_id=int(invoice_id), env=env)
            db.add(registry)
        registry.einv_gstin_config_id = config["einv_gstin_config_id"]
        registry.co_id = header.get("co_id")
        registry.branch_id = header.get("branch_id")
        registry.request_payload = json.dumps(payload)
        registry.updated_by = token_data.get("user_id")
        db.flush()

        client = EInvoiceClient(
            config,
            log_sink=_make_log_sink(db, config["einv_gstin_config_id"], int(invoice_id),
                                    registry.einv_irn_id, token_data.get("user_id")),
        )
        try:
            result = client.generate_irn(payload)
        except (EInvoiceError, RuntimeError) as exc:
            registry.einv_status = C.EINV_STATUS_FAILED
            registry.last_error = str(exc)[:1000]
            db.commit()
            raise HTTPException(status_code=502, detail=f"IRP rejected the invoice: {exc}")

        registry.einv_status = C.EINV_STATUS_GENERATED
        registry.irn = result.get("Irn")
        registry.ack_no = str(result.get("AckNo") or "") or None
        registry.ack_date = _parse_ack_date(result.get("AckDt"))
        registry.signed_invoice = result.get("SignedInvoice")
        registry.signed_qr_code = result.get("SignedQRCode")
        registry.ewb_no = str(result.get("EwbNo") or "") or None
        registry.last_error = None
        db.commit()

        return create_response(data=[{
            "invoice_id": int(invoice_id),
            "env": env,
            "irn": registry.irn,
            "ack_no": registry.ack_no,
            "ack_date": str(registry.ack_date) if registry.ack_date else None,
            "status": registry.einv_status,
        }])
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Cancel IRN
# ---------------------------------------------------------------------------
@router.post("/cancel/{invoice_id}")
def cancel_irn(invoice_id: int, request: Request, db: Session = Depends(get_tenant_db),
               token_data: dict = Depends(get_current_user_with_refresh)):
    try:
        body = parse_json_body(request)
        env = str(body.get("env") or C.ENV_SANDBOX).upper()
        reason_code = str(body.get("reason_code") or "").strip()
        remarks = str(body.get("remarks") or "").strip()
        if reason_code not in (C.CANCEL_REASON_DUPLICATE, C.CANCEL_REASON_DATA_ENTRY_MISTAKE,
                               C.CANCEL_REASON_ORDER_CANCELLED, C.CANCEL_REASON_OTHERS):
            raise HTTPException(status_code=400, detail="reason_code must be 1, 2, 3 or 4")
        if not remarks:
            raise HTTPException(status_code=400, detail="remarks is required")

        registry = (
            db.query(EinvIrnRegistry)
            .filter(EinvIrnRegistry.invoice_id == int(invoice_id), EinvIrnRegistry.env == env)
            .first()
        )
        if registry is None or registry.einv_status != C.EINV_STATUS_GENERATED or not registry.irn:
            raise HTTPException(status_code=400, detail=f"No generated IRN for invoice {invoice_id} in {env}")

        config_row = db.query(EinvGstinConfig).filter(
            EinvGstinConfig.einv_gstin_config_id == registry.einv_gstin_config_id
        ).first()
        if config_row is None:
            raise HTTPException(status_code=400, detail="Credential config used for generation no longer exists")
        config = {c.name: getattr(config_row, c.name) for c in EinvGstinConfig.__table__.columns}

        client = EInvoiceClient(
            config,
            log_sink=_make_log_sink(db, config["einv_gstin_config_id"], int(invoice_id),
                                    registry.einv_irn_id, token_data.get("user_id")),
        )
        try:
            result = client.cancel_irn(registry.irn, reason_code, remarks)
        except (EInvoiceError, RuntimeError) as exc:
            registry.last_error = str(exc)[:1000]
            db.commit()
            raise HTTPException(status_code=502, detail=f"IRP cancel failed: {exc}")

        registry.einv_status = C.EINV_STATUS_CANCELLED
        registry.cancel_date = _parse_ack_date(result.get("CancelDate")) or datetime.now()
        registry.cancel_reason_code = reason_code
        registry.cancel_remarks = remarks[:255]
        registry.updated_by = token_data.get("user_id")
        db.commit()

        return create_response(data=[{
            "invoice_id": int(invoice_id), "env": env, "irn": registry.irn,
            "status": registry.einv_status, "cancel_date": str(registry.cancel_date),
        }])
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Status + logs
# ---------------------------------------------------------------------------
@router.get("/status/{invoice_id}")
def einvoice_status(invoice_id: int, request: Request, db: Session = Depends(get_tenant_db),
                    token_data: dict = Depends(get_current_user_with_refresh)):
    try:
        env = str(request.query_params.get("env") or C.ENV_SANDBOX).upper()
        row = db.execute(
            get_irn_registry_for_invoice(), {"invoice_id": int(invoice_id), "env": env}
        ).fetchone()
        data = [dict(row._mapping)] if row is not None else []
        return create_response(data=data)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs/{invoice_id}")
def einvoice_logs(invoice_id: int, db: Session = Depends(get_tenant_db),
                  token_data: dict = Depends(get_current_user_with_refresh)):
    try:
        rows = db.execute(get_api_logs_for_invoice(), {"invoice_id": int(invoice_id)}).fetchall()
        return create_response(data=[dict(r._mapping) for r in rows])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
