"""SQLAlchemy ORM models for the Long Absenteeism follow-up (Jute Production).

Six tenant-DB tables (schema source of truth:
dbqueries/migrations/20261011_jute_prod_absent_tables.sql; spec:
docs/superpowers/specs/2026-10-11-long-absenteeism-followup.md):

  jute_prod_absent_rule          -- per-company thresholds / clause text / labels
                                    (code defaults when no row, like jute_po_settle_rule).
  jute_prod_absent_case          -- one case per long-absent worker (eb_id) in one
                                    branch, with a FROZEN snapshot of the employee
                                    master + last attendance, the ladder stage, the
                                    exit, and the Excel-style free text. status
                                    1 open / 2 parked / 0 closed. At most one live
                                    (status <> 0) active row per (co_id, branch_id,
                                    eb_id): guarded in code AND by the functional
                                    unique key uq_jpac_live (DDL-only, not declared).
  jute_prod_absent_letter        -- one letter per stage per case, text frozen at
                                    issue. issue_date = printed date; sent_date =
                                    when it actually went out (the legal sequence
                                    and the next wait count from sent_date).
  jute_prod_absent_union_receipt -- one row per working union served with the
                                    gate notice.
  jute_prod_absent_event         -- the dated timeline (user REMARK / FEEDBACK /
                                    REPLY + system entries).
  jute_prod_absent_template      -- letter templates per company + language
                                    (code defaults when no row).

Leave link on the case (TKT-000223, migration 20261012_jute_prod_absent_leave_link.sql):
leave_type_id / leave_type_name = the hrms_leave_types_mst type picked on /verify
or /exit (esi_stl_result / exit_reason 'LEAVE') or copied from the approved leave
found in the leave module; leave_from / leave_to / leave_transaction_id = that
approved leave (leave_transactions, status 3, ending after the last working date),
refreshed on /list, /get and after /open / /reopen. leave_transaction_id NULL = no
module leave (a manual /exit pick keeps only the type). leave_parked = 1 while the
case is PARKED automatically because that leave covers today (owner ruling
12.10.2026: the leave module is the source, no manual verification step); back
to 0 when the case resumes (leave ended / withdrawn, or a user resumes it).

Letter entered after the fact (owner 2026-10-12, migration
20261012b_jute_prod_absent_letter_recorded.sql): jute_prod_absent_letter.recorded
= 1 for a letter recorded through /letter_record (issued outside VoWERP, dates
typed in), 0 for letters issued here. Mapped DEFERRED with no Python default so
loads / inserts of the other letter paths never name the column (they keep
working on a tenant where the migration has not run); the raw SQL reads use
``l.*`` and long_absenteeism_common.model_to_dict only reports it when loaded.

Conventions follow low_performer_followup_models.py: shared Base from
src/models/mst.py, SQLAlchemy 2.0 Mapped / mapped_column, soft delete (active),
updated_by + updated_date_time (no created_* -- DB triggers).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models.mst import Base


class JuteProdAbsentRule(Base):
    """Per-company rule row (PK co_id). Defaults live in long_absenteeism_common.RULE_DEFAULTS."""

    __tablename__ = "jute_prod_absent_rule"

    co_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    detect_days: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90, server_default="90")
    wait_w2_days: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    wait_w3_days: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    wait_sc_days: Mapped[int] = mapped_column(Integer, nullable=False, default=15, server_default="15")
    sc_reply_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    notice_reply_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7, server_default="7")
    dismiss_snooze_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    exclude_t_codes: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")  # retired (11.17)
    # Comma-separated single letters ('T,C,R'); NULL = the code default (amendment 11.17).
    exclude_prefixes: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    clause_text: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    signatory: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    extra_1_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    extra_2_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    extra_3_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    extra_4_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    extra_5_label: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdAbsentCase(Base):
    """One case of one long-absent worker in one branch."""

    __tablename__ = "jute_prod_absent_case"
    __table_args__ = (
        Index("idx_jpac_scope", "co_id", "branch_id", "status", "active", "stage_code"),
        Index("idx_jpac_eb", "eb_id", "active"),
        Index("idx_jpac_due", "co_id", "branch_id", "next_due_date"),
    )

    case_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    eb_id: Mapped[int] = mapped_column(Integer, nullable=False)
    case_no: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # snapshot
    emp_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    emp_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    dept_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    dept_desc: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    sub_dept_desc: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    designation: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    father_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    mobile: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    date_of_join: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    esi_no: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    pf_no: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    last_machine: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    last_spell: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # absence facts
    last_working_date: Mapped[date] = mapped_column(Date, nullable=False)
    detected_date: Mapped[date] = mapped_column(Date, nullable=False)
    absent_days_at_open: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rejoined_seen_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # ladder
    stage_code: Mapped[str] = mapped_column(String(20), nullable=False, default="DETECTED", server_default="DETECTED")
    stage_date: Mapped[date] = mapped_column(Date, nullable=False)
    next_due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    habitual: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # verification
    esi_stl_result: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    timekeeper_confirmed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    verified_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    verify_note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    # exit
    exit_reason: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    exit_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    exit_remarks: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    expected_return_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # leave link (TKT-000223): manual pick (type only) or the leave module's approved leave
    leave_type_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    leave_type_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    leave_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    leave_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    leave_transaction_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    leave_parked: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    snooze_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    struck_off_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # struck-off approval (standard engine)
    status_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approval_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # performance snapshot
    perf_avg_eff: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 2), nullable=True)
    perf_kg_per_hr: Mapped[Optional[Decimal]] = mapped_column(Numeric(9, 3), nullable=True)
    perf_days_worked: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    perf_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    perf_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    perf_dept: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Excel-style free text
    remarks: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    extra_1: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    extra_2: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    extra_3: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    extra_4: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    extra_5: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdAbsentLetter(Base):
    """One issued letter (text frozen) of a case."""

    __tablename__ = "jute_prod_absent_letter"
    __table_args__ = (
        Index("idx_jpal_case", "case_id", "active", "stage_code"),
        Index("idx_jpal_batch", "batch_ref"),
    )

    letter_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(Integer, ForeignKey("jute_prod_absent_case.case_id"), nullable=False)
    stage_code: Mapped[str] = mapped_column(String(20), nullable=False)
    letter_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    template_code: Mapped[str] = mapped_column(String(20), nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reply_due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    dispatch_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    sent_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    ad_no: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    delivery_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    delivery_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    gate_display_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    dept_display_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    batch_ref: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    forced: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # 1 = recorded as already issued (/letter_record). Deferred, no Python default:
    # see the module docstring (migration 20261012b).
    recorded: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", deferred=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdAbsentUnionReceipt(Base):
    """One working union served with the gate notice."""

    __tablename__ = "jute_prod_absent_union_receipt"
    __table_args__ = (Index("idx_jpaur_letter", "letter_id", "active"),)

    receipt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    letter_id: Mapped[int] = mapped_column(Integer, ForeignKey("jute_prod_absent_letter.letter_id"), nullable=False)
    union_name: Mapped[str] = mapped_column(String(100), nullable=False)
    received_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    received_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdAbsentEvent(Base):
    """One dated timeline row of a case."""

    __tablename__ = "jute_prod_absent_event"
    __table_args__ = (Index("idx_jpae_case", "case_id", "active", "event_date"),)

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(Integer, ForeignKey("jute_prod_absent_case.case_id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    contact_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    text: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    ref_letter_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )


class JuteProdAbsentTemplate(Base):
    """Letter template per company + code + language."""

    __tablename__ = "jute_prod_absent_template"
    __table_args__ = (UniqueConstraint("co_id", "template_code", "lang", name="uq_jpat_code"),)

    template_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    co_id: Mapped[int] = mapped_column(Integer, nullable=False)
    template_code: Mapped[str] = mapped_column(String(20), nullable=False)
    lang: Mapped[str] = mapped_column(String(5), nullable=False, default="en", server_default="en")
    subject: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_by: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_date_time: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.current_timestamp()
    )
