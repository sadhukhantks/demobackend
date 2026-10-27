# GST e-Invoice — Integration Notes (Backend)

Technical companion to [`SANDBOX_SETUP_GUIDE.md`](./SANDBOX_SETUP_GUIDE.md).
Covers our tables, endpoints, the API transport, and the INV-01 field mapping.

---

## 1. Design constraints (locked)

- **No changes to existing sales tables.** `sales_invoice` keeps its manual
  `irn`/`ack_no`/`ack_date`/`qr_code` columns and the `e_invoice_responses`
  audit table exactly as they are. The API integration lives in **new**
  tables connected by FK.
- Sandbox first, on **dev3**.

## 2. New tables (migration: `dbqueries/migrations/einvoice_integration_tables.sql`)

```
co_mst / branch_mst ◄── einv_gstin_config    credentials + endpoint config per (GSTIN, env, provider)
sales_invoice       ◄── einv_irn_registry    IRN lifecycle, UNIQUE(invoice_id, env)
einv_irn_registry   ◄── einv_api_log         every HTTP call (secrets redacted)
```

ORM models: `src/models/einvoice.py`. Statuses on `einv_irn_registry.einv_status`:
`PENDING → GENERATED → CANCELLED`, or `FAILED` (retryable — next generate
attempt reuses the row).

## 3. Module layout (`src/sales/einvoice/`)

| File | Purpose |
|------|---------|
| `constants.py` | envs, providers, statuses, INV-01 codes, provider endpoint defaults, `EINVOICE_*` env var names |
| `crypto_utils.py` | e-invoice standard crypto: RSA/PKCS1 (password/AppKey), AES-256/ECB/PKCS7 (SEK payloads) |
| `query.py` | read-only SQL: invoice header+parties, lines+GST, config lookup, registry/log reads |
| `payload_builder.py` | invoice → INV-01 v1.1 JSON + all validation errors in one list |
| `client.py` | `EInvoiceClient` — auth (token cached 6 h per (gstin, env, provider), thread-safe), generate/cancel/get IRN, api-log sink |
| `router.py` | portal endpoints under `/api/salesEInvoice` |

## 4. Endpoints (`/api/salesEInvoice`, portal persona, tenant DB)

| Method + path | Purpose |
|---|---|
| `GET /config?co_id=` | list credential configs (secrets masked) |
| `POST /config` | upsert credentials for (gstin, env, provider); masked `"***"` secrets are ignored on update |
| `POST /auth-test` | force a fresh IRP auth; body `{co_id, branch_id?, env?}` |
| `GET /payload-preview/{invoice_id}` | dry-run INV-01 build; returns `payload` + `errors[]` |
| `POST /generate/{invoice_id}` | build + submit; persists IRN/Ack/signed QR into `einv_irn_registry` |
| `POST /cancel/{invoice_id}` | cancel within 24 h; body `{env?, reason_code, remarks}` |
| `GET /status/{invoice_id}?env=` | registry row |
| `GET /logs/{invoice_id}` | last 100 API-log rows |

Guard rails: `PRODUCTION` generate requires invoice `status_id = 3` (Approved);
a second generate for the same (invoice, env) is rejected while `GENERATED`.

## 5. Transport (the e-invoice standard flow)

```
auth:      POST {base}{auth_path}
           headers: client_id, client_secret, gstin, user_name
           body:    {"Data": b64( {UserName, Password:RSA(pubkey), AppKey:RSA(pubkey),
                                    ForceRefreshAccessToken} )}
           resp:    Data.AuthToken (6 h), Data.Sek = AES(sek, AppKey)

core call: POST {base}{path}
           headers: + AuthToken
           body:    {"Data": AES-256-ECB-PKCS7(json, Sek) base64}
           resp:    Data (same AES) → {Irn, AckNo, AckDt, SignedInvoice,
                                       SignedQRCode, Status, EwbNo?...}
errors:    {"Status":"0"|..., "ErrorDetails":[{ErrorCode, ErrorMessage}]}
```

- ECB mode is **mandated by the IRP spec** — do not "fix" it to CBC/GCM.
- `encryption_mode=PLAIN` sends/receives plain JSON for IRP endpoints that
  accept it (confirm per provider in its console).
- 401/403 clears the cached token so the next call re-authenticates.
- Token refresh: within 10 min of expiry call auth with
  `ForceRefreshAccessToken=true` (we simply re-auth 15 min early).

Config resolution precedence: `EINVOICE_*` env var → `einv_gstin_config`
column → `constants.PROVIDER_DEFAULTS`. NIC sandbox defaults are prefilled;
IRIS values must come from its developer console.

## 6. INV-01 v1.1 field mapping (ERP → payload)

| INV-01 field | Source | Notes |
|---|---|---|
| `Version` | constant `"1.1"` | |
| `TranDtls.TaxSch` | `"GST"` | |
| `TranDtls.SupTyp` | `"B2B"` (today) | SEZ/export types constant-ready, not auto-detected yet |
| `DocDtls.Typ` | `"INV"` | CRN/DBN once credit/debit notes are wired |
| `DocDtls.No` | `sales_invoice.invoice_no` | ≤16 chars, `[a-zA-Z0-9/-]`, no leading `0 . -` — validated |
| `DocDtls.Dt` | `sales_invoice.invoice_date` | `dd/mm/yyyy` |
| `SellerDtls.Gstin` | `branch_mst.gst_no` | regex-validated |
| `SellerDtls.LglNm/Addr/Loc/Pin/Stcd` | `branch_mst` + `state_mst.state_code` | PIN must be 6 digits |
| `BuyerDtls.*` | `party_branch_mst` (via `billing_to_id`) + `party_mst.supp_name` + `state_mst` | `Pos` = buyer state code |
| `ShipDtls.*` | `party_branch_mst` (via `shipping_to_id`) | only when ship-to ≠ bill-to |
| `ItemList[].HsnCd` | `sales_invoice_dtl.hsn_code` | 4–8 digits — validated |
| `ItemList[].Qty/UnitPrice/AssAmt/Discount` | `sales_invoice_dtl` | 2-dec round-half-up |
| `ItemList[].Unit` | `uom_mst.uom_name` → UQC map | unmapped names pass through uppercased (sandbox will flag) |
| `ItemList[].GstRt/CgstAmt/SgstAmt/IgstAmt` | `sales_invoice_dtl_gst` | intra vs inter from `intra_inter_state` (1 = intra) |
| `ValDtls.*` | Σ lines + `sales_invoice.round_off` | `TotInvVal` = assessable + taxes + round-off |

Not yet mapped (extend `payload_builder.py` when needed): `PayDtls`,
`RefDtls`, `ExpDtls` (exports), `EwbDtls` (e-way bill with IRN), Discounts at
header level, `sales_invoice_additional` charges (currently **not** included in
`OthChrg` — flagged for the next iteration; sandbox totals validation will
catch any mismatch for invoices that carry additional charges).

## 7. Key GSTN validation rules to remember

- IRN = SHA-256 hash of `SellerGstin + FY + DocTyp + DocNo` — uniqueness is per
  seller+FY+type+number; a duplicate returns error **2150** with the existing IRN.
- Amount tolerance ±₹1 on totals; 2-decimal rounding everywhere.
- Cancellation window: **24 hours** from IRN generation (after that: credit note).
- Reporting window: 30 days from document date.
- Documents older than 180 days can't get an e-way bill.
- `Pin` 999999 + `Stcd` "96" conventions apply to exports (`ExpDtls`) — not
  wired yet.

## 8. Testing

- `src/test/test_sales_einvoice.py` — payload builder (mapping + validations),
  crypto round-trip, client behavior with mocked HTTP, router validation.
- Terminal probe (no ERP/browser needed): `tools/einvoice_sandbox_probe.py`
  — auth-test against any base URL with creds passed as flags/env vars.
- Full-flow QA on dev3 once creds exist: `/qa-portal-page` on the sales
  invoice page + the endpoints in §4.

## 9. IRIS sandbox account facts (confirmed 2026-08-27)

- Portal: https://sandbox.einvoice6.gst.gov.in/login — login `app@vowerp.com`.
- Portal backend API host: `api.sandbox.portal.irisirp.com` (`/portal/user/login`,
  `/portal/auth/tokenvalidate`). This is the PORTAL API, not the IRN API base.
- Account: "VOW ERP Solutions Pvt. Ltd.", userType `API_INTEGRATOR`,
  GSTIN `29AAICV4142M1ZT` (type INTERMEDIARY), status `TEST_GSTIN_ADDED`.
- Gotcha: an **expired portal password** makes the login UI show a bare
  "Authentication failed" — but `/portal/user/login` actually returns
  `status:1` with `passwordExpired:"true"`; the follow-up `tokenvalidate`
  401s. Check the network tab before assuming wrong creds.
- IP whitelisting: required for API access; office/dev public IP as of
  2026-08-27 was `106.219.132.45`.
