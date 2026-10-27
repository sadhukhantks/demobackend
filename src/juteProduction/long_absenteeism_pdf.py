"""Long Absenteeism letters as an A4 PDF (reportlab) — GET /letters_pdf.

Owner decision 2026-10-12 (D): a PDF of the issued letters without any browser
involvement (no "Save as PDF", no browser header / footer). One letter per page
(a long body flows on to the next page): the company letterhead (logo when
co_mst.co_logo is a decodable base64 data URL, company name in bold, address,
GSTIN / CIN as the screen letterhead shows them), the subject centred, bold and
underlined, then the stored body with its line breaks kept and long lines
word-wrapped (Times-Roman 11 pt). The signature block is part of the body text.

Pure: ``build_letters_pdf(letters, company) -> bytes`` takes already-loaded rows,
so it is unit-tested without a DB (long_absenteeism_letters.py loads them).
"""

import base64
import binascii
import re
from io import BytesIO
from typing import Optional
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PAGE_MARGIN_X = 20 * mm
PAGE_MARGIN_Y = 15 * mm
LOGO_MAX_W = 42 * mm
LOGO_MAX_H = 18 * mm

ST_COMPANY = ParagraphStyle("la_company", fontName="Times-Bold", fontSize=15, leading=18)
ST_ADDRESS = ParagraphStyle("la_address", fontName="Times-Roman", fontSize=9.5, leading=12)
ST_SUBJECT = ParagraphStyle(
    "la_subject", fontName="Times-Bold", fontSize=11, leading=14, alignment=TA_CENTER, spaceBefore=2, spaceAfter=10
)
ST_BODY = ParagraphStyle("la_body", fontName="Times-Roman", fontSize=11, leading=14)

_DATA_URL_RE = re.compile(r"^data:image/[a-z0-9.+-]+;base64,(.+)$", re.IGNORECASE | re.DOTALL)
_LEADING_SPACES_RE = re.compile(r"^ +")
_SPACE_RUN_RE = re.compile(r" {2,}")


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def decode_logo(value) -> Optional[bytes]:
    """Bytes of a logo stored as a base64 data URL (co_mst.co_logo), else None:
    not a data URL, bad base64, or not an image reportlab can read."""
    if not isinstance(value, str):
        return None
    m = _DATA_URL_RE.match(value.strip())
    if not m:
        return None
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", m.group(1)), validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    try:
        w, h = ImageReader(BytesIO(raw)).getSize()
    except Exception:
        return None
    return raw if w and h else None


def letterhead_lines(company: Optional[dict]) -> tuple:
    """(company name, address line, "GSTIN: … | CIN: …" line) as the screen
    letterhead composes them (useCompanyHeader): address lines 1 + 2, then
    "state - PIN"; a blank part is left out."""
    company = company or {}
    name = _clean(company.get("co_name"))
    parts = [_clean(company.get(k)) for k in ("co_address1", "co_address2")]
    address = ", ".join(p for p in parts if p)
    zipcode = company.get("co_zipcode")
    pin = _clean(zipcode) if zipcode not in (None, 0) else ""
    tail = " - ".join(p for p in (_clean(company.get("state_name")), pin) if p)
    if tail:
        address = f"{address}, {tail}" if address else tail
    ids = []
    if _clean(company.get("gst_no")):
        ids.append(f"GSTIN: {_clean(company.get('gst_no'))}")
    if _clean(company.get("co_cin_no")):
        ids.append(f"CIN: {_clean(company.get('co_cin_no'))}")
    return name, address, "  |  ".join(ids)


def _line_markup(line: str) -> str:
    """One body line as Paragraph markup: escaped, leading indentation and runs
    of spaces kept (Paragraph would collapse them)."""
    s = escape(line.expandtabs(4))
    s = _LEADING_SPACES_RE.sub(lambda m: "&nbsp;" * len(m.group(0)), s)
    return _SPACE_RUN_RE.sub(lambda m: " " + "&nbsp;" * (len(m.group(0)) - 1), s)


def body_flowables(body: Optional[str]) -> list:
    """The stored body, one Paragraph per line (word-wrapped), a blank line kept
    as one line of space."""
    out = []
    text_value = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    for line in text_value.split("\n"):
        if not line.strip():
            out.append(Spacer(1, ST_BODY.leading))
        else:
            out.append(Paragraph(_line_markup(line), ST_BODY))
    return out


def _logo_flowable(raw: bytes) -> Image:
    w, h = ImageReader(BytesIO(raw)).getSize()
    scale = min(LOGO_MAX_W / float(w), LOGO_MAX_H / float(h))   # fit the box, keep the ratio
    img = Image(BytesIO(raw), width=w * scale, height=h * scale)
    img.hAlign = "LEFT"
    return img


def _letterhead(company: Optional[dict], logo: Optional[bytes], width: float) -> list:
    name, address, ids = letterhead_lines(company)
    text_col = []
    if name:
        text_col.append(Paragraph(escape(name), ST_COMPANY))
    if address:
        text_col.append(Paragraph(escape(address), ST_ADDRESS))
    if ids:
        text_col.append(Paragraph(escape(ids), ST_ADDRESS))
    if not text_col and logo is None:
        return []
    if logo is not None:
        img = _logo_flowable(logo)
        cells = [[img, text_col or ""]]
        col_widths = [img.drawWidth + 4 * mm, width - img.drawWidth - 4 * mm]
    else:
        cells = [[text_col]]
        col_widths = [width]
    head = Table(cells, colWidths=col_widths)
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.75, "black"),
    ]))
    return [head, Spacer(1, 5 * mm)]


def build_letters_pdf(letters: list, company: Optional[dict]) -> bytes:
    """A4 PDF of ``letters`` (dicts with ``subject`` and ``body``, in print
    order), each starting on a new page under the letterhead of ``company``
    (co_name, co_address1 / 2, state_name, co_zipcode, gst_no, co_cin_no,
    co_logo). ValueError when there is no letter."""
    if not letters:
        raise ValueError("No letter to print")
    company = company or {}
    logo = decode_logo(company.get("co_logo"))
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=PAGE_MARGIN_X,
        rightMargin=PAGE_MARGIN_X,
        topMargin=PAGE_MARGIN_Y,
        bottomMargin=PAGE_MARGIN_Y,
        title="Long Absenteeism letters",
        author=_clean(company.get("co_name")) or "VoWERP",
        creator="VoWERP",
    )
    story: list = []
    for i, letter in enumerate(letters):
        if i:
            story.append(PageBreak())
        story += _letterhead(company, logo, doc.width)
        subject = _clean((letter or {}).get("subject"))
        if subject:
            story.append(Paragraph(f"<u>{escape(subject)}</u>", ST_SUBJECT))
        story += body_flowables((letter or {}).get("body"))
    doc.build(story)
    return buf.getvalue()


def letters_pdf_filename(on_date) -> str:
    return f"long-absenteeism-letters-{on_date:%Y%m%d}.pdf"
