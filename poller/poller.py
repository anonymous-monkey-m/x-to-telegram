#!/usr/bin/env python3
"""Poll RSSHub for each X account and forward new posts to Telegram chats.
Stdlib only. State = last seen item id per account, stored as JSON.
On first run for an account it seeds state WITHOUT posting.
"""
import json, os, sys, time, urllib.request, urllib.parse
import xml.etree.ElementTree as ET

BASE = os.environ.get("RSSHUB_BASE", "http://rsshub:1200").rstrip("/")
KEY = os.environ.get("RSSHUB_ACCESS_KEY", "")
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHATS = [c.strip() for c in os.environ["TELEGRAM_CHAT_IDS"].split(",") if c.strip()]
ACCOUNTS = [a.strip().lstrip("@") for a in os.environ["X_ACCOUNTS"].split(",") if a.strip()]
POLL = int(os.environ.get("POLL_SECONDS", "300"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "x-feeds-poller/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()

def send(chat_id, text):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text,
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception:
        return {}

def save_state(s):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f: json.dump(s, f)
    os.replace(tmp, STATE_FILE)

def items_for(account):
    q = f"?key={KEY}" if KEY else ""
    url = f"{BASE}/twitter/user/{urllib.parse.quote(account)}{q}"
    root = ET.fromstring(fetch(url))
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        title = (it.findtext("title") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        out.append({"id": guid, "link": link, "title": title})
    return out

def poll_once(state):
    for acct in ACCOUNTS:
        try:
            items = items_for(acct)
        except Exception as e:
            print(f"[{acct}] fetch failed: {e}", flush=True); continue
        if not items: continue
        seen = state.get(acct)
        if seen is None:
            state[acct] = items[0]["id"]; save_state(state)
            print(f"[{acct}] seeded at {items[0]['id']} (no flood)", flush=True); continue
        new = []
        for it in items:
            if it["id"] == seen: break
            new.append(it)
        for it in reversed(new):
            text = f"@{acct}\n{it['title']}\n{it['link']}".strip()
            for chat in CHATS:
                try: send(chat, text[:4096])
                except Exception as e: print(f"[{acct}] send to {chat} failed: {e}", flush=True)
            print(f"[{acct}] posted {it['id']}", flush=True)
        if new:
            state[acct] = items[0]["id"]; save_state(state)

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
