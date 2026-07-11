"""
One-off inspector, round 5. We don't know which page embeds the EGX30
mini-chart widget that calls getIndexChartData, or how its `gtk` token
is generated (likely a rotating anti-scraping token tied to the
session/timestamp - not safe to hardcode).

Instead of guessing, this script listens to ALL network requests while
visiting the most likely candidate pages (starting with Indices.aspx,
where we already do the EGX30 postback for the main scraper) and logs
any request whose URL contains "getIndexChartData" or "WebService.asmx"
- that tells us the real page and the real request shape without needing
to reverse-engineer the token.
"""
from playwright.sync_api import sync_playwright

CANDIDATE_PAGES = [
    "https://www.egx.com.eg/en/Indices.aspx",
    "https://www.egx.com.eg/ar/Indices.aspx",
]

LOG_FILE = "captured_requests.txt"


def main():
    captured = []

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

        def on_request(request):
            url = request.url
            if "getIndexChartData" in url or "WebService.asmx" in url:
                captured.append(f"[REQUEST] {request.method} {url}")
                print(f"[+] Captured: {request.method} {url}")

        def on_response(response):
            url = response.url
            if "getIndexChartData" in url:
                try:
                    body = response.text()
                except Exception as e:
                    body = f"<could not read body: {e}>"
                captured.append(f"[RESPONSE {response.status}] {url}\n{body[:2000]}\n")
                print(f"[+] Captured response for: {url}")

        context.on("request", on_request)
        context.on("response", on_response)

        for url in CANDIDATE_PAGES:
            print(f"\nNavigating to {url} ...")
            page = context.new_page()
            try:
                page.goto(url, wait_until="commit", timeout=60000)
                page.wait_for_timeout(8000)

                # Trigger the EGX30 tab, same postback the main scraper
                # already does - if the chart lives here, this is likely
                # what loads it.
                try:
                    page.evaluate("__doPostBack('ctl00$C$M$lnkEGX30', '');")
                    page.wait_for_timeout(6000)
                except Exception as e:
                    print(f"[-] Postback attempt failed (page may not have it): {e}")

            except Exception as e:
                print(f"[-] Failed on {url}: {e}")
            finally:
                page.close()

        context.close()
        browser.close()

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        if captured:
            f.write("\n".join(captured))
        else:
            f.write("No matching requests captured on any candidate page.\n")

    print(f"\nDone. {len(captured)} matching entries saved to {LOG_FILE}")


if __name__ == "__main__":
    main()
