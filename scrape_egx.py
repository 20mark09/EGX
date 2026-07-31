import json
import re
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def scrape_egx_prices():
    # Correct EGX trading data URL
    url = "https://www.egx.com.eg/en/prices.aspx"
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        print("Navigating to EGX Market Watch...")
        page.goto(url, wait_until="networkidle")

        # 1. Click the Market Segment / Main Board tab if present
        try:
            print("Clicking tab...")
            page.click("#ctl00_C_S_lkMarket")
        except Exception as e:
            print(f"Tab click bypassed or non-interactive: {e}")

        # 2. Wait explicitly for ASP.NET RadGrid table rows to be rendered in the DOM
        print("Waiting for grid rows to render...")
        grid_selector = "#ctl00_C_S_RadGrid2_ctl00 tr.GridRow_Default, #ctl00_C_S_RadGrid2_ctl00 tr.GridAltRow_Default"
        
        try:
            page.wait_for_selector(grid_selector, state="visible", timeout=20000)
            print("Grid rows successfully loaded.")
        except Exception:
            print("Timeout waiting for explicit grid rows. Attempting to parse existing DOM...")

        # 3. Grab full updated page HTML
        html_content = page.content()
        browser.close()

    # 4. Parse DOM using BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")
    rows = soup.select("#ctl00_C_S_RadGrid2_ctl00 tr[class*='GridRow_Default'], #ctl00_C_S_RadGrid2_ctl00 tr[class*='GridAltRow_Default']")
    
    scraped_data = []

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 12:
            continue

        # Extract Company Name
        name_elem = row.select_one("span[id*='lblName']")
        company_name = name_elem.get_text(strip=True) if name_elem else cols[0].get_text(strip=True)

        # Extract ISIN Code from detail link attribute
        isin_link = row.select_one("a[href*='ISIN=']")
        isin = ""
        if isin_link and "href" in isin_link.attrs:
            match = re.search(r"ISIN=([A-Z0-9]+)", isin_link["href"])
            if match:
                isin = match.group(1)

        # Extract numeric values
        sector = cols[2].get_text(strip=True) if len(cols) > 2 else ""
        prev_close = cols[3].get_text(strip=True) if len(cols) > 3 else ""
        open_price = cols[4].get_text(strip=True) if len(cols) > 4 else ""
        last_price = cols[7].get_text(strip=True) if len(cols) > 7 else ""
        volume = cols[11].get_text(strip=True) if len(cols) > 11 else ""

        item = {
            "isin": isin,
            "name": company_name,
            "sector": sector,
            "prev_close": prev_close,
            "open": open_price,
            "last": last_price,
            "volume": volume
        }
        scraped_data.append(item)

    print(f"\nScraped {len(scraped_data)} stocks successfully.\n")
    return scraped_data


if __name__ == "__main__":
    results = scrape_egx_prices()
    
    # Preview first 5 items
    print(json.dumps(results[:5], indent=2, ensure_ascii=False))
