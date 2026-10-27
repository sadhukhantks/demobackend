"""Quick WhatsApp test from the shell — no login needed.

MUST be run from the vowerp3be directory (src/config/db.py loads
env/database.env by relative path):

    cd /root/projects/vowerp3/vowerp3be
    uv run python -m src.common.whatsapp.send_test --tenant dev3 --to <phone>
        [--text "..."] [--party-id N] [--dry-run]

  --text      send this text message
  --party-id  send the Party Details PDF of this party (read from the tenant DB)
  --dry-run   send nothing: print the text and write the PDF to /tmp

With neither --text nor --party-id a default test text is sent. Attempts are
logged to whatsapp_message_log when the table exists in the tenant DB.
Exit code is 1 when a send failed.
"""

import argparse
import os
import sys

from src.common.whatsapp import client
from src.common.whatsapp import query as q
from src.common.whatsapp.party_pdf import PartyNotFoundError, build_party_details_pdf

DEFAULT_TEXT = "VoWERP WhatsApp test message."


def _log(db, **kwargs) -> None:
    """Best-effort: the log table may not exist on this tenant yet."""
    try:
        q.log_message(db, **kwargs)
        db.commit()
    except Exception as exc:
        db.rollback()
        print(f"  (not logged to whatsapp_message_log: {type(exc).__name__})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.common.whatsapp.send_test",
        description="Send a WhatsApp test text and/or a party-details PDF (run from the vowerp3be directory).",
    )
    parser.add_argument("--tenant", required=True, help="tenant DB name, e.g. dev3")
    parser.add_argument("--to", default=None, help="recipient phone (required unless --dry-run)")
    parser.add_argument("--text", default=None, help="text message to send")
    parser.add_argument("--party-id", type=int, default=None, help="send this party's details PDF")
    parser.add_argument("--dry-run", action="store_true", help="send nothing, write the PDF to /tmp")
    args = parser.parse_args(argv)

    if not args.to and not args.dry_run:
        parser.error("--to is required unless --dry-run")
    try:
        to = client.normalize_phone(args.to) if args.to else None
    except ValueError as exc:
        parser.error(str(exc))
    text_body = args.text if args.text is not None else (None if args.party_id else DEFAULT_TEXT)

    from src.config.db import Session, get_tenant_engine  # needs cwd = vowerp3be

    failed = False
    db = Session(get_tenant_engine(args.tenant), autoflush=False)
    try:
        if text_body:
            if args.dry_run:
                print(f"DRY RUN text -> {to or '(no --to)'}: {text_body}")
            else:
                try:
                    wa_id = client.message_id(client.send_text(to, text_body, db=db))
                    print(f"SENT text -> {to}: message id {wa_id}")
                    _log(db, phone_no=to, message_type="text", status="SENT", wa_message_id=wa_id)
                except client.WhatsAppConfigError as exc:
                    print(f"NOT SENT: {exc}")
                    return 1
                except client.WhatsAppApiError as exc:
                    failed = True
                    print(f"FAILED text -> {to}: {exc}")
                    _log(db, phone_no=to, message_type="text", status="FAILED", error_text=str(exc))

        if args.party_id:
            try:
                pdf_bytes, filename = build_party_details_pdf(db, args.party_id)
            except PartyNotFoundError as exc:
                print(f"ERROR: {exc} (tenant {args.tenant})")
                return 1
            if args.dry_run:
                path = os.path.join("/tmp", filename)
                with open(path, "wb") as fh:
                    fh.write(pdf_bytes)
                print(f"DRY RUN document -> {to or '(no --to)'}: wrote {path} ({len(pdf_bytes)} bytes)")
            else:
                try:
                    resp = client.send_document(to, pdf_bytes, filename, caption="Party Details", db=db)
                    wa_id = client.message_id(resp)
                    print(f"SENT document {filename} -> {to}: message id {wa_id}")
                    _log(db, phone_no=to, message_type="document", status="SENT",
                         file_name=filename, wa_message_id=wa_id)
                except client.WhatsAppConfigError as exc:
                    print(f"NOT SENT: {exc}")
                    return 1
                except client.WhatsAppApiError as exc:
                    failed = True
                    print(f"FAILED document -> {to}: {exc}")
                    _log(db, phone_no=to, message_type="document", status="FAILED",
                         file_name=filename, error_text=str(exc))
    finally:
        db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
