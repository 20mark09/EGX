"""
One-off inspector, round 6. We need real ticker codes for the Top
Gainers/Losers table (parse_gl_table currently only extracts Company
Name / Currency / Prev Close / Open / Close / %CHG - no code column).

Many of EGX's ASP.NET grids link the company name to a details page
with the code/ISIN embedded in the href (that's how NewsID was found
for News/Disclosures/Bulletin). This just dumps the raw HTML so we can
check whether the same pattern holds here.
"""
from playwright.sync_api import sync_playwright

URL = "https://www.egx.com.eg/en/Top_GL.aspx"
OUTPUT_FILE = "top_gl.html"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )

        page = context.new_page()
        print(f"Navigating to {URL} ...")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000)

        try:
            page.wait_for_selector("#ctl00_C_Top_GL1_GridView1", timeout=15000)
        except Exception:
            pass

        html = page.content()
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[+] Saved {OUTPUT_FILE} ({len(html)} chars)")

        page.close()
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
