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
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from zoneinfo import ZoneInfo
from egx_common import (
    launch_browser_context, human_delay, now_utc, safe_num,
    parse_gl_table, load_company_codes, attach_company_codes,
    parse_panel_metrics, parse_prices_table,
    load_company_codes, attach_company_codes,
     parse_live_market_status, parse_chart_data, normalize_chart_index_name,
    fetch_investor_json, parse_investor_tables, parse_pie_chart, parse_stack_chart,
)

OUTPUT_FILE = "egx.json"
RADGRID_SELECTOR = "table#ctl00_C_S_RadGrid2_ctl00"

def scrape_indices(page):
    indices_output = {}
    postback_actions = {
        "SHARIAH": "ctl00$C$M$LIEGXSHARIAH",
        "EGX30": "ctl00$C$M$lnkEGX30",
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

def scrape_live_status_and_charts(page):
    live_status = {"text_ar": None, "color": None}
    index_charts = {}

    print("\nNavigating to Homepage for live market status and chart data...")
    try:
        def handle_chart_response(response):
            if "getIndexChartData" not in response.url:
                return
            try:
                query = parse_qs(urlparse(response.url).query)
                raw_index_name = query.get("index", ["UNKNOWN"])[0]
                index_name = normalize_chart_index_name(raw_name=raw_index_name)
                data_points = parse_chart_data(response.text())
                if data_points:
                    index_charts[index_name] = data_points
                    print(f"[+] Captured chart data for {index_name} ({len(data_points)} points)")
            except Exception as capture_error:
                print(f"[-] Failed to parse a captured chart response: {capture_error}")

        page.on("response", handle_chart_response)
        page.goto("https://www.egx.com.eg/ar/homepage.aspx", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)

        live_status = parse_live_market_status(page.content())
        print(f"[+] Live market status: {live_status}")

        chart_tabs = ["EGX30", "EGX_33_Shariah", "EGX70_EWI", "EGX100_EWI"]
        for tab_value in chart_tabs:
            selector = f'div[dataindex="{tab_value}"]'
            if page.locator(selector).count() > 0:
                print(f"[*] Switching chart workspace to: {tab_value}")
                try:
                    page.locator(selector).evaluate("el => el.click()")
                    page.wait_for_timeout(3000)
                except Exception as click_error:
                    print(f"[-] Interaction skipped on tab {tab_value}: {click_error}")
    except Exception as status_error:
        print(f"[-] Failed to fetch live market status/chart data: {status_error}")

    return live_status, index_charts

def scrape_gainers_losers(page):
    gainers, losers = [], []
    print("\nNavigating to Top Gainers/Losers Desk...")
    try:
        page.goto("https://www.egx.com.eg/en/Top_GL.aspx", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        gl_soup = BeautifulSoup(page.content(), "html.parser")
        gainers = parse_gl_table(gl_soup, "ctl00_C_Top_GL1_GridView1")
        losers = parse_gl_table(gl_soup, "ctl00_C_Top_GL1_GridView2")

        company_codes = load_company_codes()
        attach_company_codes(gainers, company_codes)
        attach_company_codes(losers, company_codes)
        matched = sum(1 for m in gainers + losers if "code" in m)
        print(f"[+] Successfully scraped {len(gainers)} gainers and {len(losers)} losers "
              f"({matched}/{len(gainers) + len(losers)} matched to a ticker code).")
    except Exception as gl_error:
        print(f"[-] Failed to fetch Top Gainers/Losers: {gl_error}")
    return gainers, losers

def scrape_investor_activity(context, page):
    investor_activity = {
        "byGroup": {}, "nationalityBreakdownPct": [],
        "individualsByNationality": [], "institutionsByNationality": [],
    }
    print("\nFetching Investor Type data...")
    try:
        investor_referer = "https://www.egx.com.eg/en/InvestorsTypeCharts.aspx"
        page.goto(investor_referer, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)

        tables_raw = fetch_investor_json(context, "https://www.egx.com.eg/WebService.asmx/GetInvestorTables?Lang=ar&SB=1", investor_referer)
        investor_activity["byGroup"] = parse_investor_tables(tables_raw)

        pie2_raw = fetch_investor_json(context, "https://www.egx.com.eg/WebService.asmx/InvPieCharts?Lang=ar&SB=1&Type=2", investor_referer)
        investor_activity["nationalityBreakdownPct"] = parse_pie_chart(pie2_raw)

        indiv_raw = fetch_investor_json(context, "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart?Lang=ar&SB=1&Type=1", investor_referer)
        investor_activity["individualsByNationality"] = parse_stack_chart(indiv_raw)

        inst_raw = fetch_investor_json(context, "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart?Lang=ar&SB=1&Type=2", investor_referer)
        investor_activity["institutionsByNationality"] = parse_stack_chart(inst_raw)

        populated = {k: len(v) for k, v in investor_activity.items()}
        print(f"[+] Investor activity fetch complete. Populated counts: {populated}")
        if all(count == 0 for count in populated.values()):
            print("[-] WARNING: all investor activity fields came back empty. "
                  "This has happened before due to EGX's own backend erroring "
                  "(e.g. a raw Oracle DB error instead of JSON) - check the "
                  "'[-] Non-JSON response' lines above for the actual cause.")
    except Exception as inv_error:
        print(f"[-] Failed to fetch Investor Type data: {inv_error}")

    return investor_activity
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
        live_status, index_charts = scrape_live_status_and_charts(page)
        human_delay()
        gainers, losers = scrape_gainers_losers(page)
        human_delay()
        prices = scrape_prices(page)
        investor_activity = scrape_investor_activity(context, page)
        human_delay()

        context.close()
        browser.close()

    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": datetime.now(ZoneInfo("Africa/Cairo")),
        "indices": indices_output,
        "prices": prices,
        "gainers": gainers,
        "losers": losers,
        "liveMarketStatus": live_status,
        "indexCharts": index_charts,
        "investorActivity": investor_activity
        
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nFast run complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
