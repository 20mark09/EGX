import json
import re
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

def scrape_egx_prices():
    url = "https://www.egx.com.eg/en/prices.aspx"
    
    with sync_playwright() as p:
        # Launch Chromium with extra options for heavy/slow legacy sites
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # Set generous default navigation and timeout limits (60 seconds)
        page.set_default_navigation_timeout(60000)
        page.set_default_timeout(60000)

        print("Navigating to EGX Market Watch (waiting for DOM content)...")
        # 'domcontentloaded' is safer than 'networkidle' for slow ASP.NET pages with background polls
        page.goto(url, wait_until="domcontentloaded")

        # Allow extra time for ASP.NET scripts to finish booting up
        print("Waiting 10 seconds for initial page scripts to stabilize...")
        page.wait_for_timeout(10000)

        tab_selector = "#ctl00_C_S_lkMarket"
        grid_selector = "#ctl00_C_S_RadGrid2_ctl00 tr.GridRow_Default, #ctl00_C_S_RadGrid2_ctl00 tr.GridAltRow_Default"

        # 1. Attempt Tab Click with explicit locator wait
        try:
            print("Waiting for tab element to become visible...")
            page.wait_for_selector(tab_selector, state="visible", timeout=30000)
            print("Clicking tab...")
            page.click(tab_selector)
            
            # Wait for ASP.NET AJAX UpdatePanel response to replace DOM content
            page.wait_for_timeout(5000)
        except Exception as e:
            print(f"Tab click failed/timed out: {e}")

        # 2. Wait explicitly for grid rows to appear after AJAX render
        print("Waiting for grid rows to render...")
        try:
            page.wait_for_selector(grid_selector, state="attached", timeout=40000)
            print("Grid rows successfully loaded into DOM.")
        except Exception:
            print("Timeout waiting for explicit grid rows. Parsing current DOM snapshot...")

        # Grab full updated HTML
        html_content = page.content()
        browser.close()

    # 3. Parse DOM using BeautifulSoup
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
    print(json.dumps(results[:5], indent=2, ensure_ascii=False))
