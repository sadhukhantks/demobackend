"""
Shared jute bill-pass financial computations.

Owner decision (2026-07): jute bill-pass net_total MUST deduct TDS.
    net_pre   = total_amount - claim_amount - tds_amount
    roundoff  = round(net_pre) - net_pre          (makes net a whole rupee)
    net_total = net_pre + roundoff

Used by:
    - src/juteProcurement/mr.py        (final-approval amount calculation)
    - src/juteProcurement/billPass.py  (server-side recompute on bill-pass update)
    - src/accounting/posting_service.py (defensive recompute before posting)
"""
from decimal import Decimal, ROUND_HALF_UP


def compute_jute_totals(total_amount, claim_amount, tds_amount):
    """
    Compute (roundoff, net_total) for a jute MR / bill pass, deducting TDS.

    Args:
        total_amount: gross material value (sum of line values)
        claim_amount: total claims deducted from the supplier bill
        tds_amount: TDS deducted at source

    Returns:
        tuple (roundoff, net_total) — both rounded to 2 decimal places;
        net_total is always a whole rupee (roundoff absorbs the paise).
    """
    net_pre = round(
        float(total_amount or 0) - float(claim_amount or 0) - float(tds_amount or 0), 2
    )
    roundoff = round(round(net_pre) - net_pre, 2)
    net_total = round(net_pre + roundoff, 2)
    return roundoff, net_total


def round_kg(value) -> int:
    """Jute procurement stock is kept in WHOLE KG (owner rule).

    Half-up (2.5 -> 3, matching the frontend's Math.round), never banker's
    rounding, and always an int so FLOAT columns get an exact value. Every
    writer of jute_mr_li.actual_weight / accepted_weight and jute_issue.weight
    goes through here -- do not add a second rounding rule beside it.
    """
    if value is None:
        return 0
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def sync_mr_weight_from_lines(db, mr_id: int, *, only_if_blank: bool = True) -> None:
    """Make jute_mr.mr_weight (the header's "MR Weight") follow the MR's active lines.

    mr_weight = SUM(COALESCE(accepted_weight, actual_weight, 0)) over active lines —
    the same weight basis the MR money maths uses (mr.py calculate_mr_amounts).
    Only MR save (update_jute_mr) used to write it, so an MR that went QC -> gate-out
    -> Pending/Approved without a Save kept NULL/0 and the MR list showed "-"
    (TKT-000207).

    only_if_blank=True (default) fills a NULL/0 header and never overwrites a stored
    value, so a deliberate MR-save override or a legacy header in another unit is
    safe. Pass False when the lines were just rewritten in the same call.
    A header whose lines sum to 0 is left alone. A single UPDATE, no fetch.
    """
    from sqlalchemy import text  # local: keep this module import-light for repair scripts

    blank_guard = "AND COALESCE(jm.mr_weight, 0) = 0" if only_if_blank else ""
    db.execute(text(f"""
        UPDATE jute_mr jm
        JOIN (
            SELECT jute_mr_id, SUM(COALESCE(accepted_weight, actual_weight, 0)) AS line_kg
            FROM jute_mr_li
            WHERE jute_mr_id = :mr_id AND (active = 1 OR active IS NULL)
            GROUP BY jute_mr_id
        ) s ON s.jute_mr_id = jm.jute_mr_id
        SET jm.mr_weight = ROUND(s.line_kg)
        WHERE jm.jute_mr_id = :mr_id AND s.line_kg > 0 {blank_guard}
    """), {"mr_id": int(mr_id)})


def plan_mr_line_weights(header_kg, lines: list[dict]) -> list[dict]:
    """Split the header net (jute_mr.actual_weight) over the MR lines by
    actual_qty — whole kg, largest-remainder so the split sums to the header
    exactly — then moisture-deduct each line the way mr.py update_mr does.

    lines: dicts with jute_mr_li_id, actual_qty, allowable_moisture,
    actual_moisture. Returns one dict per line (same order) with actual_weight,
    shortage_kgs, accepted_weight. Zero-qty lines get 0.
    """
    total = round_kg(header_kg)
    qtys = [float(line.get("actual_qty") or 0) for line in lines]
    qty_sum = sum(q for q in qtys if q > 0)
    raw = [total * q / qty_sum if q > 0 and qty_sum else 0.0 for q in qtys]
    weights = [int(r) for r in raw]
    remainder = total - sum(weights) if qty_sum else 0
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - weights[i], reverse=True):
        if remainder <= 0:
            break
        if qtys[i] > 0:
            weights[i] += 1
            remainder -= 1

    plan = []
    for line, w in zip(lines, weights):
        try:
            actual_moisture = float(line.get("actual_moisture") or 0)
        except (TypeError, ValueError):
            actual_moisture = 0.0
        allowable = line.get("allowable_moisture")
        deduction = actual_moisture - float(allowable) if allowable is not None and actual_moisture > float(allowable) else 0.0
        shortage = round_kg(w * deduction / 100.0) if w > 0 and deduction > 0 else 0
        plan.append({
            "jute_mr_li_id": line["jute_mr_li_id"],
            "actual_weight": w,
            "shortage_kgs": shortage,
            "accepted_weight": max(0, w - shortage),
        })
    return plan


def redistribute_mr_line_weights(db, mr_id: int) -> int:
    """Gate-out weighed the lorry: push jute_mr.actual_weight down to the active
    lines (QC saved before gate-out leaves them at 0 kg, invisible to the issue
    picker by weight). No-op while the header has no weight. Returns lines written.
    Also re-syncs the header mr_weight to the new line weights.
    Money (total_price / header totals) is NOT recomputed here — MR save / approve
    own that via recompute_mr_money."""
    from sqlalchemy import text  # local: keep this module import-light for repair scripts

    row = db.execute(
        text("SELECT actual_weight FROM jute_mr WHERE jute_mr_id = :mr_id"), {"mr_id": int(mr_id)}
    ).fetchone()
    header_kg = float(row._mapping.get("actual_weight") or 0) if row else 0.0
    if header_kg <= 0:
        return 0
    lines = [dict(r._mapping) for r in db.execute(text("""
        SELECT jute_mr_li_id, actual_qty, allowable_moisture, actual_moisture
        FROM jute_mr_li
        WHERE jute_mr_id = :mr_id AND active = 1
    """), {"mr_id": int(mr_id)}).fetchall()]
    for p in plan_mr_line_weights(header_kg, lines):
        db.execute(text("""
            UPDATE jute_mr_li
            SET actual_weight = :actual_weight, shortage_kgs = :shortage_kgs, accepted_weight = :accepted_weight
            WHERE jute_mr_li_id = :jute_mr_li_id
        """), p)
    # The lines were just rewritten, so the header MR Weight must follow them
    # (it stayed NULL/0 for QC -> gate-out MRs; TKT-000207).
    sync_mr_weight_from_lines(db, mr_id, only_if_blank=False)
    return len(lines)
