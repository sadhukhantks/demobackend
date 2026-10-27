# Invoice Type System

Reference documentation for the VoWERP3 invoice type architecture across Sales Order (SO), Delivery Order (DO), and Sales Invoice (SI).

---

## 1. Invoice Type Master

Types are defined in `invoice_type_mst` and mapped to companies via `invoice_type_co_map`.

| ID | Name | Description |
|----|------|-------------|
| 1 | Regular | Standard invoice, no extension tables |
| 2 | Hessian | Bale-based jute cloth products (qty in bales, rate per MT/bale) |
| 3 | Jute Yarn | Yarn products with PCSO/container tracking |
| 4 | Jute Invoice | Raw/processed jute with claims, MR references, mukam |
| 5 | Govt SKG | Government/SKG orders with PCSO, admin office, rail head, weights |

**Company mapping:** `invoice_type_co_map` links types to companies with an `active` flag. A company only sees types mapped to it.

---

## 2. Extension Table Architecture

Each document type (SI, SO) maintains **independent** extension tables per invoice type. This is critical because:
- An invoice can be created without a SO/DO reference
- Extension data may differ between the order and the final invoice
- Each document is independently editable

### Pattern

```
Document Header (e.g., sales_order)
  ├── Type Extension Header (e.g., sales_order_jute)        — 1:1 with header
  │     FK: sales_order_id
  ├── Type Extension Detail (e.g., sales_order_jute_dtl)     — 1:1 with each line item
  │     FK: sales_order_dtl_id
  └── Additional Charges (e.g., sales_order_additional)       — 1:N with header
        FK: sales_order_id
        └── GST (sales_order_additional_gst)                  — 1:1 with each charge
```

### Table Map

| Type | Sales Order Tables | Sales Invoice Tables | Delivery Order |
|------|-------------------|---------------------|----------------|
| Hessian (2) | `sales_order_dtl_hessian` | `sales_invoice_hessian` + `_dtl` | No tables (stores `invoice_type` only) |
| Jute Yarn (3) | `sales_order_juteyarn` | `sales_invoice_juteyarn` + `_dtl` | Shows SO data read-only |
| Jute (4) | `sales_order_jute` + `_dtl` | `sales_invoice_jute` + `_dtl` | Shows SO data read-only |
| Govt SKG (5) | `sales_order_govtskg` + `_dtl` | `sales_invoice_govtskg` + `_dtl` | Shows SO data read-only |

**DO behavior:** Stores `invoice_type` column but has no extension tables. When a DO is linked to a SO, the DO's get-by-id endpoint fetches the SO's extension data and returns it as `soExtensionData` for read-only display.

---

## 3. Per-Type Field Reference

### Regular (type 1)
No extension fields.

### Hessian (type 2)

| Level | Field | Type | Description |
|-------|-------|------|-------------|
| Detail | `qty_bales` | Double | Quantity in bales (user input) |
| Detail | `rate_per_bale` | Double | Rate per bale (computed) |
| Detail | `billing_rate_mt` | Double | Billing rate per MT after brokerage (computed) |
| Detail | `billing_rate_bale` | Double | Billing rate per bale after brokerage (computed) |

**Calculations:**
- `billing_rate_mt = raw_rate_mt - (raw_rate_mt * brokerage_pct / 100)`
- `rate_per_bale = raw_rate_mt / conversion_factor`
- `billing_rate_bale = billing_rate_mt / conversion_factor`
- Main `quantity` field stores MT value: `qty_bales / conversion_factor`

### Jute Yarn (type 3)

| Level | Field | Type | Description |
|-------|-------|------|-------------|
| Header | `pcso_no` | VARCHAR(100) | Purchase/Contract/Sales Order number |
| Header | `container_no` | VARCHAR(100) | Container number |
| Header | `customer_ref_no` | VARCHAR(100) | Customer reference number |

Detail table exists as placeholder (no custom fields currently).

### Jute Invoice (type 4)

| Level | Field | Type | Description |
|-------|-------|------|-------------|
| Header | `mr_no` | VARCHAR(50) | Material Receipt number |
| Header | `mr_id` | BIGINT | FK to jute MR |
| Header | `claim_amount` | DECIMAL(12,2) | Total claim amount |
| Header | `claim_description` | VARCHAR(255) | Claim description |
| Header | `mukam_id` | INT | FK to `jute_mukam_mst` |
| Header | `other_reference` | VARCHAR(100) | Other reference |
| Header | `unit_conversion` | VARCHAR(50) | Unit conversion note |
| Detail | `claim_amount_dtl` | Double | Per-line claim amount |
| Detail | `claim_desc` | VARCHAR(255) | Per-line claim description |
| Detail | `claim_rate` | Double | Claim rate per unit |
| Detail | `unit_conversion` | VARCHAR(255) | Unit conversion per line |
| Detail | `qty_untit_conversion` | INT | Quantity after conversion (typo preserved from production) |

**Special behavior:** For raw jute sub-types, `claim_amount` is auto-summed from line items' `claim_amount_dtl`.

### Govt SKG (type 5)

| Level | Field | Type | Description |
|-------|-------|------|-------------|
| Header | `pcso_no` | VARCHAR(100) | PCSO number (e.g., PBMF181125EIC58713) |
| Header | `pcso_date` | DATE | PCSO date |
| Header | `administrative_office_address` | VARCHAR(500) | Admin office address |
| Header | `destination_rail_head` | VARCHAR(100) | Destination rail head |
| Header | `loading_point` | VARCHAR(100) | Loading point |
| Detail | `pack_sheet` | Double | Pack sheet per line |
| Detail | `net_weight` | Double | Net weight (MT) per line |
| Detail | `total_weight` | Double | Total weight per line |

**Physical invoice format:** Quantity in BALES, rate PER 100 BAGS, Net wt in MT. Additional charges (Printing, Handling) use the structured additional charges section.

---

## 4. Additional Charges System

Structured mechanism for surcharges like Printing, Handling, Loading, Insurance, etc.

### Master Table
`additional_charges_mst` — shared with procurement module.
- `additional_charges_id` (PK)
- `additional_charges_name` (e.g., "Printing Charge", "2nd Handling Charge")
- `default_value` — default tax percentage
- `active` — soft delete flag

### Per-Document Tables

| Document | Charges Table | GST Table |
|----------|--------------|-----------|
| Sales Order | `sales_order_additional` | `sales_order_additional_gst` |
| Sales Invoice | `sales_invoice_additional` | `sales_invoice_additional_gst` |
| Purchase Order | `proc_po_additional` | `po_gst` |
| Procurement Inward | `proc_inward_additional` | (inline) |

Each charge row: `additional_charges_id` (FK to master) + `qty` + `rate` + `net_amount` + `remarks`.
Each charge row has paired GST: IGST/CGST/SGST amounts and percentages.

### Totals Calculation
```
net_amount = line_items_total + line_items_gst + additional_charges_total + additional_charges_gst + freight + round_off
```

---

## 5. Document Flow & Type Propagation

```
Quotation (no type)
    └─→ Sales Order (invoice_type + own extensions + additional charges)
            ├─→ Delivery Order (invoice_type stored, SO extensions shown read-only, no own extensions)
            └─→ Sales Invoice (invoice_type + own extensions + additional charges, independent)
```

- **SO** sets the invoice type at order creation
- **DO** inherits type from linked SO (auto-populated on SO selection); stores it independently
- **SI** maintains its own type + extension data; can be set independently of SO/DO
- Each document's extension data is independent — no cross-references between SO and SI extensions

---

## 6. Backend Patterns

### Payload Structure
Type-specific data is sent as nested objects:
```json
{
  "branch": "1",
  "invoice_type": "5",
  "items": [
    {
      "item": "10", "quantity": "48", "rate": "7458.81",
      "govtskg_dtl": { "pack_sheet": 1.5, "net_weight": 13.92, "total_weight": 14.1 }
    }
  ],
  "govtskg": {
    "pcso_no": "PBMF181125EIC58713",
    "pcso_date": "2025-11-18",
    "administrative_office_address": "...",
    "destination_rail_head": "GONIANA",
    "loading_point": "Titagarh"
  },
  "additional_charges": [
    { "additional_charges_id": 1, "qty": 1, "rate": 12000, "net_amount": 12000, "remarks": "Printing" }
  ]
}
```

### Create/Update Pattern
```python
# 1. Insert/update header
# 2. Insert line items (with per-line extension data)
# 3. Insert header-level extensions (jute, govtskg, juteyarn)
# 4. Insert additional charges + GST
# On update: delete old extensions/charges BEFORE re-inserting
```

### Get-by-id Pattern
```python
# 1. Load header
# 2. Check invoice_type
# 3. Conditionally load extension header + detail map
# 4. Load additional charges
# 5. Build response with extension data per line + header
```

### Query Naming Convention
```
insert_sales_order_{type}()          — INSERT header extension
delete_sales_order_{type}()          — DELETE header extension by sales_order_id
get_sales_order_{type}_by_id()       — SELECT header extension
insert_sales_order_{type}_dtl()      — INSERT detail extension per line
delete_sales_order_{type}_dtl()      — DELETE detail extensions via JOIN
get_sales_order_{type}_dtl_by_order_id() — SELECT all detail extensions
```

---

## 7. Frontend Patterns

### Constants & Helpers
Each document type defines constants:
```typescript
export const REGULAR_TYPE_ID = "1";
export const HESSIAN_TYPE_ID = "2";
export const JUTE_YARN_TYPE_ID = "3";
export const JUTE_TYPE_ID = "4";
export const GOVT_SKG_TYPE_ID = "5";

export const isJuteInvoice = (id?: string | null) => String(id) === JUTE_TYPE_ID;
export const isGovtSkgInvoice = (id?: string | null) => String(id) === GOVT_SKG_TYPE_ID;
```

### Conditional Section Rendering
```tsx
{isJuteOrder(invoiceTypeId) && <MuiForm schema={juteSchema} ... />}
{isGovtSkgOrder(invoiceTypeId) && <MuiForm schema={govtskgSchema} ... />}
{isJuteYarnOrder(invoiceTypeId) && <MuiForm schema={juteyarnSchema} ... />}
```

### Conditional Line Item Columns
```tsx
const isHessian = invoiceTypeId === "2";
const displayValue = isHessian ? (item.qtyBales ?? "") : item.quantity;
```

### Form Submission
```typescript
// Header-level extension:
if (isJuteOrder(typeId)) payload.jute = { mr_no, mukam_id, claim_amount, claim_description };

// Per-line extension:
govtskg_dtl: isGovtSkgOrder(typeId) ? { pack_sheet, net_weight, total_weight } : undefined,
```

---

## 8. Key Files

### Backend
| File | Purpose |
|------|---------|
| `src/models/sales.py` | ORM models (source of truth for schema) |
| `src/sales/query.py` | All SQL query functions |
| `src/sales/salesOrder.py` | SO endpoints |
| `src/sales/deliveryOrder.py` | DO endpoints |
| `src/sales/salesInvoice.py` | SI endpoints |
| `src/sales/constants.py` | Status IDs, document type prefixes |
| `src/models/mst.py` | `AdditionalChargesMst` model |

### Frontend
| File | Purpose |
|------|---------|
| `.../salesInvoice/.../utils/salesInvoiceConstants.ts` | SI type constants + helpers |
| `.../salesOrder/.../utils/salesOrderConstants.ts` | SO type constants + helpers |
| `.../salesInvoice/.../hooks/useSalesInvoiceFormSchemas.ts` | SI jute header schema |
| `.../salesOrder/.../components/SalesOrderLineItemsTable.tsx` | SO line items with hessian handling |
| `.../salesOrder/.../utils/hessianCalculations.ts` | Hessian computation utilities |

---

## 9. Known Production Typos (DO NOT RENAME)

| Column | Table | Should Be |
|--------|-------|-----------|
| `qty_untit_conversion` | `sales_invoice_jute_dtl`, `sales_order_jute_dtl` | `qty_unit_conversion` |
| `sale_invoice_govtskg_dtl` | (table name) | `sales_invoice_govtskg_dtl` |
| `sale_invoice_govtskg_id` | `sales_invoice_govtskg` (PK) | `sales_invoice_govtskg_id` |

These exist in production. Match them exactly in code and queries.
