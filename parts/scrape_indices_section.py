"""
Runs only the Indices section of the fast scrape, in its own process (and,
when run as its own job in egx_fast_split.yml, its own GitHub Actions
runner/IP - see that workflow for why this is split out).

Writes egx_indices.json. If this job's IP gets blocked, only this file is
missing/empty - the other sections' jobs are unaffected.
"""

import json
from playwright.sync_api import sync_playwright
from egx_common import launch_browser_context
from scrape_egx_fast import scrape_indices

OUTPUT_FILE = "egx_indices.json"


def main():
    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        indices_output = scrape_indices(page)

        context.close()
        browser.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"indices": indices_output}, f, indent=2, ensure_ascii=False)

    print(f"\nIndices section complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
