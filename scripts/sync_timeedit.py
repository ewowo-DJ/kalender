"""Copy the TimeEdit timetables into the calendar's Firestore database.

Runs daily from GitHub Actions. Repository secrets:
  TIMEEDIT_URL               Ewoud's TimeEdit .ics subscription link
  TIMEEDIT_URL_HELEN         Helen's TimeEdit .ics subscription link (optional)
  FIREBASE_SERVICE_ACCOUNT   the full JSON of a Firebase service-account key
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore
from icalendar import Calendar

TZ = ZoneInfo("Europe/Brussels")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "calendar-sync"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def clean_title(summary):
    first = summary.split(",")[0].strip()
    first = re.sub(r"^[A-Z0-9\-]+/", "", first)
    return first or summary.strip()


def clean_location(loc):
    loc = (loc or "").strip()
    loc = re.sub(r"\.\s*Campus:\s*", ", Campus ", loc)
    loc = re.sub(r"\s+", " ", loc)
    return loc.strip(" ,.")


def to_local(v):
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=TZ)
        v = v.astimezone(TZ)
        return v.strftime("%Y-%m-%d"), v.strftime("%H:%M")
    if isinstance(v, date):
        return v.isoformat(), ""
    raise ValueError(f"unexpected date value {v!r}")


def parse(raw, who="both"):
    cal = Calendar.from_ical(raw)
    out = {}
    for ev in cal.walk("VEVENT"):
        uid = str(ev.get("UID", "")).strip()
        if not uid:
            continue
        key = re.split(r"[-@]", uid)[0] or re.sub(r"[^A-Za-z0-9]", "", uid)[:40]
        d, start = to_local(ev.decoded("DTSTART"))
        _, end = to_local(ev.decoded("DTEND")) if ev.get("DTEND") else (d, "")
        summary = str(ev.get("SUMMARY", "")).strip()
        notes = ""
        desc = str(ev.get("DESCRIPTION", ""))
        if "niet verplicht" in desc.lower() or "not required" in desc.lower():
            notes = "Optional"
        out["te-" + key] = {
            "type": "event",
            "who": who,
            "title": clean_title(summary),
            "date": d,
            "start": start,
            "end": end,
            "location": clean_location(str(ev.get("LOCATION", ""))),
            "notes": notes,
            "color": "teal",
            "source": "timeedit",
        }
    return out


def main():
    sa = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    feeds = [(w, os.environ.get(k, "").strip()) for w, k in (("ewoud", "TIMEEDIT_URL"), ("helen", "TIMEEDIT_URL_HELEN"))]
    feeds = [(w, u) for w, u in feeds if u]
    if not feeds or not sa:
        sys.exit("FIREBASE_SERVICE_ACCOUNT and at least one TimeEdit link (TIMEEDIT_URL) are required")

    feed = {}
    for who, url in feeds:
        part = parse(fetch(url), who)
        if not part:
            sys.exit(f"The TimeEdit feed for {who} had no classes; changing nothing to be safe.")
        print(f"{who}: {len(part)} classes")
        for doc_id, body in part.items():
            if doc_id in feed and feed[doc_id]["who"] != who:
                feed[doc_id]["who"] = "both"
            else:
                feed[doc_id] = body

    firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
    db = firestore.client()
    col = db.collection("items")

    existing = {doc.id: doc.to_dict() for doc in col.where("source", "==", "timeedit").stream()}
    today = datetime.now(TZ).date().isoformat()

    added = updated = removed = 0
    batch, n = db.batch(), 0

    def commit_if_full():
        nonlocal batch, n
        if n >= 400:
            batch.commit()
            batch, n = db.batch(), 0

    for doc_id, body in feed.items():
        old = existing.get(doc_id)
        if old == body:
            continue
        batch.set(col.document(doc_id), body)
        n += 1
        if old is None:
            added += 1
        else:
            updated += 1
        commit_if_full()

    for doc_id, old in existing.items():
        if doc_id not in feed and old.get("date", "") >= today:
            batch.delete(col.document(doc_id))
            n += 1
            removed += 1
            commit_if_full()

    if n:
        batch.commit()
    print(f"TimeEdit sync: {len(feed)} classes in feed, {added} added, {updated} updated, {removed} removed.")


if __name__ == "__main__":
    main()
