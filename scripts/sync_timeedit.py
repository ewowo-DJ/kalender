"""Copy the TimeEdit timetable into the calendar's Firestore database.

Runs daily from GitHub Actions.

The TimeEdit links are pasted in the app ("TimeEdit koppelen") and stored in
Firestore at settings/timeedit. The repository secrets TIMEEDIT_URL and
TIMEEDIT_URL_HELEN are only used as a fallback when no link is set in the app.
FIREBASE_SERVICE_ACCOUNT (the service-account key JSON) is always required.
  FIREBASE_SERVICE_ACCOUNT   the full JSON of a Firebase service-account key

Each class becomes the document items/te-<id>, labelled for Ewoud, Helen, or both
when the same class is in both timetables. Classes that disappear from the
feed are removed (future ones only). Items people added themselves are never touched.
"""
import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse
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


def valid_link(url):
    """Only real TimeEdit .ics links over https are fetched."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "timeedit.net" or host.endswith(".timeedit.net")) and u.path.endswith(".ics")


def clean_title(summary):
    first = summary.split(",")[0].strip()
    first = re.sub(r"^[A-Z0-9\-]+/", "", first)  # drop course-code prefix like "PBA-BM/"
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
    if not sa:
        sys.exit("The FIREBASE_SERVICE_ACCOUNT secret is required")
    firebase_admin.initialize_app(credentials.Certificate(json.loads(sa)))
    db = firestore.client()
    col = db.collection("items")

    # Links set in the app win; the repository secrets are a fallback.
    snap = db.document("settings/timeedit").get()
    in_app = snap.to_dict() if snap.exists else {}
    feeds = []
    for who, secret in (("ewoud", "TIMEEDIT_URL"), ("helen", "TIMEEDIT_URL_HELEN")):
        url = (in_app.get(who) or "").strip() if who in in_app else os.environ.get(secret, "").strip()
        if not url:
            continue
        if not valid_link(url):
            print(f"{who}: link is not a TimeEdit .ics link, skipping it")
            continue
        feeds.append((who, url))
    if not feeds:
        sys.exit("No TimeEdit link is set. Paste one in the app under 'TimeEdit koppelen'.")

    # Fetch every feed first; if any fails, the script stops before changing anything.
    feed = {}
    for who, url in feeds:
        part = parse(fetch(url), who)
        if not part:
            sys.exit(f"The TimeEdit feed for {who} had no classes; changing nothing to be safe.")
        print(f"{who}: {len(part)} classes")
        for doc_id, body in part.items():
            if doc_id in feed and feed[doc_id]["who"] != who:
                feed[doc_id]["who"] = "both"   # same class in both timetables
            else:
                feed[doc_id] = body

    existing = {
        doc.id: doc.to_dict()
        for doc in col.where("source", "==", "timeedit").stream()
    }
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
