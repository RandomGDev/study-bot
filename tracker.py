import os
import requests
import json

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
SEEN_FILE = "seen_ads.json"

WATCHLIST = [
    ("PowerShot", 5000),
    ("Cyber-shot", 5000),
    ("Coolpix", 5000),
    ("Ryzen 5 3600", 5000),
    ("Mechanical Keyboard", 2000),
    ("Digital Camera", 5000),
    ("DSLR", 5000),
    ("Power supply", 1750)
]

DELHI_LOCATION_ID = "1000001"

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen(seen_set):
    # Keep the list pruned to last 300 ads to stay fast
    pruned = list(seen_set)[-300:]
    with open(SEEN_FILE, "w") as f:
        json.dump(pruned, f)

def send_alert(title, price, ad_id, loc_name):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    link = f"https://www.olx.in/item/{ad_id}"
    text = (
        f"🚨 *NEW OLX DEAL SPOTTED!*\n\n"
        f"📦 *Item:* {title}\n"
        f"💰 *Price:* ₹{price:,}\n"
        f"📍 *Location:* {loc_name}\n\n"
        f"🔗 [Open on OLX]({link})"
    )
    requests.post(url, json={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }, timeout=10)

def main():
    seen_ads = load_seen()
    new_found = False
    send_alert("⚡ TEST ALERT", 999, "test-item", "System Online")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    for query, max_price in WATCHLIST:
        api_url = "https://www.olx.in/api/relevance/v4/search"
        params = {
            "query": query,
            "location": DELHI_LOCATION_ID,
            "facet_limit": 30,
            "spellcheck": "true"
        }

        try:
            res = requests.get(api_url, headers=headers, params=params, timeout=10)
            if res.status_code == 200:
                for item in res.json().get("data", []):
                    ad_id = str(item.get("id"))
                    title = item.get("title", "No Title")
                    price = item.get("price", {}).get("value", {}).get("raw")
                    loc = item.get("locations_resolved", {}).get("ADMIN_LEVEL_3_name") or "Delhi"

                    if not price or ad_id in seen_ads:
                        continue

                    seen_ads.add(ad_id)
                    new_found = True

                    if price <= max_price:
                        send_alert(title, price, ad_id, loc)
        except Exception as e:
            print(f"Error checking {query}: {e}")

    if new_found:
        save_seen(seen_ads)

if __name__ == "__main__":
    main()
  
