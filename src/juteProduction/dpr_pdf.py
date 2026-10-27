"""Daily Production Report (DPR) — server-side PDF.

Renders the same payload the DPR page shows (`build_dpr_data` in
dpr_reports.py) as an A4 PDF with reportlab, with no browser involved, so a
scheduled job can send the sheet out (e.g. over WhatsApp).

The layout mirrors the frontend page
(`vowerp3ui/.../juteProduction/report/dailyProductionReport/page.tsx`): same
section order, column headings, units and rounding. When the page changes,
change this file with it.

Public API:
    build_dpr_pdf(db, co_id, branch_id, report_d) -> (pdf_bytes, filename)
    render_dpr_pdf(data, company_name, branch_name) -> pdf_bytes   (pure, no DB)
"""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Optional
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    CondPageBreak,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.juteProduction.bale_reports_query import get_branch_name_query
from src.juteProduction.dpr_reports import build_dpr_data

DASH = "—"  # em dash — what the page shows for a missing value

PAGE_SIZE = A4
MARGIN = 9 * mm
FRAME_PAD = 6  # platypus Frame's default inner padding (pt)
CONTENT_W = PAGE_SIZE[0] - 2 * MARGIN
PAIR_GAP = 4 * mm

# KPI thresholds — same as page.tsx.
UTIL_GOOD_PCT = 80
UTIL_WARN_PCT = 50
JUTE_COVER_GOOD_DAYS = 30
JUTE_COVER_WARN_DAYS = 10

C_HEAD_BG = colors.HexColor("#E3E8EF")
C_TOTAL_BG = colors.HexColor("#F1F3F6")
C_BAND_BG = colors.HexColor("#E9ECEF")
C_GRID = colors.HexColor("#9AA3AE")
C_MUTED = colors.HexColor("#555B63")
C_GOOD = colors.HexColor("#1B7A34")
C_WARN = colors.HexColor("#B26A00")
C_BAD = colors.HexColor("#C62828")
STATE_COLORS = {"good": C_GOOD, "warn": C_WARN, "bad": C_BAD}

FONT = "Helvetica"
FONT_B = "Helvetica-Bold"
CELL_PT = 7.5

ST_CELL = ParagraphStyle("dpr_cell", fontName=FONT, fontSize=CELL_PT, leading=CELL_PT + 1.5)
ST_CELL_B = ParagraphStyle("dpr_cell_b", parent=ST_CELL, fontName=FONT_B)
ST_HEAD = ParagraphStyle("dpr_head", parent=ST_CELL_B)
ST_HEAD_R = ParagraphStyle("dpr_head_r", parent=ST_CELL_B, alignment=TA_RIGHT)
ST_COMPANY = ParagraphStyle(
    "dpr_company", fontName=FONT_B, fontSize=12, leading=14, alignment=TA_CENTER
)
ST_TITLE = ParagraphStyle(
    "dpr_title", fontName=FONT_B, fontSize=10.5, leading=13, alignment=TA_CENTER
)
ST_META = ParagraphStyle(
    "dpr_meta", fontName=FONT, fontSize=8, leading=10, alignment=TA_CENTER
)
ST_SECTION = ParagraphStyle(
    "dpr_section", fontName=FONT_B, fontSize=9, leading=11, spaceBefore=0, spaceAfter=0
)
ST_SUB = ParagraphStyle(
    "dpr_sub", fontName=FONT, fontSize=6.5, leading=8, textColor=C_MUTED, spaceAfter=1.5
)
ST_NOTE = ParagraphStyle(
    "dpr_note", fontName=FONT, fontSize=6.5, leading=8, textColor=C_MUTED, spaceBefore=1.5
)
ST_SUBHEAD = ParagraphStyle(
    "dpr_subhead", fontName=FONT_B, fontSize=8, leading=10, spaceBefore=4, spaceAfter=1.5
)
ST_KPI_LABEL = ParagraphStyle(
    "dpr_kpi_label", fontName=FONT, fontSize=6.5, leading=8, textColor=C_MUTED
)
ST_KPI_VALUE = ParagraphStyle("dpr_kpi_value", fontName=FONT_B, fontSize=11, leading=13)
ST_KPI_SUB = ParagraphStyle(
    "dpr_kpi_sub", fontName=FONT, fontSize=6, leading=7.2, textColor=C_MUTED
)

# Characters outside the standard-font (WinAnsi) set that the payload can carry.
_CHAR_FALLBACK = {
    "−": "-",
    "↔": "<->",
    "→": "->",
    "←": "<-",
    "≥": ">=",
    "≤": "<=",
    "₹": "Rs.",
}


# ---------------------------------------------------------------------------
# Formatting helpers (mirror DprPieces.fmt — en-IN grouping, "—" for missing)
# ---------------------------------------------------------------------------


def _safe(value: Any) -> str:
    """Text the standard PDF fonts can draw: anything outside cp1252 is mapped
    to an ASCII fallback (or '?') instead of rendering as a black box."""
    s = "" if value is None else str(value)
    out = []
    for ch in s:
        if ch in _CHAR_FALLBACK:
            out.append(_CHAR_FALLBACK[ch])
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def _p(value: Any, style: ParagraphStyle = ST_CELL) -> Paragraph:
    return Paragraph(escape(_safe(value)), style)


def _num(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _group_indian(int_digits: str) -> str:
    """'1234567' -> '12,34,567' (en-IN grouping: last three, then twos)."""
    if len(int_digits) <= 3:
        return int_digits
    head, tail = int_digits[:-3], int_digits[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def fmt(v: Any, digits: int = 2) -> str:
    """Same output as the page's fmt(): fixed decimals, Indian grouping, and an
    em dash for None / NaN / non-numeric."""
    d = _num(v)
    if d is None:
        return DASH
    q = d.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    sign = "-" if q < 0 else ""
    int_part, _, frac = f"{abs(q):f}".partition(".")
    body = _group_indian(int_part)
    if digits > 0:
        body = f"{body}.{frac.ljust(digits, '0')[:digits]}"
    # No "-0.00".
    if sign and not any(c in "123456789" for c in body):
        sign = ""
    return sign + body


def _kg_to_mt(kg: Any) -> Optional[float]:
    d = _num(kg)
    return None if d is None else float(d) / 1000.0


def _iso_ddmmyyyy(iso: Any) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(iso or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else str(iso or "")


def _iso_ddmm(iso: Any) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(iso or ""))
    return f"{m.group(3)}-{m.group(2)}" if m else str(iso or "")


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _first(*values: Any) -> Any:
    """JS `a ?? b ?? c`."""
    for v in values:
        if v is not None:
            return v
    return None


def _util_state(pct: Any) -> Optional[str]:
    d = _num(pct)
    if d is None:
        return None
    return "good" if d >= UTIL_GOOD_PCT else "warn" if d >= UTIL_WARN_PCT else "bad"


def _days_stock_state(days: Any) -> Optional[str]:
    d = _num(days)
    if d is None:
        return None
    return "good" if d >= JUTE_COVER_GOOD_DAYS else "warn" if d >= JUTE_COVER_WARN_DAYS else "bad"


def _tone_color(value: Any, tone: str):
    """NumTd tone: 'pos-bad' | 'neg-bad' | 'nonzero-bad' -> text colour or None."""
    d = _num(value)
    if d is None or d == 0:
        return None
    if tone == "nonzero-bad":
        bad = True
    elif tone == "pos-bad":
        bad = d > 0
    else:
        bad = d < 0
    return C_BAD if bad else C_GOOD


# ---------------------------------------------------------------------------
# Table / section building blocks
# ---------------------------------------------------------------------------


def _table(
    head: list,
    rows: list,
    col_fracs: list,
    width: float,
    *,
    left_cols: tuple = (0,),
    bold_rows: tuple = (),
    band_rows: tuple = (),
    text_colors: Optional[list] = None,
) -> Table:
    """Grid table with a repeating header row.

    rows: lists of plain strings (already formatted). Row indexes in
    bold_rows / band_rows / text_colors are 0-based over `rows` (header
    excluded). band_rows are full-width label rows (first cell spans).
    """
    ncols = len(head)
    left = set(left_cols)
    cell_colors = {(ri, ci): c for ri, ci, c in (text_colors or []) if c is not None}
    data = [
        [_p(h, ST_HEAD if i in left else ST_HEAD_R) for i, h in enumerate(head)]
    ]
    bold = set(bold_rows)
    band = set(band_rows)
    for ri, row in enumerate(rows):
        cells = []
        for ci in range(ncols):
            val = row[ci] if ci < len(row) else ""
            if ci in left or ri in band:
                st = ST_CELL_B if (ri in bold or ri in band) else ST_CELL
                # TEXTCOLOR table styles do not reach Paragraph cells.
                if (ri, ci) in cell_colors:
                    st = ParagraphStyle("dpr_cell_tone", parent=st, textColor=cell_colors[(ri, ci)])
                cells.append(_p(val, st))
            else:
                cells.append(_safe(val))
        data.append(cells)

    total_frac = float(sum(col_fracs)) or 1.0
    col_widths = [width * f / total_frac for f in col_fracs]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, -1), FONT),
        ("FONTSIZE", (0, 0), (-1, -1), CELL_PT),
        ("LEADING", (0, 0), (-1, -1), CELL_PT + 1.5),
        ("GRID", (0, 0), (-1, -1), 0.4, C_GRID),
        ("BACKGROUND", (0, 0), (-1, 0), C_HEAD_BG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    for ci in left:
        style.append(("ALIGN", (ci, 0), (ci, -1), "LEFT"))
    for ri in bold:
        style.append(("FONTNAME", (0, ri + 1), (-1, ri + 1), FONT_B))
        style.append(("BACKGROUND", (0, ri + 1), (-1, ri + 1), C_TOTAL_BG))
    for ri in band:
        style.append(("SPAN", (0, ri + 1), (-1, ri + 1)))
        style.append(("BACKGROUND", (0, ri + 1), (-1, ri + 1), C_BAND_BG))
    for (ri, ci), color in cell_colors.items():
        style.append(("TEXTCOLOR", (ci, ri + 1), (ci, ri + 1), color))
    t.setStyle(TableStyle(style))
    return t


def _section(title: str, subtitle: Optional[str], body: list) -> list:
    out = [_p(title, ST_SECTION)]
    if subtitle:
        out.append(_p(subtitle, ST_SUB))
    else:
        out.append(Spacer(1, 1.5))
    out.extend(body)
    return out


def _pair(left: Optional[list], right: Optional[list]) -> list:
    """Two small sections side by side (reading order left then right)."""
    half = (CONTENT_W - PAIR_GAP) / 2.0
    t = Table(
        [[left or "", "", right or ""]],
        colWidths=[half, PAIR_GAP, half],
    )
    t.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return [t]


# ---------------------------------------------------------------------------
# Sections (same order, headings, digits as page.tsx)
# ---------------------------------------------------------------------------


def _jute_basis_text(basis: dict) -> str:
    if basis.get("mode") == "average":
        days = basis.get("days")
        return (
            f"{fmt(basis.get('production_kg'), 0)} kg/day avg spinning, last {days} "
            f"producing day{'' if days == 1 else 's'} "
            f"({_iso_ddmm(basis.get('date_from'))} to {_iso_ddmm(basis.get('date_to'))})"
        )
    return (
        f"{fmt(basis.get('production_kg'), 0)} kg spinning on "
        f"{_iso_ddmmyyyy(basis.get('date'))}"
    )


PART_DAY_NOTE = "report date was a holiday / part day"


def _jute_waste_split_text(jute: dict) -> str:
    waste = _num(jute.get("waste_closing_weight"))
    if waste is None or not waste > 0:
        return ""
    return (
        f"Jute {fmt(jute.get('raw_jute_closing_weight'), 0)} + "
        f"Waste {fmt(jute.get('waste_closing_weight'), 0)} kg"
    )


def _jute_cover_kpi_sub(jute: Optional[dict]) -> str:
    if not jute:
        return "select a branch"
    split = _jute_waste_split_text(jute)
    basis = _d(jute.get("cover_basis"))
    if jute.get("days_stock") is None or not basis:
        return " · ".join(x for x in (split, "no recent spinning production") if x)
    cover = (
        f"{fmt(jute.get('days_stock'), 1)} days cover · ÷ {_jute_basis_text(basis)}"
        f"{'' if basis.get('is_report_date') else f' {DASH} {PART_DAY_NOTE}'}"
    )
    return " · ".join(x for x in (split, cover) if x)


def _jute_cover_subtitle(jute: dict) -> str:
    basis = _d(jute.get("cover_basis"))
    if jute.get("days_stock") is None or not basis:
        return f"Days of cover: {DASH} (no spinning production in the look-back window)"
    parts = [
        f"Days of cover: {fmt(jute.get('days_stock'), 1)} = closing "
        f"{fmt(_d(jute.get('totals')).get('closing_weight'), 0)} kg ÷ {_jute_basis_text(basis)}"
    ]
    if not basis.get("is_report_date"):
        parts.append(PART_DAY_NOTE)
    waste = _num(jute.get("waste_closing_weight"))
    if waste is not None and waste > 0 and jute.get("days_stock_raw_jute") is not None:
        waste_days = (
            f" ({fmt(jute.get('days_stock_waste'), 1)} days)"
            if jute.get("days_stock_waste") is not None
            else ""
        )
        parts.append(
            f"of which jute stock {fmt(jute.get('raw_jute_closing_weight'), 0)} kg "
            f"({fmt(jute.get('days_stock_raw_jute'), 1)} days) + waste stock "
            f"{fmt(jute.get('waste_closing_weight'), 0)} kg{waste_days}"
        )
    return " · ".join(parts)


def _kpi_strip(data: dict) -> list:
    kpi = _d(data.get("kpi"))
    spinning = _d(data.get("spinning"))
    sp_total = _d(spinning.get("total"))
    shifts_worked = _d(data.get("shifts_worked"))
    emulsion = data.get("emulsion") if isinstance(data.get("emulsion"), dict) else None
    jute = data.get("jute") if isinstance(data.get("jute"), dict) else None

    cards: list = []  # (label, value, sub, state)
    cards.append(
        (
            "Spinning (MT)",
            fmt(kpi.get("spinning_mt"), 3),
            f"{fmt(sp_total.get('frame_runs'), 0)} frames run · MTD "
            f"{fmt(kpi.get('spinning_mtd_mt'), 1)} MT",
            None,
        )
    )
    if kpi.get("winding_kg") is None:
        winding_sub = "select a branch"
    else:
        winding_sub = f"MTD {fmt(_kg_to_mt(kpi.get('winding_mtd_kg')), 1)} MT"
        if kpi.get("twisting_kg"):
            winding_sub += f" · twisting {fmt(_kg_to_mt(kpi.get('twisting_kg')), 3)} MT"
    cards.append(("Winding (MT)", fmt(_kg_to_mt(kpi.get("winding_kg")), 3), winding_sub, None))
    cards.append(
        (
            "Weaving (MT)",
            fmt(kpi.get("weaving_mt"), 3),
            f"MTD {fmt(kpi.get('weaving_mtd_mt'), 1)} MT",
            None,
        )
    )
    day_shifts = [str(s) for s in _l(shifts_worked.get("day_shifts"))]
    cards.append(
        (
            "Shifts Worked",
            fmt(kpi.get("shifts_worked"), 0),
            f"MTD {fmt(kpi.get('shifts_worked_mtd'), 0)}"
            + (f" · {', '.join(day_shifts)}" if day_shifts else ""),
            None,
        )
    )
    # TKT-000185: per MT of cloth = yarn stage + cloth stage; the fallback
    # (no departments resolvable) is the old all-hands ÷ spinning figure.
    if kpi.get("hands_stage_basis"):
        hands_sub = (
            f"yarn {fmt(kpi.get('hands_per_mt_yarn'), 1)} + "
            f"cloth {fmt(kpi.get('hands_per_mt_cloth'), 1)} · all hands ÷ spinning "
            f"{fmt(kpi.get('hands_per_mt_spinning_basis'), 1)}"
        )
    else:
        hands_sub = (
            f"{fmt(kpi.get('total_hands'), 0)} hands ÷ "
            f"{fmt(kpi.get('spinning_mt'), 3)} MT spinning"
        )
    cards.append(
        (
            "Hands / MT",
            fmt(kpi.get("hands_per_mt"), 1),
            f"{hands_sub} · MTD {fmt(kpi.get('hands_per_mt_mtd'), 1)}",
            None,
        )
    )
    cards.append(
        (
            "Jute Closing Stock",
            fmt(kpi.get("jute_closing_weight"), 0),
            _jute_cover_kpi_sub(jute),
            _days_stock_state(kpi.get("jute_days_stock")),
        )
    )
    cards.append(
        (
            "Frame Utilization",
            f"{fmt(kpi.get('frame_utilization_pct'), 1)}%",
            f"Doff eff {fmt(kpi.get('spinning_eff_pct'), 1)}%",
            _util_state(kpi.get("frame_utilization_pct")),
        )
    )
    avg = spinning.get("avg_count") if isinstance(spinning.get("avg_count"), dict) else None
    if avg:
        value, sub = avg_count_kpi(avg)
        cards.append(("Avg Count (lb)", value, sub, None))
    cards.append(
        (
            "Loom Utilization",
            f"{fmt(kpi.get('loom_utilization_pct'), 1)}%",
            f"Weaving eff {fmt(kpi.get('weaving_eff_pct'), 1)}%",
            _util_state(kpi.get("loom_utilization_pct")),
        )
    )
    em_day = _d(emulsion.get("day")) if emulsion else {}
    if em_day:
        status = em_day.get("status")
        cards.append(
            (
                "JBO / Oil %",
                f"{fmt(kpi.get('jbo_pct'), 2)}%",
                f"std {fmt(em_day.get('std_low'), 0)}–{fmt(em_day.get('std_high'), 0)}%",
                None if status is None else ("good" if status == "OK" else "warn"),
            )
        )

    per_row = 5
    cell_w = CONTENT_W / per_row
    grid_rows = []
    style = [
        ("BOX", (0, 0), (-1, -1), 0.4, C_GRID),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, C_GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for start in range(0, len(cards), per_row):
        row = []
        for label, value, sub, state in cards[start : start + per_row]:
            value_style = ST_KPI_VALUE
            if state in STATE_COLORS:
                value_style = ParagraphStyle(
                    f"dpr_kpi_value_{state}", parent=ST_KPI_VALUE, textColor=STATE_COLORS[state]
                )
            row.append([_p(label, ST_KPI_LABEL), _p(value, value_style), _p(sub, ST_KPI_SUB)])
        row.extend([""] * (per_row - len(row)))
        grid_rows.append(row)
    t = Table(grid_rows, colWidths=[cell_w] * per_row)
    t.setStyle(TableStyle(style))
    return [t]


def _spinning_shift_section(spinning: dict, width: float) -> list:
    total = _d(spinning.get("total"))
    mtd = _d(spinning.get("mtd"))
    rows = []
    for s in _l(spinning.get("shifts")):
        s = _d(s)
        rows.append(
            [
                s.get("shift") or "",
                fmt(s.get("production_mt"), 3),
                fmt(s.get("frame_runs"), 0),
                fmt(s.get("kg_per_frame"), 1),
                fmt(s.get("utilization_pct"), 1),
                fmt(s.get("eff_pct"), 1),
            ]
        )
    total_idx = len(rows)
    rows.append(
        [
            "O/A",
            fmt(total.get("production_mt"), 3),
            fmt(total.get("frame_runs"), 0),
            fmt(total.get("kg_per_frame"), 1),
            fmt(total.get("utilization_pct"), 1),
            fmt(total.get("eff_pct"), 1),
        ]
    )
    rows.append(
        [
            "MTD",
            fmt(mtd.get("production_mt"), 3),
            fmt(mtd.get("frame_runs"), 0),
            fmt(mtd.get("kg_per_frame"), 1),
            DASH,
            DASH,
        ]
    )
    return _section(
        f"Spinning Production {DASH} Shift Wise",
        f"{fmt(spinning.get('total_frames'), 0)} installed frames · MTD "
        f"{fmt(mtd.get('production_mt'), 1)} MT",
        [
            _table(
                ["Shift", "Production (MT)", "Frames Run", "Kg/Frame", "Utilization %", "Eff % (vs Std)"],
                rows,
                [1.2, 1.4, 1.2, 1.2, 1.2, 1.3],
                width,
                bold_rows=(total_idx,),
            )
        ],
    )


def _missing_note(missing: list, kind: str, where: str) -> Optional[Paragraph]:
    if not missing:
        return None
    n = len(missing)
    return _p(
        f"Eff % is blank for {n} qualit{'y' if n == 1 else 'ies'} with no {kind}: "
        f"{', '.join(str(m) for m in missing[:6])}{' …' if n > 6 else ''}. "
        f"Set them in {where} to see efficiency.",
        ST_NOTE,
    )


def _names(names: list, limit: int = 6) -> str:
    return f"{', '.join(str(n) for n in names[:limit])}{' …' if len(names) > limit else ''}"


def spinning_missing_lines(spinning: dict) -> list[str]:
    """Plain-text explanation of blank Target/Frame-Shift and Eff % cells in the
    spinning quality table (TKT-000234) — mirrors the page's footnote: a
    headline count, then one line per missing input with where to set it."""
    lines = []
    tgt = _l(spinning.get("targets_missing"))
    eff = _l(spinning.get("standards_missing"))
    head = []
    if tgt:
        head.append(f"Target not shown for {len(tgt)} qualit{'y' if len(tgt) == 1 else 'ies'}")
    if eff:
        head.append(f"Eff % not shown for {len(eff)} qualit{'y' if len(eff) == 1 else 'ies'}")
    if head:
        lines.append("; ".join(head) + ".")
    for m in _l(spinning.get("missing_summary")):
        m = _d(m)
        qs = _l(m.get("qualities"))
        if m.get("code") == "target_eff_fallback":
            n = len(qs)
            lines.append(
                f"Target uses Standard Eff % for {n} qualit{'y' if n == 1 else 'ies'} "
                f"(Target Eff % not set): {_names(qs)} {DASH} set in {m.get('where')}."
            )
            continue
        lines.append(f"{m.get('label')} {DASH} {len(qs)}: {_names(qs)}. Set in {m.get('where')}.")
    return lines


AVG_COUNT_KEYS = ("by_frames", "by_production", "by_std_production")


def _has_sqc_count(avg: dict) -> bool:
    """True when the payload carries the TKT-000201 actual-count figures and at
    least one quality had an SQC count reading for the day."""
    return "act_by_production" in avg and (_num(avg.get("act_measured_kg")) or 0) > 0


def avg_count_kpi(avg: dict) -> tuple[str, str]:
    """Value + caption of the "Avg Count (lb)" KPI card — mirrors the page's
    avgCountKpi(). Headline = the SQC actual count on the three weightings
    (frames / production / std production, TKT-000201); the std-count figures
    ride in the caption. With no SQC reading for the day the card falls back to
    the std-count figures and says so, rather than presenting std as actual."""
    std_vals = " / ".join(fmt(avg.get(k), 2) for k in AVG_COUNT_KEYS)
    if not _has_sqc_count(avg):
        no_std = " (std not entered)" if avg.get("by_std_production") is None else ""
        if "act_by_production" in avg:
            return std_vals, f"std count · frames / prod / std{no_std} · no SQC count entered"
        return std_vals, f"frames / production / std{no_std}"
    act_vals = " / ".join(fmt(avg.get(f"act_{k}"), 2) for k in AVG_COUNT_KEYS)
    sub = f"SQC actual (frames / prod / std) · std count {std_vals}"
    fallback = _l(avg.get("act_fallback"))
    if fallback:
        sub += f" · {len(fallback)} w/o SQC"
    return act_vals, sub


def avg_count_std_basis(avg: dict) -> str:
    """What the std-production-weighted row was averaged over — or why it is blank."""
    if avg.get("by_std_production") is None and avg.get("act_by_std_production") is None:
        return "no std production (std TPI / efficiency or SQC count missing)"
    text_ = f"{fmt(avg.get('std_production_kg'), 0)} kg std expected"
    sq, q = _num(avg.get("std_qualities")), _num(avg.get("qualities"))
    if sq is not None and q is not None and sq < q:
        text_ += f" · {avg.get('std_qualities')} of {avg.get('qualities')} qualities have std production"
    return text_


def avg_count_note(avg: dict) -> str:
    """Caption under the Average Count table — mirrors the page's avgCountNote()."""
    has_act = "act_by_production" in avg
    if has_act:
        basis = (
            "day's average moisture-corrected count entered in Spinning SQC"
            if avg.get("count_basis") == "corrected"
            else "day's average count entered in Spinning SQC"
        )
        note = (
            "Std count = the quality's count on the yarn master. Actual count = the "
            f"{basis} (the Act Count column above). "
            "Each is weighted by the frame-shifts the quality ran, by the kg it actually "
            "produced, and by the kg its std speed / TPI / efficiency expected from those "
            "frames."
        )
        prod = _num(avg.get("production_kg")) or 0
        measured = _num(avg.get("act_measured_kg")) or 0
        if measured <= 0:
            note += (
                " No SQC count was entered for the day, so the actual column repeats "
                "the std count."
            )
        elif measured < prod:
            note += (
                f" SQC count covers {fmt(measured, 0)} of {fmt(prod, 0)} kg."
            )
        fallback = _l(avg.get("act_fallback"))
        if fallback and measured > 0:
            note += (
                " No SQC count for the day, so the std count stands in the actual "
                f"column: {', '.join(str(c) for c in fallback[:6])}"
                f"{' …' if len(fallback) > 6 else ''}."
            )
    else:
        note = (
            "Each quality's standard count, weighted by the frame-shifts it ran, by the kg it "
            "actually produced, and by the kg its std speed / TPI / efficiency expected from "
            "those frames."
        )
    sq, q = _num(avg.get("std_qualities")), _num(avg.get("qualities"))
    if avg.get("by_std_production") is None or (sq is not None and q is not None and sq < q):
        note += (
            " Qualities with no std production (std TPI / efficiency not entered in "
            "Spinning Target Map, or no SQC count for the day) are left out of the third "
            f"row, not counted as zero {DASH} hence its kg can differ from the others."
        )
    count_missing = _l(avg.get("count_missing"))
    if count_missing:
        note += (
            " No count on the yarn master, so left out of the std column: "
            f"{', '.join(str(c) for c in count_missing[:6])}"
            f"{' …' if len(count_missing) > 6 else ''}."
        )
    return note


def _avg_count_block(avg: dict, width: float) -> list:
    has_act = "act_by_production" in avg

    def row(label: str, key: str, basis: str) -> list:
        cells = [label, fmt(avg.get(key))]
        if has_act:
            cells.append(fmt(avg.get(f"act_{key}")))
        return cells + [basis]

    rows = [
        row("Frames run", "by_frames", f"{fmt(avg.get('frame_shifts'), 0)} frame-shifts"),
        row("Actual production", "by_production", f"{fmt(avg.get('production_kg'), 0)} kg doffed"),
        row("Std production (std parameters)", "by_std_production", avg_count_std_basis(avg)),
    ]
    if has_act:
        head = ["Weighted By", "Std Count (lb)", "SQC Actual Count (lb)", "Weighted Over"]
        col_w = [2.0, 1.1, 1.4, 3.0]
        left = (0, 3)
    else:
        head = ["Basis", "Average Count (lb)", "Weighted Over"]
        col_w = [2.2, 1.3, 3.5]
        left = (0, 2)
    return [
        _p("Average Count", ST_SUBHEAD),
        _table(head, rows, col_w, width, left_cols=left),
        _p(avg_count_note(avg), ST_NOTE),
    ]


def _spinning_quality_section(spinning: dict, width: float) -> list:
    rows = []
    for q in _l(spinning.get("quality")):
        q = _d(q)
        rows.append(
            [
                _first(q.get("item_name"), q.get("item_code"), q.get("item_id"), ""),
                fmt(q.get("act_count")),
                fmt(q.get("std_count")),
                fmt(q.get("production_kg"), 0),
                fmt(q.get("kg_per_frame_shift"), 1),
                fmt(q.get("target_per_frame_shift"), 1),
                fmt(q.get("winding_kg"), 0),
                fmt(q.get("eff_pct"), 1),
            ]
        )
    body: list = [
        _table(
            [
                "Quality",
                "Act Count (lb)",
                "Std Count (lb)",
                "Spg Prod (kg)",
                "Kg/Frame-Shift",
                "Target/Frame-Shift",
                "Winding (kg)",
                "Eff %",
            ],
            rows,
            [2.2, 1.1, 1.1, 1.1, 1.2, 1.5, 1.1, 0.8],
            width,
        )
    ]
    summary = _l(spinning.get("missing_summary"))
    if summary:
        # TKT-000234: name every missing input behind a blank Target / Eff %.
        body.extend(_p(line, ST_NOTE) for line in spinning_missing_lines(spinning))
    else:
        note = _missing_note(
            _l(spinning.get("standards_missing")),
            "spinning standard configured (TPI / efficiency)",
            "Spinning Target Map",
        )
        if note:
            body.append(note)
    avg = spinning.get("avg_count")
    if isinstance(avg, dict) and avg:
        body.extend(_avg_count_block(avg, width))
    return _section(
        f"Spinning / Winder {DASH} Quality Wise",
        "Act count is production-weighted; winding kg is the reconciled winder output "
        "allocated to the quality",
        body,
    )


def _winding_section(winding: dict, width: float) -> list:
    total = _d(winding.get("total"))
    qualities = [_d(q) for q in _l(winding.get("qualities"))]
    wind_rows = [q for q in qualities if q.get("is_twisted") != 1]
    twist_rows = [q for q in qualities if q.get("is_twisted") == 1]
    has_twisting = bool(twist_rows)

    def quality_row(q: dict) -> list:
        return [
            _first(q.get("item_name"), q.get("item_code"), "Unmapped"),
            fmt(q.get("production_kg"), 0),
            fmt(q.get("winders"), 0),
            fmt(q.get("kg_per_winder"), 1),
            fmt(q.get("mtd_production_kg"), 0),
            fmt(q.get("mtd_kg_per_winder_shift"), 1),
        ]

    def total_row(label: str, t: dict) -> list:
        return [
            label,
            fmt(t.get("production_kg"), 0),
            fmt(t.get("winders"), 0),
            fmt(t.get("kg_per_winder"), 1),
            fmt(t.get("mtd_production_kg"), 0),
            DASH,
        ]

    rows = [quality_row(q) for q in wind_rows]
    bold = [len(rows)]
    band = []
    rows.append(total_row("Total Winding (excl. twisting)" if has_twisting else "Total", total))
    if has_twisting:
        twisting = _d(total.get("twisting"))
        band.append(len(rows))
        rows.append([f"Twisting Production {DASH} ply yarn, not part of the winding total"])
        rows.extend(quality_row(q) for q in twist_rows)
        bold.append(len(rows))
        rows.append(total_row("Total Twisting", twisting))
        rows.append(total_row("Gross winder output (winding + twisting)", _d(total.get("gross"))))

    body: list = [
        _table(
            ["Quality", "Production (kg)", "Winders", "Kg/Winder", "MTD (kg)", "MTD Kg/Winder-Shift"],
            rows,
            [3.0, 1.3, 1.0, 1.1, 1.3, 1.5],
            width,
            bold_rows=tuple(bold),
            band_rows=tuple(band),
        )
    ]
    if winding.get("twisting_available") is False:
        body.append(
            _p(
                f"Twisting is not split out on this tenant yet {DASH} the is_twisted "
                "yarn-master flag is missing, so any twisted (ply) yarn is still included "
                "in the winding total.",
                ST_NOTE,
            )
        )
    elif has_twisting:
        body.append(
            _p(
                "Twisted (ply) yarn is single yarn that was already wound once, so its "
                f"{fmt(_d(total.get('twisting')).get('production_kg'), 0)} kg is listed "
                "separately and kept out of the winding total to avoid counting it twice. "
                "The gross line is what the winders physically turned out.",
                ST_NOTE,
            )
        )
    return _section(
        f"Winder Production {DASH} Quality Wise",
        "Reconciled kg (doff − opening jugar + closing jugar) per winder · MTD winding "
        f"{fmt(total.get('mtd_production_kg'), 0)} kg",
        body,
    )


def _weaving_section(weaving: dict, width: float) -> list:
    total = _d(weaving.get("total"))
    shift_totals = _d(weaving.get("shift_totals"))
    rows = []
    for q in _l(weaving.get("qualities")):
        q = _d(q)
        rows.append(
            [
                _first(q.get("quality_name"), q.get("quality_code"), ""),
                fmt(q.get("shift_A_mt"), 3),
                fmt(q.get("shift_B_mt"), 3),
                fmt(q.get("shift_C_mt"), 3),
                fmt(q.get("production_mt"), 3),
                fmt(q.get("looms_run"), 0),
                fmt(q.get("kg_per_loom"), 1),
                fmt(q.get("eff_pct"), 1),
                fmt(q.get("mtd_production_mt"), 1),
            ]
        )
    total_idx = len(rows)
    rows.append(
        [
            "Total",
            fmt(shift_totals.get("A"), 3),
            fmt(shift_totals.get("B"), 3),
            fmt(shift_totals.get("C"), 3),
            fmt(total.get("production_mt"), 3),
            fmt(total.get("loom_runs"), 0),
            fmt(total.get("kg_per_loom"), 1),
            fmt(total.get("eff_pct"), 1),
            fmt(total.get("mtd_production_mt"), 1),
        ]
    )
    body: list = [
        _table(
            [
                "Quality",
                "A (MT)",
                "B (MT)",
                "C (MT)",
                "Total (MT)",
                "Looms Run",
                "Kg/Loom",
                "Eff %",
                "MTD (MT)",
            ],
            rows,
            [2.6, 1, 1, 1, 1.1, 1, 1, 0.9, 1.1],
            width,
            bold_rows=(total_idx,),
        )
    ]
    note = _missing_note(
        _l(weaving.get("standards_missing")),
        "weaving standard configured",
        "Weaving Target Map",
    )
    if note:
        body.append(note)
    return _section(
        f"Weaving Production {DASH} Quality & Shift Wise",
        f"{fmt(weaving.get('total_looms'), 0)} installed looms · utilization "
        f"{fmt(total.get('utilization_pct'), 1)}% · MTD "
        f"{fmt(total.get('mtd_production_mt'), 1)} MT",
        body,
    )


def _finishing_section(processes: list, width: float) -> list:
    rows = []
    bold = []
    for p in processes:
        p = _d(p)
        process = str(p.get("process") or "")
        if process == "balepress":
            bold.append(len(rows))
        rows.append(
            [
                process.capitalize(),
                p.get("uom") or "",
                fmt(p.get("day_qty"), 0),
                fmt(p.get("day_wt_kg"), 0),
                fmt(p.get("mtd_qty"), 0),
                fmt(p.get("mtd_wt_kg"), 0),
            ]
        )
    return _section(
        "Finishing",
        "Bale press row = press production; other processes shown for context",
        [
            _table(
                ["Process", "UoM", "Today Qty", "Today Wt (kg)", "MTD Qty", "MTD Wt (kg)"],
                rows,
                [1.6, 0.8, 1.1, 1.2, 1.1, 1.2],
                width,
                left_cols=(0, 1),
                bold_rows=tuple(bold),
            )
        ],
    )


def _emulsion_section(emulsion: dict, width: float) -> list:
    day = _d(emulsion.get("day"))
    mtd = _d(emulsion.get("mtd"))
    days_logged = f"{fmt(mtd.get('days'), 0)} day(s) logged"
    subtitle = (
        f"Std band {fmt(day.get('std_low'), 0)}–{fmt(day.get('std_high'), 0)}% · {days_logged}"
        if day
        else days_logged
    )
    rows = [
        ["JBO (ltr)", fmt(day.get("jbo_ltr"), 1), fmt(mtd.get("jbo_ltr"), 1)],
        ["Ecofin (ltr)", fmt(day.get("eco_fin_ltr"), 1), fmt(mtd.get("eco_fin_ltr"), 1)],
        ["Oil used (ltr)", fmt(day.get("oil_used_ltr"), 1), DASH],
        ["Tank capacity (ltr)", fmt(day.get("tank_capacity_ltr"), 1), DASH],
        ["Oil % in emulsion", fmt(day.get("oil_pct"), 2), fmt(mtd.get("avg_oil_pct"), 2)],
    ]
    text_colors = []
    status = day.get("status")
    if status:
        text_colors.append(
            (len(rows), 1, C_GOOD if status == "OK" else C_WARN if status == "LOW" else C_BAD)
        )
        rows.append(["Status", str(status), DASH])
    return _section(
        "Batching Oil (JBO / Ecofin)",
        subtitle,
        [_table(["Measure", "Today", "MTD"], rows, [2, 1, 1], width, text_colors=text_colors)],
    )


def _yarn_heavy_light_section(rows_in: list, width: float) -> list:
    rows = []
    text_colors = []
    for r in rows_in:
        r = _d(r)
        ri = len(rows)
        remark = r.get("remark")
        text_colors.append((ri, 4, _tone_color(r.get("hy_lt_pct"), "nonzero-bad")))
        if remark:
            text_colors.append((ri, 5, C_GOOD if remark == "OK" else C_BAD))
        rows.append(
            [
                _first(r.get("item_name"), r.get("item_code"), ""),
                fmt(r.get("std_count")),
                fmt(r.get("obs_count")),
                fmt(r.get("corr_count")),
                fmt(r.get("hy_lt_pct")),
                remark or DASH,
                fmt(r.get("day_tests"), 0),
                fmt(r.get("mtd_corr_count")),
            ]
        )
    return _section(
        "Yarn Heavy / Light (SQC)",
        "Corrected count vs standard; +% = heavy, −% = light",
        [
            _table(
                [
                    "Quality",
                    "Std (lb)",
                    "Observed (lb)",
                    "Corrected (lb)",
                    "HY/LT %",
                    "Remark",
                    "Tests",
                    "MTD Corrected",
                ],
                rows,
                [2.6, 1, 1.1, 1.1, 1, 1, 0.8, 1.2],
                width,
                left_cols=(0, 5),
                text_colors=text_colors,
            )
        ],
    )


def _jute_section(jute: dict, width: float) -> list:
    groups = [_d(g) for g in _l(jute.get("groups"))]

    def row(label: Any, w: dict) -> list:
        return [
            label or "",
            fmt(w.get("opening_weight"), 0),
            fmt(w.get("receipt_weight"), 0),
            fmt(w.get("issue_weight"), 0),
            fmt(w.get("closing_weight"), 0),
            fmt(w.get("mtd_receipt_weight"), 0),
            fmt(w.get("mtd_issue_weight"), 0),
        ]

    waste_split = bool(
        jute.get("jute_totals") and jute.get("waste_totals") and any(g.get("is_waste") for g in groups)
    )
    rows = [row(g.get("item_group_name"), g) for g in groups if not g.get("is_waste")]
    bold = []
    if waste_split:
        bold.append(len(rows))
        rows.append(row("Jute Stock", _d(jute.get("jute_totals"))))
        rows.extend(row(g.get("item_group_name"), g) for g in groups if g.get("is_waste"))
        bold.append(len(rows))
        rows.append(row("Waste Stock", _d(jute.get("waste_totals"))))
    bold.append(len(rows))
    rows.append(row("Total (Jute + Waste)" if waste_split else "Total", _d(jute.get("totals"))))
    return _section(
        "Jute Stock Position (kg)",
        _jute_cover_subtitle(jute),
        [
            _table(
                ["Group", "Opening", "Receipt", "Issue", "Closing", "MTD Receipt", "MTD Issue"],
                rows,
                [2.6, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2],
                width,
                bold_rows=tuple(bold),
            )
        ],
    )


def _shifts_worked_section(sw: dict, width: float) -> list:
    rows = []
    for s in _l(sw.get("sections")):
        s = _d(s)
        rows.append(
            [
                s.get("section") or "",
                fmt(s.get("day"), 0),
                ", ".join(str(x) for x in _l(s.get("day_shifts"))) or DASH,
                fmt(s.get("mtd"), 0),
            ]
        )
    total_idx = len(rows)
    rows.append(
        [
            "Mill",
            fmt(sw.get("day"), 0),
            ", ".join(str(x) for x in _l(sw.get("day_shifts"))) or DASH,
            fmt(sw.get("mtd"), 0),
        ]
    )
    return _section(
        "Shifts Worked",
        "A shift counts as worked when that section has a production entry in it; the mill "
        "row is the union across sections, not their sum",
        [
            _table(
                ["Section", "Today", "Shifts", "To Date"],
                rows,
                [1.6, 0.9, 1.2, 0.9],
                width,
                left_cols=(0, 2),
                bold_rows=(total_idx,),
            )
        ],
    )


def _stage_depts_text(stages: dict) -> str:
    """Which departments landed in which stage — the mapping is by name, so a
    tenant whose names do not fit must be able to see it on the sheet."""
    parts = []
    for key in ("yarn", "cloth", "common"):
        s = _d(stages.get(key))
        depts = ", ".join(str(x) for x in _l(s.get("depts"))) or "none"
        parts.append(f"{s.get('label') or key}: {depts}")
    return "Departments — " + "; ".join(parts) + "."


def _signed(v: Any, digits: int) -> str:
    d = _num(v)
    if d is None:
        return DASH
    return ("+" if d >= 0 else "") + fmt(d, digits)


def _yarn_balance_text(balance: dict) -> str:
    day = _d(balance.get("day"))
    mtd = _d(balance.get("mtd"))
    if balance.get("purchased_available"):
        purchased = f" + purchased yarn issued {fmt(day.get('purchased_issued_mt'), 3)} MT"
    else:
        purchased = " (purchased yarn not tracked on this tenant)"
    return (
        f"Yarn balance: spun {fmt(day.get('spun_mt'), 3)} MT{purchased} − woven "
        f"{fmt(day.get('woven_mt'), 3)} MT = {_signed(day.get('surplus_mt'), 3)} MT to "
        f"yarn stock / sale (MTD {_signed(mtd.get('surplus_mt'), 1)} MT)."
    )


def _hands_section(hands: dict, width: float) -> list:
    total = _d(hands.get("total"))
    rows = []
    for s in _l(hands.get("shifts")):
        s = _d(s)
        rows.append(
            [
                s.get("shift") or "",
                fmt(s.get("day_hours"), 0),
                fmt(s.get("day_hands")),
                fmt(s.get("mtd_hours"), 0),
                fmt(s.get("mtd_hands"), 0),
            ]
        )
    total_idx = len(rows)
    rows.append(
        [
            "Total",
            fmt(total.get("day_hours"), 0),
            fmt(total.get("day_hands")),
            fmt(total.get("mtd_hours"), 0),
            fmt(total.get("mtd_hands"), 0),
        ]
    )
    body: list = [
        _table(
            ["Shift", "Hours", "Hands", "MTD Hours", "MTD Hands"],
            rows,
            [1, 1, 1, 1.2, 1.2],
            width,
            bold_rows=(total_idx,),
        )
    ]
    stages = _d(hands.get("stages"))
    balance = _d(hands.get("yarn_balance"))
    if stages.get("available"):
        # TKT-000185: each stage over its own tonnage, common hands spread
        # pro-rata, the headline the sum (hands per MT of cloth).
        srows = []
        for key in ("yarn", "cloth"):
            st = _d(stages.get(key))
            srows.append(
                [
                    f"{st.get('label') or key} ÷ {st.get('basis_label') or ''}",
                    fmt(st.get("day_hands")),
                    fmt(st.get("day_mt"), 3),
                    fmt(st.get("hands_per_mt"), 1),
                    fmt(st.get("mtd_hands_per_mt"), 1),
                ]
            )
        common = _d(stages.get("common"))
        share = _num(common.get("yarn_share_pct"))
        split = (
            f"{fmt(share, 0)}% yarn / {fmt(100 - share, 0)}% cloth"
            if share is not None
            else "pro-rata"
        )
        srows.append(
            [f"Common hands, spread {split}", fmt(common.get("day_hands")), DASH, DASH, DASH]
        )
        tot = _d(stages.get("total"))
        srows.append(
            [
                "Hands per MT of cloth (yarn + cloth)",
                fmt(tot.get("day_hands")),
                DASH,
                fmt(tot.get("hands_per_mt"), 1),
                fmt(tot.get("mtd_hands_per_mt"), 1),
            ]
        )
        total_row = len(srows) - 1
        # The previous figure, for continuity with earlier sheets.
        prev = _d(hands.get("spinning_basis"))
        if prev:
            srows.append(
                [
                    prev.get("label") or "All hands ÷ spinning output (previous basis)",
                    fmt(prev.get("day_hands")),
                    fmt(prev.get("day_mt"), 3),
                    fmt(prev.get("hands_per_mt"), 1),
                    fmt(prev.get("mtd_hands_per_mt"), 1),
                ]
            )
        body.append(Spacer(1, 3))
        body.append(
            _table(
                ["Stage", "Hands", "MT", "Hands/MT", "MTD"],
                srows,
                [2.1, 0.75, 0.8, 1.0, 0.7],
                width,
                bold_rows=(total_row,),
            )
        )
        body.append(_p(_stage_depts_text(stages), ST_NOTE))
    else:
        body.append(
            _p(
                f"Overall labour productivity: {fmt(total.get('day_hands'), 2)} hands ÷ "
                f"{fmt(_d(balance.get('day')).get('spun_mt'), 3)} MT "
                f"({hands.get('basis_label') or ''}) = "
                f"{fmt(hands.get('hands_per_mt'), 1)} hands per MT "
                f"(MTD {fmt(hands.get('mtd_hands_per_mt'), 1)}).",
                ST_NOTE,
            )
        )
    if balance:
        body.append(_p(_yarn_balance_text(balance), ST_NOTE))
    return _section(
        "Workers / Hands",
        "Hands = worked hours ÷ 8 (8-hour shifts); approved attendance only",
        body,
    )


def _machine_run_section(machine_run: list, width: float) -> list:
    rows = []
    text_colors = []
    for m in machine_run:
        m = _d(m)
        text_colors.append((len(rows), 7, _tone_color(m.get("excess_machines"), "pos-bad")))
        rows.append(
            [
                m.get("section") or "",
                str(m.get("basis") or "").upper(),
                fmt(m.get("avg_per_machine"), 1),
                fmt(m.get("avg_act_per_machine"), 1),
                fmt(m.get("total_prod_kg"), 0),
                fmt(m.get("should_run")),
                fmt(m.get("act_run"), 0),
                fmt(m.get("excess_machines")),
            ]
        )
    return _section(
        "Extra Machine Run Analysis",
        "Machines the output justified vs machines actually run; positive excess = extra "
        "machines running below par",
        [
            _table(
                [
                    "Section",
                    "Basis",
                    "Avg Norm/Machine (kg)",
                    "Avg Act/Machine (kg)",
                    "Total Prod (kg)",
                    "Should Run",
                    "Act Run",
                    "Excess",
                ],
                rows,
                [1.4, 1, 1.6, 1.6, 1.3, 1.1, 1, 1],
                width,
                left_cols=(0, 1),
                text_colors=text_colors,
            )
        ],
    )


POWER_NOT_KEYED_NOTE = "Power readings not keyed for this date"
POWER_SUBTITLE = "Main meter kWh, DG hours and kWh per tonne (ITM-004)"


def _power_section(power: Optional[dict], width: float) -> list:
    """Power line (ITM-004). ``power`` is build_power_section's dict or None
    (table absent on the tenant). Not keyed -> a note, plus the MTD figures from
    the days that were keyed when there are any."""
    power = _d(power)
    mtd = _d(power.get("mtd"))
    mtd_days = int(_num(mtd.get("days")) or 0)
    if not power.get("keyed"):
        note = POWER_NOT_KEYED_NOTE
        body: list = []
        if mtd_days:
            note += " — MTD figures shown from the days that were keyed"
            body.append(Paragraph(escape(note), ST_NOTE))
            body.append(
                _table(
                    [
                        "MTD days",
                        "MTD kWh",
                        "DG kWh",
                        "DG hours",
                        "DG litres",
                        "kWh/t cloth",
                        "kWh/t yarn",
                        "DG share %",
                    ],
                    [
                        [
                            fmt(mtd_days, 0),
                            fmt(mtd.get("kwh"), 0),
                            fmt(mtd.get("dg_kwh"), 0),
                            fmt(mtd.get("dg_hours"), 1),
                            fmt(mtd.get("dg_litres"), 0),
                            fmt(mtd.get("kwh_per_t_cloth"), 0),
                            fmt(mtd.get("kwh_per_t_yarn"), 0),
                            fmt(mtd.get("dg_share_pct"), 1),
                        ]
                    ],
                    [1, 1, 1, 1, 1, 1, 1, 1],
                    width,
                    left_cols=(),
                )
            )
        else:
            body.append(Paragraph(escape(note), ST_NOTE))
        return _section("Power", POWER_SUBTITLE, body)

    day = _d(power.get("day"))
    rows = [
        ["Total kWh", fmt(day.get("kwh"), 0), fmt(mtd.get("kwh"), 0)],
        ["Grid kWh", fmt(day.get("grid_kwh"), 0), fmt(mtd.get("grid_kwh"), 0)],
        ["DG kWh", fmt(day.get("dg_kwh"), 0), fmt(mtd.get("dg_kwh"), 0)],
        ["DG hours", fmt(day.get("dg_hours"), 1), fmt(mtd.get("dg_hours"), 1)],
        ["DG litres", fmt(day.get("dg_litres"), 0), fmt(mtd.get("dg_litres"), 0)],
        ["kWh per t cloth", fmt(day.get("kwh_per_t_cloth"), 0), fmt(mtd.get("kwh_per_t_cloth"), 0)],
        ["kWh per t yarn", fmt(day.get("kwh_per_t_yarn"), 0), fmt(mtd.get("kwh_per_t_yarn"), 0)],
        ["DG share %", fmt(day.get("dg_share_pct"), 1), fmt(mtd.get("dg_share_pct"), 1)],
    ]
    subtitle = POWER_SUBTITLE + (f" · MTD over {mtd_days} keyed day(s)" if mtd_days else "")
    return _section(
        "Power",
        subtitle,
        [_table(["", "Day", "MTD"], rows, [2.2, 1, 1], width, left_cols=(0,))],
    )


def _unavailable_section(unavailable: list) -> list:
    body = []
    for u in unavailable:
        u = _d(u)
        body.append(
            Paragraph(
                f"<b>{escape(_safe(u.get('section') or ''))}</b> {DASH} "
                f"{escape(_safe(u.get('reason') or ''))}",
                ST_NOTE,
            )
        )
    return _section(
        "Not Yet Captured in ERP",
        "These DPR sections need new entry screens before they can appear here",
        body,
    )


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(FONT, 6.5)
    canvas.setFillColor(C_MUTED)
    canvas.drawString(MARGIN, 5 * mm, _safe(getattr(doc, "dpr_footer_text", "")))
    canvas.drawRightString(PAGE_SIZE[0] - MARGIN, 5 * mm, f"Page {doc.page}")
    canvas.restoreState()


def render_dpr_pdf(data: dict, company_name: Optional[str], branch_name: Optional[str]) -> bytes:
    """Render a DPR payload (the dict build_dpr_data returns) to PDF bytes.

    Pure: no DB access. Tolerates None / missing sections and empty lists — a
    section the page would hide is skipped here too.
    """
    data = _d(data)
    report_date = data.get("report_date")
    date_from = data.get("date_from")
    half = (CONTENT_W - PAIR_GAP) / 2.0
    gap = Spacer(1, 5)

    story: list = []
    if company_name:
        story.append(_p(company_name, ST_COMPANY))
    story.append(_p("DAILY PRODUCTION REPORT", ST_TITLE))
    meta = []
    if branch_name:
        meta.append(f"Branch: {branch_name}")
    meta.append(f"Date: {_iso_ddmmyyyy(report_date)}")
    if date_from:
        meta.append(f"MTD from {_iso_ddmmyyyy(date_from)}")
    story.append(_p(" · ".join(meta), ST_META))
    story.append(Spacer(1, 4))

    story.extend(_kpi_strip(data))
    story.append(gap)

    def add(flowables: list, keep: bool = True) -> None:
        if not flowables:
            return
        if keep:
            story.append(KeepTogether(flowables))
        else:
            # Long tables split across pages (header row repeats); just make
            # sure the title is not stranded at the foot of a page.
            story.append(CondPageBreak(30 * mm))
            story.extend(flowables)
        story.append(gap)

    spinning = data.get("spinning") if isinstance(data.get("spinning"), dict) else None
    if spinning:
        add(_spinning_shift_section(spinning, CONTENT_W))
        if _l(spinning.get("quality")):
            quality = _spinning_quality_section(spinning, CONTENT_W)
            add(quality, keep=len(_l(spinning.get("quality"))) <= 25)

    winding = data.get("winding") if isinstance(data.get("winding"), dict) else None
    if winding:
        add(_winding_section(winding, CONTENT_W), keep=len(_l(winding.get("qualities"))) <= 25)

    weaving = data.get("weaving") if isinstance(data.get("weaving"), dict) else None
    if weaving:
        add(_weaving_section(weaving, CONTENT_W), keep=len(_l(weaving.get("qualities"))) <= 25)

    # Finishing and Batching Oil are small — set side by side when both exist.
    processes = _l(_d(data.get("finishing")).get("processes"))
    emulsion = data.get("emulsion") if isinstance(data.get("emulsion"), dict) else None
    if processes and emulsion and len(processes) <= 15:
        add(_pair(_finishing_section(processes, half), _emulsion_section(emulsion, half)))
    else:
        if processes:
            add(_finishing_section(processes, CONTENT_W), keep=len(processes) <= 25)
        if emulsion:
            add(_emulsion_section(emulsion, CONTENT_W))

    yhl = _l(data.get("yarn_heavy_light"))
    if yhl:
        add(_yarn_heavy_light_section(yhl, CONTENT_W), keep=len(yhl) <= 25)

    jute = data.get("jute") if isinstance(data.get("jute"), dict) else None
    if jute:
        add(_jute_section(jute, CONTENT_W), keep=len(_l(jute.get("groups"))) <= 25)

    # Shifts Worked and Workers / Hands always render on the page.
    add(
        _pair(
            _shifts_worked_section(_d(data.get("shifts_worked")), half),
            _hands_section(_d(data.get("hands")), half),
        )
    )

    machine_run = _l(data.get("machine_run"))
    if machine_run:
        add(_machine_run_section(machine_run, CONTENT_W))

    # Power (ITM-004) always renders: "not keyed" when absent.
    add(_power_section(data.get("power") if isinstance(data.get("power"), dict) else None, CONTENT_W))

    unavailable = _l(data.get("unavailable"))
    if unavailable:
        add(_unavailable_section(unavailable))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=PAGE_SIZE,
        # Frame padding is taken off the margins so paragraphs and full-width
        # tables share the same left/right edges.
        leftMargin=MARGIN - FRAME_PAD,
        rightMargin=MARGIN - FRAME_PAD,
        topMargin=MARGIN - FRAME_PAD,
        bottomMargin=11 * mm,
        title=f"Daily Production Report {report_date or ''}".strip(),
        author=_safe(company_name or ""),
    )
    doc.dpr_footer_text = " · ".join(
        x
        for x in (
            company_name,
            branch_name,
            f"Daily Production Report {_iso_ddmmyyyy(report_date)}" if report_date else None,
        )
        if x
    )
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def _fetch_company_name(db: Session, co_id: int) -> Optional[str]:
    row = db.execute(
        text("SELECT co_name FROM co_mst WHERE co_id = :co_id LIMIT 1"),
        {"co_id": int(co_id)},
    ).first()
    return row._mapping["co_name"] if row else None


def _fetch_branch_name(db: Session, co_id: int, branch_id: int) -> Optional[str]:
    row = db.execute(
        get_branch_name_query(), {"branch_id": int(branch_id), "co_id": int(co_id)}
    ).first()
    return row._mapping["branch_name"] if row else None


def _filename_part(name: Optional[str], fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", name or "").strip("_")
    return cleaned or fallback


def build_dpr_pdf(db: Session, co_id: int, branch_id: int, report_d: date) -> tuple[bytes, str]:
    """Build the Daily Production Report PDF for one (company, branch, date).

    Returns (pdf_bytes, filename), e.g. filename 'DPR_FACTORY_2026-09-30.pdf'.
    Read-only on the DB. Raises HTTPException(400) (from build_dpr_data) when
    branch_id does not belong to co_id.
    """
    data = build_dpr_data(db, int(co_id), int(branch_id), report_d)
    company_name = _fetch_company_name(db, co_id)
    branch_name = _fetch_branch_name(db, co_id, branch_id)
    pdf_bytes = render_dpr_pdf(data, company_name, branch_name)
    filename = (
        f"DPR_{_filename_part(branch_name, f'branch{int(branch_id)}')}_{report_d.isoformat()}.pdf"
    )
    return pdf_bytes, filename
