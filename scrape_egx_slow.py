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

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from datetime import datetime
from zoneinfo import ZoneInfo

from egx_common import (
    launch_browser_context, human_delay, now_utc,
    parse_market_summary, parse_news_grid, parse_sectors,
   
   
    parse_index_constituents,
)

OUTPUT_FILE = "egx_other.json"





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

        
        market_summary = scrape_market_summary(page)
        human_delay()

        news = scrape_news(page)
        human_delay()

        sectors = scrape_sectors(page)
        human_delay()

        disclosures = scrape_disclosures(page)
        human_delay()

        

        

        index_constituents = scrape_index_constituents(page)

        context.close()
        browser.close()

    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d %I:%M:%S %p"),
        "marketSummary": market_summary,
        "sectors": sectors,
        "news": news,
        "disclosures": disclosures,
        
        "indexConstituents": index_constituents,
        
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSlow run complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
