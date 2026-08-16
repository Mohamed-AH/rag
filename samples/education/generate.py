"""Generate test packets for the `education_admissions` vertical.

Mirrors `samples/customs/generate.py`: one consistent applicant rendered as digital PDFs
(text layer -> text path), scanned-style PNGs (no text layer -> multimodal path), and two
combined multi-page PDFs. A clean packet should audit CLEAR against `education.yaml`.

Run:  pip install fpdf2 Pillow  &&  python samples/education/generate.py

NOTE: the dates below are chosen to be valid "now"; if you test far in the future, bump
`TEST_DATE` (must be < 2 years old) and `EXPIRY` (must be in the future).
"""

import os
import random

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

random.seed(11)
BASE = os.path.dirname(os.path.abspath(__file__))

APPLICANT = "Jane Q. Applicant"
TEST_DATE = "2025-09-01"  # < 2 years old -> rule.language_score_recent passes
EXPIRY = "2031-01-01"  # in the future -> rule.passport_valid passes

DOCS = {
    "passport": [
        "PASSPORT",
        "",
        "Type: P        Country Code: USA",
        "Passport No: X1234567",
        "Surname: Applicant        Given Names: Jane Q",
        f"Name: {APPLICANT}",
        "Nationality: United States of America",
        "Date of Birth: 1999-03-14",
        f"Date of Expiry: {EXPIRY}",
    ],
    "transcript": [
        "ACADEMIC TRANSCRIPT",
        "",
        "Institution: State University",
        "Student Name: Jane Applicant",
        "Program: B.Sc. Computer Science",
        "Completion Date: 2025-06-15",
        "GPA: 3.8 / 4.0",
    ],
    "language_score": [
        "IELTS ACADEMIC - TEST REPORT FORM",
        "",
        f"Candidate Name: {APPLICANT}",
        f"Test Date: {TEST_DATE}",
        "Overall Band Score: 7.5",
        "Listening 7.5    Reading 7.0    Writing 7.0    Speaking 8.0",
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
    # Digital PDFs (real text layer).
    for name, lines in DOCS.items():
        pdf = FPDF(format="A4")
        pdf.add_page()
        for ln in lines:
            style = "B" if ln.isupper() and ln.strip() else ""
            pdf.set_font("Courier", style=style, size=14 if style else 11)
            pdf.cell(0, 8, ln, ln=1)
        pdf.output(f"{BASE}/digital/{name}.pdf")

    # Scanned-style PNGs (no text layer).
    big, reg = _font(30), _font(22)
    for name, lines in DOCS.items():
        w, h = 1240, 1754
        img = Image.new("RGB", (w, h), (248, 247, 243))
        d = ImageDraw.Draw(img)
        y = 90
        for ln in lines:
            f = big if (ln.isupper() and ln.strip()) else reg
            d.text((90, y), ln, fill=(17, 17, 17), font=f)
            y += 44 if f is big else 34
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
            pdf.set_font("Courier", style=style, size=14 if style else 11)
            pdf.cell(0, 8, ln, ln=1)
    pdf.output(f"{BASE}/combined/packet_digital.pdf")

    pages = [Image.open(f"{BASE}/scanned/{n}.png").convert("RGB") for n in DOCS]
    pages[0].save(f"{BASE}/combined/packet_scanned.pdf", save_all=True, append_images=pages[1:])

    print("digital:", sorted(os.listdir(f"{BASE}/digital")))
    print("scanned:", sorted(os.listdir(f"{BASE}/scanned")))
    print("combined:", sorted(os.listdir(f"{BASE}/combined")))


if __name__ == "__main__":
    main()
