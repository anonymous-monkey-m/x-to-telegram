#!/usr/bin/env python3
"""Poll RSSHub for each X account and forward new posts to Telegram chats.
Stdlib only. State per account = {"last": newest id, "seen": [recent ids]}.
The seen-set (not just the last id) is what guarantees no repeating tweets:
an item whose id is already in seen is never posted again, even if the
feed window shifts or the last-seen tweet drops out of the feed.
On first run for an account it seeds state WITHOUT posting (avoids flooding
the channel with old tweets), then posts only new items after that.
Paid-partnership / ad tweets (disclosed in the tweet text) are skipped:
they are marked seen but never posted.
"""
import json, os, re, sys, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

BASE = os.environ.get("RSSHUB_BASE", "http://rsshub:1200").rstrip("/")
KEY = os.environ.get("RSSHUB_ACCESS_KEY", "")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHATS = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",") if c.strip()]
ACCOUNTS = [a.strip().lstrip("@") for a in os.environ["X_ACCOUNTS"].split(",") if a.strip()]
POLL = int(os.environ.get("POLL_SECONDS", "300"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
MAX_SEEN = 300

# --- Paid-partnership / ad filter -------------------------------------
# Phrases matched as plain substrings (case-insensitive) and hashtags
# matched as whole tokens, so "#ad" does not fire on "#advice".
AD_PHRASES = ["paid partnership", "paid promotion", "sponsored post",
              "promo code", "discount code", "use code",
              "rainbet", "stake.com", "bet365", "draftkings", "fanduel",
              "betmgm", "prizepicks"]
AD_TAGS = ["#ad", "#sponsored", "#paidpartnership", "#paidpromotion"]
_extra = os.environ.get("AD_MARKERS", "")
if _extra:
    for m in _extra.split(","):
        m = m.strip().lower()
        if not m:
            continue
        (AD_TAGS if m.startswith("#") else AD_PHRASES).append(m)

def is_ad(text):
    t = (text or "").lower()
    if any(p in t for p in AD_PHRASES):
        return True
    tokens = re.findall(r"#\w+", t)
    return any(tag in tokens for tag in AD_TAGS)

# --- Telegram ----------------------------------------------------------
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "x-feeds-poller/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def send(chat, text):
    # chat may be "chat_id" or "chat_id:thread_id" (Telegram topic in a forum group)
    parts = str(chat).split(":")
    payload = {
        "chat_id": parts[0], "text": text,
        "disable_web_page_preview": "false",
    }
    if len(parts) > 1 and parts[1]:
        payload["message_thread_id"] = parts[1]
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# --- State -------------------------------------------------------------
def norm_state(raw):
    """Accept both the legacy format {acct: last_id} and the new
    {acct: {"last": id, "seen": [ids]}}. Legacy entries are flagged so the
    next poll re-seeds silently instead of re-posting the whole feed."""
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, dict):
            seen = [s for s in v.get("seen", []) if s]
            last = v.get("last")
            if last and last not in seen:
                seen.append(last)
            out[k] = {"last": last, "seen": seen[-MAX_SEEN:]}
        elif v:
            out[k] = {"last": v, "seen": [v], "legacy": True}
    return out

def load_state():
    try:
        with open(STATE_FILE) as f:
            return norm_state(json.load(f))
    except Exception:
        return {}

def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)

# --- Polling -----------------------------------------------------------
def items_for(account):
    q = f"?key={KEY}" if KEY else ""
    url = f"{BASE}/twitter/user/{urllib.parse.quote(account)}{q}"
    root = ET.fromstring(fetch(url))
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        title = (it.findtext("title") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        desc = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        out.append({"id": guid, "link": link, "title": title, "desc": desc})
    return out  # RSSHub returns newest first

def poll_once(state):
    for acct in ACCOUNTS:
        try:
            items = items_for(acct)
        except Exception as e:
            print(f"[{acct}] fetch failed: {e}", flush=True); continue
        if not items:
            continue
        feed_ids = [it["id"] for it in items]
        entry = state.get(acct)
        if entry is None:
            state[acct] = {"last": feed_ids[0], "seen": feed_ids[-MAX_SEEN:]}
            save_state(state)
            print(f"[{acct}] seeded at {feed_ids[0]} (no flood)", flush=True); continue
        if entry.pop("legacy", False):
            # Migrating from the old last-id-only format: absorb the whole
            # current feed into seen WITHOUT posting, so nothing repeats.
            entry["seen"] = list(dict.fromkeys(entry["seen"] + feed_ids))[-MAX_SEEN:]
            entry["last"] = feed_ids[0]
            save_state(state)
            print(f"[{acct}] state migrated, re-seeded at {feed_ids[0]} (no flood)", flush=True); continue
        seen = set(entry["seen"])
        new = [it for it in items if it["id"] not in seen]
        for it in reversed(new):  # oldest new first
            if is_ad(it["title"]) or is_ad(it.get("desc", "")) or is_ad(it["link"]):
                print(f"[{acct}] skipped paid partnership {it['id']}", flush=True)
                continue
            text = f"@{acct}\n{it['title']}\n{it['link']}".strip()
            for chat in CHATS:
                try:
                    send(chat, text[:4096])
                except Exception as e:
                    print(f"[{acct}] send to {chat} failed: {e}", flush=True)
            print(f"[{acct}] posted {it['id']}", flush=True)
        # Everything in the current feed has now been considered: record all
        # of it as seen (posted, ad-skipped, or already known) so none of it
        # can ever be posted again.
        entry["seen"] = list(dict.fromkeys(entry["seen"] + feed_ids))[-MAX_SEEN:]
        entry["last"] = feed_ids[0]
        save_state(state)

def main():
    state = load_state()
    if os.environ.get("ONCE") == "1":
        poll_once(state); return
    print(f"Polling {ACCOUNTS} -> {CHATS} every {POLL}s", flush=True)
    while True:
        poll_once(state)
        time.sleep(POLL)

if __name__ == "__main__":
    main()
