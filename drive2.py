from playwright.sync_api import sync_playwright
import glob
exe=glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")[0]
with sync_playwright() as p:
    b=p.chromium.launch(executable_path=exe); errs=[]
    pg=b.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8393/", wait_until="networkidle"); pg.wait_for_timeout(300)
    # open keys panel, pick Groq, enter key, save
    pg.locator("#keysDetails summary").click(); pg.wait_for_timeout(100)
    pg.select_option("#keyProvider","groq")
    pg.fill("#byoKey","gsk-test-123")
    pg.locator("#saveKey").click(); pg.wait_for_timeout(150)
    pill = pg.locator("#keyMode").text_content()
    # verify the header the app would send
    hdr = pg.evaluate("authHeaders()")
    print("pill:", pill.strip())
    print("authHeaders:", hdr)
    # switch to mistral, clear -> shared
    pg.locator("#clearKey").click(); pg.wait_for_timeout(100)
    print("after clear pill:", pg.locator("#keyMode").text_content().strip())
    print("after clear authHeaders:", pg.evaluate("authHeaders()"))
    print("JS ERRORS:", errs or "none")
    b.close()
