from bs4 import BeautifulSoup

html = page.content()
soup = BeautifulSoup(html, "html.parser")

stock_prices = []

rows = soup.select(
    "tr.GridRow_Default, tr.GridAltRow_Default"
)

print(f"Found {len(rows)} stock rows")

for row in rows:
    cols = row.find_all("td")

    if len(cols) < 14:
        continue

    try:
        name = cols[1].get_text(" ", strip=True)
        sector = cols[2].get_text(" ", strip=True)

        stock_prices.append({
            "name": name,
            "sector": sector,
            "previous_close": safe_num(cols[3].get_text(strip=True)),
            "open": safe_num(cols[4].get_text(strip=True)),
            "close": safe_num(cols[5].get_text(strip=True)),
            "change_pct": safe_num(cols[6].get_text(strip=True)),
            "last_price": safe_num(cols[7].get_text(strip=True)),
            "high": safe_num(cols[8].get_text(strip=True)),
            "low": safe_num(cols[9].get_text(strip=True)),
            "value": safe_num(cols[10].get_text(strip=True)),
            "volume": safe_num(cols[11].get_text(strip=True)),
            "trades": safe_num(cols[12].get_text(strip=True)),
            "market_cap": safe_num(cols[13].get_text(strip=True)),
        })

    except Exception as e:
        print("Row failed:", e)

print(f"Successfully scraped {len(stock_prices)} stocks")
