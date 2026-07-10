"""
One-off inspector. Does NOT parse anything - just saves the fully
rendered HTML of the target pages so we can see the real table/element
IDs and build proper parsers next, the same way Top_GL.aspx was mapped
out for the existing scraper.

Uses the exact same browser launch pattern (stealth args, wait timings,
UA) as scrape_egx.py so we don't trip anything the working scraper
doesn't already trip.
"""
from playwright.sync_api import sync_playwright

TARGETS = {
    "market_summary.html": "https://www.egx.com.eg/en/MarketSummry.aspx",
    "news_list.html": "https://www.egx.com.eg/en/NewsList.aspx",
}


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

        for filename, url in TARGETS.items():
            print(f"Navigating to {url} ...")
            page = context.new_page()
            page.goto(url, wait_until="commit", timeout=60000)

            print("Pausing 10 seconds to let JavaScript firewall challenge pass...")
            page.wait_for_timeout(10000)

            html = page.content()
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[+] Saved {filename} ({len(html)} chars)")

            page.close()

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
