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

    date_match = re.search(r"Date\s*:\s*(\d{2}/\d{2}/\\d{4})", text, re.IGNORECASE)
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
    """Finds tables on Top_GL.aspx using their precise client IDs and maps the columns."""
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


def parse_news_grid(html_content, table_id, base_url="https://www.egx.com.eg/en/"):
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


NATIONALITY_MAP = {
    "مصريين": "egyptians",
    "عرب": "arabs",
    "أجانب": "foreigners",
    "اجانب": "foreigners",
}

INVESTOR_GROUP_MAP = {"1": "total", "2": "individuals", "3": "institutions"}


def fetch_investor_json(context, url, referer):
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


_CHART_INDEX_ALIASES = {
    "EGX30": "EGX30",
    "EGX_33_SHARIAH": "SHARIAH",
    "SHARIAH": "SHARIAH",
    "EGX70": "EGX70",
    "EGX70EWI": "EGX70",
    "EGX70_EWI": "EGX70",
    "EGX100": "EGX100",
    "EGX100EWI": "EGX100",
    "EGX100_EWI": "EGX100",
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

        # --- PART 1: SCRAPE INDICES (Safe tabs with standard network waiting) ---
        postback_actions = {
            "EGX30": "ctl00$C$M$lnkEGX30",
            "SHARIAH": "ctl00$C$M$lnkSHARIAH",
            "EGX70": "ctl00$C$M$lnkEGX70EWI",
            "EGX100": "ctl00$C$M$lnkEGX100EWI"
        }

        for tracking_name, event_target in postback_actions.items():
            print(f"Opening fresh tab state for {tracking_name}...")
            try:
                idx_page = context.new_page()
                idx_page.goto("https://www.egx.com.eg/en/Indices.aspx", wait_until="networkidle", timeout=60000)
                idx_page.wait_for_timeout(4000)

                idx_page.evaluate(f"__doPostBack('{event_target}', '');")
                idx_page.wait_for_timeout(4000)

                updated_html = idx_page.content()
                metrics = parse_panel_metrics(updated_html)
                
                if metrics.get("value") is not None:
                    indices_output[tracking_name] = metrics
                    print(f"[+] Extracted values completely for {tracking_name}: {metrics['value']}")
                else:
                    print(f"[-] Received empty payload for {tracking_name}, running backup fallback...")
                    idx_page.reload(wait_until="networkidle")
                    idx_page.wait_for_timeout(4000)
                    idx_page.evaluate(f"__doPostBack('{event_target}', '');")
                    idx_page.wait_for_timeout(4000)
                    indices_output[tracking_name] = parse_panel_metrics(idx_page.content())

                idx_page.close()
            except Exception as loop_error:
                print(f"[-] Error on index loop {tracking_name}: {loop_error}")
                indices_output[tracking_name] = {k: None for k in ["date", "value", "open", "high", "low", "change_pct", "ytd_pct"]}


        # --- PART 2: SCRAPE TOP GAINERS & LOSERS ---
        print("\nNavigating to Top Gainers/Losers Desk...")
        try:
            gl_page = context.new_page()
            gl_page.goto("https://www.egx.com.eg/en/Top_GL.aspx", wait_until="networkidle", timeout=60000)
            gl_page.wait_for_timeout(4000)

            gl_soup = BeautifulSoup(gl_page.content(), "html.parser")
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
            ms_page.goto("https://www.egx.com.eg/en/MarketSummry.aspx", wait_until="networkidle", timeout=60000)
            ms_page.wait_for_timeout(4000)
            market_summary = parse_market_summary(ms_page.content())
            print(f"[+] Successfully scraped market summary.")
            ms_page.close()
        except Exception as ms_error:
            print(f"[-] Failed to fetch Market Summary: {ms_error}")


        # --- PART 4: SCRAPE NEWS ---
        print("\nNavigating to News List...")
        try:
            news_page = context.new_page()
            news_page.goto("https://www.egx.com.eg/en/NewsList.aspx", wait_until="networkidle", timeout=60000)
            news_page.wait_for_timeout(4000)
            news = parse_news_grid(news_page.content(), "ctl00_C_N_GridView1")
            print(f"[+] Successfully scraped {len(news)} news items.")
            news_page.close()
        except Exception as news_error:
            print(f"[-] Failed to fetch News: {news_error}")


        # --- PART 5: SCRAPE SECTORS ---
        print("\nNavigating to Market Watch - Sectors...")
        try:
            sectors_page = context.new_page()
            sectors_page.goto("https://www.egx.com.eg/en/MarketWatchSectors.aspx", wait_until="networkidle", timeout=60000)
            sectors_page.wait_for_timeout(4000)
            sectors = parse_sectors(sectors_page.content())
            print(f"[+] Successfully scraped {len(sectors)} sectors.")
            sectors_page.close()
        except Exception as sectors_error:
            print(f"[-] Failed to fetch Sectors: {sectors_error}")


        # --- PART 6: SCRAPE DISCLOSURES ---
        print("\nNavigating to Disclosures search...")
        try:
            today = datetime.now(timezone.utc)
            three_months_ago = today - timedelta(days=90)
            from_str = three_months_ago.strftime("%d/%m/%Y")
            to_str = today.strftime("%d/%m/%Y")
            disclosures_url = f"https://www.egx.com.eg/en/NewsSearch.aspx?com=&word=&from={from_str}&to={to_str}&isin=&sec_id=20"

            disc_page = context.new_page()
            disc_page.goto(disclosures_url, wait_until="networkidle", timeout=60000)
            disc_page.wait_for_timeout(4000)
            disclosures = parse_news_grid(disc_page.content(), "ctl00_C_N_GVNews")
            print(f"[+] Successfully scraped {len(disclosures)} disclosures.")
            disc_page.close()
        except Exception as disc_error:
            print(f"[-] Failed to fetch Disclosures: {disc_error}")


        # --- PART 7: SCRAPE BULLETIN ---
        print("\nNavigating to Bulletin News...")
        try:
            bulletin_page = context.new_page()
            bulletin_page.goto("https://www.egx.com.eg/ar/BulletinNews.aspx", wait_until="networkidle", timeout=60000)
            bulletin_page.wait_for_timeout(4000)
            bulletin = parse_news_grid(bulletin_page.content(), "ctl00_C_BulletinNews1_GVNews", base_url="https://www.egx.com.eg/ar/")
            print(f"[+] Successfully scraped {len(bulletin)} bulletin items.")
            bulletin_page.close()
        except Exception as bulletin_error:
            print(f"[-] Failed to fetch Bulletin: {bulletin_error}")


        # --- PART 8: SCRAPE LIVE MARKET STATUS & ALL INDEX GRAPH DATA ---
        print("\nNavigating to Homepage for live market status and chart data...")
        try:
            home_page = context.new_page()

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

            home_page.on("response", handle_chart_response)
            home_page.goto("https://www.egx.com.eg/ar/homepage.aspx", wait_until="networkidle", timeout=60000)
            home_page.wait_for_timeout(5000)

            live_status = parse_live_market_status(home_page.content())
            print(f"[+] Live market status: {live_status}")

            # Identifiers matching the div[dataindex] chart tabs
            chart_tabs = ["EGX30", "EGX_33_Shariah", "EGX70_EWI", "EGX100_EWI"]
            
            for tab_value in chart_tabs:
                selector = f'div[dataindex="{tab_value}"]'
                if home_page.locator(selector).count() > 0:
                    print(f"[*] Switching chart workspace to: {tab_value}")
                    try:
                        # Click explicitly using standard dispatch parameters
                        home_page.locator(selector).click(force=True)
                        home_page.wait_for_timeout(4000)
                    except Exception as click_error:
                        print(f"[-] Interaction skipped on tab {tab_value}: {click_error}")
                else:
                    print(f"[-] Layout element missing for chart tab: {tab_value}")

            home_page.close()
        except Exception as status_error:
            print(f"[-] Failed to fetch live market status/chart data: {status_error}")


        # --- PART 9: SCRAPE INVESTOR ACTIVITY ---
        print("\nFetching Investor Type data...")
        try:
            investor_referer = "https://www.egx.com.eg/en/InvestorsTypeCharts.aspx"
            inv_page = context.new_page()
            inv_page.goto(investor_referer, wait_until="networkidle", timeout=60000)
            inv_page.wait_for_timeout(4000)
            inv_page.close()

            tables_raw = fetch_investor_json(context, "https://www.egx.com.eg/WebService.asmx/GetInvestorTables?Lang=ar&SB=1", investor_referer)
            investor_activity["byGroup"] = parse_investor_tables(tables_raw)

            pie2_raw = fetch_investor_json(context, "https://www.egx.com.eg/WebService.asmx/InvPieCharts?Lang=ar&SB=1&Type=2", investor_referer)
            investor_activity["nationalityBreakdownPct"] = parse_pie_chart(pie2_raw)

            indiv_raw = fetch_investor_json(context, "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart?Lang=ar&SB=1&Type=1", investor_referer)
            investor_activity["individualsByNationality"] = parse_stack_chart(indiv_raw)

            inst_raw = fetch_investor_json(context, "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart?Lang=ar&SB=1&Type=2", investor_referer)
            investor_activity["institutionsByNationality"] = parse_stack_chart(inst_raw)
            print(f"[+] Successfully scraped investor activity.")
        except Exception as inv_error:
            print(f"[-] Failed to fetch Investor Type data: {inv_error}")


        # --- PART 10: SCRAPE EGX30 CONSTITUENTS ---
        print("\nNavigating to EGX30 Constituents...")
        try:
            cic_page = context.new_page()
            cic_page.goto("https://www.egx.com.eg/ar/currentindexconstituntes.aspx?type=1&nav=1", wait_until="networkidle", timeout=60000)
            cic_page.wait_for_timeout(5000)
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
