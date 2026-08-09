"""
Runs only the Top Gainers/Losers section, in its own process/runner.
Writes egx_gl.json. See egx_fast_split.yml.
"""

import json
from playwright.sync_api import sync_playwright
from egx_common import launch_browser_context
from scrape_egx_fast import scrape_gainers_losers

OUTPUT_FILE = "egx_gl.json"


def main():
    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        gainers, losers = scrape_gainers_losers(page)

        context.close()
        browser.close()

    output = {"gainers": gainers, "losers": losers}
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nGainers/Losers section complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
