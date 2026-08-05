"""
Scrapes the two things that genuinely benefit from a tight refresh:
- All four indices (EGX30, SHARIAH, EGX70, EGX100) from Indices.aspx
- The full per-stock prices table from prices.aspx (Market Segment view)

Meant to run every 15 minutes during trading hours (see the
egx_fast.yml workflow). Writes egx_prices.json.
"""

import json
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from egx_common import (
    launch_browser_context, human_delay, now_utc, safe_num,
    parse_panel_metrics, parse_prices_table,
    load_company_codes, attach_company_codes,
)

OUTPUT_FILE = "egx_prices.json"
RADGRID_SELECTOR = "table#ctl00_C_S_RadGrid2_ctl00"


def scrape_indices(page):
    indices_output = {}
    postback_actions = {
        "EGX30": "ctl00$C$M$lnkEGX30",
        "SHARIAH": "ctl00$C$M$lnkSHARIAH",
        "EGX70": "ctl00$C$M$lnkEGX70EWI",
        "EGX100": "ctl00$C$M$lnkEGX100EWI",
    }

    print("Navigating to Indices Workspace...")
    try:
        page.goto("https://www.egx.com.eg/en/Indices.aspx", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

        for tracking_name, event_target in postback_actions.items():
            print(f"[*] Processing panel view click for: {tracking_name}")
            page.evaluate(f"__doPostBack('{event_target}', '');")
            page.wait_for_timeout(5000)

            metrics = parse_panel_metrics(page.content())
            if metrics.get("value") is not None:
                indices_output[tracking_name] = metrics
                print(f"[+] Extracted panel data for {tracking_name}: {metrics['value']}")
            else:
                print(f"[-] Structural parse returned empty for {tracking_name}")
                indices_output[tracking_name] = {k: None for k in
                    ["date", "value", "open", "high", "low", "change_pct", "ytd_pct"]}
    except Exception as loop_error:
        print(f"[-] Catastrophic stop on indices interface: {loop_error}")

    return indices_output


def scrape_prices(page):
    prices = []
    print("\nNavigating to Prices (Market Watch)...")
    try:
        page.goto("https://www.egx.com.eg/en/prices.aspx", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

        market_link_selector = "[id$='lkMarket']"
        link_count = page.locator(market_link_selector).count()

        if link_count > 0:
            # Fresh sessions default to the "Company" tab (just a search
            # box - no data at all). The full listing lives under the
            # "Market Segment" tab's RadGrid2. Trigger its postback
            # directly (matches the link's own javascript: href exactly)
            # rather than relying on Playwright's click simulating a
            # javascript: href, which proved unreliable in headless.
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

            grid_appeared = False
            for attempt in range(1, 3):
                print(f"[*] Triggering Market Segment postback directly (attempt {attempt}/2)...")
                try:
                    page.evaluate("__doPostBack('ctl00$C$S$lkMarket', '');")
                    page.wait_for_selector(RADGRID_SELECTOR, timeout=20000)
                    print("[+] RadGrid2 (Market Segment grid) appeared.")
                    grid_appeared = True
                    break
                except Exception as wait_err:
                    print(f"[!] RadGrid2 didn't appear on attempt {attempt}: {wait_err}")
                    page.wait_for_timeout(2000)

            if not grid_appeared:
                print("[!] RadGrid2 never appeared after 2 attempts.")

        html = page.content()
        prices = parse_prices_table(html)

        if not prices:
            print("[-] No RadGrid2 rows parsed. Table ids present on page:")
            soup_dbg = BeautifulSoup(html, "html.parser")
            for t in soup_dbg.find_all("table"):
                print(f"[diag]   id={t.get('id')!r} class={t.get('class')!r}")

        company_codes = load_company_codes()
        attach_company_codes(prices, company_codes)
        matched = sum(1 for s in prices if "code" in s)
        print(f"[+] Successfully scraped {len(prices)} stock prices "
              f"({matched}/{len(prices)} matched to a ticker code).")
    except Exception as prices_error:
        print(f"[-] Failed to fetch full stock prices: {prices_error}")

    return prices


def main():
    indices_output = {}
    prices = []

    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        indices_output = scrape_indices(page)
        human_delay()
        prices = scrape_prices(page)

        context.close()
        browser.close()

    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": now_utc(),
        "indices": indices_output,
        "prices": prices,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nFast run complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
