"""
Scrapes Bulletin News and sends a push notification for anything new.
This is the one thing that needs to be as fresh as possible - meant to
run every 5 minutes (see egx_bulletin.yml; that's the shortest interval
GitHub Actions' scheduled workflows practically support). Writes
egx_bulletin.json and updates bulletin_state.json.
"""

import json
from playwright.sync_api import sync_playwright

from egx_common import (
    launch_browser_context, now_utc,
    parse_news_grid, notify_new_bulletins,
)

OUTPUT_FILE = "egx_bulletin.json"


def main():
    bulletin = []

    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        print("Navigating to Bulletin News...")
        try:
            page.goto("https://www.egx.com.eg/ar/BulletinNews.aspx", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            bulletin = parse_news_grid(page.content(), "ctl00_C_BulletinNews1_GVNews", base_url="https://www.egx.com.eg/ar/")
            print(f"[+] Successfully scraped {len(bulletin)} bulletin items.")
            notify_new_bulletins(bulletin)
        except Exception as bulletin_error:
            print(f"[-] Failed to fetch Bulletin: {bulletin_error}")

        context.close()
        browser.close()

    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": now_utc(),
        "bulletin": bulletin,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nBulletin run complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
