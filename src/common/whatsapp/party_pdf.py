"""One-page "Party Details" PDF (reportlab) — the test payload for WhatsApp documents."""

import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import text

from src.masters.query import get_party_branch_by_party_id, get_party_by_id


class PartyNotFoundError(LookupError):
    """No such party (or it belongs to another company)."""


# Same fallback as src/masters/party.py party_edit_setup: tenants whose
# party_branch_mst has no state_id resolve the state through city_mst.
_LEGACY_BRANCH_SQL = text("""
SELECT
  pbm.active,
  pbm.party_mst_branch_id,
  pbm.gst_no,
  pbm.address,
  pbm.address_additional,
  pbm.zip_code,
  cm.state_id,
  sm.state,
  pbm.contact_no,
  pbm.contact_person
FROM party_branch_mst pbm
LEFT JOIN city_mst cm ON cm.city_id = pbm.city_id
LEFT JOIN state_mst sm ON sm.state_id = cm.state_id
WHERE pbm.party_id = :party_id;
""")


def fetch_party(db, party_id: int, co_id: int | None = None) -> tuple[dict, list[dict]]:
    """(party row, branch rows). Raises PartyNotFoundError."""
    row = db.execute(get_party_by_id(party_id), {"party_id": party_id}).fetchone()
    if row is None:
        raise PartyNotFoundError(f"Party {party_id} not found")
    party = dict(row._mapping)
    if co_id is not None and party.get("co_id") is not None and int(party["co_id"]) != int(co_id):
        raise PartyNotFoundError(f"Party {party_id} not found in company {co_id}")

    try:
        rows = db.execute(get_party_branch_by_party_id(party_id), {"party_id": party_id}).fetchall()
    except Exception as branch_err:
        if "Unknown column 'pbm.state_id'" not in str(branch_err):
            raise
        db.rollback()
        rows = db.execute(_LEGACY_BRANCH_SQL, {"party_id": party_id}).fetchall()
    return party, [dict(r._mapping) for r in rows]


def _txt(value) -> str:
    if value is None:
        return "-"
    s = str(value).strip()
    return s if s else "-"


def _yes_no(value) -> str:
    if value is None or str(value).strip() == "":
        return "-"
    return "Yes" if str(value).strip().lower() in ("1", "true", "y", "yes") else "No"


def render_party_pdf(party: dict, branches: list[dict]) -> bytes:
    """Pure rendering from already-fetched rows."""
    styles = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=8.5, leading=11)
    cell_bold = ParagraphStyle("cell_bold", parent=cell, fontName="Helvetica-Bold")
    head = ParagraphStyle("head", parent=cell_bold, textColor=colors.white)
    small = ParagraphStyle("small", parent=styles["Normal"], fontSize=7.5, textColor=colors.grey)
    title = ParagraphStyle("title", parent=styles["Title"], fontSize=15, spaceAfter=2)
    name_style = ParagraphStyle("pname", parent=styles["Normal"], fontName="Helvetica-Bold",
                                fontSize=12, leading=15, alignment=1, spaceAfter=6)

    def p(value, style=cell):
        return Paragraph(escape(_txt(value)), style)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm, title="Party Details",
    )
    width = doc.width
    story = [
        Paragraph("Party Details", title),
        Paragraph(escape(_txt(party.get("supp_name"))), name_style),
    ]

    fields = [
        ("Party Code", _txt(party.get("supp_code")), "Status", "Active" if party.get("active") in (1, True, "1") else "Inactive"),
        ("Contact Person", _txt(party.get("supp_contact_person")), "Designation", _txt(party.get("supp_contact_designation"))),
        ("Phone", _txt(party.get("phone_no")), "Email", _txt(party.get("supp_email_id"))),
        ("PAN", _txt(party.get("party_pan_no")), "CIN", _txt(party.get("cin"))),
        ("MSME Certified", _yes_no(party.get("msme_certified")), "Entity Type", _txt(party.get("entity_type_name"))),
        ("Country", _txt(party.get("country")), "Party ID", _txt(party.get("party_id"))),
    ]
    info = Table(
        [[p(a, cell_bold), p(b), p(c, cell_bold), p(d)] for a, b, c, d in fields],
        colWidths=[width * 0.16, width * 0.34, width * 0.16, width * 0.34],
    )
    info.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
        ("BACKGROUND", (2, 0), (2, -1), colors.whitesmoke),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [info, Spacer(1, 6 * mm)]

    story.append(Paragraph(f"<b>Branches ({len(branches)})</b>", styles["Normal"]))
    story.append(Spacer(1, 2 * mm))
    rows = [[p(h, head) for h in ("#", "Address", "State", "Zip", "GST No", "Contact", "Status")]]
    for i, b in enumerate(branches, start=1):
        address = ", ".join(
            s for s in (str(b.get("address") or "").strip(), str(b.get("address_additional") or "").strip()) if s
        )
        contact = " / ".join(
            s for s in (str(b.get("contact_person") or "").strip(), str(b.get("contact_no") or "").strip()) if s
        )
        rows.append([
            p(i), p(address), p(b.get("state")), p(b.get("zip_code")), p(b.get("gst_no")), p(contact),
            p("Active" if b.get("active") in (1, True, "1") else "Inactive"),
        ])
    if not branches:
        rows.append([p(""), p("No branches recorded"), p(""), p(""), p(""), p(""), p("")])
    branch_table = Table(
        rows, repeatRows=1,
        colWidths=[width * w for w in (0.04, 0.29, 0.17, 0.08, 0.18, 0.15, 0.09)],
    )
    branch_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#455A64")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.whitesmoke]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [branch_table, Spacer(1, 5 * mm)]
    story.append(Paragraph(f"Generated {datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%d-%b-%Y %H:%M')} IST — VoWERP", small))

    doc.build(story)
    return buf.getvalue()


def party_pdf_filename(party: dict) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", str(party.get("supp_name") or "")).strip("_")[:40]
    return f"Party_{party.get('party_id')}_{slug}.pdf" if slug else f"Party_{party.get('party_id')}.pdf"


def build_party_details_pdf(db, party_id: int, co_id: int | None = None) -> tuple[bytes, str]:
    """(pdf_bytes, filename) for one party. Raises PartyNotFoundError."""
    party, branches = fetch_party(db, party_id, co_id)
    return render_party_pdf(party, branches), party_pdf_filename(party)
