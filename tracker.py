import os
import requests
import json
import re

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
SEEN_FILE = "seen_ads.json"

WATCHLIST = [
    ("power supply", 2000),
    ("PowerShot", 5000),
    ("Cyber-shot", 5000),
    ("Coolpix", 5000),
    ("Ryzen 5 3600", 5000),
    ("Mechanical Keyboard", 2000),
    ("Digital Camera", 5000)
]

# Correct OLX Delhi Region ID (or leave as None for nationwide)
DELHI_LOCATION_ID = "4058659"

def escape_markdown(text):
    """Escapes special markdown characters so Telegram doesn't reject messages."""
    return re.sub(r'([_*\[\]()~`>#+=|{}.!-])', r'\\\1', str(text))

def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_seen(seen_set):
    pruned = list(seen_set)[-400:]
    with open(SEEN_FILE, "w") as f:
        json.dump(pruned, f)

def send_alert(title, price, ad_id, loc_name):
    if not BOT_TOKEN or not CHAT_ID:
        print("[!] Missing BOT_TOKEN or CHAT_ID environment variables!")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    link = f"https://www.olx.in/item/{ad_id}"
    
    clean_title = escape_markdown(title)
    clean_loc = escape_markdown(loc_name)
    
    text = (
        f"🚨 *NEW OLX DEAL SPOTTED\\!*\n\n"
        f"📦 *Item:* {clean_title}\n"
        f"💰 *Price:* ₹{price:,}\n"
        f"📍 *Location:* {clean_loc}\n\n"
        f"[Open on OLX]({link})"
    )
    
    try:
        r = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": False
        }, timeout=10)
        print(f"[Telegram Post] Status: {r.status_code}, Response: {r.text}")
    except Exception as e:
        print(f"[!] Telegram Request Failed: {e}")

def main():
    print("[*] Starting OLX Tracker run...")
    seen_ads = load_seen()
    new_found = False

    # Immediate Test Ping to confirm Telegram pipe
    print("[*] Sending startup ping...")
    send_alert("⚡ SYSTEM ONLINE: OLX Tracker is running!", 0, "test", "Delhi")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    for query, max_price in WATCHLIST:
        print(f"\n[*] Searching for: '{query}' (Max Price: ₹{max_price})")
        api_url = "https://www.olx.in/api/relevance/v4/search"
        params = {
            "query": query,
            "location": DELHI_LOCATION_ID,
            "facet_limit": 30,
            "spellcheck": "true"
        }

        try:
            res = requests.get(api_url, headers=headers, params=params, timeout=12)
            print(f"    -> OLX HTTP Status: {res.status_code}")
            
            if res.status_code == 200:
                data = res.json().get("data", [])
                print(f"    -> Extracted {len(data)} items from API.")

                for item in data:
                    ad_id = str(item.get("id"))
                    title = item.get("title", "No Title")
                    price_info = item.get("price", {}).get("value", {})
                    price = price_info.get("raw")
                    loc = item.get("locations_resolved", {}).get("ADMIN_LEVEL_3_name") or "Delhi"

                    if not price or ad_id in seen_ads:
                        continue

                    seen_ads.add(ad_id)
                    new_found = True

                    print(f"    Found: '{title}' | ₹{price}")

                    if price <= max_price:
                        print(f"    🔥 [MATCH FOUND] {title} at ₹{price}")
                        send_alert(title, price, ad_id, loc)
            else:
                print(f"[!] Error reading API response: {res.text[:200]}")

        except Exception as e:
            print(f"[!] Exception querying '{query}': {e}")

    if new_found:
        save_seen(seen_ads)
        print("\n[*] Saved new seen ads to cache.")

if __name__ == "__main__":
    main()
