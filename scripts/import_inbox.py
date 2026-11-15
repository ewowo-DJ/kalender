"""Put what Ewoud sends to Claude into the calendar.

Claude writes encrypted files to inbox/ in this repository (the repository is
public, so the contents are encrypted with the INBOX_KEY secret). On every push
to inbox/, .github/workflows/inbox.yml runs this script, which decrypts the files
and writes the items to Firestore. What was imported is remembered in
settings/inbox, so deleting an item in the app never brings it back.

File format after decryption (JSON):
  {"items": [ {"id": "cl-…", "type": "event"|"task"|"birthday", "who": "ewoud"|"helen"|"both",
               "title": "…", "date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM",
               "location": "…", "notes": "…", "color": "#rrggbb", "repeat": "weekly", "until": "YYYY-MM-DD"} ],
   "delete": ["cl-…"]}
"""
import glob
import json
import os
import re
import subprocess
import sys

import firebase_admin
from firebase_admin import credentials, firestore

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
ID = re.compile(r"^cl-[A-Za-z0-9_-]{4,60}$")


def fernet(key):
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "cryptography"])
        from cryptography.fernet import Fernet
    return Fernet(key.encode())


def clean(it):
    """Only known fields with sane values reach the calendar."""
    if not ID.match(str(it.get("id", ""))):
        raise ValueError("bad id")
    typ = it.get("type", "event")
    if typ not in ("event", "task", "birthday"):
        raise ValueError("bad type")
    who = it.get("who", "both")
    if who not in ("ewoud", "helen", "both"):
        raise ValueError("bad who")
    title = str(it.get("title", "")).strip()[:200]
    date = str(it.get("date", ""))
    if not title or not DATE.match(date):
        raise ValueError("title and date are required")
    body = {"type": typ, "who": who, "title": title, "date": date, "addedBy": "Claude",
            "notes": str(it.get("notes", "")).strip()[:500]}
    if typ == "birthday":
        body["yearKnown"] = bool(it.get("yearKnown", True))
        return body
    for k in ("start", "end"):
        v = str(it.get(k, "") or "")
        if v and not TIME.match(v):
            raise ValueError(f"bad {k}")
        if k == "start" or typ == "event":
            body[k] = v
    if typ == "event":
        body["location"] = str(it.get("location", "")).strip()[:200]
    color = str(it.get("color", "") or "")
    if re.match(r"^#[0-9a-fA-F]{6}$", color) or color in ("blue", "green", "rose", "violet", "teal"):
        body["color"] = color
    elif typ == "event":
        body["color"] = "blue"
    if typ == "task":
        body["done"] = False
    if it.get("repeat") == "weekly":
        body["repeat"] = "weekly"
        until = str(it.get("until", "") or "")
        if until and DATE.match(until):
            body["until"] = until
    return body


def main():
    key = os.environ.get("INBOX_KEY", "").strip()
    sa = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    if not key or not sa:
        sys.exit("The INBOX_KEY and FIREBASE_SERVICE_ACCOUNT secrets are required")
    f = fernet(key)
    firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
    db = firestore.client()
    col = db.collection("items")
    state_ref = db.document("settings/inbox")
    snap = state_ref.get()
    done = set((snap.to_dict() or {}).get("done", [])) if snap.exists else set()

    added = deleted = skipped = 0
    for path in sorted(glob.glob("inbox/*.enc")):
        name = os.path.basename(path)
        if name in done:
            continue
        try:
            data = json.loads(f.decrypt(open(path, "rb").read().strip()))
        except Exception as e:
            print(f"{name}: could not be read ({e.__class__.__name__}), skipped")
            skipped += 1
            continue
        for it in data.get("items", []):
            try:
                body = clean(it)
            except ValueError as e:
                print(f"{name}: one item skipped ({e})")
                skipped += 1
                continue
            col.document(it["id"]).set(body)
            added += 1
        for doc_id in data.get("delete", []):
            if ID.match(str(doc_id)):
                col.document(doc_id).delete()
                deleted += 1
        done.add(name)
        state_ref.set({"done": sorted(done)}, merge=True)
    print(f"Inbox: {added} added, {deleted} removed, {skipped} skipped.")


if __name__ == "__main__":
    main()
