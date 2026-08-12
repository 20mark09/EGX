"""
Full replacement for the old ASP.NET-site scraper, rewritten against
beta.egx.com.eg's /api/bff/egx/* JSON API. See egx_beta_common.py for
why the requests are shaped the way they are.

Confirmed endpoints (as of Aug 2026):
- market-status                      GET   open/closed indicator
- indices-summary?indexName=X        GET   per-index summary panel
- index-data?interval=1&indexName=X  GET   per-index chart points
- market-watch?Page=&PageSize=...    GET   full stock prices table (paginated)
- egx30-weights                      GET   EGX30 constituents + weights
- index-constituents?indexName=X     GET   constituents for other indices
- investor-full-statistics           GET   investor type/nationality breakdown
- news-search                        POST  media-center news/disclosures

indexName values seen in the wild: CASE30 (=EGX30), EGX_SHARIAH,
EGX70_EWI, EGX100_EWI. CASE30 has its own weights endpoint (egx30-weights)
rather than the general index-constituents one - presumably because it's
cap-weighted while the EWI ("equal weighted index") ones aren't.

KNOWN GAP: news-search's secIds parameter was only confirmed for the
"disclosure" media-center tab ([3,4,5,6,7,8,16]). The "financials" and
"listing" tabs almost certainly use the same endpoint with a different
secIds list, but that hasn't been confirmed with a real curl yet - see
NEWS_TABS below. Fill in the real secIds once you grab those two tabs'
network requests; until then this only pulls "disclosure".
"""

import json
from datetime import datetime, timedelta
from egx_beta_common import (
    launch_browser_context, warm_up_session, bff_get, bff_post, now_cairo_str,
)
from playwright.sync_api import sync_playwright

OUTPUT_FILE = "egx.json"

INDEX_NAMES = ["CASE30", "EGX_SHARIAH", "EGX70_EWI", "EGX100_EWI"]

# Friendly labels for output, matching the old scraper's naming so
# downstream consumers of egx.json don't need to change.
INDEX_LABELS = {
    "CASE30": "EGX30",
    "EGX_SHARIAH": "SHARIAH",
    "EGX70_EWI": "EGX70",
    "EGX100_EWI": "EGX100",
}

# CONFIRMED from a real curl (media-center?tab=disclosure).
# UNCONFIRMED for financials/listing - same shape assumed, needs
# verification. If these are wrong, news_by_tab below will just come
# back with whatever secIds actually maps to (i.e. probably still
# "disclosure"-like content) rather than erroring outright.
NEWS_TABS = {
    "disclosure": [3, 4, 5, 6, 7, 8, 16],
    "financials": None,   # TODO: confirm real secIds via DevTools
    "listing": None,      # TODO: confirm real secIds via DevTools
}


def scrape_market_status(page):
    data = bff_get(page, "market-status")
    if data and data.get("success"):
        d = data["data"]
        return {"status": d.get("status"), "status_ar": d.get("status_Ar"), "statusDate": d.get("statusDate")}
    return {"status": None, "status_ar": None, "statusDate": None}


def scrape_indices(page):
    """Summary panel (value/open/high/low/change) for all four indices."""
    indices_output = {}
    for index_name in INDEX_NAMES:
        label = INDEX_LABELS[index_name]
        data = bff_get(page, "indices-summary", {"indexName": index_name})
        if data and data.get("success"):
            d = data["data"]
            indices_output[label] = {
                "value": d.get("value"),
                "valPer": d.get("valPer"),
                "volume": d.get("volume"),
                "volPer": d.get("volPer"),
                "trades": d.get("trades"),
                "trdPer": d.get("trdPer"),
                "mc": d.get("mc"),
                "mcPer": d.get("mcPer"),
            }
            print(f"[+] {label} summary: {indices_output[label]}")
        else:
            print(f"[-] {label} summary failed")
            indices_output[label] = None
    return indices_output


def scrape_index_charts(page, interval=1):
    """Chart points + live OHLC for all four indices."""
    charts_output = {}
    for index_name in INDEX_NAMES:
        label = INDEX_LABELS[index_name]
        data = bff_get(page, "index-data", {"interval": interval, "indexName": index_name})
        if data and data.get("success"):
            d = data["data"]
            live = d.get("liveIndex", {})
            points = [
                {"time": p.get("cDay"), "value": p.get("indexValue")}
                for p in d.get("dailyIndex", [])
            ]
            charts_output[label] = {
                "live": {
                    "change": live.get("change"),
                    "changePer": live.get("changePer"),
                    "high": live.get("high"),
                    "close": live.get("indexClose"),
                    "open": live.get("indexOpen"),
                    "low": live.get("low"),
                    "ytdPercent": live.get("ytdPercent"),
                    "date": live.get("indexDay"),
                },
                "points": points,
            }
            print(f"[+] {label} chart: {len(points)} points")
        else:
            print(f"[-] {label} chart failed")
            charts_output[label] = None
    return charts_output


def scrape_index_constituents(page):
    """EGX30 uses its own weighted endpoint; the others use the general
    index-constituents endpoint."""
    constituents_output = {}

    weights_data = bff_get(page, "egx30-weights")
    if weights_data and weights_data.get("success"):
        constituents_output["EGX30"] = weights_data["data"].get("items", [])
        print(f"[+] EGX30 weights: {len(constituents_output['EGX30'])} constituents")
    else:
        print("[-] EGX30 weights failed")
        constituents_output["EGX30"] = []

    for index_name in ["EGX_SHARIAH", "EGX70_EWI", "EGX100_EWI"]:
        label = INDEX_LABELS[index_name]
        data = bff_get(page, "index-constituents", {"indexName": index_name})
        if data and data.get("success"):
            # index-constituents returns "data" as a bare list (unlike
            # egx30-weights, which nests it under data.items) - confirmed
            # from the EGX_SHARIAH sample.
            constituents_output[label] = data.get("data", [])
            print(f"[+] {label} constituents: {len(constituents_output[label])}")
        else:
            print(f"[-] {label} constituents failed")
            constituents_output[label] = []

    return constituents_output


def scrape_market_watch(page, page_size=300):
    """Full stock prices table. Tries a large page_size first to get
    everything in one call (last seen: 222 total stocks); falls back to
    paginating through totalPages if the server caps page_size lower
    than the real total.
    """
    all_rows = []
    data = bff_get(page, "market-watch", {
        "Page": 1, "PageSize": page_size, "SortBy": "value", "SortDescending": "true",
    })
    if not (data and data.get("success")):
        print("[-] market-watch page 1 failed")
        return []

    inner = data["data"]
    all_rows.extend(inner.get("data", []))
    total_pages = inner.get("totalPages", 1)
    total_count = inner.get("totalCount", len(all_rows))

    if len(all_rows) < total_count and total_pages > 1:
        print(f"[*] market-watch: got {len(all_rows)}/{total_count} in one page, "
              f"paginating through {total_pages} pages instead...")
        all_rows = []
        for page_num in range(1, total_pages + 1):
            page_data = bff_get(page, "market-watch", {
                "Page": page_num, "PageSize": 25, "SortBy": "value", "SortDescending": "true",
            })
            if page_data and page_data.get("success"):
                all_rows.extend(page_data["data"].get("data", []))
            else:
                print(f"[-] market-watch page {page_num}/{total_pages} failed")

    print(f"[+] market-watch: {len(all_rows)} stocks (expected {total_count})")
    return all_rows


def derive_gainers_losers(prices, top_n=5):
    """The old site scraped Top_GL.aspx as a separate page. The new API
    doesn't need that at all - every market-watch row already carries
    chgPer, so gainers/losers is just a client-side sort. One less
    endpoint to depend on.
    """
    ranked = [s for s in prices if s.get("chgPer") is not None]
    ranked.sort(key=lambda s: s["chgPer"], reverse=True)
    gainers = ranked[:top_n]
    losers = list(reversed(ranked[-top_n:])) if len(ranked) >= top_n else []
    return gainers, losers


def scrape_investor_activity(page):
    data = bff_get(page, "investor-full-statistics")
    if data and data.get("success"):
        print("[+] investor-full-statistics fetched")
        return data["data"]
    print("[-] investor-full-statistics failed")
    return {}


def scrape_news(page, days_back=30, page_size=20):
    """Media-center news/disclosures. See NEWS_TABS docstring note above
    - only "disclosure" has a confirmed secIds list.
    """
    news_output = {}
    date_to = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    for tab_name, sec_ids in NEWS_TABS.items():
        if sec_ids is None:
            print(f"[!] Skipping media-center tab '{tab_name}' - secIds not yet confirmed "
                  f"(see NEWS_TABS in this file).")
            news_output[tab_name] = []
            continue

        body = {
            "marketSessionNews": False,
            "secIds": sec_ids,
            "interval": 50,
            "pageNumber": 1,
            "pageSize": page_size,
            "dateFrom": date_from,
            "dateTo": date_to,
            "count": page_size,
        }
        data = bff_post(page, "news-search", body)
        if data and data.get("success"):
            news_output[tab_name] = data.get("data", [])
            print(f"[+] media-center/{tab_name}: {len(news_output[tab_name])} items")
        else:
            print(f"[-] media-center/{tab_name} failed")
            news_output[tab_name] = []

    return news_output


def main():
    with sync_playwright() as p:
        print("Launching browser context...")
        browser, context = launch_browser_context(p)
        page = context.new_page()

        print("Warming up session (acquiring bot-defense + session cookies)...")
        warm_up_session(page)

        print("\nFetching market status...")
        market_status = scrape_market_status(page)

        print("\nFetching indices summary...")
        indices_output = scrape_indices(page)

        print("\nFetching index charts...")
        index_charts = scrape_index_charts(page)

        print("\nFetching index constituents...")
        constituents = scrape_index_constituents(page)

        print("\nFetching market watch (stock prices)...")
        prices = scrape_market_watch(page)
        gainers, losers = derive_gainers_losers(prices)
        print(f"[+] Derived {len(gainers)} gainers, {len(losers)} losers from market-watch data")

        print("\nFetching investor activity...")
        investor_activity = scrape_investor_activity(page)

        print("\nFetching media-center news...")
        news = scrape_news(page)

        context.close()
        browser.close()

    output = {
        "source": "https://beta.egx.com.eg",
        "lastUpdated": now_cairo_str(),
        "marketStatus": market_status,
        "indices": indices_output,
        "indexCharts": index_charts,
        "constituents": constituents,
        "prices": prices,
        "gainers": gainers,
        "losers": losers,
        "investorActivity": investor_activity,
        "news": news,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nRun complete! Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
