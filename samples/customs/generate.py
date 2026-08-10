import os, random
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont, ImageFilter

random.seed(7)
BASE = "/home/user/rag/samples/customs"

# One coherent, internally-consistent shipment. A clean packet should audit CLEAR.
DOCS = {
    "commercial_invoice": [
        "COMMERCIAL INVOICE",
        "",
        "Exporter / Shipper:",
        "  Acme Manufacturing Ltd",
        "  12 Industrial Road, Shenzhen, China",
        "",
        "Consignee:",
        "  Globex Imports Inc",
        "  400 Market Street, San Francisco, CA 94105, USA",
        "",
        "Invoice No: INV-2026-0815        Date: 2026-08-05",
        "Country of Origin: China",
        "HTS Code: 8471.30.0100",
        "Currency: USD",
        "",
        "Description: Portable computers (laptops)",
        "Quantity: 200 units        Unit Price (USD): 50.00",
        "Total Cartons: 20",
        "Net Weight: 500 kg        Gross Weight: 540 kg",
        "Total Amount (USD): 10000.00",
    ],
    "packing_list": [
        "PACKING LIST",
        "",
        "Exporter: Acme Manufacturing Ltd, Shenzhen, China",
        "Consignee: Globex Imports Inc, San Francisco, CA, USA",
        "Reference Invoice: INV-2026-0815",
        "",
        "Marks & Numbers: GLOBEX / SF",
        "Description: Portable computers (laptops)",
        "Total Cartons: 20",
        "Net Weight: 500 kg",
        "Gross Weight: 540 kg",
        "Total Amount (USD): 10000.00",
    ],
    "bill_of_lading": [
        "BILL OF LADING",
        "",
        "Shipper: Acme Manufacturing Ltd, 12 Industrial Road, Shenzhen, China",
        "Consignee: Globex Imports Inc, 400 Market Street, San Francisco, CA, USA",
        "B/L No: MAEU123456789        Vessel: MAERSK ELBA",
        "Port of Loading: Shenzhen, CN     Port of Discharge: Oakland, US",
        "",
        "Description of Goods: Portable computers (laptops), 20 cartons",
        "Total Cartons: 20",
        "Net Weight: 500 kg",
        "Gross Weight: 540 kg",
    ],
    "certificate_of_origin": [
        "CERTIFICATE OF ORIGIN",
        "",
        "Certificate No: COO-CN-2026-3345",
        "Exporter: Acme Manufacturing Ltd, Shenzhen, China",
        "Consignee: Globex Imports Inc, San Francisco, CA, USA",
        "",
        "Country of Origin: China",
        "Description: Portable computers (laptops), 200 units, 20 cartons",
        "Declaration: The undersigned hereby certifies that the goods",
        "described above originate in China.",
    ],
}

# ---- Digital PDFs (real text layer -> router text path) ----
for name, lines in DOCS.items():
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.set_font("Courier", size=12)
    for ln in lines:
        style = "B" if ln.isupper() and ln.strip() else ""
        pdf.set_font("Courier", style=style, size=14 if style else 11)
        pdf.cell(0, 8, ln, ln=1)
    pdf.output(f"{BASE}/digital/{name}.pdf")

# ---- Scanned-style PNGs (raster, no text layer -> router image/multimodal path) ----
def font(sz):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, sz)
    return ImageFont.load_default()

BIG, REG = font(30), font(22)
for name, lines in DOCS.items():
    W, H = 1240, 1754  # ~150 DPI A4
    img = Image.new("RGB", (W, H), (248, 247, 243))  # off-white "paper"
    d = ImageDraw.Draw(img)
    y = 90
    for ln in lines:
        f = BIG if (ln.isupper() and ln.strip()) else REG
        d.text((90, y), ln, fill=(17, 17, 17), font=f)
        y += 44 if f is BIG else 34
    # Simulate a scan: light rotation + blur + speckle noise.
    img = img.rotate(random.uniform(-1.2, 1.2), expand=False, fillcolor=(248, 247, 243))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    px = img.load()
    for _ in range(9000):
        x, yy = random.randint(0, W - 1), random.randint(0, H - 1)
        v = random.randint(150, 210)
        px[x, yy] = (v, v, v)
    img.save(f"{BASE}/scanned/{name}.png", "PNG")

print("digital:", sorted(os.listdir(f"{BASE}/digital")))
print("scanned:", sorted(os.listdir(f"{BASE}/scanned")))
