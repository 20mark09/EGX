"""
Shared helpers for scraping beta.egx.com.eg's /api/bff/egx/* JSON API.

Background: EGX replaced the old ASP.NET WebForms site (postbacks,
RadGrid HTML tables, .asmx web services) with a Next.js app that calls a
clean internal "BFF" (Backend-For-Frontend) JSON API. Every endpoint
returns the same envelope:

    {"data": ..., "success": true, "message": "...",
     "totalCount": N, "pageNumber": N, "pageSize": N, "totalPages": N}

The BFF still sits behind the same F5 bot-defense product as the old
site (visible via the TS*/TSPD_* cookies), and additionally requires a
custom `x-egx-bff-request: 1` header - hitting these URLs without that
header returns a bare 404 rather than real data or an auth error, which
is a deliberate "don't look like you're calling this directly" signal.

The approach that worked for the old site's investor-activity endpoint
applies here too, and is simpler now that there's no ASP.NET postback
dance involved at all: load a real page once with Playwright (which
naturally acquires the F5 cookies and NextAuth's authjs.* session
cookies, the same way a real visit would), then issue every API call via
page.evaluate(fetch(...)) from inside that already-authenticated
session, with the required headers set explicitly.
"""

import json
import random
import time
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

BASE = "https://beta.egx.com.eg"
API_BASE = f"{BASE}/api/bff/egx"

BROWSER_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
EXTRA_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


def launch_browser_context(p):
    browser = p.chromium.launch(headless=True, args=BROWSER_LAUNCH_ARGS)
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 720},
        extra_http_headers=EXTRA_HEADERS,
        locale="en-US",
    )
    return browser, context


def warm_up_session(page, landing_path="/en/market/market-watch"):
    """Loads a real page once so the browser naturally picks up the F5
    bot-defense cookies and NextAuth session cookies, before any BFF
    calls are made. Every BFF call after this reuses this same page's
    cookie jar automatically (page.evaluate(fetch(...)) runs in-page).
    """
    page.goto(f"{BASE}{landing_path}", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(random.randint(2500, 4500))


def _bff_headers_js():
    # Built inline in the fetch() call below; kept here only as
    # documentation of what's required. x-egx-bff-request is the custom
    # header the BFF checks for; without it these routes 404.
    return {
        "Accept": "application/json",
        "x-egx-bff-request": "1",
    }


def bff_get(page, endpoint, params=None, referer=None, retries=2, retry_delay=3):
    """GET a /api/bff/egx/<endpoint> route via the page's own fetch(),
    so it goes out through Chromium's real network stack with the
    session's real cookies - see module docstring for why that matters
    here. Returns the unwrapped "data" field, or None on failure.
    """
    query = ""
    if params:
        parts = [f"{k}={v}" for k, v in params.items() if v is not None]
        if parts:
            query = "?" + "&".join(parts)
    url = f"{API_BASE}/{endpoint}{query}"

    for attempt in range(retries + 1):
        try:
            result = page.evaluate(
                """async ({ url }) => {
                    const res = await fetch(url, {
                        method: "GET",
                        headers: {
                            "Accept": "application/json",
                            "x-egx-bff-request": "1",
                        },
                        credentials: "include",
                    });
                    const text = await res.text();
                    return { status: res.status, text };
                }""",
                {"url": url},
            )
            status = result.get("status") if result else None
            text = result.get("text", "") if result else ""
            if status == 200 and text:
                try:
                    parsed = json.loads(text)
                except Exception as e:
                    print(f"[-] {endpoint}: 200 but not valid JSON ({e}): {text[:150]!r}")
                    parsed = None
                if parsed is not None:
                    if parsed.get("success") is False:
                        print(f"[-] {endpoint}: success=false, message={parsed.get('message')!r}")
                    return parsed
            else:
                print(f"[-] {endpoint} (attempt {attempt + 1}/{retries + 1}): "
                      f"status={status} body={text[:150]!r}")
        except Exception as e:
            print(f"[-] {endpoint} (attempt {attempt + 1}/{retries + 1}) raised: {e}")

        if attempt < retries:
            time.sleep(retry_delay)

    return None


def bff_post(page, endpoint, body, referer=None, retries=2, retry_delay=3):
    """POST to a /api/bff/egx/<endpoint> route with a JSON body (some
    BFF routes, like news-search, are POST-only). Same in-page fetch()
    approach as bff_get for the same reason.
    """
    url = f"{API_BASE}/{endpoint}"

    for attempt in range(retries + 1):
        try:
            result = page.evaluate(
                """async ({ url, body }) => {
                    const res = await fetch(url, {
                        method: "POST",
                        headers: {
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                            "x-egx-bff-request": "1",
                        },
                        credentials: "include",
                        body: body,
                    });
                    const text = await res.text();
                    return { status: res.status, text };
                }""",
                {"url": url, "body": json.dumps(body)},
            )
            status = result.get("status") if result else None
            text = result.get("text", "") if result else ""
            if status == 200 and text:
                try:
                    parsed = json.loads(text)
                except Exception as e:
                    print(f"[-] {endpoint}: 200 but not valid JSON ({e}): {text[:150]!r}")
                    parsed = None
                if parsed is not None:
                    if parsed.get("success") is False:
                        print(f"[-] {endpoint}: success=false, message={parsed.get('message')!r}")
                    return parsed
            else:
                print(f"[-] {endpoint} (attempt {attempt + 1}/{retries + 1}): "
                      f"status={status} body={text[:150]!r}")
        except Exception as e:
            print(f"[-] {endpoint} (attempt {attempt + 1}/{retries + 1}) raised: {e}")

        if attempt < retries:
            time.sleep(retry_delay)

    return None


def now_cairo_str():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d %I:%M:%S %p")
