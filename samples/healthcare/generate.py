"""Generate test packets for the `healthcare_credentialing` vertical.

One clinician (Dr. Alex Rivera) rendered as digital PDFs, scanned-style PNGs, and two
combined multi-page PDFs. A clean packet audits CLEAR against `healthcare.yaml`.

Run:  pip install fpdf2 Pillow  &&  python samples/healthcare/generate.py

NOTE: every `*_EXPIRY` below must stay in the future for the not-expired checks to pass;
bump them if you test far ahead.
"""

import os
import random

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

random.seed(29)
BASE = os.path.dirname(os.path.abspath(__file__))

LICENSE_EXPIRY = "2028-04-30"
DEA_EXPIRY = "2028-02-28"
BOARD_EXPIRY = "2029-12-31"

DOCS = {
    "medical_license": [
        "STATE MEDICAL LICENSE",
        "",
        "Practitioner: Dr. Alex Rivera",
        "License Number: MD-556677",
        "State: California",
        f"Expiration Date: {LICENSE_EXPIRY}",
    ],
    "dea_registration": [
        "DEA REGISTRATION CERTIFICATE",
        "",
        "Registrant: Alex Rivera, MD",
        "DEA Number: BR1234563",
        "Schedules: 2, 2N, 3, 3N, 4, 5",
        f"Expiration Date: {DEA_EXPIRY}",
    ],
    "board_certification": [
        "AMERICAN BOARD OF INTERNAL MEDICINE",
        "",
        "Certifies: Alex Rivera, MD",
        "Specialty: Internal Medicine",
        f"Valid Through: {BOARD_EXPIRY}",
    ],
    "npi_record": [
        "NPPES NPI CONFIRMATION",
        "",
        "Provider: Alex Rivera",
        "NPI: 1234567893",
        "Taxonomy: 207R00000X Internal Medicine",
    ],
    "immunization": [
        "IMMUNIZATION RECORD",
        "",
        "Provider: Alex Rivera",
        "Completed Date: 2025-10-01",
        "Includes: Hep B, MMR, Varicella, Tdap, Influenza",
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
