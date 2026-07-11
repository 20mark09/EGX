"""
One-off inspector, round 4. Dumps CurrentIndexConstituntes.aspx for a
range of `type` values so we can see which type maps to which index
(EGX30/SHARIAH/EGX70/EGX100) and find the real table/element IDs for
constituent name + weight %.

Same stealth Playwright pattern as always.
"""
from playwright.sync_api import sync_playwright

BASE = "https://www.egx.com.eg/ar/currentindexconstituntes.aspx"

# Grabbing a spread of `type` values since we don't know the mapping yet -
# whichever ones turn out to be EGX30/SHARIAH/EGX70/EGX100 is what matters.
TYPES = [1, 2, 3, 4, 5, 6]


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

        for t in TYPES:
            url = f"{BASE}?type={t}&nav=1"
            filename = f"constituents_type{t}.html"
            print(f"Navigating to {url} ...")
            page = context.new_page()
            try:
                page.goto(url, wait_until="commit", timeout=60000)
                page.wait_for_timeout(10000)
                html = page.content()
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"[+] Saved {filename} ({len(html)} chars)")
            except Exception as e:
                print(f"[-] Failed on {url}: {e}")
            finally:
                page.close()

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
