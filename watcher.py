#!/usr/bin/env python3
"""Doorzo keyword watcher.

Searches Doorzo for each keyword in config.json, remembers which items it
has already seen in seen.json, and sends a push notification through ntfy
for every new item. Uses only the Python standard library.

Usage:
  python3 watcher.py            # normal check
  python3 watcher.py --dry-run  # print what would be sent, send nothing
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SEEN_PATH = os.path.join(HERE, "seen.json")

SEARCH_URL = "https://www.doorzo.com/sigapi"
DOORZO = "https://www.doorzo.com/en"
NTFY_URL = "https://ntfy.sh/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128 Safari/537.36"
)
# Keep at most this many item IDs per keyword. A search across every
# marketplace returns about 100 items, so this holds several checks' worth.
SEEN_LIMIT = 1000

# Doorzo numbers each marketplace with the item's "Type". For each one: the
# label shown in the alert, and how Doorzo builds that item's page URL
# (copied from the link builder in Doorzo's own search page).
MARKETPLACES = {
    1: ("Mercari", lambda i: "/mall/mercari/detail/%s" % i["Url"]),
    2: ("Rakuma", lambda i: "/mall/rakuma/detail/%s" % i["Url"]),
    3: ("Doorzo Market", lambda i: "/mall/market/detail/%s" % i["Url"]),
    4: ("Surugaya", lambda i: "/mall/surugaya/detail/%s" % quote(i["Url"])),
    5: ("PayPay Flea Market", lambda i: "/mall/paypay/detail/%s" % i["Url"]),
    6: ("Rakuten", lambda i: "/mall/rakuten/detail/%s" % quote(i["Url"])),
    7: ("Yahoo Auctions", lambda i: "/mall/yahoo/detail/%s" % quote(i["Url"])),
    8: ("minne", lambda i: "/mall/minne/detail?url=%s&id=%s" % (i["Url"], i.get("Asin", ""))),
    9: ("Amazon", lambda i: "/mall/amazon/detail/%s" % i["Asin"]),
    10: ("Lashinbang", lambda i: "/mall/lashinbang/detail/%s" % i["Asin"]),
    11: ("Bunjang", lambda i: "/mall/bunjang/detail/%s" % i["Asin"]),
    12: ("Snkrdunk", lambda i: "/mall/snkrdunk/detail/%s" % quote(i["Asin"])),
}


def quote(value):
    return urllib.parse.quote(value, safe="")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def search(watch):
    """Return the items Doorzo lists for one watch entry."""
    params = {
        "n": "Sig.Front.SubSite.AppGlobal.MixSearch",
        "from": "INTERNATIONAL",
        "isNew": "15",
        "language": "en",
        "keyword": watch["keyword"],
        "filter": "lashinbang",
        "onlyInStock": "1",
        # "created_desc" is Doorzo's "newest first" sort. Without it, Mercari
        # results come back in relevance order and new listings are missed.
        "orderBy": watch.get("sort", "created_desc"),
    }
    # No "sites" means every marketplace Doorzo searches.
    if watch.get("sites"):
        params["website"] = ",".join(watch["sites"])
    # Doorzo category code, for example "1172" for Sports & Outdoors > Fishing.
    if watch.get("category"):
        params["category"] = str(watch["category"])
    if watch.get("max_price"):
        params["priceMax"] = str(watch["max_price"])
    if watch.get("min_price"):
        params["priceMin"] = str(watch["min_price"])

    req = urllib.request.Request(
        SEARCH_URL + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("code") != 200:
        raise RuntimeError("Doorzo returned code %s: %s" % (body.get("code"), body.get("msg")))
    return (body.get("data") or {}).get("items") or []


def item_id(item):
    """A stable ID per item. Some marketplaces give no Asin, but every item has a Url."""
    key = item.get("Asin") or item.get("Url")
    return "%s:%s" % (item.get("Type"), key) if key else None


def marketplace_label(item):
    if item.get("ItemType") == "ITEM_TYPE_BEYOND":
        return "Mercari Shops"  # business sellers on Mercari
    return MARKETPLACES.get(item.get("Type"), ("Doorzo", None))[0]


def item_link(item):
    build = MARKETPLACES.get(item.get("Type"), (None, None))[1]
    try:
        if build:
            return DOORZO + build(item)
    except KeyError:
        pass
    # Unknown marketplace or missing field: fall back to a Doorzo search for the title.
    return DOORZO + "/search?" + urllib.parse.urlencode({"keywords": item.get("Name", "")})


def price_text(item):
    if item.get("JPYPriceStr"):
        return "¥" + item["JPYPriceStr"]
    # Auctions have a current bid and sometimes a buy-now price instead.
    parts = []
    if item.get("BidJPYPriceStr"):
        parts.append("bid ¥" + item["BidJPYPriceStr"])
    if item.get("BuyNowPriceStr") and item.get("BuyNowPriceStr") != item.get("BidJPYPriceStr"):
        parts.append("buy now ¥" + item["BuyNowPriceStr"])
    return ", ".join(parts) or "price not shown"


def word_in_title(word, title):
    """Case-insensitive match. A number such as "200" must stand alone, so
    it matches "ライアン200" and "Ryan 200" but not "20000" or "2000"."""
    word = word.lower()
    if word.isdigit():
        return re.search(r"(?<!\d)%s(?!\d)" % word, title) is not None
    return word in title


def title_matches(watch, item):
    """Doorzo also returns loosely related items. "title_must_include" keeps an
    item only if its title has at least one of those words; "title_must_also_include"
    adds a second group that must also match (for example a size)."""
    title = item.get("Name", "").lower()
    for group in ("title_must_include", "title_must_also_include"):
        words = watch.get(group)
        if words and not any(word_in_title(w, title) for w in words):
            return False
    return True


def current_price(item):
    """Price in yen as listed now: the fixed price, or an auction's current bid."""
    for field in ("JPYPrice", "BidJPYPrice", "BuyNowPrice"):
        try:
            if item.get(field):
                return int(item[field])
        except (TypeError, ValueError):
            pass
    return None


def price_matches(watch, item):
    """Doorzo ignores its own price filter for some marketplaces, so check here too."""
    price = current_price(item)
    if price is None:
        return True  # price not shown: alert rather than miss it
    if watch.get("max_price") and price > watch["max_price"]:
        return False
    if watch.get("min_price") and price < watch["min_price"]:
        return False
    return True


def keyword_label(watch):
    """The keyword as shown to a person: "note" is a reminder only, never searched."""
    if watch.get("note"):
        return "%s (%s)" % (watch["keyword"], watch["note"])
    return watch["keyword"]


def notify(topic, watch, item, dry_run):
    message = {
        "topic": topic,
        "title": item.get("Name", "New item")[:120],
        "message": "%s · %s · keyword: %s" % (price_text(item), marketplace_label(item), keyword_label(watch)),
        "click": item_link(item),
        "tags": ["jp"],
    }
    if item.get("ImageUrl"):
        message["attach"] = item["ImageUrl"]
    if dry_run:
        print("  would send:", json.dumps(message, ensure_ascii=False))
        return
    req = urllib.request.Request(
        NTFY_URL,
        data=json.dumps(message).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=30).read()


def check(watch, seen_ids, topic, dry_run):
    """Search one keyword, notify for new items, return the updated seen list."""
    # Items outside the price range are not remembered, so a listing whose
    # price is later cut into the range still alerts once.
    items = [i for i in search(watch) if item_id(i) and title_matches(watch, i) and price_matches(watch, i)]
    ids = [item_id(i) for i in items]
    if seen_ids is None:
        # First run for this keyword: remember what is listed now, send nothing.
        # An empty list is not a first run: it means nothing matched last time.
        print("  first run: recorded %d existing items, no alerts" % len(ids))
        return ids[:SEEN_LIMIT]

    seen = set(seen_ids)
    new_items = [i for i in items if item_id(i) not in seen]
    # Shops often list one item on several marketplaces with the same title and
    # price. Alert once per title and price; the copies are still remembered.
    unique, copies = [], set()
    for item in new_items:
        fingerprint = (item.get("Name", "").strip(), current_price(item))
        if fingerprint not in copies:
            copies.add(fingerprint)
            unique.append(item)
    limit = watch.get("max_alerts_per_check", 3)
    for item in unique[:limit]:
        notify(topic, watch, item, dry_run)
    print("  %d new, %d alerted" % (len(new_items), min(len(unique), limit)))
    # Current results first, so the most recent IDs survive the trim.
    current = set(ids)
    return (ids + [i for i in seen_ids if i not in current])[:SEEN_LIMIT]


def main():
    dry_run = "--dry-run" in sys.argv
    config = load_json(CONFIG_PATH, None)
    if not config:
        sys.exit("config.json is missing")
    # The topic is a secret: in GitHub Actions it comes from the NTFY_TOPIC
    # repository secret; locally it can also sit in config.json.
    topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic")
    if not topic:
        sys.exit("no ntfy topic: set NTFY_TOPIC or ntfy_topic in config.json")
    seen = load_json(SEEN_PATH, {})
    failures = 0
    for watch in config["watches"]:
        # Changing what a keyword searches starts it fresh (one silent first run).
        key = json.dumps([watch["keyword"], watch.get("sites"), watch.get("category"),
                          watch.get("max_price"), watch.get("min_price")], ensure_ascii=False)
        print("checking %s" % keyword_label(watch))
        try:
            seen[key] = check(watch, seen.get(key), topic, dry_run)
        except Exception as e:  # one broken keyword must not stop the others
            failures += 1
            print("  failed: %s" % e)
        time.sleep(1)  # be gentle with Doorzo between keywords
    if not dry_run:
        save_json(SEEN_PATH, seen)
    sys.exit(1 if failures == len(config["watches"]) else 0)


if __name__ == "__main__":
    main()
