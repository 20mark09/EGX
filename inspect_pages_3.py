"""
One-off inspector, round 3. These are AJAX endpoints (WebService.asmx)
that the InvestorsTypeCharts.aspx page calls client-side to draw its
charts - that's why the plain page HTML was empty.

We visit the real page first (same stealth pattern as always) so the
browser context picks up whatever cookies/session token the site issues,
then hit the JSON endpoints through that same context with a Referer
header, exactly like the page's own JS would.

Saves each response body as-is (usually JSON) to a .json file, plus the
HTTP status, so we can see if any of them get blocked/need different
headers.
"""
from playwright.sync_api import sync_playwright

REFERER = "https://www.egx.com.eg/en/InvestorsTypeCharts.aspx"

ENDPOINTS = {
    "investor_nat_column_chart.json": "https://www.egx.com.eg/WebService.asmx/InvestorNatColumnChart?Lang=ar&SB=1",
    "investor_indiv_inst_column_chart.json": "https://www.egx.com.eg/WebService.asmx/InvestorIndivInstColumnChart?Lang=ar&SB=1",
    "indiv_by_nat_stack_chart_type1.json": "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart?Lang=ar&SB=1&Type=1",
    "indiv_by_nat_stack_chart_type2.json": "https://www.egx.com.eg/WebService.asmx/IndivByNatStackChart?Lang=ar&SB=1&Type=2",
    "inv_pie_charts_type1.json": "https://www.egx.com.eg/WebService.asmx/InvPieCharts?Lang=ar&SB=1&Type=1",
    "inv_pie_charts_type2.json": "https://www.egx.com.eg/WebService.asmx/InvPieCharts?Lang=ar&SB=1&Type=2",
    "get_investor_tables.json": "https://www.egx.com.eg/WebService.asmx/GetInvestorTables?Lang=ar&SB=1",
}


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )

        # Step 1: visit the real page so the context picks up cookies/session.
        page = context.new_page()
        print(f"Navigating to {REFERER} to establish a session...")
        page.goto(REFERER, wait_until="commit", timeout=60000)
        page.wait_for_timeout(10000)
        page.close()

        # Step 2: hit each JSON endpoint through the same context.
        for filename, url in ENDPOINTS.items():
            print(f"Fetching {url} ...")
            try:
                response = context.request.get(
                    url,
                    headers={
                        "Referer": REFERER,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                    },
                )
                status = response.status
                body = response.text()
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(body)
                print(f"[+] {filename}: HTTP {status}, {len(body)} chars")
            except Exception as e:
                print(f"[-] Failed on {url}: {e}")

        context.close()
        browser.close()


if __name__ == "__main__":
    main()
