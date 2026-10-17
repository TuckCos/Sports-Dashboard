#!/usr/bin/env python3
"""Update dashboard crop prices from Tronson Grain's published Doyon cash bids.

Does not invent prices. Leaves fertilizer empty until a verifiable local source exists.
"""
import datetime as dt
import json
import pathlib
import re
import urllib.request
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent
URL = "https://www.tronsongrain.com/cash-bids"
OUT = ROOT / "prices.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SportsDashboard/1.0)", "Accept": "text/html"}
CROPS = {"Spring Wheat": "wheat", "Soybeans": "soybeans", "Corn": "corn",
         "Canola": "canola", "Pinto Beans": "pinto"}
FERT = ("urea", "map", "potash", "ams")
NUMBER = re.compile(r"^-?\$?(\d+(?:,\d{3})*(?:\.\d+)?)s?$")
DATE = re.compile(r"Cash bids for\s+([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})", re.I)
MONTH = re.compile(r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", re.I)

def number(s):
    m = NUMBER.fullmatch(s.strip().replace("\xa0", ""))
    return (-1 if s.strip().startswith("-") else 1) * float(m.group(1).replace(",", "")) if m else None

def extract(page):
    soup = BeautifulSoup(page, "html.parser")
    for el in soup(["script", "style", "noscript"]):
        el.decompose()
    # Bushel cash-bid pages expose headings and their first delivery row in readable HTML.
    text = soup.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    lines = [x for x in lines if x]
    date_match = DATE.search(" ".join(lines))
    if not date_match:
        raise ValueError("No cash-bid publication date found; page structure may have changed")
    bid_date = dt.datetime.strptime(date_match.group(1), "%A, %B %d, %Y").date()
    today = dt.datetime.now(dt.timezone.utc).date()
    if bid_date > today + dt.timedelta(days=1) or today - bid_date > dt.timedelta(days=7):
        raise ValueError(f"Cash bids are stale ({bid_date}); refusing to publish old prices")
    # Restrict to Doyon elevator section, not any news or other site content.
    start = next((i for i, x in enumerate(lines) if x == "Doyon"), None)
    if start is None:
        raise ValueError("Doyon location not found")
    end = next((i for i in range(start+1, len(lines))
                if lines[i] in ("Data provided by Bushel", "Contact Us")), len(lines))
    section = lines[start:end]
    found = {}
    for heading, key in CROPS.items():
        indices = [i for i, x in enumerate(section) if x.lower() == heading.lower()]
        if not indices:
            continue
        i = indices[0]
        next_heading = min([j for j in range(i+1, len(section))
                            if section[j] in CROPS or section[j] in ("Sunflowers", "Barley")] or [len(section)])
        part = section[i+1:next_heading]
        # First row follows the column labels, which vary by crop.
        try:
            col = next(j for j, x in enumerate(part) if x.lower() == "delivery")
            bid_col = next(j for j in range(col+1, len(part)) if part[j].lower() == "bid")
        except StopIteration:
            continue
        # Header sequence finishes at "Last Trade" for grains, or second blank column for beans.
        header_end = next((j for j in range(bid_col+1, len(part))
                           if part[j].lower() == "last trade"), None)
        if header_end is not None:
            row = part[header_end+1:]
        else:
            # For specialty crops, the first delivery appears after header labels.
            row = part[bid_col+1:]
            while row and row[0].lower() in ("basis", "futures", "change", "futures month", "last trade"):
                row.pop(0)
        # Locate a delivery label followed by a bid. Never infer values from unrelated text.
        for j in range(len(row)-1):
            delivery = row[j]
            cash = number(row[j+1])
            if cash is None or not (0.1 <= cash <= 100):
                continue
            if number(delivery) is not None or delivery.lower() in ("basis", "futures", "change", "bid"):
                continue
            # Bushel may append 'Image' to delivery text.
            delivery = re.sub(r"Image$", "", delivery).strip()
            if not delivery or len(delivery) > 45:
                continue
            unit = "cwt" if key == "pinto" else ("cwt" if key == "canola" else "bu")
            entry = {"price": cash, "unit": unit, "source": "Tronson Grain - Doyon",
                     "delivery": delivery, "bid_date": bid_date.isoformat()}
            if key in ("corn", "wheat", "soybeans"):
                # A full grain row is Delivery, Bid, Basis, Futures, Change, Futures Month.
                if j+5 < len(row):
                    basis = number(row[j+2])
                    future = number(row[j+3])
                    month = row[j+5]
                    if (basis is not None and future is not None and
                        MONTH.match(month) and abs((future + basis)-cash) < 0.06):
                        entry["futures"] = {"price": future, "contract": month, "unit": "bu"}
            found[key] = entry
            break
    if not found:
        raise ValueError("No verified cash bids found; page format may have changed")
    return bid_date, found

def previous_fertilizer():
    try:
        previous = json.loads(OUT.read_text(encoding="utf-8"))
        saved = previous.get("fertilizer", {})
        return {
            name: saved.get(name, {})
            if isinstance(saved.get(name, {}), dict)
            else {}
            for name in FERT
        }
    except (OSError, ValueError, TypeError):
        return {name: {} for name in FERT}
def main():
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as resp:
        html = resp.read().decode("utf-8", "replace")
    bid_date, crops = extract(html)
    output = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cash_bid_date": bid_date.isoformat(),
        "cash_bid_source": URL,
        "crops": crops,
        "fertilizer": previous_fertilizer()
    }
    OUT.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(crops)} verified Tronson Grain bids dated {bid_date}")
    for name, value in crops.items():
        print(name, value["price"], value["unit"], value.get("futures", {}))

if __name__ == "__main__":
    main()
