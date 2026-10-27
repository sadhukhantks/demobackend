"""
E-invoice (IRP integration) models.

New tables only — the existing sales tables (sales_invoice with its manual
irn/ack_no/qr_code columns, e_invoice_responses) are intentionally NOT touched.
These tables link to the base tables by FK:

    co_mst / branch_mst  <-- einv_gstin_config   (per-GSTIN IRP credentials)
    sales_invoice        <-- einv_irn_registry   (IRN lifecycle per invoice+env)
    einv_irn_registry    <-- einv_api_log        (every IRP HTTP call)

DDL: dbqueries/migrations/einvoice_integration_tables.sql
Docs: docs/einvoice/
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for e-invoice models."""
    pass


class EinvGstinConfig(Base):
    """IRP API credentials + endpoint config per (GSTIN, environment, provider)."""
    __tablename__ = "einv_gstin_config"

    einv_gstin_config_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, ForeignKey("co_mst.co_id"), nullable=False)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("branch_mst.branch_id"), nullable=True)
    gstin: Mapped[str] = mapped_column(String(15), nullable=False)
    irp_provider: Mapped[str] = mapped_column(String(20), nullable=False, default="IRIS", server_default="IRIS")
    env: Mapped[str] = mapped_column(String(10), nullable=False, default="SANDBOX", server_default="SANDBOX")
    api_base_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    auth_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    generate_irn_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cancel_irn_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    get_irn_path: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    encryption_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="NIC", server_default="NIC")
    client_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    client_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    api_username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    api_password: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    public_key_pem: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT, nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    irn_entries: Mapped[List["EinvIrnRegistry"]] = relationship(back_populates="config")


class EinvIrnRegistry(Base):
    """IRN lifecycle of one sales invoice in one environment (SANDBOX/PRODUCTION)."""
    __tablename__ = "einv_irn_registry"

    einv_irn_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sales_invoice.invoice_id"), nullable=False, index=True
    )
    einv_gstin_config_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("einv_gstin_config.einv_gstin_config_id"), nullable=True
    )
    co_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    branch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    env: Mapped[str] = mapped_column(String(10), nullable=False, default="SANDBOX", server_default="SANDBOX")
    einv_status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", server_default="PENDING")
    irn: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    ack_no: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    ack_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    signed_invoice: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT, nullable=True)
    signed_qr_code: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT, nullable=True)
    ewb_no: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    ewb_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ewb_valid_till: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancel_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    cancel_reason_code: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    cancel_remarks: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    request_payload: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )

    config: Mapped[Optional["EinvGstinConfig"]] = relationship(back_populates="irn_entries")
    api_logs: Mapped[List["EinvApiLog"]] = relationship(back_populates="irn_entry")


class EinvApiLog(Base):
    """Immutable audit trail of every HTTP call to the IRP (secrets redacted)."""
    __tablename__ = "einv_api_log"

    einv_api_log_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    einv_irn_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("einv_irn_registry.einv_irn_id"), nullable=True, index=True
    )
    einv_gstin_config_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("einv_gstin_config.einv_gstin_config_id"), nullable=True
    )
    invoice_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("sales_invoice.invoice_id"), nullable=True, index=True
    )
    api_name: Mapped[str] = mapped_column(String(30), nullable=False)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    success: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    request_json: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT, nullable=True)
    response_json: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    called_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    called_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.current_timestamp())

    irn_entry: Mapped[Optional["EinvIrnRegistry"]] = relationship(back_populates="api_logs")
