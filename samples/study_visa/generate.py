"""Generate test packets for the `study_visa_funds` vertical.

One applicant (Priya Nair) rendered as digital PDFs, scanned-style PNGs, and two combined
multi-page PDFs. A clean packet audits CLEAR against `study_visa.yaml` — balances meet the
USD 25,000 minimum, the statement is recent, and the affidavit is signed.

Run:  pip install fpdf2 Pillow  &&  python samples/study_visa/generate.py

NOTE: `STATEMENT_DATE` must stay within the last year for rule.statement_recent to pass;
bump it if you test far ahead.
"""

import os
import random

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

random.seed(31)
BASE = os.path.dirname(os.path.abspath(__file__))

STATEMENT_DATE = "2026-06-30"  # within the last year -> rule.statement_recent passes

DOCS = {
    "bank_statement": [
        "BANK STATEMENT",
        "",
        "Account Holder: Priya Nair",
        "Account Number: ****4821",
        "Closing Balance: USD 30,000.00",
        f"Statement Date: {STATEMENT_DATE}",
    ],
    "admission_letter": [
        "LETTER OF ADMISSION",
        "",
        "Student Name: Priya Nair",
        "Institution: State University",
        "Program: M.Sc. Data Science",
        "Intake: Fall 2026",
    ],
    "sponsorship_affidavit": [
        "AFFIDAVIT OF FINANCIAL SUPPORT",
        "",
        "Sponsor: Rajesh Nair",
        "Relationship: Parent",
        "Sponsored Amount: USD 40,000.00",
        "Signed Date: 2026-02-01",
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

    for name, lines in DOCS.items():
        pdf = FPDF(format="A4")
        pdf.add_page()
        for ln in lines:
            style = "B" if ln.isupper() and ln.strip() else ""
            pdf.set_font("Courier", style=style, size=13 if style else 11)
            pdf.cell(0, 8, ln, ln=1)
        pdf.output(f"{BASE}/digital/{name}.pdf")

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
