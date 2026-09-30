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
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SEEN_PATH = os.path.join(HERE, "seen.json")

SEARCH_URL = "https://www.doorzo.com/sigapi"
ITEM_URL = "https://www.doorzo.com/en/mall/{site}/detail/{url}"
NTFY_URL = "https://ntfy.sh/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128 Safari/537.36"
)
# Keep at most this many item IDs per keyword, so seen.json stays small.
SEEN_LIMIT = 500

# Doorzo reports the source marketplace as ItemType. Each maps to the site
# name Doorzo uses in its item page URLs, and a label for the notification.
# ITEM_TYPE_BEYOND is Mercari Shops (business sellers on Mercari).
SITE_BY_ITEM_TYPE = {
    "ITEM_TYPE_MERCARI": ("mercari", "Mercari"),
    "ITEM_TYPE_BEYOND": ("mercari", "Mercari Shops"),
    "ITEM_TYPE_YAHOO": ("yahoo", "Yahoo Auctions"),
    "ITEM_TYPE_RAKUMA": ("rakuma", "Rakuma"),
    "ITEM_TYPE_PAYPAY": ("paypay", "PayPay Flea Market"),
}


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
    """Return the newest items for one watch entry, newest first."""
    params = {
        "n": "Sig.Front.SubSite.AppGlobal.MixSearch",
        "from": "INTERNATIONAL",
        "isNew": "15",
        "language": "en",
        "keyword": watch["keyword"],
        "filter": "lashinbang",
        "onlyInStock": "1",
        # "created_desc" is Doorzo's "newest first" sort for Mercari. Without
        # it, results come back in relevance order and new listings are missed.
        "orderBy": watch.get("sort", "created_desc"),
    }
    if watch.get("sites"):
        params["website"] = ",".join(watch["sites"])
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


def item_link(item):
    site = SITE_BY_ITEM_TYPE.get(item.get("ItemType"), (None, None))[0]
    if site and item.get("Url") and "/" not in item["Url"]:
        return ITEM_URL.format(site=site, url=item["Url"])
    # Unknown marketplace: fall back to a Doorzo search for the item title.
    return "https://www.doorzo.com/en/search?" + urllib.parse.urlencode({"keywords": item.get("Name", "")})


def keyword_label(watch):
    """The keyword as shown to a person: "note" is a reminder only, never searched."""
    if watch.get("note"):
        return "%s (%s)" % (watch["keyword"], watch["note"])
    return watch["keyword"]


def notify(topic, watch, item, dry_run):
    label = SITE_BY_ITEM_TYPE.get(item.get("ItemType"), (None, "Doorzo"))[1]
    message = {
        "topic": topic,
        "title": item.get("Name", "New item")[:120],
        "message": "¥%s · %s · keyword: %s" % (item.get("JPYPriceStr", "?"), label, keyword_label(watch)),
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
    items = search(watch)
    ids = [i["Asin"] for i in items if i.get("Asin")]
    if not seen_ids:
        # First run for this keyword: remember what is listed now, send nothing.
        print("  first run: recorded %d existing items, no alerts" % len(ids))
        return ids[:SEEN_LIMIT]

    seen = set(seen_ids)
    new_items = [i for i in items if i.get("Asin") and i["Asin"] not in seen]
    limit = watch.get("max_alerts_per_check", 3)
    for item in new_items[:limit]:
        notify(topic, watch, item, dry_run)
    print("  %d new, %d alerted" % (len(new_items), min(len(new_items), limit)))
    # Newest first, so the most recent IDs survive the trim.
    return (ids + [i for i in seen_ids if i not in set(ids)])[:SEEN_LIMIT]


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
        key = json.dumps([watch["keyword"], watch.get("sites"), watch.get("max_price"), watch.get("min_price")],
                         ensure_ascii=False)
        print("checking %s" % keyword_label(watch))
        try:
            seen[key] = check(watch, seen.get(key, []), topic, dry_run)
        except Exception as e:  # one broken keyword must not stop the others
            failures += 1
            print("  failed: %s" % e)
        time.sleep(1)  # be gentle with Doorzo between keywords
    if not dry_run:
        save_json(SEEN_PATH, seen)
    sys.exit(1 if failures == len(config["watches"]) else 0)


if __name__ == "__main__":
    main()
