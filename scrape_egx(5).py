import json
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

OUTPUT_FILE = "egx.json"


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


def main():
    indices_output = {}
    gainers = []
    losers = []

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

        context.close()
        browser.close()

    # --- SAVE STRUCTURED RESULTS ---
    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": now_utc(),
        "indices": indices_output,
        "gainers": gainers,
        "losers": losers
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nFinal run complete! Tracking metrics saved perfectly to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
