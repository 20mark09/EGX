"""
Merges the per-section partial JSON files (produced by the parallel jobs
in egx_fast_split.yml) into the single egx.json this project has always
published.

Runs as the final "merge" job, after all section jobs, with `if: always()`
so it still runs even if some section jobs failed - a partial run should
still publish whatever sections did succeed, rather than the whole run
failing because e.g. investor-activity's IP got blocked.

Missing files (job failed / didn't upload its artifact) fall back to the
same empty defaults main() in scrape_egx_fast.py has always used, so
egx.json's shape never changes regardless of which sections came through.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

OUTPUT_FILE = "egx.json"


def load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[!] {path} not found - that section's job likely failed or was blocked. Using default.")
        return default
    except json.JSONDecodeError as e:
        print(f"[!] {path} exists but isn't valid JSON ({e}). Using default.")
        return default


def main():
    indices = load("egx_indices.json", {"indices": {}})
    homepage = load("egx_homepage.json", {
        "liveMarketStatus": {"text_ar": None, "color": None},
        "indexCharts": {},
    })
    gl = load("egx_gl.json", {"gainers": [], "losers": []})
    prices = load("egx_prices_section.json", {"prices": []})
    investor = load("egx_investor.json", {"investorActivity": {
        "byGroup": {}, "nationalityBreakdownPct": [],
        "individualsByNationality": [], "institutionsByNationality": [],
    }})

    output = {
        "source": "https://www.egx.com.eg",
        "lastUpdated": datetime.now(ZoneInfo("Africa/Cairo")).strftime("%Y-%m-%d %I:%M:%S %p"),
        "indices": indices.get("indices", {}),
        "prices": prices.get("prices", []),
        "gainers": gl.get("gainers", []),
        "losers": gl.get("losers", []),
        "liveMarketStatus": homepage.get("liveMarketStatus"),
        "indexCharts": homepage.get("indexCharts", {}),
        "investorActivity": investor.get("investorActivity", {}),
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    sections_present = {
        "indices": bool(output["indices"]),
        "prices": bool(output["prices"]),
        "gainers/losers": bool(output["gainers"] or output["losers"]),
        "liveMarketStatus": output["liveMarketStatus"].get("text_ar") is not None,
        "investorActivity": any(output["investorActivity"].get(k) for k in output["investorActivity"]),
    }
    print(f"\nMerge complete! Saved to {OUTPUT_FILE}")
    print(f"Sections present this run: {sections_present}")


if __name__ == "__main__":
    main()
