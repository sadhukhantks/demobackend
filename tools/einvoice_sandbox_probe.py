"""
Standalone sandbox connectivity probe for the e-invoice IRP APIs.

Runs the auth call (and optionally a get-IRN) straight from the terminal —
no ERP, no DB, no browser. Use it the moment the IRP developer console hands
you sandbox credentials, before wiring anything into a tenant.

Examples:
    # NIC sandbox (public key downloaded from einv-apisandbox.nic.in)
    python tools/einvoice_sandbox_probe.py \
        --provider NIC --env SANDBOX \
        --gstin 01AAAAB1234C1Z0 --client-id X --client-secret Y \
        --username tester --password 'Abc@123' \
        --public-key-file ./nic_sandbox_pubkey.pem

    # IRIS sandbox (base URL + paths from the developer console)
    python tools/einvoice_sandbox_probe.py \
        --provider IRIS --env SANDBOX \
        --base-url https://<from-console> --auth-path /<from-console> \
        --gstin <dummy-gstin> --client-id X --client-secret Y \
        --username tester --password 'Abc@123' \
        --public-key-file ./iris_pubkey.pem

Credentials may also come from the EINVOICE_* env vars (see
src/sales/einvoice/constants.py). Docs: docs/einvoice/SANDBOX_SETUP_GUIDE.md
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sales.einvoice.client import EInvoiceClient, EInvoiceError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="e-invoice IRP sandbox connectivity probe")
    parser.add_argument("--provider", default="IRIS", choices=["IRIS", "NIC"])
    parser.add_argument("--env", default="SANDBOX", choices=["SANDBOX", "PRODUCTION"])
    parser.add_argument("--base-url", default=None, help="override IRP API base URL")
    parser.add_argument("--auth-path", default=None, help="override auth endpoint path")
    parser.add_argument("--gstin", default=None)
    parser.add_argument("--client-id", default=None)
    parser.add_argument("--client-secret", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--public-key-file", default=None, help="IRP RSA public key PEM file (NIC mode)")
    parser.add_argument("--encryption-mode", default="NIC", choices=["NIC", "PLAIN"])
    parser.add_argument("--get-irn", default=None, metavar="IRN", help="after auth, fetch this IRN")
    args = parser.parse_args()

    public_key_pem = None
    if args.public_key_file:
        public_key_pem = Path(args.public_key_file).read_text(encoding="utf-8")

    config = {
        "irp_provider": args.provider,
        "env": args.env,
        "api_base_url": args.base_url,
        "auth_path": args.auth_path,
        "gstin": args.gstin,
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "api_username": args.username,
        "api_password": args.password,
        "encryption_mode": args.encryption_mode,
        "public_key_pem": public_key_pem,
    }

    def print_log(entry: dict):
        print(f"  [{entry['api_name']}] http={entry.get('http_status')} "
              f"success={entry.get('success')} {entry.get('error_message') or ''}")

    client = EInvoiceClient(config, log_sink=print_log)
    print(f"Probing {args.provider} {args.env} -> {client.base_url or '(no base URL configured!)'}")
    try:
        auth = client.authenticate(force=True)
        print("AUTH OK — token acquired" + (", SEK received" if auth.get("sek") else ", no SEK (plain mode?)"))
    except (EInvoiceError, RuntimeError) as exc:
        print(f"AUTH FAILED: {exc}")
        return 1

    if args.get_irn:
        try:
            details = client.get_irn(args.get_irn)
            print(json.dumps(details, indent=2)[:4000])
        except (EInvoiceError, RuntimeError) as exc:
            print(f"GET-IRN FAILED: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
