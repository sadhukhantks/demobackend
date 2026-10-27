# GST e-Invoice — Sandbox Registration & Setup Guide (Developer Runbook)

Step-by-step notes to get from zero to a working **sandbox** IRN generation
call. Target IRP: **IRIS IRP — einvoice6.gst.gov.in** (GSTN-authorized, run by
IRIS Business Services). The NIC sandbox is documented as an alternative at the
end.

> Companion doc: [`INTEGRATION_NOTES.md`](./INTEGRATION_NOTES.md) — API
> mechanics, payload mapping, our tables/endpoints.
> Everything below that carries a link was taken from the official IRIS IRP
> knowledge base; open the links in a browser for screenshots.

---

## 0. Background — what and why

- Under the e-invoice mandate, businesses with **AATO > ₹5 crore** must report
  B2B/SEZ/export invoices (INV/CRN/DBN) to an IRP, which returns an **IRN**
  (64-char hash), an **acknowledgement number/date** and a **signed QR code**
  that must be printed on the invoice.
- Since 2023 invoices must be reported **within 30 days** of the document date
  (enforced for larger taxpayers; treat it as the rule).
- There are 6 GSTN-authorized IRPs: `einvoice1`/`einvoice2` (NIC),
  `einvoice3` (Cygnet), `einvoice4` (Clear), `einvoice5` (EY),
  **`einvoice6` (IRIS)** — all accept the same INV-01 schema; credentials and
  onboarding differ per portal.
- IRIS IRP's **essential APIs (auth, generate IRN, cancel IRN, get IRN) are
  free**; value-added APIs are paid. Sandbox testing is a **precondition** for
  production API access.

## 1. What you need before starting

| Item | Notes |
|------|-------|
| GSTIN of the seller company/branch | For sandbox you can use IRIS-generated **dummy GSTINs** — no real GSTIN needed to start |
| Email + Indian mobile of the person registering | OTP verification (OTP valid 10 min) |
| Decision: user type | **API Integrator** (that's us — an ERP/solution provider) or Taxpayer direct |

## 2. Register on the IRIS IRP portal

1. Open <https://einvoice6.gst.gov.in/login> → **Sign Up**.
2. Choose user type. For VoW ERP integration choose **API Integrator**
   (taxpayers who want to self-integrate pick *Taxpayer*; both paths exist).
   Reference: [API Integrator Registration](https://einvoice6.gst.gov.in/content/kb/api-integrator-registration/).
3. Verify the 6-digit OTP sent to the email/mobile (valid 10 minutes).
4. Complete the profile. Onboarding reference:
   [User Onboarding](https://einvoice6.gst.gov.in/content/kb/user-onboarding/).

## 3. Get Sandbox access

Reference: [Access to Sandbox](https://einvoice6.gst.gov.in/content/kb/access-to-sandbox/)
and [Manage Sandbox Access](https://einvoice6.gst.gov.in/content/kb/manage-sandbox-access/).

1. In the portal, open the **Sandbox** section and request access. You will be
   asked for:
   - user type (**Taxpayer / Intermediary / E-commerce Operator** — pick
     Intermediary for API-integrator style access, or per instruction on the
     form),
   - the GSTIN(s) for which API integration is to be done.
2. **Create a test GSTIN**: select taxpayer type (*Regular* or *SEZ
   Developer*) and a state — IRIS **auto-generates a dummy GSTIN** for you.
3. Set the **IRP credentials** for that test GSTIN
   ([Set IRP Credentials](https://einvoice6.gst.gov.in/content/kb/set-irp-credentials/)):
   - **username**: unique, max **15 chars**;
   - **password**: max **8 chars**, needs ≥1 capital, ≥1 special char, ≥1 number.
4. From **Sandbox → Test GSTINs** you can view/manage the dummy GSTINs.
5. Collect the **API credentials** from the developer console
   ([API Credentials](https://einvoice6.gst.gov.in/content/kb/api-credentials/)):
   - **client_id** and **client_secret** (client_secret can be **regenerated**
     from Manage Sandbox Access at any time);
   - the **sandbox API base URL** and per-API endpoint paths shown in the
     console ([API List](https://einvoice6.gst.gov.in/content/kb/api-list/),
     [Overview of Core APIs](https://einvoice6.gst.gov.in/content/kb/overview-of-core-apis/));
   - the **IRP public key** if shown (needed for the encrypted auth flow).

> ⚠️ The exact sandbox hostname + endpoint paths are displayed **inside** the
> developer console after signup — they are not published on the public pages
> we could crawl. That is why our config stores them per GSTIN
> (`einv_gstin_config.api_base_url` + `*_path` columns) instead of hardcoding.

## 4. Wire the credentials into VoW ERP

Everything is per-tenant; run against **dev3** first.

1. Run the migration (creates `einv_gstin_config`, `einv_irn_registry`,
   `einv_api_log`; **no existing sales table is altered**):
   use the `run-migration` skill with
   `dbqueries/migrations/einvoice_integration_tables.sql`, target **dev3**.
2. `pip install -r requirements.txt` (new dependency: `cryptography`).
3. Save the credentials (secrets go to the DB row; for production prefer the
   `EINVOICE_*` env vars which override the row):

   ```bash
   curl -X POST http://localhost:8000/api/salesEInvoice/config \
     -H 'Content-Type: application/json' -b "access_token=<jwt>" \
     -d '{
       "co_id": 1,
       "branch_id": 4,
       "gstin": "<dummy GSTIN from sandbox>",
       "irp_provider": "IRIS",
       "env": "SANDBOX",
       "api_base_url": "<sandbox base URL from developer console>",
       "auth_path": "<auth endpoint path from console>",
       "generate_irn_path": "<generate endpoint path>",
       "cancel_irn_path": "<cancel endpoint path>",
       "get_irn_path": "<get-by-IRN path, keep {irn} placeholder>",
       "encryption_mode": "NIC",
       "client_id": "<client id>",
       "client_secret": "<client secret>",
       "api_username": "<IRP username, max 15 chars>",
       "api_password": "<IRP password, max 8 chars>",
       "public_key_pem": "-----BEGIN PUBLIC KEY-----\n..."
     }'
   ```

4. **Smoke-test auth** (this is the "are we connected" moment):

   ```bash
   curl -X POST http://localhost:8000/api/salesEInvoice/auth-test \
     -H 'Content-Type: application/json' -b "access_token=<jwt>" \
     -d '{"co_id": 1, "branch_id": 4, "env": "SANDBOX"}'
   ```

   Every attempt (success or failure) is logged in `einv_api_log` with secrets
   redacted — check there when debugging.

5. **Preview the payload** for a real dev3 invoice (dry run, no IRP call):

   ```
   GET /api/salesEInvoice/payload-preview/{invoice_id}
   ```

   Fix every item in `errors` (missing GSTINs on `branch_mst`/
   `party_branch_mst`, missing `state_mst.state_code`, non-6-digit PINs,
   missing HSN on lines, invoice-number format) before submitting.

6. **Generate the IRN** on sandbox:

   ```
   POST /api/salesEInvoice/generate/{invoice_id}   body: {"env": "SANDBOX"}
   ```

   Then `GET /api/salesEInvoice/status/{invoice_id}` and
   `GET /api/salesEInvoice/logs/{invoice_id}`.

7. Cancel within 24 h if needed:

   ```
   POST /api/salesEInvoice/cancel/{invoice_id}
   body: {"env": "SANDBOX", "reason_code": "2", "remarks": "test cancel"}
   ```

   Reason codes: 1 duplicate, 2 data-entry mistake, 3 order cancelled, 4 others.

There is also a terminal probe that skips the ERP entirely —
`tools/einvoice_sandbox_probe.py` (run `python tools/einvoice_sandbox_probe.py --help`).

## 5. Moving to production (later)

1. Complete the sandbox test cases; per IRIS policy, **production access is
   granted after sandbox integration + submission of test cases**
   ([Manage Production Access](https://einvoice6.gst.gov.in/content/kb/manage-production-access/)).
2. The taxpayer's **real GSTIN** must be registered/enabled for e-invoicing;
   AATO is verified during enablement.
3. Create a second `einv_gstin_config` row with `env=PRODUCTION` and the
   production base URL/credentials. Keep secrets in `EINVOICE_CLIENT_SECRET` /
   `EINVOICE_API_PASSWORD` env vars rather than the DB for production.
4. Note: `POST /generate` refuses non-Approved (status ≠ 3) invoices when
   `env=PRODUCTION`; sandbox allows any status for testing.

## 6. Alternative: NIC sandbox (einv-apisandbox.nic.in)

The NIC developer sandbox is fully public and our client's defaults already
point at it (`irp_provider=NIC`, `env=SANDBOX`):

1. Register via the **Login → Register** tab on <https://einv-apisandbox.nic.in>.
2. Credentials model: **client_id/client_secret per PAN**, plus a
   **username/password created per GSTIN**.
3. Download the **sandbox public key** from the portal (needed for RSA
   encryption of password/AppKey during auth).
4. Endpoints (publicly documented):
   - `POST /eivital/v1.04/auth`
   - `POST /eicore/v1.03/Invoice` (generate IRN)
   - `POST /eicore/v1.03/Invoice/Cancel`
   - `GET  /eicore/v1.03/Invoice/irn/{irn}`
5. Auth-token validity is **60 min on sandbox** (360 min in production).
6. Direct **production** NIC API access is restricted (historically AATO > ₹100
   crore; smaller taxpayers go via GSP/ASP or use IRIS/other IRPs — which is
   exactly why IRIS IRP is our primary target).

## 7. Known gaps / to confirm in the developer console

- [ ] Exact IRIS sandbox base URL + endpoint paths (console-only) → fill into
      `einv_gstin_config`.
- [ ] Whether IRIS core APIs on sandbox require the full NIC-style RSA/AES
      encryption (`encryption_mode=NIC`) or accept plain JSON
      (`encryption_mode=PLAIN`) — their KB references the standard SEK flow,
      so `NIC` is the default; flip per config row if the console says
      otherwise.
- [ ] IRIS auth request/response exact field names (we handle both
      `Data/AuthToken/Sek` PascalCase and camelCase).
- [ ] Header names for core calls (`client_id`, `client_secret`, `gstin`,
      `user_name`, `AuthToken` — per e-invoice standard; adjust in
      `client.py::_http_headers` if the console differs).
- [ ] E-way-bill-by-IRN endpoint (not wired yet; `ewb_no` columns are ready).

### Primary sources

- IRIS IRP mandate page: <https://einvoice6.gst.gov.in/content/einvoice-mandate/>
- IRIS IRP developer portal: <https://einvoice6.gst.gov.in/content/api-integration/>
- IRIS IRP KB (sandbox/auth/API list): linked inline above
- GSTN "API details of all 6 IRPs" PDF: <https://tutorial.gst.gov.in/downloads/news/e-invoice_api_integration_guide_irps.pdf>
- NIC sandbox portal: <https://einv-apisandbox.nic.in>
