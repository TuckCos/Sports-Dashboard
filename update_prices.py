#!/usr/bin/env python3
"""Update Tronson Grain cash bids and verified national fertilizer benchmarks.

Each fertilizer retains its last verified quote when no newer quote is available.
"""
import datetime as dt
import json
import pathlib
import re
import urllib.request
import math
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent
URL = "https://www.tronsongrain.com/cash-bids"
FERT_URL = "https://fertilizerprice.com/"
OUT = ROOT / "prices.json"
LOCAL_FERT = ROOT / "local_fertilizer_prices.json"
DEVILS_LAKE = (48.1128, -98.8651)
MAX_LOCAL_MILES = 100
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SportsDashboard/1.0)", "Accept": "text/html"}
CROPS = {"Spring Wheat": "wheat", "Soybeans": "soybeans", "Corn": "corn", "Canola": "canola", "Pinto Beans": "pinto"}
FERT = ("urea", "map", "potash", "ams", "anhydrous")
FORMULAS = {"urea": "46-0-0", "map": "11-52-0", "potash": "0-0-60", "ams": "21-0-0-24S", "anhydrous": "82-0-0"}
NUMBER = re.compile(r"^-?\$?(\d+(?:,\d{3})*(?:\.\d+)?)s?$")
DATE = re.compile(r"Cash bids for\s+([A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4})", re.I)
MONTH = re.compile(r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", re.I)
FERT_DATE = re.compile(r"Updated\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})", re.I)


def get_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", "replace")


def number(s):
    m = NUMBER.fullmatch(s.strip().replace("\xa0", ""))
    return (-1 if s.strip().startswith("-") else 1) * float(m.group(1).replace(",", "")) if m else None


def extract(page):
    soup = BeautifulSoup(page, "html.parser")
    for el in soup(["script", "style", "noscript"]):
        el.decompose()
    lines = [re.sub(r"\s+", " ", x).strip() for x in soup.get_text("\n", strip=True).splitlines()]
    lines = [x for x in lines if x]
    date_match = DATE.search(" ".join(lines))
    if not date_match:
        raise ValueError("No cash-bid publication date found; page structure may have changed")
    bid_date = dt.datetime.strptime(date_match.group(1), "%A, %B %d, %Y").date()
    today = dt.datetime.now(dt.timezone.utc).date()
    if bid_date > today + dt.timedelta(days=1) or today - bid_date > dt.timedelta(days=7):
        raise ValueError(f"Cash bids are stale ({bid_date}); refusing to publish old prices")
    start = next((i for i, x in enumerate(lines) if x == "Doyon"), None)
    if start is None:
        raise ValueError("Doyon location not found")
    end = next((i for i in range(start + 1, len(lines)) if lines[i] in ("Data provided by Bushel", "Contact Us")), len(lines))
    section = lines[start:end]
    found = {}
    for heading, key in CROPS.items():
        indices = [i for i, x in enumerate(section) if x.lower() == heading.lower()]
        if not indices:
            continue
        i = indices[0]
        next_heading = min([j for j in range(i + 1, len(section)) if section[j] in CROPS or section[j] in ("Sunflowers", "Barley")] or [len(section)])
        part = section[i + 1:next_heading]
        try:
            col = next(j for j, x in enumerate(part) if x.lower() == "delivery")
            bid_col = next(j for j in range(col + 1, len(part)) if part[j].lower() == "bid")
        except StopIteration:
            continue
        header_end = next((j for j in range(bid_col + 1, len(part)) if part[j].lower() == "last trade"), None)
        if header_end is not None:
            row = part[header_end + 1:]
        else:
            row = part[bid_col + 1:]
            while row and row[0].lower() in ("basis", "futures", "change", "futures month", "last trade"):
                row.pop(0)
        for j in range(len(row) - 1):
            delivery = row[j]
            cash = number(row[j + 1])
            if cash is None or not (0.1 <= cash <= 100):
                continue
            if number(delivery) is not None or delivery.lower() in ("basis", "futures", "change", "bid"):
                continue
            delivery = re.sub(r"Image$", "", delivery).strip()
            if not delivery or len(delivery) > 45:
                continue
            unit = "cwt" if key in ("pinto", "canola") else "bu"
            entry = {"price": cash, "unit": unit, "source": "Tronson Grain - Doyon", "delivery": delivery, "bid_date": bid_date.isoformat()}
            if key in ("corn", "wheat", "soybeans") and j + 5 < len(row):
                basis = number(row[j + 2])
                future = number(row[j + 3])
                month = row[j + 5]
                if basis is not None and future is not None and MONTH.match(month) and abs((future + basis) - cash) < 0.06:
                    entry["futures"] = {"price": future, "contract": month, "unit": "bu"}
            found[key] = entry
            break
    if not found:
        raise ValueError("No verified cash bids found; page format may have changed")
    return bid_date, found


def previous_data():
    try:
        saved = json.loads(OUT.read_text(encoding="utf-8"))
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def extract_fertilizer(page):
    soup = BeautifulSoup(page, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    date_match = FERT_DATE.search(text)
    if not date_match:
        raise ValueError("Fertilizer publication date missing")
    published = dt.datetime.strptime(date_match.group(1).replace(",", ""), "%b %d %Y").date()
    today = dt.datetime.now(dt.timezone.utc).date()
    if published > today + dt.timedelta(days=1) or published < today - dt.timedelta(days=45):
        raise ValueError(f"Fertilizer report outside freshness window: {published}")
    found = {}
    # Read only product rows from the current-price table; do not scrape news or charts.
    for tr in soup.select("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"], recursive=False)]
        if len(cells) < 3:
            continue
        product, formula, price_text = cells[:3]
        normalized = re.sub(r"\s+", " ", product).strip().lower()
        key = None
        if normalized == "urea":
            key = "urea"
        elif normalized == "map":
            key = "map"
        elif normalized in ("potash (mop)", "potash", "muriate of potash"):
            key = "potash"
        elif normalized in ("ammonium sulfate (ams)", "ammonium sulfate", "ams"):
            key = "ams"
        elif normalized in ("anhydrous ammonia", "anhydrous"):
            key = "anhydrous"
        if key is None or formula.replace(" ", "").upper() != FORMULAS[key].upper():
            continue
        price = number(price_text)
        if price is None or not (100 <= price <= 3000):
            continue
        found[key] = {"price": price, "unit": "short ton", "source": "FertilizerPrice.com - National Average", "price_date": published.isoformat(), "region": "National Average"}
    if not found:
        raise ValueError("No fertilizer table rows matched; website may have changed")
    return found


def merge_fertilizer(previous, fresh):
    merged = {}
    for key in FERT:
        old = previous.get(key, {})
        old = old if isinstance(old, dict) else {}
        new = fresh.get(key)
        # Do not replace a dated quote with an older one.
        if new and (not old.get("price_date") or new["price_date"] >= old["price_date"]):
            merged[key] = new
        else:
            merged[key] = old
    return merged
    
def distance_miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    a1, a2 = math.radians(lat1), math.radians(lat2)
    da = math.radians(lat2 - lat1)
    db = math.radians(lon2 - lon1)
    h = math.sin(da / 2)**2 + math.cos(a1) * math.cos(a2) * math.sin(db / 2)**2
    return 2 * r * math.asin(min(1, math.sqrt(h)))


def local_fertilizer_prices():
    if not LOCAL_FERT.exists():
        return {}

    data = json.loads(LOCAL_FERT.read_text(encoding="utf-8"))
    quotes = data.get("quotes", [])
    if not isinstance(quotes, list):
        raise ValueError("Local fertilizer quotes must be a list")

    found = {}
    today = dt.datetime.now(dt.timezone.utc).date()

    for q in quotes:
        try:
            key = str(q["product"]).lower().strip()
            if key not in FERT:
                continue

            lat = float(q["latitude"])
            lon = float(q["longitude"])
            miles = distance_miles(*DEVILS_LAKE, lat, lon)
            if miles > MAX_LOCAL_MILES:
                continue

            price = float(q["price"])
            if not 100 <= price <= 3000:
                continue

            date = dt.date.fromisoformat(q["price_date"])
            if date > today or (today - date).days > 45:
                continue

            supplier = str(q["supplier"]).strip()
            if not supplier:
                continue

            if str(q.get("unit", "short ton")).lower() != "short ton":
                continue

            entry = {
                "price": price,
                "unit": "short ton",
                "source": supplier,
                "price_date": date.isoformat(),
                "region": "Local - Devils Lake Area",
                "distance_miles": round(miles, 1)
            }

            if key not in found or date.isoformat() > found[key]["price_date"]:
                found[key] = entry

        except (KeyError, ValueError, TypeError, OverflowError):
            continue

    return found

def main():
    old = previous_data()
    fertilizer = old.get("fertilizer", {})
    if not isinstance(fertilizer, dict):
        fertilizer = {}
    try:
        fresh_fert = extract_fertilizer(get_html(FERT_URL))
        print(f"Verified {len(fresh_fert)} fertilizer prices from {FERT_URL}")
    except Exception as exc:
        fresh_fert = {}
        print(f"Fertilizer source unavailable; retaining last prices: {exc}")
    fertilizer = merge_fertilizer(fertilizer, fresh_fert)
        try:
        local = local_fertilizer_prices()
        for key, quote in local.items():
            fertilizer[key] = quote
        print(f"Applied {len(local)} verified local fertilizer prices")
    except Exception as exc:
        print(f"Local fertilizer data unavailable: {exc}")

    # Crop updates are independent of fertilizer updates.
    try:
        bid_date, crops = extract(get_html(URL))
        print(f"Verified {len(crops)} Tronson Grain crop bids dated {bid_date}")
        crop_date = bid_date.isoformat()
    except Exception as exc:
        print(f"Crop source unavailable; retaining last crop prices: {exc}")
        crops = old.get("crops", {})
        crop_date = old.get("cash_bid_date")
        if not isinstance(crops, dict):
            crops = {}

    if not crops and not any(v.get("price") is not None for v in fertilizer.values()):
        raise RuntimeError("No verified crop or fertilizer prices to publish")
    output = {"updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "cash_bid_date": crop_date, "cash_bid_source": URL, "crops": crops, "fertilizer": fertilizer}
    OUT.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print("Saved prices.json; last verified fertilizer quotes retained when missing")


if __name__ == "__main__":
    main()
