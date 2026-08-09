"""
Shared helpers for the EGX scrapers.

This used to be one monolithic script (scrape_egx.py) that scraped
everything and ran on a single schedule. It's now split into three
scripts that each own a subset of the site and run on their own
schedule, based on how often each thing actually needs to be fresh:

  - scrape_egx_fast.py      -> indices + full stock prices, every 15 min
  - scrape_egx_bulletin.py  -> bulletin news + push notifications, every 5 min
  - scrape_egx_slow.py      -> everything else (gainers/losers, market
                                summary, news, sectors, disclosures, live
                                market status, index charts, investor
                                activity, index constituents), hourly

All the actual scraping/parsing logic is identical to the original
script - this file is just that logic factored out so three scripts can
share it instead of copy-pasting ~600 lines three times.
"""

import json
import os
import re
import time
import random
from datetime import datetime, timezone
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Top_GL.aspx (Top Gainers/Losers) has no ticker code or link anywhere in
# its markup - confirmed by inspecting the raw HTML - so there's no way
# to scrape a code for movers directly. company_codes.json (committed
# alongside this script) is a hand-maintained name -> code lookup to
# fill that gap. Missing/unmapped names just get no code, which the app
# already handles gracefully (falls back to a generated avatar).
COMPANY_CODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "company_codes.json")
BULLETIN_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bulletin_state.json")

BROWSER_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# A bare user_agent + viewport is a thinner fingerprint than a real Chrome
# request - a real browser sends this whole set of headers on every
# navigation. Doesn't fix IP-level blocking, but rules out "obviously not
# a browser" as a contributing signal.
EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_dumps")


def launch_browser_context(p):
    """Launches a browser + context with the same settings every script
    used before the split, so all three keep the same fingerprint."""
    browser = p.chromium.launch(headless=True, args=BROWSER_LAUNCH_ARGS)
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 720},
        extra_http_headers=EXTRA_HEADERS,
        locale="en-US",
    )
    return browser, context


def new_page_fresh_context(browser, old_context=None):
    """Closes old_context (if given) and opens a brand new context + page.

    scrape_egx_fast.py used to run every section (indices, live status,
    gainers/losers, prices, investor activity) through one long-lived
    context/connection - five page loads plus ~10 postback/XHR calls back
    to back with only a few seconds between them. That's a much stronger
    "this is a script" signature than scrape_egx_bulletin.py's single
    page load, and is the likely reason fast.py gets blocked mid-run while
    bulletin.py doesn't. Starting a clean context per section (no shared
    cookies, no kept-alive connection carrying the previous section's
    traffic) makes each section look like an independent visit rather than
    one continuous crawl.
    """
    if old_context is not None:
        try:
            old_context.close()
        except Exception:
            pass
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 720},
        extra_http_headers=EXTRA_HEADERS,
        locale="en-US",
    )
    return context, context.new_page()


def human_delay():
    time.sleep(random.uniform(3.5, 6.0))


def jittered_delay(min_seconds, max_seconds):
    """Same idea as human_delay but with a caller-specified range, so
    section-to-section gaps and postback waits don't all land on the same
    fixed intervals (a flat 5000ms wait every time is itself a fingerprint)."""
    time.sleep(random.uniform(min_seconds, max_seconds))


def dump_debug_snapshot(page, label):
    """Saves title/URL/text-snippet to the log and the full HTML to
    debug_dumps/, on any failure worth investigating. The goal is that the
    next time something comes back empty, there's an actual artifact
    (what page did we really get - the real page, a WAF challenge page, a
    rate-limit notice, something else) instead of just a bare exception.
    Upload debug_dumps/ as a GitHub Actions artifact if you want to pull
    these down after a scheduled run.
    """
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        title, url, html = None, None, ""
        try:
            title = page.title()
        except Exception:
            pass
        try:
            url = page.url
        except Exception:
            pass
        try:
            html = page.content()
        except Exception:
            pass

        snippet = re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text()).strip()[:300] if html else ""
        print(f"[debug] {label}: title={title!r} url={url!r}")
        print(f"[debug] {label}: text snippet: {snippet!r}")

        if html:
            fname = os.path.join(DEBUG_DIR, f"{ts}_{slugify(label)}.html")
            with open(fname, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[debug] {label}: full HTML saved to {fname}")
    except Exception as dump_error:
        print(f"[debug] {label}: failed to capture debug snapshot: {dump_error}")


def goto_resilient(page, url, label, wait_until="domcontentloaded", timeout=45000, retries=2):
    """page.goto with retry+backoff on transient failures (connection
    resets, timeouts) and a debug snapshot on final failure, so a blocked
    run is diagnosable ("we got a challenge page" / "we got reset every
    time" / "the real page loaded but was missing X") rather than just a
    one-line exception in the Action log.

    Backoff is randomized and grows with attempt number specifically so
    retries themselves don't add another fixed-interval fingerprint.
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return True
        except Exception as e:
            last_error = e
            print(f"[-] goto {label} failed (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt < retries:
                backoff = random.uniform(8, 16) * (attempt + 1)
                print(f"[*] Backing off {backoff:.1f}s before retrying {label}...")
                time.sleep(backoff)

    print(f"[-] goto {label} exhausted all {retries + 1} attempt(s). Last error: {last_error}")
    dump_debug_snapshot(page, f"{label}_goto_failure")
    return False


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def slugify(label):
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


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


# --------------------------------------------------------------------
# Company code lookup (used by fast.py for prices, slow.py for movers)
# --------------------------------------------------------------------

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


# --------------------------------------------------------------------
# Indices panel (Indices.aspx) - used by fast.py
# --------------------------------------------------------------------

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
        "ytd_pct": safe_num_match(ytd_match),
    }


# --------------------------------------------------------------------
# Full stock prices (prices.aspx / Market Segment RadGrid) - fast.py
# --------------------------------------------------------------------

def parse_prices_table(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", {"id": "ctl00_C_S_RadGrid2_ctl00"})

    if table:
        stocks = []
        current_currency = None

        for row in table.find_all("tr"):
            classes = row.get("class") or []

            if "GroupHeader_Default" in classes:
                label_cell = row.find("td", attrs={"colspan": True})
                if label_cell:
                    current_currency = label_cell.get_text(strip=True)
                continue

            if "GridRow_Default" not in classes and "GridAltRow_Default" not in classes:
                continue

            cols = row.find_all("td")

            if len(cols) < 14:
                continue

            try:
                name_cell = cols[1]
                name_span = name_cell.find("span")
                name = name_span.get_text(strip=True) if name_span else name_cell.get_text(strip=True)

                if not name:
                    continue

                isin = None
                isin_link = name_cell.find("a", href=lambda h: h and "ISIN=" in h)
                if isin_link:
                    isin_match = re.search(r"ISIN=([A-Za-z0-9]+)", isin_link["href"])
                    isin = isin_match.group(1) if isin_match else None

                stocks.append({
                    "name": name,
                    "isin": isin,
                    "sector": cols[2].get_text(strip=True),
                    "currency": current_currency,
                    "prev_close": safe_num(cols[3].get_text(strip=True)),
                    "open": safe_num(cols[4].get_text(strip=True)),
                    "close": safe_num(cols[5].get_text(strip=True)),
                    "change_pct": safe_num(cols[6].get_text(strip=True)),
                    "last_price": safe_num(cols[7].get_text(strip=True)),
                    "high": safe_num(cols[8].get_text(strip=True)),
                    "low": safe_num(cols[9].get_text(strip=True)),
                    "value": safe_num(cols[10].get_text(strip=True)),
                    "volume": safe_num(cols[11].get_text(strip=True)),
                    "trades": safe_num(cols[12].get_text(strip=True)),
                    "market_cap_million": safe_num(cols[13].get_text(strip=True)),
                })
            except Exception:
                continue

        if stocks:
            return stocks

    # Generic Telerik grid fallback, in case the id/markup ever shifts.
    grid_table = soup.select_one("table.rgMasterTable, table[id*='RadGrid']")
    if not grid_table:
        return []

    headers = []
    header_row = grid_table.select_one("thead tr")
    if header_row:
        for th in header_row.find_all(["th", "td"]):
            text = re.sub(r"\s+", " ", th.get_text()).strip()
            if text:
                headers.append(text)
    if not headers:
        headers = ["Ticker", "Name", "Sector", "Last Price", "Change %",
                   "Open", "High", "Low", "Volume", "Value", "Trades"]

    scraped_data = []
    rows = grid_table.select("tbody tr.rgRow, tbody tr.rgAltRow, tbody tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        row_values = [re.sub(r"\s+", " ", cell.get_text()).strip() for cell in cells]
        stock_entry = {}
        for idx, value in enumerate(row_values):
            col_key = headers[idx] if idx < len(headers) else f"Column_{idx + 1}"
            stock_entry[col_key] = value
        if stock_entry:
            scraped_data.append(stock_entry)
    return scraped_data


# --------------------------------------------------------------------
# Top Gainers/Losers (Top_GL.aspx) - slow.py
# --------------------------------------------------------------------

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
                    "change_pct": float(cols[5].get_text(strip=True).replace(",", "").replace("%", "")),
                })
            except Exception:
                continue
    return stocks


# --------------------------------------------------------------------
# Market summary (MarketSummry.aspx) - slow.py
# --------------------------------------------------------------------

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
                        "no": safe_num(values[0]), "volume": safe_num(values[1]),
                        "value": safe_num(values[2]), "trades": safe_num(values[3]),
                    }
                continue

            if label == "Total":
                section = None
                result["main_market"]["total"] = {
                    "no": safe_num(values[0]), "volume": safe_num(values[1]),
                    "value": safe_num(values[2]), "trades": safe_num(values[3]),
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
                    "no": safe_num(values[0]), "volume": safe_num(values[1]),
                    "value": safe_num(values[2]), "trades": safe_num(values[3]),
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
                    "no": safe_num(values[0]), "volume": safe_num(values[1]),
                    "value": safe_num(values[2]), "trades": safe_num(values[3]),
                }
            else:
                result["breadth"][key] = values
    return result


# --------------------------------------------------------------------
# News grids (NewsList.aspx, NewsSearch.aspx, BulletinNews.aspx)
# used by slow.py (news/disclosures) and bulletin.py (bulletin)
# --------------------------------------------------------------------

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
            items.append({"id": news_id, "title": title, "date": date_text, "url": url})
        except Exception:
            continue
    return items


# --------------------------------------------------------------------
# Sectors (MarketWatchSectors.aspx) - slow.py
# --------------------------------------------------------------------

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


# --------------------------------------------------------------------
# Live market status + index charts (homepage.aspx) - slow.py
# --------------------------------------------------------------------

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


# --------------------------------------------------------------------
# Investor activity (InvestorsTypeCharts.aspx WebService calls) - slow.py
# --------------------------------------------------------------------

NATIONALITY_MAP = {"مصريين": "egyptians", "عرب": "arabs", "أجانب": "foreigners", "اجانب": "foreigners"}
INVESTOR_GROUP_MAP = {"1": "total", "2": "individuals", "3": "institutions"}


def fetch_investor_json(context, url, referer, retries=2, retry_delay=3):
    """Fetches one investor-activity WebService.asmx endpoint. These have
    been observed occasionally returning a raw backend error string
    (e.g. 'ORA-12521: TNS:listener does not currently know...') instead
    of JSON, or an empty response, even though the HTTP request itself
    succeeds - that's EGX's own backend having a bad moment, not a
    scraping problem. We retry a couple of times before giving up, and
    log the actual bad response so a future empty result is diagnosable
    straight from the Action log instead of needing another round of
    "here's my JSON, something's missing".
    """
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
                "buy": row.get("Buy"), "sell": row.get("Sell"), "net": row.get("Net"),
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


# IndivByNatStackChart returns the same {Label, Value, Color}-shaped rows
# as the pie chart endpoint - the original script called an undefined
# parse_stack_chart() here (a leftover bug from before the split, always
# silently caught by the surrounding try/except so it never surfaced).
# Reusing parse_pie_chart is a straightforward, tested fix.
parse_stack_chart = parse_pie_chart


# --------------------------------------------------------------------
# Index constituents (currentindexconstituntes.aspx) - slow.py
# --------------------------------------------------------------------

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


# --------------------------------------------------------------------
# Bulletin state tracking + FCM push notifications - bulletin.py
# --------------------------------------------------------------------

def _bulletin_item_key(item):
    """A stable identifier for one bulletin item. Bulletin items don't
    reliably have a NewsID-style id the way News/Disclosures items do
    (we only ever confirmed Bulletin's *empty* state's markup, never a
    populated one) - falling back to title+date when id is missing means
    real items never get silently dropped from tracking just because
    they lack a link/id.
    """
    if item.get("id"):
        return f"id:{item['id']}"
    return f"td:{item.get('title', '')}|{item.get('date', '')}"


def load_bulletin_state():
    """The set of bulletin item keys seen as of the last run, so we only
    notify on genuinely new items rather than re-notifying every run."""
    try:
        with open(BULLETIN_STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_bulletin_state(keys):
    with open(BULLETIN_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(keys), f, indent=2)


def get_fcm_access_token():
    """Exchanges the FCM_SERVICE_ACCOUNT_JSON GitHub Actions secret (a
    Firebase service account key) for a short-lived OAuth2 access token,
    used to authenticate calls to FCM's HTTP v1 send API. Returns
    (None, None) if the secret isn't configured, so this fails quietly
    rather than crashing the whole scrape run.
    """
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
    """Sends a push notification to every app instance subscribed to the
    'egx_bulletins' topic, via FCM's HTTP v1 API."""
    import requests

    token, project_id = get_fcm_access_token()
    if not token or not project_id:
        print("[-] FCM not configured (FCM_SERVICE_ACCOUNT_JSON secret missing) - skipping push notification.")
        return

    url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
    payload = {"message": {"topic": "egx_bulletins", "notification": {"title": title, "body": body}}}
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
    """Compares this run's bulletin items against the last known state
    (bulletin_state.json, committed to the repo) and sends one push
    notification per genuinely new item, so the app gets notified "as
    soon as something new shows up" rather than re-notifying the same
    items every single run.
    """
    if not bulletin_items:
        return

    seen_keys = load_bulletin_state()
    current_keys = {_bulletin_item_key(item) for item in bulletin_items}
    new_keys = current_keys - seen_keys

    if new_keys:
        new_items = [item for item in bulletin_items if _bulletin_item_key(item) in new_keys]
        for item in new_items:
            send_fcm_notification("EGX Bulletin", item.get("title", "New bulletin item"))
        print(f"[+] {len(new_items)} new bulletin item(s) - notification(s) sent.")
    else:
        print("[+] No new bulletin items since last run - no notifications sent.")

    save_bulletin_state(current_keys | seen_keys)
