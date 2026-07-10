"""
One-off inspector, round 2. Does NOT parse anything - just saves the fully
rendered HTML of the target pages so we can find the real table/element
IDs and build proper parsers next.

Same browser launch pattern (stealth args, wait timings, UA) as
scrape_egx.py.

NOTE on NewsSearch.aspx: the from/to dates in that URL are whatever your
browser generated when you opened the page (looks like a rolling 3-month
window). Feel free to edit NEWS_SEARCH_URL below to widen/narrow it before
running - the params are just a normal querystring.
"""
from playwright.sync_api import sync_playwright

NEWS_SEARCH_URL = (
    "https://www.egx.com.eg/en/NewsSearch.aspx"
    "?com=&word=&from=10/04/2026&to=10/07/2026&isin=&sec_id=20"
)

TARGETS = {
    "market_watch_sectors.html": "https://www.egx.com.eg/en/MarketWatchSectors.aspx",
    "investors_type_charts.html": "https://www.egx.com.eg/en/InvestorsTypeCharts.aspx",
    "news_search_disclosures.html": NEWS_SEARCH_URL,
    "bulletin_news_ar.html": "https://www.egx.com.eg/ar/BulletinNews.aspx",
    "homepage_ar.html": "https://www.egx.com.eg/ar/homepage.aspx",
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
            try:
                page.goto(url, wait_until="commit", timeout=60000)

                print("Pausing 10 seconds to let JavaScript firewall challenge pass...")
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
