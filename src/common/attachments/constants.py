"""Constants & allow-lists for the generic attachment subsystem.

Keep these tight — the router uses them to validate user-supplied
`module` / `file_type` form fields and to map (module, file_type) to a
source-table column that should track the active attachment_id.
"""

# Images are converted server-side to PDF before hitting S3 (see router._to_pdf_bytes).
IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_MIME = {"application/pdf"} | IMAGE_MIME
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_MODULES = {
    "billpass",
    "proc_billpass",
    "jute_prod_followup",
    "jute_prod_absent_letter",
    "jute_prod_absent_receipt",
    "jute_prod_absent_event",
}

# Allowed file_types per module. Used to validate the multipart form input.
ALLOWED_FILE_TYPES = {
    # "billpass" module == jute_mr row: slots are shared by gate entry, QC, MR and bill pass pages.
    "billpass": {"invoice", "challan", "weightslip"},
    "proc_billpass": {"invoice", "ewaybill"},
    # Low Performer Follow-up: entity_id = jute_prod_followup_event.followup_event_id
    # (a NOTICE row's scanned letter). No source-table column — the event list
    # joins file_attachment instead.
    "jute_prod_followup": {"notice"},
    # Long Absenteeism (gate: long_absenteeism_letters.check_absent_attachment_access).
    # Letter scans (entity = jute_prod_absent_letter.letter_id), the union's signed
    # copy of the gate notice (entity = jute_prod_absent_union_receipt.receipt_id) and
    # a worker's reply / medical papers on a timeline entry (entity =
    # jute_prod_absent_event.event_id). No source-table columns.
    "jute_prod_absent_letter": {"signed_copy", "ad_card"},
    "jute_prod_absent_receipt": {"union_receipt"},
    "jute_prod_absent_event": {"reply", "medical", "other"},
}

# Maps (module, file_type) → (source_table, primary_key_column, attachment_column).
# The router uses this allow-list to safely build the UPDATE / NULL-ing SQL
# that writes the new attachment_id back to the source table (e.g.
# jute_mr.invoice_upload / jute_mr.challan_upload).
#
# NEVER interpolate user input into SQL — only column names looked up via
# this dict are eligible for the dynamic UPDATE in query.py.
MODULE_COLUMN_MAP = {
    ("billpass", "invoice"): ("jute_mr", "jute_mr_id", "invoice_upload"),
    ("billpass", "challan"): ("jute_mr", "jute_mr_id", "challan_upload"),
}
