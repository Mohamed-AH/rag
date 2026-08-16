"""Generate test packets for the `procurement` vertical.

Mirrors the other generators: one consistent vendor (Northwind Traders LLC) rendered as
digital PDFs, scanned-style PNGs, and two combined multi-page PDFs. A clean packet audits
CLEAR against `procurement.yaml`.

Run:  pip install fpdf2 Pillow  &&  python samples/procurement/generate.py

NOTE: `POLICY_EXPIRY` must be in the future for rule.coi_not_expired to pass; bump it if
you test far ahead.
"""

import os
import random

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

random.seed(23)
BASE = os.path.dirname(os.path.abspath(__file__))

VENDOR = "Northwind Traders LLC"
POLICY_EXPIRY = "2027-06-30"  # in the future -> rule.coi_not_expired passes

DOCS = {
    "w9": [
        "FORM W-9 - REQUEST FOR TAXPAYER IDENTIFICATION NUMBER",
        "",
        f"Name (legal entity): {VENDOR}",
        "Business type: Limited Liability Company",
        "Employer Identification Number (EIN): 12-3456789",
        "Address: 500 Commerce Way, Seattle, WA",
        "Signature: /s/ A. Vendor        Date: 2026-01-10",
    ],
    "coi": [
        "CERTIFICATE OF LIABILITY INSURANCE",
        "",
        f"Insured: {VENDOR}",
        "Coverage Type: Commercial General Liability",
        "Each Occurrence Limit: $2,000,000",
        "Policy Number: GL-99182734",
        f"Policy Expiration Date: {POLICY_EXPIRY}",
    ],
    "nda": [
        "MUTUAL NON-DISCLOSURE AGREEMENT",
        "",
        f"Between: Acme Corp and {VENDOR}",
        f"Counterparty: {VENDOR}",
        "Effective Date: 2026-01-15",
        "Signed Date: 2026-01-15",
        "Signature: /s/ A. Vendor",
    ],
    "soc2": [
        "SOC 2 TYPE II REPORT",
        "",
        f"Service Organization: {VENDOR}",
        "Report Date: 2025-11-01",
        "Trust Services Criteria: Security, Availability, Confidentiality",
    ],
}


def _font(size):
    for p in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def main() -> None:
    for sub in ("digital", "scanned", "combined"):
        os.makedirs(f"{BASE}/{sub}", exist_ok=True)

    # Digital PDFs (real text layer).
    for name, lines in DOCS.items():
        pdf = FPDF(format="A4")
        pdf.add_page()
        for ln in lines:
            style = "B" if ln.isupper() and ln.strip() else ""
            pdf.set_font("Courier", style=style, size=13 if style else 11)
            pdf.cell(0, 8, ln, ln=1)
        pdf.output(f"{BASE}/digital/{name}.pdf")

    # Scanned-style PNGs (no text layer).
    big, reg = _font(26), _font(22)
    for name, lines in DOCS.items():
        w, h = 1240, 1754
        img = Image.new("RGB", (w, h), (248, 247, 243))
        d = ImageDraw.Draw(img)
        y = 90
        for ln in lines:
            f = big if (ln.isupper() and ln.strip()) else reg
            d.text((90, y), ln, fill=(17, 17, 17), font=f)
            y += 42 if f is big else 34
        img = img.rotate(random.uniform(-1.2, 1.2), expand=False, fillcolor=(248, 247, 243))
        img = img.filter(ImageFilter.GaussianBlur(0.6))
        px = img.load()
        for _ in range(9000):
            x, yy = random.randint(0, w - 1), random.randint(0, h - 1)
            v = random.randint(150, 210)
            px[x, yy] = (v, v, v)
        img.save(f"{BASE}/scanned/{name}.png", "PNG")

    # Combined multi-page files (all docs in one file).
    pdf = FPDF(format="A4")
    for name, lines in DOCS.items():
        pdf.add_page()
        for ln in lines:
            style = "B" if ln.isupper() and ln.strip() else ""
            pdf.set_font("Courier", style=style, size=13 if style else 11)
            pdf.cell(0, 8, ln, ln=1)
    pdf.output(f"{BASE}/combined/packet_digital.pdf")

    pages = [Image.open(f"{BASE}/scanned/{n}.png").convert("RGB") for n in DOCS]
    pages[0].save(f"{BASE}/combined/packet_scanned.pdf", save_all=True, append_images=pages[1:])

    print("digital:", sorted(os.listdir(f"{BASE}/digital")))
    print("scanned:", sorted(os.listdir(f"{BASE}/scanned")))
    print("combined:", sorted(os.listdir(f"{BASE}/combined")))


if __name__ == "__main__":
    main()
