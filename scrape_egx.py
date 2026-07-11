import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

OUTPUT_FILE = "egx.json"


def safe_num(text):
    """Parse a comma-formatted number string into int/float, or None."""
    if text is None:
        return None
    text = text.replace(",", "").strip()
    if text == "":
        return None
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def slugify(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def parse_panel_metrics(html_content):
    """Parses out numerical metrics from the actively visible tab panel."""
    text = BeautifulSoup(html_content, "html.parser").get_text("\n", strip=True)

    date_match = re.search(r"Date\s*:\s*(\d{2}/\d{2}/\d{4})", text, re.IGNORECASE)
    value_match = re.search(r"Value\s*:\s*([\d,.]+)", text, re.IGNORECASE)
    open_match = re.search(r"Open\s*:\s*([\d,.]+)", text, re.IGNORECASE)
    high_match = re.search(r"High\s*:\s*([\d,.]+)", text, re.IGNORECASE)
    low_match = re.search(r"Low\s*:\s*([\d,.]+)", text, re.IGNORECASE)
    change_match = re.search(r"Change\s*:\s*(-?[\d,.]+)", text, re.IGNORECASE)
    ytd_match = re.search(r"YTD%\s*Change\s*:\s*(-?[\d,.]+)", text, re.IGNORECASE)

    def safe_str(match):
        return match.group(1) if match else None

    def safe_num(match):
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None

    return {
        "date": safe_str(date_match),
        "value": safe_num(value_match),
        "open": safe_num(open_match),
        "high": safe_num(high_match),
        "low": safe_num(low_match),
        "change_pct": safe_num(change_match),
        "ytd_pct": safe_num(ytd_match)
    }


def parse_gl_table(soup, table_id):
    """Finds tables on Top_GL.aspx using their precise client IDs and maps the 6 columns layout."""
    table = soup.find("table", {"id": table_id})
    stocks = []
    
    if not table:
        return stocks

    # Find all table rows, safely skipping the first row containing the headers
    rows = table.find_all("tr")[1:]
    
    for row in rows:
        cols = row.find_all("td")
        # Ensure we have the full 6 columns present (Company Name, Currency, Prev Close, Open, Close, %CHG)
        if len(cols) >= 6:
            try:
                name_text = cols[0].get_text(strip=True)
                if not name_text or "No data available" in name_text:
                    continue
                    
                stocks.append({
                    "name": name_text,
                    "price": float(cols[4].get_text(strip=True).replace(",", "")),       # Close column
                    "change_pct": float(cols[5].get_text(strip=True).replace(",", "").replace("%", ""))  # %CHG column
                })
            except Exception:
                continue
    return stocks


def parse_market_summary(html_content):
    """Parses MarketSummry.aspx: the two 'TableStatic' blocks -
    main market activity (Listed / Stocks / Bonds / SMEs / OTC / Total)
    and market breadth (Listed stocks / Gainers / Decliners / Unchanged).
    """
    soup = BeautifulSoup(html_content, "html.parser")
    tables = soup.find_all("table", class_="TableStatic")
    result = {"main_market": {}, "breadth": {}}

    # Rows under these labels start a new sub-section (e.g. everything
    # after "OTC" until "Total" belongs to the OTC block: OTC/Bonds/Deals/Orders).
    section_starts = {"Listed": "listed", "SMEs Market": "smes", "OTC": "otc"}

    if len(tables) >= 1:
        section = None
        for row in tables[0].find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            label = cells[0].get_text(strip=True)
            if label == "No.":
                continue
            values = [c.get_text(strip=True) for c in cells[1:]]

            if label in section_starts:
                section = section_starts[label]
                if any(values):
                    result["main_market"][f"{section}_total"] = {
                        "no": safe_num(values[0]),
                        "volume": safe_num(values[1]),
                        "value": safe_num(values[2]),
                        "trades": safe_num(values[3]),
                    }
                continue

            if label == "Total":
                section = None
                result["main_market"]["total"] = {
                    "no": safe_num(values[0]),
                    "volume": safe_num(values[1]),
                    "value": safe_num(values[2]),
                    "trades": safe_num(values[3]),
                }
                continue

            if label == "Total Market Cap (LE)":
                result["main_market"]["total_market_cap"] = safe_num(values[0]) if values else None
                continue

            if not any(values):
                # section-header row with no data of its own (e.g. "SMEs Market")
                continue

            key = (f"{section}_" if section else "") + slugify(label)
            if len(values) == 4:
                result["main_market"][key] = {
                    "no": safe_num(values[0]),
                    "volume": safe_num(values[1]),
                    "value": safe_num(values[2]),
                    "trades": safe_num(values[3]),
                }
            else:
                result["main_market"][key] = values

    if len(tables) >= 2:
        for row in tables[1].find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            label = cells[0].get_text(strip=True)
            if label == "No.":
                continue
            values = [c.get_text(strip=True) for c in cells[1:]]
            if not any(values):
                continue
            key = slugify(label)
            if len(values) == 4:
                result["breadth"][key] = {
                    "no": safe_num(values[0]),
                    "volume": safe_num(values[1]),
                    "value": safe_num(values[2]),
                    "trades": safe_num(values[3]),
                }
            else:
                result["breadth"][key] = values

    return result


def parse_news_grid(html_content, table_id, base_url="https://www.egx.com.eg/en/"):
    """Parses any of EGX's News-style GridViews (News, Disclosures search
    results, Bulletin) - they all share the same markup, just a different
    table id. Title and date spans share the same repeater-item ID prefix
    (e.g. '..._ctl02'), so we pair them by that prefix rather than by DOM
    position - more robust if EGX ever tweaks the row markup. Returns an
    empty list if the grid is empty (e.g. Bulletin with no session today,
    which renders a 'No data' placeholder row with no matching spans).
    """
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", {"id": table_id})
    items = []

    if not table:
        return items

    title_spans = table.find_all("span", id=lambda x: x and x.endswith("_lblTitle"))
    for title_span in title_spans:
        try:
            title = title_span.get_text(strip=True)
            link_tag = title_span.find_parent("a")
            href = link_tag["href"] if link_tag and link_tag.has_attr("href") else None
            url = urljoin(base_url, href) if href else None

            news_id = None
            if href:
                id_match = re.search(r"NewsID=(\d+)", href)
                news_id = id_match.group(1) if id_match else None

            prefix = title_span["id"].rsplit("_lblTitle", 1)[0]
            date_span = soup.find("span", id=f"{prefix}_lblDate")
            date_text = date_span.get_text(strip=True) if date_span else None

            items.append({
                "id": news_id,
                "title": title,
                "date": date_text,
                "url": url,
            })
        except Exception:
            continue

    return items


def parse_sectors(html_content):
    """Parses MarketWatchSectors.aspx's GridView (ctl00_C_M_GridView2).
    Columns: Sector Name | (icon, empty) | Value(LE) | %Value |
    Volume | %Volume | Market Cap(LE) | %Market Cap.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", {"id": "ctl00_C_M_GridView2"})
    sectors = []

    if not table:
        return sectors

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 8:
            continue  # skips the <th> header row, which has no <td>s
        try:
            name = cells[0].get_text(strip=True)
            if not name:
                continue
            sectors.append({
                "name": name,
                "value": safe_num(cells[2].get_text(strip=True)),
                "value_pct": safe_num(cells[3].get_text(strip=True)),
                "volume": safe_num(cells[4].get_text(strip=True)),
                "volume_pct": safe_num(cells[5].get_text(strip=True)),
                "market_cap": safe_num(cells[6].get_text(strip=True)),
                "market_cap_pct": safe_num(cells[7].get_text(strip=True)),
            })
        except Exception:
            continue

    return sectors


def parse_live_market_status(html_content):
    """Parses the live status badge on the Arabic homepage
    (ctl00_C_lblMarketStatus). This reflects EGX's own real-time /
    holiday-aware status, which is more reliable than computing it
    from a fixed schedule.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    el = soup.find(id="ctl00_C_lblMarketStatus")
    if not el:
        return {"text_ar": None, "color": None}

    style = el.get("style", "")
    color_match = re.search(r"color\s*:\s*([A-Za-z]+)", style, re.IGNORECASE)

    return {
        "text_ar": el.get_text(strip=True),
        "color": color_match.group(1) if color_match else None,
    }


# Arabic label -> stable English key, for the investor-type endpoints.
# Two spellings of "foreigners" show up in the wild (with/without hamza),
# so both map to the same key.
NATIONALITY_MAP = {
    "مصريين": "egyptians",
    "عرب": "arabs",
    "أجانب": "foreigners",
    "اجانب": "foreigners",
}

# GetInvestorTables groups: 1=Total, 2=Individuals, 3=Institutions
# (verified: group2 buy + group3 buy ~= group1 buy on a real sample).
INVESTOR_GROUP_MAP = {"1": "total", "2": "individuals", "3": "institutions"}


def fetch_investor_json(context, url, referer):
    """Fetches one WebService.asmx endpoint via the browser context's
    request API - these are pure JSON/AJAX endpoints, no page rendering
    needed. Returns raw response text, or None if the request itself
    fails (network/timeout). Note: EGX's backend has been observed
    returning a raw Oracle error string (e.g. 'ORA-12521: TNS:listener
    does not currently know...') instead of JSON on some endpoints -
    that's a transient server-side issue, not a scraping problem, and
    the parse_* functions below handle it by returning empty results
    rather than crashing.
    """
    try:
        response = context.request.get(
            url,
            headers={
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            timeout=20000,
        )
        return response.text()
    except Exception as e:
        print(f"[-] Failed to fetch {url}: {e}")
        return None


def parse_investor_tables(raw_text):
    """Parses GetInvestorTables: [{Group, Type, Buy, Sell, Net}, ...]
    into {"total": {...}, "individuals": {...}, "institutions": {...}},
    each keyed by nationality with buy/sell/net.
    """
    result = {}
    if not raw_text:
        return result
    try:
        rows = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return result

    for row in rows:
        try:
            group_key = INVESTOR_GROUP_MAP.get(str(row.get("Group")), str(row.get("Group")))
            nat_key = NATIONALITY_MAP.get(row.get("Type"), row.get("Type"))
            result.setdefault(group_key, {})[nat_key] = {
                "buy": row.get("Buy"),
                "sell": row.get("Sell"),
                "net": row.get("Net"),
            }
        except Exception:
            continue
    return result


def parse_pie_chart(raw_text):
    """Parses InvPieCharts: [{Label, Value, Color}, ...]."""
    items = []
    if not raw_text:
        return items
    try:
        rows = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return items

    for row in rows:
        try:
            items.append({
                "label_ar": row.get("Label"),
                "value": row.get("Value"),
                "color": row.get("Color"),
            })
        except Exception:
            continue
    return items


def parse_stack_chart(raw_text):
    """Parses IndivByNatStackChart (and, best-effort, the similarly
    shaped InvestorNatColumnChart / InvestorIndivInstColumnChart):
    [{Type, Buy, Sell}, ...].
    """
    items = []
    if not raw_text:
        return items
    try:
        rows = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return items

    for row in rows:
        try:
            nat_key = NATIONALITY_MAP.get(row.get("Type"), row.get("Type"))
            items.append({
                "nationality": nat_key,
                "buy": row.get("Buy"),
                "sell": row.get("Sell"),
            })
        except Exception:
            continue
    return items


def parse_index_constituents(html_content):
    """Parses CurrentIndexConstituntes.aspx (ctl00_C_CIC_GridView1):
    ISIN | Reuters code | Company name (Arabic) | Relative weight (%).
    Confirmed: type=1 is EGX30 (31 constituents, weights sum to ~100%).
    Other type values (2/4/5/6) redirected to a generic landing page
    instead of rendering a constituents table when hit via plain
    querystring - that index selector likely needs a dropdown postback
    rather than a URL param, so only EGX30 is wired up for now.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", {"id": "ctl00_C_CIC_GridView1"})
    items = []

    if not table:
        return items

    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        try:
            isin = cells[0].get_text(strip=True)
            code = cells[1].get_text(strip=True)
            name_ar = cells[2].get_text(strip=True)
            weight = safe_num(cells[3].get_text(strip=True))
            if not name_ar:
                continue
            items.append({
                "isin": isin,
                "code": code,
                "name_ar": name_ar,
                "weight_pct": weight,
            })
        except Exception:
            continue

    return items


def parse_chart_data(raw_text):
    """Parses getIndexChartData's response: [{"CDAY": "2026-07-11T09:58:00",
    "INDEX_VALUE": 52028.37}, ...] - 5-minute intraday points. This is
    NOT fetched by URL (its `gtk` query param is a rotating token tied to
    the browser session, not safe to hardcode or regenerate ourselves).
    Instead we capture whatever request EGX's own homepage JS fires on
    its own when the page loads (see the response listener in main()),
    so we never need to know how `gtk` is produced.
    """
    points = []
    if not raw_text:
        return points
    try:
        rows = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return points

    for row in rows:
        try:
            points.append({
                "time": row.get("CDAY"),
                "value": row.get("INDEX_VALUE"),
            })
        except Exception:
            continue

    return points


# The homepage chart widget identifies indices with its own internal
# names, which don't match the keys our `indices` dict uses (those come
# from the postback-based Indices.aspx scrape in Part 1: EGX30, SHARIAH,
# EGX70, EGX100). Observed so far: "EGX30" maps directly, but SHARIAH
# showed up as "EGX_33_Shariah". Aliases here are exact matches we've
# actually seen; the substring fallback below catches likely variants
# for EGX70/EGX100 we haven't observed yet, so a chart doesn't silently
# get dropped just because of a naming mismatch.
_CHART_INDEX_ALIASES = {
    "EGX30": "EGX30",
    "EGX_33_SHARIAH": "SHARIAH",
    "SHARIAH": "SHARIAH",
    "EGX70": "EGX70",
    "EGX70EWI": "EGX70",
    "EGX100": "EGX100",
    "EGX100EWI": "EGX100",
}


def normalize_chart_index_name(raw_name):
    if not raw_name:
        return raw_name
    key = raw_name.upper()
    if key in _CHART_INDEX_ALIASES:
        return _CHART_INDEX_ALIASES[key]
    # Fallback: substring match against our known index names, in case
    # the widget uses some other variant we haven't seen yet.
    for known in ("EGX30", "SHARIAH", "EGX70", "EGX100"):
        if known in key:
            return known
    return raw_name  # unrecognized - keep as-is rather than silently dropping it


def main():
    indices_output = {}
    gainers = []
    losers = []
    market_summary = {"main_market": {}, "breadth": {}}
    news = []
    sectors = []
    disclosures = []
    bulletin = []
    live_status = {"text_ar": None, "color": None}
    investor_activity = {
        "byGroup": {},
        "nationalityBreakdownPct": [],
        "individualsByNationality": [],
        "institutionsByNationality": [],
    }
    egx30_constituents = []
    index_charts = {}

    with sync_playwright() as p:
        print("Launching secure browser context...")
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )

        # --- PART 1: SCRAPE INDICES ---
        page = context.new_page()
        print("Navigating to Portal Landing View...")
        page.goto("https://www.egx.com.eg/en/Indices.aspx", wait_until="commit", timeout=60000)
        
        print("Pausing 10 seconds to let JavaScript firewall challenge pass...")
        page.wait_for_timeout(10000)

        postback_actions = {
            "EGX30": "ctl00$C$M$lnkEGX30",
            "SHARIAH": "ctl00$C$M$lnkSHARIAH",
            "EGX70": "ctl00$C$M$lnkEGX70EWI",
            "EGX100": "ctl00$C$M$lnkEGX100EWI"
        }

        for tracking_name, event_target in postback_actions.items():
            print(f"Requesting data compilation state for {tracking_name}...")
            try:
                page.evaluate(f"__doPostBack('{event_target}', '');")
                page.wait_for_timeout(4000)

                updated_html = page.content()
                indices_output[tracking_name] = parse_panel_metrics(updated_html)
                print(f"[+] Extracted values completely for {tracking_name}")

            except Exception as loop_error:
                print(f"[-] Error on index loop {tracking_name}: {loop_error}")
                indices_output[tracking_name] = {k: None for k in ["date", "value", "open", "high", "low", "change_pct", "ytd_pct"]}

        page.close()

        # --- PART 2: SCRAPE TOP GAINERS & LOSERS ---
        print("\nNavigating to Top Gainers/Losers Desk...")
        try:
            gl_page = context.new_page()
            gl_page.goto("https://www.egx.com.eg/en/Top_GL.aspx", wait_until="commit", timeout=60000)
            gl_page.wait_for_timeout(10000)  # Wait for JavaScript shield to settle

            # Wait for the specific GridView container element to ensure data has rendered
            try:
                gl_page.wait_for_selector("#ctl00_C_Top_GL1_GridView1", timeout=15000)
            except Exception:
                pass

            gl_soup = BeautifulSoup(gl_page.content(), "html.parser")
            
            # Scrape using the specific IDs seen directly in your raw layout response
            gainers = parse_gl_table(gl_soup, "ctl00_C_Top_GL1_GridView1")
            losers = parse_gl_table(gl_soup, "ctl00_C_Top_GL1_GridView2")
            
            print(f"[+] Successfully scraped {len(gainers)} gainers and {len(losers)} losers.")
            gl_page.close()
            
        except Exception as gl_error:
            print(f"[-] Failed to fetch Top Gainers/Losers: {gl_error}")

        # --- PART 3: SCRAPE MARKET SUMMARY STATISTICS ---
        print("\nNavigating to Market Summary...")
        try:
            ms_page = context.new_page()
            ms_page.goto("https://www.egx.com.eg/en/MarketSummry.aspx", wait_until="commit", timeout=60000)
            ms_page.wait_for_timeout(10000)  # Wait for JavaScript shield to settle

            market_summary = parse_market_summary(ms_page.content())
            print(f"[+] Successfully scraped market summary "
                  f"({len(market_summary['main_market'])} main-market rows, "
                  f"{len(market_summary['breadth'])} breadth rows).")
            ms_page.close()

        except Exception as ms_error:
            print(f"[-] Failed to fetch Market Summary: {ms_error}")

        # --- PART 4: SCRAPE NEWS ---
        print("\nNavigating to News List...")
        try:
            news_page = context.new_page()
            news_page.goto("https://www.egx.com.eg/en/NewsList.aspx", wait_until="commit", timeout=60000)
            news_page.wait_for_timeout(10000)  # Wait for JavaScript shield to settle

            news = parse_news_grid(news_page.content(), "ctl00_C_N_GridView1")
            print(f"[+] Successfully scraped {len(news)} news items.")
            news_page.close()

        except Exception as news_error:
            print(f"[-] Failed to fetch News: {news_error}")

        # --- PART 5: SCRAPE SECTORS ---
        print("\nNavigating to Market Watch - Sectors...")
        try:
            sectors_page = context.new_page()
            sectors_page.goto("https://www.egx.com.eg/en/MarketWatchSectors.aspx", wait_until="commit", timeout=60000)
            sectors_page.wait_for_timeout(10000)

            sectors = parse_sectors(sectors_page.content())
            print(f"[+] Successfully scraped {len(sectors)} sectors.")
            sectors_page.close()

        except Exception as sectors_error:
            print(f"[-] Failed to fetch Sectors: {sectors_error}")

        # --- PART 6: SCRAPE DISCLOSURES (last 3 months, latest page only) ---
        print("\nNavigating to Disclosures search...")
        try:
            today = datetime.now(timezone.utc)
            three_months_ago = today - timedelta(days=90)
            from_str = three_months_ago.strftime("%d/%m/%Y")
            to_str = today.strftime("%d/%m/%Y")
            disclosures_url = (
                "https://www.egx.com.eg/en/NewsSearch.aspx"
                f"?com=&word=&from={from_str}&to={to_str}&isin=&sec_id=20"
            )

            disc_page = context.new_page()
            disc_page.goto(disclosures_url, wait_until="commit", timeout=60000)
            disc_page.wait_for_timeout(10000)

            disclosures = parse_news_grid(disc_page.content(), "ctl00_C_N_GVNews")
            print(f"[+] Successfully scraped {len(disclosures)} disclosures.")
            disc_page.close()

        except Exception as disc_error:
            print(f"[-] Failed to fetch Disclosures: {disc_error}")

        # --- PART 7: SCRAPE BULLETIN (Arabic - can be empty on non-session days) ---
        print("\nNavigating to Bulletin News...")
        try:
            bulletin_page = context.new_page()
            bulletin_page.goto("https://www.egx.com.eg/ar/BulletinNews.aspx", wait_until="commit", timeout=60000)
            bulletin_page.wait_for_timeout(10000)

            bulletin = parse_news_grid(
                bulletin_page.content(), "ctl00_C_BulletinNews1_GVNews",
                base_url="https://www.egx.com.eg/ar/",
            )
            print(f"[+] Successfully scraped {len(bulletin)} bulletin items.")
            bulletin_page.close()

        except Exception as bulletin_error:
            print(f"[-] Failed to fetch Bulletin: {bulletin_error}")

        # --- PART 8: SCRAPE LIVE MARKET STATUS (Arabic homepage badge)
        # + INDEX CHART DATA (intraday sparkline points, captured live) ---
        print("\nNavigating to Homepage for live market status and chart data...")
        try:
            home_page = context.new_page()

            def handle_chart_response(response):
                # The homepage's own JS calls getIndexChartData to draw its
                # mini index chart widget - we just listen for that request
                # rather than building the URL (and its rotating `gtk`
                # token) ourselves.
                if "getIndexChartData" not in response.url:
                    return
                try:
                    query = parse_qs(urlparse(response.url).query)
                    raw_index_name = query.get("index", ["UNKNOWN"])[0]
                    index_name = normalize_chart_index_name(raw_index_name)
                    index_charts[index_name] = parse_chart_data(response.text())
                    print(f"[+] Captured chart data for {index_name} "
                          f"(raw name: {raw_index_name}, {len(index_charts[index_name])} points)")
                except Exception as capture_error:
                    print(f"[-] Failed to parse a captured chart response: {capture_error}")

            home_page.on("response", handle_chart_response)

            home_page.goto("https://www.egx.com.eg/ar/homepage.aspx", wait_until="commit", timeout=60000)
            home_page.wait_for_timeout(10000)

            live_status = parse_live_market_status(home_page.content())
            print(f"[+] Live market status: {live_status}")
            home_page.close()

        except Exception as status_error:
            print(f"[-] Failed to fetch live market status/chart data: {status_error}")

        # --- PART 9: SCRAPE INVESTOR ACTIVITY (Egyptian/Arab/Foreign, Individuals/Institutions) ---
        print("\nFetching Investor Type data...")
        try:
            investor_referer = "https://www.egx.com.eg/en/InvestorsTypeCharts.aspx"

            # Visit the real page first so the context picks up cookies/session -
            # these are AJAX endpoints the page's own JS calls after loading.
            inv_page = context.new_page()
            inv_page.goto(investor_referer, wait_until="commit", timeout=60000)
            inv_page.wait_for_timeout(10000)
            inv_page.close()

            tables_raw = fetch_investor_json(
                context,
                "https://www.egx.com.eg/WebService.asmx/GetInvestorTables?Lang=ar&SB=1",
                investor_referer,
            )
            investor_activity["byGroup"] = parse_investor_tables(tables_raw)

            pie2_raw = fetch_investor_json(
                context,
                "https://www.egx.com.eg/WebService.asmx/InvPieCharts?Lang=ar&SB=1&Type=2",
                investor_referer,
            )
            investor_activity["nationalityBreakdownPct"] = parse_pie_chart(pie2_raw)

            indiv_raw = fetch_investor_json(
                context,
                "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart?Lang=ar&SB=1&Type=1",
                investor_referer,
            )
            investor_activity["individualsByNationality"] = parse_stack_chart(indiv_raw)

            inst_raw = fetch_investor_json(
                context,
                "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart?Lang=ar&SB=1&Type=2",
                investor_referer,
            )
            investor_activity["institutionsByNationality"] = parse_stack_chart(inst_raw)

            print(f"[+] Successfully scraped investor activity "
                  f"({len(investor_activity['byGroup'])} groups).")

        except Exception as inv_error:
            print(f"[-] Failed to fetch Investor Type data: {inv_error}")

        # --- PART 10: SCRAPE EGX30 CONSTITUENTS & WEIGHTS ---
        print("\nNavigating to EGX30 Constituents...")
        try:
            cic_page = context.new_page()
            cic_page.goto(
                "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=1&nav=1",
                wait_until="commit", timeout=60000,
            )
            cic_page.wait_for_timeout(10000)

            egx30_constituents = parse_index_constituents(cic_page.content())
            print(f"[+] Successfully scraped {len(egx30_constituents)} EGX30 constituents.")
            cic_page.close()

        except Exception as cic_error:
            print(f"[-] Failed to fetch EGX30 constituents: {cic_error}")

        context.close()
        browser.close()

    # --- SAVE STRUCTURED RESULTS ---
    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": now_utc(),
        "liveMarketStatus": live_status,
        "indices": indices_output,
        "gainers": gainers,
        "losers": losers,
        "marketSummary": market_summary,
        "sectors": sectors,
        "news": news,
        "disclosures": disclosures,
        "bulletin": bulletin,
        "investorActivity": investor_activity,
        "indexConstituents": {
            "EGX30": egx30_constituents
        },
        "indexCharts": index_charts
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nFinal run complete! Tracking metrics saved perfectly to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
