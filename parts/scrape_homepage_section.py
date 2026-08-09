"""
Runs only the homepage (live market status + index charts) section, in its
own process/runner. Writes egx_homepage.json. See egx_fast_split.yml.
"""

import json
from playwright.sync_api import sync_playwright
from egx_common import launch_browser_context
from scrape_egx_fast import scrape_live_status_and_charts

OUTPUT_FILE = "egx_homepage.json"


def main():
    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        live_status, index_charts = scrape_live_status_and_charts(page)

        context.close()
        browser.close()

    output = {"liveMarketStatus": live_status, "indexCharts": index_charts}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nHomepage section complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
