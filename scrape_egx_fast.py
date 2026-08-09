"""
Scrapes the two things that genuinely benefit from a tight refresh:
- All four indices (EGX30, SHARIAH, EGX70, EGX100) from Indices.aspx
- The full per-stock prices table from prices.aspx (Market Segment view)

Meant to run every 15 minutes during trading hours (see the
egx_fast.yml workflow). Writes egx_prices.json.

Anti-blocking notes (see debug_dumps/ for evidence on any given run):
- Each section below runs in its own fresh browser context (new cookies,
  new connection) instead of one long-lived session doing 5 page loads
  and ~10 postback/XHR calls back to back - that whole-run-as-one-crawl
  shape is a much stronger automation signature than any single request.
- All waits are randomized ranges, not fixed durations - a flat 5000ms
  wait on every postback is itself a fingerprint.
- Every goto goes through goto_resilient (retry + backoff + a debug
  snapshot on final failure), so a bad run tells you *what actually came
  back* (challenge page? reset? something else?) instead of just an
  exception.
"""

import json
import random
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from zoneinfo import ZoneInfo
from egx_common import (
    launch_browser_context, new_page_fresh_context, human_delay,
    jittered_delay, now_utc, safe_num, dump_debug_snapshot, goto_resilient,
    parse_gl_table, load_company_codes, attach_company_codes,
    parse_panel_metrics, parse_prices_table,
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
    empty_metrics = {k: None for k in
        ["date", "value", "open", "high", "low", "change_pct", "ytd_pct"]}

    if not goto_resilient(page, "https://www.egx.com.eg/en/Indices.aspx", "indices_page"):
        return {name: dict(empty_metrics) for name in postback_actions}

    page.wait_for_timeout(random.randint(3000, 5500))

    try:
        for tracking_name, event_target in postback_actions.items():
            print(f"[*] Processing panel view click for: {tracking_name}")

            has_postback = page.evaluate("typeof __doPostBack === 'function'")
            if not has_postback:
                print(f"[-] __doPostBack not defined on page for {tracking_name} - "
                      f"likely a challenge/blocked page rather than the real site.")
                dump_debug_snapshot(page, f"indices_{tracking_name}_no_postback")
                indices_output[tracking_name] = dict(empty_metrics)
                continue

            try:
                page.evaluate(f"__doPostBack('{event_target}', '');")
            except Exception as postback_err:
                print(f"[-] __doPostBack call failed for {tracking_name}: {postback_err}")
                dump_debug_snapshot(page, f"indices_{tracking_name}_postback_error")
                indices_output[tracking_name] = dict(empty_metrics)
                continue

            page.wait_for_timeout(random.randint(4000, 8000))

            metrics = parse_panel_metrics(page.content())
            if metrics.get("value") is not None:
                indices_output[tracking_name] = metrics
                print(f"[+] Extracted panel data for {tracking_name}: {metrics['value']}")
            else:
                print(f"[-] Structural parse returned empty for {tracking_name}")
                dump_debug_snapshot(page, f"indices_{tracking_name}_empty_parse")
                indices_output[tracking_name] = dict(empty_metrics)

            jittered_delay(1.5, 3.5)
    except Exception as loop_error:
        print(f"[-] Catastrophic stop on indices interface: {loop_error}")
        dump_debug_snapshot(page, "indices_loop_exception")
        for name in postback_actions:
            indices_output.setdefault(name, dict(empty_metrics))

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

        if not goto_resilient(page, "https://www.egx.com.eg/ar/homepage.aspx", "homepage"):
            return live_status, index_charts

        page.wait_for_timeout(random.randint(5000, 8000))

        live_status = parse_live_market_status(page.content())
        print(f"[+] Live market status: {live_status}")
        if live_status.get("text_ar") is None:
            dump_debug_snapshot(page, "homepage_no_live_status")

        chart_tabs = ["EGX30", "EGX_33_Shariah", "EGX70_EWI", "EGX100_EWI"]
        for tab_value in chart_tabs:
            selector = f'div[dataindex="{tab_value}"]'
            if page.locator(selector).count() > 0:
                print(f"[*] Switching chart workspace to: {tab_value}")
                try:
                    page.locator(selector).evaluate("el => el.click()")
                    page.wait_for_timeout(random.randint(2000, 4500))
                except Exception as click_error:
                    print(f"[-] Interaction skipped on tab {tab_value}: {click_error}")
    except Exception as status_error:
        print(f"[-] Failed to fetch live market status/chart data: {status_error}")
        dump_debug_snapshot(page, "live_status_exception")

    return live_status, index_charts

def scrape_gainers_losers(page):
    gainers, losers = [], []
    print("\nNavigating to Top Gainers/Losers Desk...")
    try:
        if not goto_resilient(page, "https://www.egx.com.eg/en/Top_GL.aspx", "top_gl"):
            return gainers, losers

        page.wait_for_timeout(random.randint(3000, 5500))
        gl_soup = BeautifulSoup(page.content(), "html.parser")
        gainers = parse_gl_table(gl_soup, "ctl00_C_Top_GL1_GridView1")
        losers = parse_gl_table(gl_soup, "ctl00_C_Top_GL1_GridView2")

        if not gainers and not losers:
            dump_debug_snapshot(page, "top_gl_empty_parse")

        company_codes = load_company_codes()
        attach_company_codes(gainers, company_codes)
        attach_company_codes(losers, company_codes)
        matched = sum(1 for m in gainers + losers if "code" in m)
        print(f"[+] Successfully scraped {len(gainers)} gainers and {len(losers)} losers "
              f"({matched}/{len(gainers) + len(losers)} matched to a ticker code).")
    except Exception as gl_error:
        print(f"[-] Failed to fetch Top Gainers/Losers: {gl_error}")
        dump_debug_snapshot(page, "top_gl_exception")
    return gainers, losers

def scrape_investor_activity(context, page):
    investor_activity = {
        "byGroup": {}, "nationalityBreakdownPct": [],
        "individualsByNationality": [], "institutionsByNationality": [],
    }
    print("\nFetching Investor Type data...")
    try:
        investor_referer = "https://www.egx.com.eg/en/InvestorsTypeCharts.aspx"
        if not goto_resilient(page, investor_referer, "investor_type_page"):
            return investor_activity

        page.wait_for_timeout(random.randint(5000, 8000))

        # fetch_investor_json now POSTs a JSON body (base URL + params
        # dict) instead of GET-with-querystring - see its docstring for
        # why the GET form was reliably failing regardless of client.
        tables_raw = fetch_investor_json(page, "https://www.egx.com.eg/WebService.asmx/GetInvestorTables", {"Lang": "ar", "SB": 1}, investor_referer)
        investor_activity["byGroup"] = parse_investor_tables(tables_raw)
        jittered_delay(1.0, 2.5)

        pie2_raw = fetch_investor_json(page, "https://www.egx.com.eg/WebService.asmx/InvPieCharts", {"Lang": "ar", "SB": 1, "Type": 2}, investor_referer)
        investor_activity["nationalityBreakdownPct"] = parse_pie_chart(pie2_raw)
        jittered_delay(1.0, 2.5)

        indiv_raw = fetch_investor_json(page, "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart", {"Lang": "ar", "SB": 1, "Type": 1}, investor_referer)
        investor_activity["individualsByNationality"] = parse_stack_chart(indiv_raw)
        jittered_delay(1.0, 2.5)

        inst_raw = fetch_investor_json(page, "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart", {"Lang": "ar", "SB": 1, "Type": 2}, investor_referer)
        investor_activity["institutionsByNationality"] = parse_stack_chart(inst_raw)

        populated = {k: len(v) for k, v in investor_activity.items()}
        print(f"[+] Investor activity fetch complete. Populated counts: {populated}")
        if all(count == 0 for count in populated.values()):
            print("[-] WARNING: all investor activity fields came back empty. "
                  "This has happened before due to EGX's own backend erroring "
                  "(e.g. a raw Oracle DB error instead of JSON) - check the "
                  "'[-] Non-JSON response' lines above for the actual cause.")
            dump_debug_snapshot(page, "investor_activity_all_empty")
    except Exception as inv_error:
        print(f"[-] Failed to fetch Investor Type data: {inv_error}")
        dump_debug_snapshot(page, "investor_activity_exception")

    return investor_activity

def scrape_prices(page):
    prices = []
    print("\nNavigating to Prices (Market Watch)...")
    try:
        if not goto_resilient(page, "https://www.egx.com.eg/en/prices.aspx", "prices_page"):
            return prices

        page.wait_for_timeout(random.randint(3000, 5500))

        market_link_selector = "[id$='lkMarket']"
        link_count = page.locator(market_link_selector).count()

        if link_count == 0:
            print("[-] Market Segment link ([id$='lkMarket']) not found on page at all - "
                  "different failure mode than the postback-fires-but-grid-never-appears "
                  "case (that one still has 'Triggering Market Segment postback' logged "
                  "before it fails; this one doesn't reach that point). Could be a timing "
                  "issue (page not fully rendered yet) or a structural change/partial block.")
            dump_debug_snapshot(page, "prices_no_market_link")

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

            # Log the postback's own network response (status + length) so
            # a "grid never appeared" failure tells us whether the server
            # actually rejected/redirected the postback vs. returned the
            # same (unchanged) view vs. something else entirely - the
            # difference between those is invisible from the DOM alone.
            postback_responses = []

            def handle_postback_response(response):
                if response.request.method == "POST" and "prices.aspx" in response.url:
                    postback_responses.append((response.status, response.url))
                    print(f"[debug] postback response: status={response.status} url={response.url}")

            page.on("response", handle_postback_response)

            grid_appeared = False
            for attempt in range(1, 3):
                print(f"[*] Triggering Market Segment postback directly (attempt {attempt}/2)...")
                try:
                    has_postback = page.evaluate("typeof __doPostBack === 'function'")
                    if not has_postback:
                        print(f"[-] __doPostBack not defined on prices page (attempt {attempt}) - "
                              f"likely a challenge/blocked page.")
                        dump_debug_snapshot(page, f"prices_no_postback_attempt{attempt}")
                        break
                    page.evaluate("__doPostBack('ctl00$C$S$lkMarket', '');")
                    page.wait_for_selector(RADGRID_SELECTOR, timeout=25000)
                    print("[+] RadGrid2 (Market Segment grid) appeared.")
                    grid_appeared = True
                    break
                except Exception as wait_err:
                    print(f"[!] RadGrid2 didn't appear on attempt {attempt}: {wait_err}")
                    # If the postback response(s) came back 200 but the grid
                    # still isn't there, we're not being blocked - the
                    # server is returning content, just not the tab we
                    # asked for (stale ViewState/EVENTVALIDATION after the
                    # wait, wrong event target, etc). If there's no 200 at
                    # all, that points back to a block on the postback
                    # request itself instead.
                    still_on_company_tab = page.locator("#ctl00_C_S_company").count() > 0
                    print(f"[debug] postback responses so far: {postback_responses}; "
                          f"still shows Company-tab search box: {still_on_company_tab}")
                    page.wait_for_timeout(random.randint(1500, 3000))

            page.remove_listener("response", handle_postback_response)

            if not grid_appeared:
                print("[!] RadGrid2 never appeared after 2 attempts.")
                dump_debug_snapshot(page, "prices_radgrid_never_appeared")

        html = page.content()
        prices = parse_prices_table(html)

        if not prices:
            print("[-] No RadGrid2 rows parsed. Table ids present on page:")
            soup_dbg = BeautifulSoup(html, "html.parser")
            for t in soup_dbg.find_all("table"):
                print(f"[diag]   id={t.get('id')!r} class={t.get('class')!r}")
            dump_debug_snapshot(page, "prices_empty_parse")

        company_codes = load_company_codes()
        attach_company_codes(prices, company_codes)
        matched = sum(1 for s in prices if "code" in s)
        print(f"[+] Successfully scraped {len(prices)} stock prices "
              f"({matched}/{len(prices)} matched to a ticker code).")
    except Exception as prices_error:
        print(f"[-] Failed to fetch full stock prices: {prices_error}")
        dump_debug_snapshot(page, "prices_exception")

    return prices


def main():
    indices_output = {}
    prices = []
    live_status = {"text_ar": None, "color": None}
    index_charts = {}
    gainers, losers = [], []
    investor_activity = {}

    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        # Each section below gets its own fresh context (see
        # new_page_fresh_context in egx_common.py) - no shared cookies or
        # kept-alive connection carrying over from the previous section,
        # so the run doesn't look like one continuous crawl of the site.

        indices_output = scrape_indices(page)
        jittered_delay(6, 12)

        context, page = new_page_fresh_context(browser, context)
        live_status, index_charts = scrape_live_status_and_charts(page)
        jittered_delay(6, 12)

        context, page = new_page_fresh_context(browser, context)
        gainers, losers = scrape_gainers_losers(page)
        jittered_delay(6, 12)

        context, page = new_page_fresh_context(browser, context)
        prices = scrape_prices(page)
        jittered_delay(6, 12)

        context, page = new_page_fresh_context(browser, context)
        investor_activity = scrape_investor_activity(context, page)

        context.close()
        browser.close()

    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d %I:%M:%S %p"),
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
