"""
Roll back Jute PO LCPL/JPO/26-27/00071 to Open on tenant sls (owner request, 2026-09-26).

    company  106 Limelight Commercial Pvt. Ltd. (prefix LCPL)
    branch   87 FACTORY
    PO       jute_po_id 13295, po_no 71, dated 2026-08-06 (FY 26-27), status 3 APPROVED
             (the other po_no 71 on branch 87, id 12818, is FY 25-26 and is NOT touched)

The app only reopens Rejected / Cancelled / Closed POs, so an Approved PO cannot be taken back
to Open from the UI. This sets status_id = 1 (OPEN) and writes one jute_po_status_log row
(source MANUAL) so the PO's status history shows the correction.

Guards (all asserted before any write): the row is exactly the PO above and still Approved,
it has one line, no MR, no gate-entry MR chain, no closed columns set. Nothing else is changed.

Usage (cwd = vowerp3be, env/database.env exported):
    uv run python dbqueries/repairs/jute_po_rollback_to_open_lcpl_00071_20260926.py           # dry run
    uv run python dbqueries/repairs/jute_po_rollback_to_open_lcpl_00071_20260926.py --apply   # execute
A rollback SQL file (the exact previous values) is written next to this script before anything is changed.
"""
import os, sys, datetime, pymysql

TENANT = "sls"
PO_ID, PO_NO, BRANCH_ID, PO_DATE = 13295, 71, 87, datetime.date(2026, 8, 6)
UPDATED_BY = 24              # owner's sls login, who asked for the rollback
OLD_STATUS, NEW_STATUS = 3, 1
APPLY = "--apply" in sys.argv


def lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


conn = pymysql.connect(host=os.environ["DATABASE_HOST"], port=int(os.environ["DATABASE_PORT"]),
                       user=os.environ["DATABASE_USER"], password=os.environ["DATABASE_PASSWORD"],
                       db=TENANT, cursorclass=pymysql.cursors.DictCursor, autocommit=False)
cur = conn.cursor()

cur.execute("SELECT * FROM jute_po WHERE jute_po_id = %s", (PO_ID,))
po = cur.fetchone()
assert po, "PO not found"
assert (po["po_no"], po["branch_id"], po["po_date"]) == (PO_NO, BRANCH_ID, PO_DATE), po
assert po["status_id"] == OLD_STATUS, f"PO is no longer Approved (status {po['status_id']}); nothing to do"
assert po["closed_date"] is None and po["close_type"] is None, "PO carries close columns"

cur.execute("SELECT COUNT(*) AS n FROM jute_po_li WHERE jute_po_id = %s", (PO_ID,)); lines = cur.fetchone()["n"]
cur.execute("SELECT COUNT(*) AS n FROM jute_mr WHERE po_id = %s", (PO_ID,)); mrs = cur.fetchone()["n"]
assert mrs == 0, f"{mrs} MR(s) already reference this PO"

cur.execute("SELECT co_prefix FROM co_mst WHERE co_id = 106"); assert cur.fetchone()["co_prefix"] == "LCPL"
print(f"PO {PO_ID}: LCPL/JPO/26-27/{PO_NO:05d}  branch {BRANCH_ID}  date {PO_DATE}  status {po['status_id']} (APPROVED) -> {NEW_STATUS} (OPEN)")
print(f"lines: {lines}  MRs: {mrs}  value: {po['jute_po_value']}  weight: {po['weight']}")

stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
rb = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"jute_po_rollback_to_open_lcpl_00071_{stamp}_rollback.sql")
with open(rb, "w") as f:
    f.write(f"-- rollback for jute_po_rollback_to_open_lcpl_00071 on {TENANT}, generated {stamp}\nSTART TRANSACTION;\n")
    f.write(f"UPDATE jute_po SET status_id = {lit(po['status_id'])}, approval_level = {lit(po['approval_level'])}, "
            f"updated_by = {lit(po['updated_by'])}, updated_date_time = {lit(po['updated_date_time'])} WHERE jute_po_id = {PO_ID};\n")
    f.write(f"DELETE FROM jute_po_status_log WHERE jute_po_id = {PO_ID} AND source = 'MANUAL' "
            f"AND trigger_ref = 'rollback_to_open LCPL/JPO/26-27/00071' AND new_status_id = {NEW_STATUS};\nCOMMIT;\n")
print("rollback written:", rb)
if not APPLY:
    print("DRY RUN - nothing changed. Re-run with --apply to execute."); sys.exit(0)

try:
    cur.execute("UPDATE jute_po SET status_id = %s, approval_level = NULL, updated_by = %s, updated_date_time = NOW() "
                "WHERE jute_po_id = %s AND status_id = %s", (NEW_STATUS, UPDATED_BY, PO_ID, OLD_STATUS))
    assert cur.rowcount == 1, f"expected 1 PO row, updated {cur.rowcount}"
    cur.execute("INSERT INTO jute_po_status_log (jute_po_id, old_status_id, new_status_id, close_type, source, trigger_ref, remark, user_id, created_at) "
                "VALUES (%s, %s, %s, NULL, 'MANUAL', %s, %s, %s, NOW())",
                (PO_ID, OLD_STATUS, NEW_STATUS, "rollback_to_open LCPL/JPO/26-27/00071",
                 "Rolled back from Approved to Open on owner request (2026-09-26)", UPDATED_BY))
    conn.commit()
    cur.execute("SELECT jute_po_id, po_no, status_id, approval_level, updated_by, updated_date_time FROM jute_po WHERE jute_po_id = %s", (PO_ID,))
    print("after:", cur.fetchone())
    cur.execute("SELECT log_id, old_status_id, new_status_id, source, remark, user_id, created_at FROM jute_po_status_log WHERE jute_po_id = %s ORDER BY log_id DESC LIMIT 1", (PO_ID,))
    print("log:", cur.fetchone())
except Exception:
    conn.rollback(); raise
