import json
import os
import re
import time
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

OUTPUT_FILE = "egx.json"

# Top_GL.aspx (Top Gainers/Losers) has no ticker code or link anywhere in
# its markup - confirmed by inspecting the raw HTML - so there's no way
# to scrape a code for movers directly. company_codes.json (committed
# alongside this script) is a hand-maintained name -> code lookup to
# fill that gap. Missing/unmapped names just get no code, which the app
# already handles gracefully (falls back to a generated avatar).
COMPANY_CODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "company_codes.json")


def _normalize_company_name(name):
    return re.sub(r"\s+", " ", name or "").strip().upper()


def load_company_codes():
    try:
        with open(COMPANY_CODES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[-] Could not load {COMPANY_CODES_FILE}: {e} (movers will have no ticker codes)")
        return {}

    return {
        _normalize_company_name(name): code.strip().upper()
        for name, code in raw.items()
        if name != "_readme" and code and code.strip()
    }


def attach_company_codes(movers, company_codes):
    for m in movers:
        code = company_codes.get(_normalize_company_name(m.get("name")))
        if code:
            m["code"] = code
    return movers


def safe_num(text):
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

    def safe_num_match(match):
        if not match:
            return None
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            return None

    return {
        "date": safe_str(date_match),
        "value": safe_num_match(value_match),
        "open": safe_num_match(open_match),
        "high": safe_num_match(high_match),
        "low": safe_num_match(low_match),
        "change_pct": safe_num_match(change_match),
        "ytd_pct": safe_num_match(ytd_match)
    }


def parse_gl_table(soup, table_id):
    table = soup.find("table", {"id": table_id})
    stocks = []
    if not table:
        return stocks
    rows = table.find_all("tr")[1:]
    for row in rows:
        cols = row.find_all("td")
        if len(cols) >= 6:
            try:
                name_text = cols[0].get_text(strip=True)
                if not name_text or "No data available" in name_text:
                    continue
                stocks.append({
                    "name": name_text,
                    "price": float(cols[4].get_text(strip=True).replace(",", "")),
                    "change_pct": float(cols[5].get_text(strip=True).replace(",", "").replace("%", ""))
                })
            except Exception:
                continue
    return stocks


def parse_market_summary(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    tables = soup.find_all("table", class_="TableStatic")
    result = {"main_market": {}, "breadth": {}}
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


def parse_news_grid(html_content, table_id, base_url="https://www.egx.com.eg/ar/"):
    """Layout-driven parser that matches elements structurally without relying on
    volatile ASP.NET local identifiers like '_lblTitle' which change in Arabic view.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", {"id": table_id})
    items = []
    if not table:
        return items

    rows = table.find_all("tr")
    for row in rows:
        link_tag = row.find("a", href=lambda x: x and "NewsID=" in x)
        if not link_tag:
            continue

        try:
            href = link_tag["href"]
            url = urljoin(base_url, href)
            
            id_match = re.search(r"NewsID=(\d+)", href)
            news_id = id_match.group(1) if id_match else None
            
            title = link_tag.get_text(strip=True)
            
            date_match = re.search(r"\d{2}/\d{2}/\d{4}", row.get_text())
            date_text = date_match.group(0) if date_match else None

            if news_id and title:
                items.append({
                    "id": news_id, 
                    "title": title, 
                    "date": date_text, 
                    "url": url
                })
        except Exception as e:
            print(f"[-] Row parse error within bulletin table: {e}")
            continue

    return items


def parse_sectors(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", {"id": "ctl00_C_M_GridView2"})
    sectors = []
    if not table:
        return sectors
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 8:
            continue
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


NATIONALITY_MAP = {"مصريين": "egyptians", "عرب": "arabs", "أجانب": "foreigners", "اجانب": "foreigners"}
INVESTOR_GROUP_MAP = {"1": "total", "2": "individuals", "3": "institutions"}


def fetch_investor_json(context, url, referer, retries=2, retry_delay=3):
    for attempt in range(retries + 1):
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
            text = response.text()
            stripped = text.strip() if text else ""
            if stripped.startswith("[") or stripped.startswith("{"):
                return text
            print(f"[-] Non-JSON response from {url} "
                  f"(attempt {attempt + 1}/{retries + 1}): {stripped[:150]!r}")
        except Exception as e:
            print(f"[-] Failed to fetch {url} (attempt {attempt + 1}/{retries + 1}): {e}")

        if attempt < retries:
            time.sleep(retry_delay)

    return None


def parse_investor_tables(raw_text):
    result = {}
    if not raw_text:
        return result
    try:
        rows = json.loads(raw_text)
    except Exception:
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
    items = []
    if not raw_text:
        return items
    try:
        rows = json.loads(raw_text)
    except Exception:
        return items
    for row in rows:
        try:
            items.append({"label_ar": row.get("Label"), "value": row.get("Value"), "color": row.get("Color")})
        except Exception:
            continue
    return items


def parse_stack_chart(raw_text):
    items = []
    if not raw_text:
        return items
    try:
        rows = json.loads(raw_text)
    except Exception:
        return items
    for row in rows:
        try:
            nat_key = NATIONALITY_MAP.get(row.get("Type"), row.get("Type"))
            items.append({"nationality": nat_key, "buy": row.get("Buy"), "sell": row.get("Sell")})
        except Exception:
            continue
    return items


def parse_index_constituents(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", {"id": "ctl00_C_CIC_GridView1"})
    items = []
    if not table:
        return items
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        try:
            isin = cells[0].get_text(strip=True)
            code = cells[1].get_text(strip=True)
            name_ar = cells[2].get_text(strip=True)
            
            weight = safe_num(cells[3].get_text(strip=True)) if len(cells) >= 4 else None
            
            if not name_ar:
                continue
            
            node = {"isin": isin, "code": code, "name_ar": name_ar}
            if weight is not None:
                node["weight_pct"] = weight
            items.append(node)
        except Exception:
            continue
    return items


def parse_chart_data(raw_text):
    points = []
    if not raw_text:
        return points
    try:
        rows = json.loads(raw_text)
    except Exception:
        return points
    for row in rows:
        try:
            points.append({"time": row.get("CDAY"), "value": row.get("INDEX_VALUE")})
        except Exception:
            continue
    return points


_CHART_INDEX_ALIASES = {
    "EGX30": "EGX30", "EGX_33_SHARIAH": "SHARIAH", "SHARIAH": "SHARIAH",
    "EGX70": "EGX70", "EGX70EWI": "EGX70", "EGX70_EWI": "EGX70",
    "EGX100": "EGX100", "EGX100EWI": "EGX100", "EGX100_EWI": "EGX100",
}


def normalize_chart_index_name(raw_name):
    if not raw_name:
        return raw_name
    key = raw_name.upper()
    if key in _CHART_INDEX_ALIASES:
        return _CHART_INDEX_ALIASES[key]
    for known in ("EGX30", "SHARIAH", "EGX70", "EGX100"):
        if known in key:
            return known
    return raw_name


def human_delay():
    time.sleep(random.uniform(3.5, 6.0))


BULLETIN_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bulletin_state.json")


def load_bulletin_state():
    try:
        with open(BULLETIN_STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_bulletin_state(ids):
    with open(BULLETIN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=2)


def get_fcm_access_token():
    sa_json = os.environ.get("FCM_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        return None, None

    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        info = json.loads(sa_json)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        credentials.refresh(Request())
        return credentials.token, info.get("project_id")
    except Exception as e:
        print(f"[-] Failed to get FCM access token: {e}")
        return None, None


def send_fcm_notification(title, body):
    import requests

    token, project_id = get_fcm_access_token()
    if not token or not project_id:
        print("[-] FCM not configured (FCM_SERVICE_ACCOUNT_JSON secret missing) - skipping push notification.")
        return

    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    payload = {
        "message": {
            "topic": "egx_bulletins",
            "notification": {"title": title, "body": body},
        }
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        if response.status_code == 200:
            print(f"[+] Push notification sent: {title}")
        else:
            print(f"[-] FCM send failed ({response.status_code}): {response.text[:200]}")
    except Exception as e:
        print(f"[-] FCM send error: {e}")


def notify_new_bulletins(bulletin_items):
    if not bulletin_items:
        return

    seen_ids = load_bulletin_state()
    current_ids = {item.get("id") for item in bulletin_items if item.get("id")}
    new_ids = current_ids - seen_ids

    if new_ids:
        new_items = [item for item in bulletin_items if item.get("id") in new_ids]
        for item in new_items:
            send_fcm_notification("EGX Bulletin", item.get("title", "New bulletin item"))
            time.sleep(1.0)  # Throttling delay to space out multi-sends inside actions
        print(f"[+] {len(new_items)} new bulletin item(s) - notification(s) sent.")
    else:
        print("[+] No new bulletin items since last run - no notifications sent.")

    save_bulletin_state(current_ids | seen_ids)


def main():
    indices_output = {}
    gainers, losers = [], []
    market_summary = {"main_market": {}, "breadth": {}}
    news, sectors, disclosures, bulletin = [], [], [], []
    live_status = {"text_ar": None, "color": None}
    investor_activity = {
        "byGroup": {}, "nationalityBreakdownPct": [],
        "individualsByNationality": [], "institutionsByNationality": []
    }
    index_constituents = {"EGX30": [], "SHARIAH": [], "EGX70": [], "EGX100": []}
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

        page = context.new_page()

        # --- PART 1: SCRAPE ALL INDICES ON A SINGLE TAB ---
        postback_actions = {
            "EGX30": "ctl00$C$M$lnkEGX30",
            "SHARIAH": "ctl00$C$M$lnkSHARIAH",
            "EGX70": "ctl00$C$M$lnkEGX70EWI",
            "EGX100": "ctl00$C$M$lnkEGX100EWI"
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
                    indices_output[tracking_name] = {k: None for k in ["date", "value", "open", "high", "low", "change_pct", "ytd_pct"]}
        except Exception as loop_error:
            print(f"[-] Catastrophic stop on indices interface: {loop_error}")

        human_delay()

        # --- PART 2: SCRAPE TOP GAINERS & LOSERS ---
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

        human_delay()

        # --- PART 3: SCRAPE MARKET SUMMARY STATISTICS ---
        print("\nNavigating to Market Summary...")
        try:
            page.goto("https://www.egx.com.eg/en/MarketSummry.aspx", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            market_summary = parse_market_summary(page.content())
            print(f"[+] Successfully scraped market summary.")
        except Exception as ms_error:
            print(f"[-] Failed to fetch Market Summary: {ms_error}")

        human_delay()

        # --- PART 4: SCRAPE NEWS ---
        print("\nNavigating to News List...")
        try:
            page.goto("https://www.egx.com.eg/en/NewsList.aspx", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            news = parse_news_grid(page.content(), "ctl00_C_N_GridView1", base_url="https://www.egx.com.eg/en/")
            print(f"[+] Successfully scraped {len(news)} news items.")
        except Exception as news_error:
            print(f"[-] Failed to fetch News: {news_error}")

        human_delay()

        # --- PART 5: SCRAPE SECTORS ---
        print("\nNavigating to Market Watch - Sectors...")
        try:
            page.goto("https://www.egx.com.eg/en/MarketWatchSectors.aspx", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            sectors = parse_sectors(page.content())
            print(f"[+] Successfully scraped {len(sectors)} sectors.")
        except Exception as sectors_error:
            print(f"[-] Failed to fetch Sectors: {sectors_error}")

        human_delay()

        # --- PART 6: SCRAPE DISCLOSURES ---
        print("\nNavigating to Disclosures search...")
        try:
            today = datetime.now(timezone.utc)
            three_months_ago = today - timedelta(days=90)
            from_str = three_months_ago.strftime("%d/%m/%Y")
            to_str = today.strftime("%d/%m/%Y")
            disclosures_url = f"https://www.egx.com.eg/en/NewsSearch.aspx?com=&word=&from={from_str}&to={to_str}&isin=&sec_id=20"

            page.goto(disclosures_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            disclosures = parse_news_grid(page.content(), "ctl00_C_N_GVNews", base_url="https://www.egx.com.eg/en/")
            print(f"[+] Successfully scraped {len(disclosures)} disclosures.")
        except Exception as disc_error:
            print(f"[-] Failed to fetch Disclosures: {disc_error}")

        human_delay()

        # --- PART 7: SCRAPE BULLETIN (ARABIC MAIN ONLY) ---
        print("\nNavigating to Bulletin News...")
        try:
            page.goto("https://www.egx.com.eg/ar/BulletinNews.aspx", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            bulletin = parse_news_grid(page.content(), "ctl00_C_BulletinNews1_GVNews", base_url="https://www.egx.com.eg/ar/")
            print(f"[+] Successfully scraped {len(bulletin)} bulletin items.")
            notify_new_bulletins(bulletin)
        except Exception as bulletin_error:
            print(f"[-] Failed to fetch Bulletin: {bulletin_error}")

        human_delay()

        # --- PART 8: SCRAPE LIVE MARKET STATUS & ALL INDEX GRAPH DATA ---
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

        human_delay()

        # --- PART 9: SCRAPE INVESTOR ACTIVITY ---
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
                print("[-] WARNING: all investor activity fields came back empty.")
        except Exception as inv_error:
            print(f"[-] Failed to fetch Investor Type data: {inv_error}")

        human_delay()

        # --- PART 10: SCRAPE ALL CONSTITUENT ENDPOINTS SEQUENTIALLY ---
        constituent_endpoints = {
            "EGX30": "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=1&nav=1",
            "SHARIAH": "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=22&nav=22",
            "EGX70": "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=16&nav=16",
            "EGX100": "https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=5&nav=4"
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
        "indexConstituents": index_constituents,
        "indexCharts": index_charts
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nFinal run complete! Tracking metrics saved perfectly to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
