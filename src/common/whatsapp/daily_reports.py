"""Send the daily reports that are due to their WhatsApp subscribers.

Reads the active rows of whatsapp_report_subscription in one tenant DB and, for
each row that is due, builds the report PDF and sends it. Meant to be run every
few minutes by cron / a systemd timer: a row is due once the IST clock has
passed its send_time and its last_sent_date is older than the report date, so
repeated runs send each report once.

Reports:
  DPR  Daily Production Report (src.juteProduction.dpr_pdf.build_dpr_pdf);
       report date = yesterday in Asia/Kolkata.

A subscription with template_name set is sent with send_template (the template
must have a DOCUMENT header and a body WITHOUT variables); otherwise as a
free-form document, which Meta only delivers inside the 24-hour window after the
recipient last messaged the business number.

CLI — MUST be run from the vowerp3be directory (src/config/db.py loads
env/database.env by relative path):

    cd /root/projects/vowerp3/vowerp3be
    uv run python -m src.common.whatsapp.daily_reports --tenant sls
        [--report DPR] [--force] [--to <phone>] [--date YYYY-MM-DD] [--dry-run]

  --force    ignore send_time and last_sent_date
  --to       only the subscription(s) of this phone number
  --date     report date instead of yesterday (IST)
  --dry-run  build the PDF(s) into /tmp and print what would be sent; no API
             call, no DB write

Exit code is 1 when any send failed.
"""

import argparse
import logging
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

from src.common.whatsapp import client
from src.common.whatsapp import query as q

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
REPORT_TITLES = {"DPR": "Daily Production Report"}
DRY_RUN_DIR = "/tmp"


def now_ist() -> datetime:
    return datetime.now(IST)


def default_report_date(report_code: str, now: datetime | None = None) -> date:
    """All current reports cover the previous day (IST)."""
    return ((now or now_ist()).date()) - timedelta(days=1)


def _as_time(value) -> time:
    """PyMySQL returns TIME columns as timedelta."""
    if isinstance(value, time):
        return value
    if isinstance(value, timedelta):
        return (datetime.min + value).time()
    if isinstance(value, str) and value:
        parts = [int(x) for x in value.split(":")]
        return time(*(parts + [0, 0])[:3])
    return time(8, 0)


def is_due(sub: dict, now: datetime, report_d: date, force: bool = False) -> tuple[bool, str]:
    """(due?, reason when not due)."""
    if force:
        return True, ""
    send_time = _as_time(sub.get("send_time"))
    if now.time().replace(tzinfo=None) < send_time:
        return False, f"before send_time {send_time.strftime('%H:%M')} IST"
    last = sub.get("last_sent_date")
    if isinstance(last, datetime):
        last = last.date()
    if last is not None and last >= report_d:
        return False, f"already sent for {last.isoformat()}"
    return True, ""


def build_report_pdf(db, report_code: str, co_id: int, branch_id: int, report_d: date) -> tuple[bytes, str]:
    if report_code == "DPR":
        from src.juteProduction.dpr_pdf import build_dpr_pdf  # lazy: heavy, and optional at import
        return build_dpr_pdf(db, int(co_id), int(branch_id), report_d)
    raise ValueError(f"Unknown report_code '{report_code}' (known: {', '.join(REPORT_TITLES)})")


def report_caption(report_code: str, branch_name: str | None, report_d: date) -> str:
    title = REPORT_TITLES.get(report_code, report_code)
    parts = [title] + ([branch_name] if branch_name else []) + [report_d.strftime("%d-%b-%Y")]
    return " – ".join(parts)


def _open_session(tenant: str):
    from src.config.db import Session, get_tenant_engine  # needs cwd = vowerp3be
    return Session(get_tenant_engine(tenant), autoflush=False)


def _load_subscriptions(db, report_code: str | None) -> list[dict]:
    params = {"report_code": report_code} if report_code else {}
    rows = db.execute(q.get_active_subscriptions(report_code), params).fetchall()
    return [dict(r._mapping) for r in rows]


def _record(db, sub: dict, phone: str, message_type: str, file_name: str | None,
            status: str, wa_message_id: str | None, error_text: str | None, report_d: date) -> None:
    """Log the attempt (+ stamp last_sent_date on success). Never raises."""
    try:
        q.log_message(
            db, phone_no=phone, message_type=message_type, status=status,
            subscription_id=sub.get("whatsapp_report_subscription_id"),
            report_code=sub.get("report_code"), file_name=file_name,
            wa_message_id=wa_message_id, error_text=error_text,
        )
        if status == "SENT":
            db.execute(q.mark_subscription_sent(), {
                "report_date": report_d,
                "subscription_id": sub.get("whatsapp_report_subscription_id"),
            })
        db.commit()
    except Exception:
        logger.exception("whatsapp: could not write message log / last_sent_date")
        try:
            db.rollback()
        except Exception:
            pass


def send_due_reports(
    tenant: str,
    *,
    report_code: str | None = None,
    force: bool = False,
    only_phone: str | None = None,
    report_date: date | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """Send every due subscription of `tenant`. One result dict per subscription
    considered: status is SENT, FAILED, SKIPPED (not due) or DRY_RUN."""
    only = client.normalize_phone(only_phone) if only_phone else None
    now = now_ist()
    results: list[dict] = []
    pdf_cache: dict[tuple, tuple] = {}  # (code, co, branch, date) -> (bytes, filename) | Exception

    db = _open_session(tenant)
    try:
        for sub in _load_subscriptions(db, report_code):
            code = sub["report_code"]
            res = {
                "subscription_id": sub.get("whatsapp_report_subscription_id"),
                "report_code": code,
                "co_id": sub.get("co_id"),
                "branch_id": sub.get("branch_id"),
                "recipient_name": sub.get("recipient_name"),
                "phone_no": sub.get("phone_no"),
            }
            try:
                phone = client.normalize_phone(sub.get("phone_no"))
            except ValueError as exc:
                if only:
                    continue
                phone = str(sub.get("phone_no") or "")
                res.update(status="FAILED", error=str(exc))
                results.append(res)
                if not dry_run:
                    _record(db, sub, phone, "document", None, "FAILED", None, str(exc), now.date())
                continue
            if only and phone != only:
                continue
            res["phone_no"] = phone

            report_d = report_date or default_report_date(code, now)
            res["report_date"] = report_d.isoformat()
            due, reason = is_due(sub, now, report_d, force)
            if not due:
                res.update(status="SKIPPED", reason=reason)
                results.append(res)
                continue

            message_type = "template" if sub.get("template_name") else "document"
            res["message_type"] = message_type
            file_name = None
            try:
                key = (code, sub["co_id"], sub["branch_id"], report_d)
                if key not in pdf_cache:
                    try:
                        pdf_cache[key] = build_report_pdf(db, code, sub["co_id"], sub["branch_id"], report_d)
                    except Exception as exc:
                        try:
                            db.rollback()
                        except Exception:
                            pass
                        pdf_cache[key] = exc
                built = pdf_cache[key]
                if isinstance(built, Exception):
                    raise RuntimeError(f"could not build {code} PDF: {type(built).__name__}: {built}") from built
                pdf_bytes, file_name = built
                caption = report_caption(code, sub.get("branch_name"), report_d)
                res.update(file_name=file_name, caption=caption)

                if dry_run:
                    path = os.path.join(DRY_RUN_DIR, file_name)
                    with open(path, "wb") as fh:
                        fh.write(pdf_bytes)
                    res.update(status="DRY_RUN", pdf_path=path, pdf_bytes=len(pdf_bytes),
                               template_name=sub.get("template_name"))
                    results.append(res)
                    continue

                if message_type == "template":
                    resp = client.send_template(
                        phone, sub["template_name"], sub.get("template_language") or "en",
                        document=(pdf_bytes, file_name), db=db,
                    )
                else:
                    resp = client.send_document(phone, pdf_bytes, file_name, caption=caption, db=db)
                wa_id = client.message_id(resp)
                res.update(status="SENT", wa_message_id=wa_id)
                _record(db, sub, phone, message_type, file_name, "SENT", wa_id, None, report_d)
            except Exception as exc:  # one recipient failing must not stop the others
                logger.warning("whatsapp: %s to %s failed: %s", code, phone, exc)
                res.update(status="FAILED", error=str(exc))
                if not dry_run:
                    _record(db, sub, phone, message_type, file_name, "FAILED", None, str(exc), report_d)
            results.append(res)
    finally:
        db.close()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.common.whatsapp.daily_reports",
        description="Send due WhatsApp daily reports for one tenant (run from the vowerp3be directory).",
    )
    parser.add_argument("--tenant", required=True, help="tenant DB name, e.g. sls or dev3")
    parser.add_argument("--report", default=None, help="only this report_code, e.g. DPR")
    parser.add_argument("--force", action="store_true", help="ignore send_time and last_sent_date")
    parser.add_argument("--to", default=None, help="only the subscription(s) of this phone number")
    parser.add_argument("--date", default=None, help="report date YYYY-MM-DD (default: yesterday IST)")
    parser.add_argument("--dry-run", action="store_true", help="build PDFs into /tmp, send nothing, write nothing")
    args = parser.parse_args(argv)

    report_d = None
    if args.date:
        try:
            report_d = date.fromisoformat(args.date)
        except ValueError:
            parser.error("--date must be YYYY-MM-DD")

    results = send_due_reports(
        args.tenant, report_code=args.report, force=args.force,
        only_phone=args.to, report_date=report_d, dry_run=args.dry_run,
    )
    if not results:
        print(f"[{args.tenant}] no active subscriptions matched")
    for r in results:
        who = f"{r.get('recipient_name') or '-'} <{r.get('phone_no')}>"
        head = f"[{args.tenant}] {r['status']:8} {r['report_code']} co {r.get('co_id')} br {r.get('branch_id')} -> {who}"
        if r["status"] == "SKIPPED":
            print(f"{head}: {r.get('reason')}")
        elif r["status"] == "DRY_RUN":
            how = f"template '{r['template_name']}'" if r.get("template_name") else "free-form document"
            print(f"{head}: would send {r['pdf_path']} ({r['pdf_bytes']} bytes) as {how}, caption \"{r['caption']}\"")
        elif r["status"] == "SENT":
            print(f"{head}: {r.get('file_name')} sent, message id {r.get('wa_message_id')}")
        else:
            print(f"{head}: {r.get('error')}")
    return 1 if any(r["status"] == "FAILED" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
