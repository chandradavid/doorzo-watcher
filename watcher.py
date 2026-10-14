#!/usr/bin/env python3
"""Doorzo keyword watcher.

Searches Doorzo for each keyword in config.json, remembers which items it
has already seen in seen.json, and sends a push notification through ntfy
for every new item. Uses only the Python standard library.

Usage:
  python3 watcher.py            # normal check
  python3 watcher.py --dry-run  # print what would be sent, send nothing
"""
import datetime
import html
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
        "tags": ["fishing_pole_and_fish"],  # shown as 🎣 in front of the title
    }
    if item.get("ImageUrl"):
        message["attach"] = item["ImageUrl"]
    send(message, dry_run)


def send(message, dry_run):
    """Publish one notification through ntfy."""
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

# ---------------------------------------------------------------------------
# Doorzo inbox. Doorzo's login needs a reCAPTCHA, so the watcher never signs in
# itself: it reuses a session the operator signed in to in a browser, stored as
# the DOORZO_COOKIE secret. It asks for the same unread counts the website
# shows on the profile icon, and alerts when one goes up.
# ---------------------------------------------------------------------------
INBOX_STATE_KEY = "__inbox__"
# Signed-in requests are rarer than searches, to look like a person checking
# now and then. Slightly under 1 hour, because runs start every 2 minutes.
INBOX_INTERVAL_SECONDS = 59 * 60 + 30
INBOX_COUNTS = {
    # count name: (what to call it in the alert, page that tapping it opens)
    "message": ("message", DOORZO + "/personal/message"),
    "notice": ("notice", DOORZO + "/personal/message"),
    "work": ("support reply", DOORZO + "/workOrder"),
}


def inbox_counts(cookie):
    """Unread counts for the signed-in account, or None when the session has expired."""
    params = {"n": "Sig.Front.Front.GlobalHomeInfo", "from": "INTERNATIONAL", "isNew": "15", "language": "en"}
    req = urllib.request.Request(
        SEARCH_URL + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en", "Cookie": cookie},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("code") != 200:
        raise RuntimeError("Doorzo returned code %s" % body.get("code"))
    data = body.get("data") or {}
    user = data.get("user")
    if not user:
        return None  # Doorzo treats the request as signed out
    by_type = user.get("messageCount") or {}
    return {
        "message": int(by_type.get("message") or 0),
        "notice": int(by_type.get("notice") or 0),
        "work": int(data.get("workUnreadNum") or 0),
    }


def inbox_messages(cookie):
    """The first page of "My Messages", newest first. This only reads; Doorzo
    marks a message read through a separate call the watcher never makes."""
    params = {"n": "Sig.Front.User.AppUser.GetMessageWeb", "from": "INTERNATIONAL", "isNew": "15",
              "language": "en", "ver": "1"}
    req = urllib.request.Request(
        SEARCH_URL + "?" + urllib.parse.urlencode(params),
        data=json.dumps({"page": 1}).encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en", "Cookie": cookie,
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("code") != 200:
        raise RuntimeError("Doorzo returned code %s" % body.get("code"))
    return (body.get("data") or {}).get("Data") or []


def plain_text(content, limit=300):
    """Message bodies are HTML; turn them into short plain text for an alert."""
    text = re.sub(r"<br\s*/?>|</p>", "\n", content or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def message_alert(topic, msg, prefix=""):
    alert = {"topic": topic,
             "title": (prefix + (msg.get("title") or "New Doorzo message"))[:120],
             "message": plain_text(msg.get("content")) or "Tap to open your Doorzo inbox.",
             "click": DOORZO + "/personal/message", "tags": ["envelope_with_arrow"]}  # 📩
    if msg.get("imageList"):
        alert["attach"] = msg["imageList"][0]
    return alert


def inbox_test(topic, dry_run, count=3):
    """Send the latest messages once, so the operator can see inbox alerts work."""
    cookie = os.environ.get("DOORZO_COOKIE", "").strip()
    if not cookie:
        print("inbox test: DOORZO_COOKIE is not set")
        return
    messages = inbox_messages(cookie)[:count]
    for msg in reversed(messages):  # oldest first, so the newest ends up on top
        send(message_alert(topic, msg, prefix="TEST: "), dry_run)
    print("inbox test: sent %d" % len(messages))


def check_inbox(state, topic, dry_run, now):
    """Alert on new unread Doorzo messages. Returns the updated state.
    Logs only whether the session works, because the repository's logs are public."""
    state = dict(state or {})
    cookie = os.environ.get("DOORZO_COOKIE", "").strip()
    if not cookie:
        return state  # inbox alerts not set up
    if now - state.get("last_check", 0) < INBOX_INTERVAL_SECONDS:
        return state
    state["last_check"] = now
    counts = inbox_counts(cookie)
    if counts is None:
        print("inbox: signed out")
        if not state.get("expired_alerted"):
            send({"topic": topic, "title": "Doorzo login expired",
                  "message": "Inbox alerts are paused. Sign in to doorzo.com again and update the DOORZO_COOKIE secret.",
                  "click": "https://github.com/chandradavid/doorzo-watcher/settings/secrets/actions",
                  "tags": ["warning"]}, dry_run)
            state["expired_alerted"] = True
        return state
    print("inbox: signed in")
    state["expired_alerted"] = False
    before = state.get("counts")
    if before is not None:  # the first check only records the counts
        for name, (label, page) in INBOX_COUNTS.items():
            added = counts[name] - before.get(name, 0)
            if added <= 0:
                continue
            if name == "message":
                # Show the new messages themselves (up to 3), falling back to a count.
                try:
                    unread = [m for m in inbox_messages(cookie) if str(m.get("hasRead")) != "1"][:min(added, 3)]
                except Exception:
                    unread = []
                if unread:
                    for msg in reversed(unread):
                        send(message_alert(topic, msg), dry_run)
                    continue
            send({"topic": topic, "title": "Doorzo: %d new %s%s" % (added, label, "" if added == 1 else "s"),
                  "message": "Tap to open your Doorzo inbox.", "click": page, "tags": ["envelope_with_arrow"]}, dry_run)
    state["counts"] = counts
    return state


# ---------------------------------------------------------------------------
# Favourite auction reminders (optional). Reads the Yahoo Auctions items the
# operator saved as favourites on Doorzo, with the same DOORZO_COOKIE session
# as the inbox alerts.
# ---------------------------------------------------------------------------
AUCTION_PAGE_LIMIT = 5  # Doorzo returns 10 favourites per page


def signed_in_list(cookie, name, filters):
    """Every page of one of Doorzo's signed-in Yahoo Auctions lists (10 items a page)."""
    items = []
    for page in range(1, AUCTION_PAGE_LIMIT + 1):
        params = {"n": name, "from": "INTERNATIONAL", "isNew": "15", "language": "en"}
        req = urllib.request.Request(
            SEARCH_URL + "?" + urllib.parse.urlencode(params),
            data=json.dumps(dict(filters, page=page), ensure_ascii=False).encode("utf-8"),
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en", "Cookie": cookie,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
        if body.get("code") != 200:
            raise RuntimeError("Doorzo returned code %s" % body.get("code"))
        data = body.get("data") or {}
        batch = data.get("Data") or []
        items.extend(batch)
        if not batch or len(items) >= int(data.get("TotalItem") or 0):
            break
        time.sleep(1)
    return items


def favourite_auctions(cookie):
    """The signed-in account's favourite Yahoo auctions that have not ended yet."""
    # "进行中" (in progress) is the filter Doorzo's own favourites page uses.
    return signed_in_list(cookie, "Sig.Front.User.AppYahooAcOrder.CollectionList", {"status": "进行中"})


def auction_link(item):
    if item.get("DetailUrl"):
        return DOORZO + "/mall/yahoo/detail/" + quote(item["DetailUrl"])
    return DOORZO + "/personal/collect"


def auction_price_text(item):
    price = item.get("Price") or {}
    parts = []
    if price.get("BidPrice"):
        parts.append("bid ¥{:,}".format(int(float(price["BidPrice"]))))
    if price.get("BuyNowPrice"):
        parts.append("buy now ¥{:,}".format(int(float(price["BuyNowPrice"]))))
    return ", ".join(parts) or "price not shown"


AUCTION_STATE_KEY = "__auctions__"
# Each favourite gets at most two reminders: one when less than a day is left,
# and one when less than an hour is left.
AUCTION_REMINDERS = (("hour", 60 * 60), ("day", 24 * 60 * 60))
JAPAN = datetime.timezone(datetime.timedelta(hours=9))  # Japan has no daylight saving
try:
    from zoneinfo import ZoneInfo
    SHOWN_ZONE, SHOWN_ZONE_NAME = ZoneInfo("Australia/Sydney"), "Sydney"
except Exception:  # no time zone data: show Japan time instead
    SHOWN_ZONE, SHOWN_ZONE_NAME = JAPAN, "Japan"
COUNTDOWN_UNITS = (
    (r"(\d+)\s*(?:d|days?|日)", 24 * 60 * 60),
    (r"(\d+)\s*(?:h|hrs?|hours?|小时|時間)", 60 * 60),
    (r"(\d+)\s*(?:m|mins?|minutes?|分钟|分)(?![a-z])", 60),
    (r"(\d+)\s*(?:s|secs?|seconds?|秒)(?![a-z])", 1),
)


def auction_end(text, now):
    """When an auction ends, as a Unix time, from Doorzo's end-time text: a
    countdown such as "10h 33m" or a Japan-time date. None if unreadable."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(tzinfo=JAPAN).timestamp()
        except ValueError:
            pass
    seconds, found = 0, False
    for pattern, size in COUNTDOWN_UNITS:
        match = re.search(pattern, text, flags=re.I)
        if match:
            seconds, found = seconds + int(match.group(1)) * size, True
    return now + seconds if found else None


def time_left_text(seconds):
    minutes = max(int(seconds // 60), 1)
    hours, minutes = divmod(minutes, 60)
    return "%dh %02dm" % (hours, minutes) if hours else "%d min" % minutes


def check_auctions(state, topic, dry_run, now, fetch):
    """Remind about favourite auctions 1 day and 1 hour before they end.
    Favourites are fetched only when fetch is true (the hourly signed-in visit);
    every other run compares the saved end times with the clock, with no request.
    Logs only counts, because the repository's logs are public."""
    state = dict(state or {})
    items = dict(state.get("items") or {})
    cookie = os.environ.get("DOORZO_COOKIE", "").strip()
    if fetch and cookie:
        fresh, unreadable = {}, 0
        for item in favourite_auctions(cookie):
            end = auction_end(item.get("BidDeadlineStr"), now)
            key = str(item.get("Asin") or item.get("DetailUrl") or "")
            if not key:
                continue
            if end is None:
                unreadable += 1
                continue
            sent = (items.get(key) or {}).get("sent") or []
            fresh[key] = {"end": end, "sent": sent, "name": item.get("Name") or "Favourite auction",
                          "price": auction_price_text(item), "link": auction_link(item),
                          "image": item.get("ImageUrl")}
        items = fresh  # an auction removed from favourites gets no more reminders
        print("auctions: %d favourites%s" % (len(items), ", %d end times unreadable" % unreadable if unreadable else ""))
    for key, item in list(items.items()):
        left = item["end"] - now
        if left <= 0:
            del items[key]
            continue
        due = [name for name, limit in AUCTION_REMINDERS if left <= limit and name not in item["sent"]]
        if not due:
            continue
        ends_at = datetime.datetime.fromtimestamp(item["end"], SHOWN_ZONE).strftime("%a %H:%M")
        alert = {"topic": topic, "title": ("Ends in %s: %s" % (time_left_text(left), item["name"]))[:120],
                 "message": "%s · ends %s %s time. Tap to bid." % (item["price"], ends_at, SHOWN_ZONE_NAME),
                 "click": item["link"], "tags": ["alarm_clock"]}  # ⏰
        if item.get("image"):
            alert["attach"] = item["image"]
        send(alert, dry_run)
        # The 1-hour reminder also covers the 1-day one, so a late favourite gets one alert.
        item["sent"] = sorted(set(item["sent"]) | {name for name, limit in AUCTION_REMINDERS if left <= limit})
    state["items"] = items
    return state


# ---------------------------------------------------------------------------
# Outbid alerts (optional). Reads the operator's active Yahoo Auctions bids
# with the same DOORZO_COOKIE session, and alerts when a bid that was leading
# has been outbid.
# ---------------------------------------------------------------------------
BIDS_STATE_KEY = "__bids__"
# Doorzo's "Status" values, the same ones its own bidding page compares against.
BID_LEADING = "竞拍中"     # bidding, and the operator's bid is the highest
BID_OUTBID = "价格被超过"  # someone has bid more
# Bids are read in the hourly signed-in visit. When a leading bid ends within
# an hour, they are also read every 10 minutes, with a short visit of their own.
BIDS_ENDGAME_SECONDS = 60 * 60
BIDS_ENDGAME_INTERVAL_SECONDS = 10 * 60 - 30


def active_bids(cookie):
    """The signed-in account's Yahoo auctions it is bidding on, leading or not."""
    return signed_in_list(cookie, "Sig.Front.User.AppYahooAcOrder.List", {"status": BID_LEADING, "sort": ""})


def bid_link(item):
    if item.get("Url"):
        return DOORZO + "/mall/yahoo/detail/" + quote(item["Url"])
    return DOORZO + "/personal/yahoo"


def bids_due(state, now):
    """True when a leading bid ends within the hour and the last read was 10 minutes ago."""
    state = state or {}
    if now - state.get("last_check", 0) < BIDS_ENDGAME_INTERVAL_SECONDS:
        return False
    return any(b["status"] == BID_LEADING and 0 < b["end"] - now <= BIDS_ENDGAME_SECONDS
               for b in (state.get("items") or {}).values())


def check_bids(state, topic, dry_run, now):
    """Read the active bids once and alert for each one that went from leading to
    outbid since the last read. The first read only records. Logs only counts."""
    state = dict(state or {})
    cookie = os.environ.get("DOORZO_COOKIE", "").strip()
    if not cookie:
        return state
    before = state.get("items")
    state["last_check"] = now
    items = {}
    for item in active_bids(cookie):
        key = str(item.get("Asin") or item.get("Url") or "")
        if not key:
            continue
        end = auction_end(item.get("BidDeadlineStr"), now)
        if end is None:  # keep the last known end time, so the 10-minute checks still work
            end = ((before or {}).get(key) or {}).get("end", 0)
        items[key] = {"status": item.get("Status") or "", "end": end}
        was = ((before or {}).get(key) or {}).get("status")
        if before is not None and was == BID_LEADING and items[key]["status"] == BID_OUTBID:
            left = end - now
            when = ("%s left · ends %s %s time" % (
                time_left_text(left), datetime.datetime.fromtimestamp(end, SHOWN_ZONE).strftime("%a %H:%M"),
                SHOWN_ZONE_NAME)) if left > 0 else "time left not shown"
            alert = {"topic": topic, "title": ("Outbid: %s" % (item.get("Name") or "Yahoo auction"))[:120],
                     "message": "Your bid ¥%s · %s. Tap to bid again." % (item.get("JPYPriceStr") or "?", when),
                     "click": bid_link(item), "tags": ["warning"]}  # ⚠️
            if item.get("ImageUrl"):
                alert["attach"] = item["ImageUrl"]
            send(alert, dry_run)
    state["items"] = items  # ended or won bids drop out of Doorzo's list, and out of here
    outbid = sum(1 for b in items.values() if b["status"] == BID_OUTBID)
    print("bids: %d active, %d outbid" % (len(items), outbid))
    return state


def auction_test(topic, dry_run, count=3):
    """Send a few favourite auctions once, with Doorzo's raw end-time text and
    field names, so the operator sees the channel work and the format can be read."""
    cookie = os.environ.get("DOORZO_COOKIE", "").strip()
    if not cookie:
        print("auction test: DOORZO_COOKIE is not set")
        return
    items = favourite_auctions(cookie)
    for item in items[:count]:
        alert = {"topic": topic, "title": ("TEST: " + (item.get("Name") or "Favourite auction"))[:120],
                 "message": "%s\nend time from Doorzo: %s\nfields: %s" % (
                     auction_price_text(item), item.get("BidDeadlineStr"), ", ".join(sorted(item))),
                 "click": auction_link(item), "tags": ["alarm_clock"]}  # ⏰
        if item.get("ImageUrl"):
            alert["attach"] = item["ImageUrl"]
        send(alert, dry_run)
    if not items:
        send({"topic": topic, "title": "TEST: no running favourite auctions",
              "message": "Doorzo returned no favourite Yahoo auctions that are still running.",
              "click": DOORZO + "/personal/collect", "tags": ["alarm_clock"]}, dry_run)
    # Only a count: the repository's logs are public.
    print("auction test: found %d, sent %d" % (len(items), min(len(items), count) or 1))
    bids = active_bids(cookie)
    for item in bids[:count]:
        alert = {"topic": topic, "title": ("TEST bid: " + (item.get("Name") or "Yahoo auction"))[:120],
                 "message": "status from Doorzo: %s (%s)\nyour bid ¥%s · end time from Doorzo: %s" % (
                     item.get("Status"), item.get("SubStatusStr"), item.get("JPYPriceStr"),
                     item.get("BidDeadlineStr")),
                 "click": bid_link(item), "tags": ["warning"]}
        if item.get("ImageUrl"):
            alert["attach"] = item["ImageUrl"]
        send(alert, dry_run)
    print("auction test: %d active bids" % len(bids))


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
    # Inbox alerts go to their own topic, so the ntfy app shows them as a
    # separate subscription with its own sound and mute settings.
    inbox_topic = os.environ.get("NTFY_INBOX_TOPIC") or topic + "-inbox"
    if os.environ.get("INBOX_TEST") == "true":
        try:
            inbox_test(inbox_topic, dry_run)
        except Exception as e:
            print("inbox test: failed (%s)" % type(e).__name__)
    # Favourite auction reminders also get their own topic.
    auction_topic = os.environ.get("NTFY_AUCTION_TOPIC") or topic + "-auction"
    if os.environ.get("AUCTION_TEST") == "true":
        try:
            auction_test(auction_topic, dry_run)
        except Exception as e:
            print("auction test: failed (%s)" % type(e).__name__)
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
    now = time.time()
    last_visit = (seen.get(INBOX_STATE_KEY) or {}).get("last_check", 0)
    try:
        seen[INBOX_STATE_KEY] = check_inbox(seen.get(INBOX_STATE_KEY), inbox_topic, dry_run, now)
    except Exception as e:  # the inbox must never stop item alerts
        print("inbox: check failed (%s)" % type(e).__name__)
    # Favourites are fetched in the same hourly signed-in visit as the inbox,
    # so Doorzo sees one visit an hour, not two.
    visited = (seen.get(INBOX_STATE_KEY) or {}).get("last_check", 0) != last_visit
    try:
        seen[AUCTION_STATE_KEY] = check_auctions(seen.get(AUCTION_STATE_KEY), auction_topic, dry_run, now, visited)
    except Exception as e:  # reminders must never stop item alerts
        print("auctions: check failed (%s)" % type(e).__name__)
    if visited or bids_due(seen.get(BIDS_STATE_KEY), now):
        try:
            seen[BIDS_STATE_KEY] = check_bids(seen.get(BIDS_STATE_KEY), auction_topic, dry_run, now)
        except Exception as e:  # outbid alerts must never stop item alerts
            print("bids: check failed (%s)" % type(e).__name__)
    if not dry_run:
        save_json(SEEN_PATH, seen)
    sys.exit(1 if failures == len(config["watches"]) else 0)


if __name__ == "__main__":
    main()
