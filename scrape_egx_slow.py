"""
Scrapes everything that doesn't need a tight refresh: top gainers/losers,
market summary stats, news, sectors, disclosures, live market status +
index charts, investor activity, and index constituents.

Meant to run hourly during trading hours (see egx_slow.yml) - all of
this changes slowly enough that a 15-minute or 5-minute cadence would
just be extra load on EGX for no real freshness gain. Writes
egx_other.json.
"""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from egx_common import (
    launch_browser_context, human_delay, now_utc,
    parse_gl_table, load_company_codes, attach_company_codes,
    parse_market_summary, parse_news_grid, parse_sectors,
    parse_live_market_status, parse_chart_data, normalize_chart_index_name,
    fetch_investor_json, parse_investor_tables, parse_pie_chart, parse_stack_chart,
    parse_index_constituents,
)

OUTPUT_FILE = "egx_other.json"


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


def scrape_market_summary(page):
    market_summary = {"main_market": {}, "breadth": {}}
    print("\nNavigating to Market Summary...")
    try:
        page.goto("https://www.egx.com.eg/en/MarketSummry.aspx", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        market_summary = parse_market_summary(page.content())
        print("[+] Successfully scraped market summary.")
    except Exception as ms_error:
        print(f"[-] Failed to fetch Market Summary: {ms_error}")
    return market_summary


def scrape_news(page):
    news = []
    print("\nNavigating to News List...")
    try:
        page.goto("https://www.egx.com.eg/en/NewsList.aspx", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        news = parse_news_grid(page.content(), "ctl00_C_N_GridView1")
        print(f"[+] Successfully scraped {len(news)} news items.")
    except Exception as news_error:
        print(f"[-] Failed to fetch News: {news_error}")
    return news


def scrape_sectors(page):
    sectors = []
    print("\nNavigating to Market Watch - Sectors...")
    try:
        page.goto("https://www.egx.com.eg/en/MarketWatchSectors.aspx", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        sectors = parse_sectors(page.content())
        print(f"[+] Successfully scraped {len(sectors)} sectors.")
    except Exception as sectors_error:
        print(f"[-] Failed to fetch Sectors: {sectors_error}")
    return sectors


def scrape_disclosures(page):
    disclosures = []
    print("\nNavigating to Disclosures search...")
    try:
        today = datetime.now(timezone.utc)
        three_months_ago = today - timedelta(days=90)
        from_str = three_months_ago.strftime("%d/%m/%Y")
        to_str = today.strftime("%d/%m/%Y")
        disclosures_url = f"https://www.egx.com.eg/en/NewsSearch.aspx?com=&word=&from={from_str}&to={to_str}&isin=&sec_id=20"

        page.goto(disclosures_url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
        disclosures = parse_news_grid(page.content(), "ctl00_C_N_GVNews")
        print(f"[+] Successfully scraped {len(disclosures)} disclosures.")
    except Exception as disc_error:
        print(f"[-] Failed to fetch Disclosures: {disc_error}")
    return disclosures


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


def scrape_index_constituents(page):
    index_constituents = {"EGX30": [], "SHARIAH": [], "EGX70": [], "EGX100": []}
    constituent_endpoints = {
        "EGX30": "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=1&nav=1",
        "SHARIAH": "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=22&nav=22",
        "EGX70": "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=16&nav=16",
        "EGX100": "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=5&nav=4",
    }

    for index_name, endpoint_url in constituent_endpoints.items():
        print(f"\nNavigating to {index_name} Constituents...")
        try:
            page.goto(endpoint_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            parsed_stocks = parse_index_constituents(page.content())
            index_constituents[index_name] = parsed_stocks
            print(f"[+] Successfully scraped {len(parsed_stocks)} {index_name} constituents.")
        except Exception as cic_error:
            print(f"[-] Failed to fetch {index_name} constituents: {cic_error}")

    return index_constituents


def main():
    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        gainers, losers = scrape_gainers_losers(page)
        human_delay()

        market_summary = scrape_market_summary(page)
        human_delay()

        news = scrape_news(page)
        human_delay()

        sectors = scrape_sectors(page)
        human_delay()

        disclosures = scrape_disclosures(page)
        human_delay()

        live_status, index_charts = scrape_live_status_and_charts(page)
        human_delay()

        investor_activity = scrape_investor_activity(context, page)
        human_delay()

        index_constituents = scrape_index_constituents(page)

        context.close()
        browser.close()

    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": now_utc(),
        "liveMarketStatus": live_status,
        "gainers": gainers,
        "losers": losers,
        "marketSummary": market_summary,
        "sectors": sectors,
        "news": news,
        "disclosures": disclosures,
        "investorActivity": investor_activity,
        "indexConstituents": index_constituents,
        "indexCharts": index_charts,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSlow run complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
