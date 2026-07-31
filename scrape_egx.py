import json
import re
import requests
from bs4 import BeautifulSoup

def get_egx_prices_via_api():
    """Fastest & most reliable method: Direct internal endpoint call."""
    print("Fetching data directly from EGX internal endpoint...")
    
    # Internal JSON endpoint used by EGX Market Watch / Prices UI
    api_url = "https://www.egx.com.eg/en/PricesData.aspx" 
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.egx.com.eg/en/prices.aspx"
    }

    try:
        response = requests.get(api_url, headers=headers, timeout=15)
        if response.status_code == 200:
            try:
                data = response.json()
                print("Successfully fetched JSON directly!")
                return data
            except json.JSONDecodeError:
                # If the endpoint returned raw HTML/UpdatePanel text instead of pure JSON
                return parse_html_table(response.text)
    except Exception as e:
        print(f"Direct API call failed: {e}. Falling back to Playwright headless scraper...")
    
    return None


def parse_html_table(html_content):
    """Fallback parser that dynamically locates any price table in the DOM."""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Find any table containing stock links or ISINs dynamically
    rows = soup.select("table tr")
    scraped_data = []

    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 5:
            continue

        # Look for ISIN code inside links or data attributes
        isin_link = row.select_one("a[href*='ISIN='], a[href*='isin=']")
        isin = ""
        if isin_link and "href" in isin_link.attrs:
            match = re.search(r"ISIN=([A-Z0-9]+)", isin_link["href"], re.IGNORECASE)
            if match:
                isin = match.group(1)

        row_text = [c.get_text(strip=True) for c in cols]
        
        # Ensure it's a valid data row (avoid headers)
        if len(row_text) >= 8 and any(char.isdigit() for char in row_text[3]):
            scraped_data.append({
                "isin": isin,
                "name": row_text[0],
                "sector": row_text[2] if len(row_text) > 2 else "",
                "prev_close": row_text[3] if len(row_text) > 3 else "",
                "last": row_text[7] if len(row_text) > 7 else "",
                "volume": row_text[-1] if len(row_text) > 0 else ""
            })

    return scraped_data


def scrape_egx_playwright_fallback():
    """Playwright backup if direct requests are blocked."""
    from playwright.sync_api import sync_playwright

    print("Launching Playwright Fallback...")
    url = "https://www.egx.com.eg/en/prices.aspx"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()

        print("Navigating to prices page...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(10000) # Give scripts time to execute

        # Grab all rendered HTML regardless of specific container IDs
        html_content = page.content()
        browser.close()

    return parse_html_table(html_content)


if __name__ == "__main__":
    # Primary attempt: Fast HTTP/API strategy
    data = get_egx_prices_via_api()

    # Secondary attempt: Browser fallback
    if not data:
        data = scrape_egx_playwright_fallback()

    print(f"\nSuccessfully extracted {len(data)} items.\n")
    if data:
        print(json.dumps(data[:5], indent=2, ensure_ascii=False))
