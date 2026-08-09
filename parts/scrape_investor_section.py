"""
Runs only the Investor Type Activity section, in its own process/runner.
Writes egx_investor.json. See egx_fast_split.yml.
"""

import json
from playwright.sync_api import sync_playwright
from egx_common import launch_browser_context
from scrape_egx_fast import scrape_investor_activity

OUTPUT_FILE = "egx_investor.json"


def main():
    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        investor_activity = scrape_investor_activity(context, page)

        context.close()
        browser.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"investorActivity": investor_activity}, f, indent=2, ensure_ascii=False)

    print(f"\nInvestor activity section complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
