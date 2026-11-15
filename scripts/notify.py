"""Send the calendar's push notifications (Firebase Cloud Messaging).

Runs every 10 minutes from GitHub Actions (.github/workflows/notify.yml).

Each person turns notifications on in the app (gear menu -> Meldingen). That stores
settings/push-<uid> = {name, person, tokens: {<fcm token>: {...}}, prefs: {...}}.

What gets sent, per person, following their prefs:
  events     reminder `before` minutes before an event (or a task with a time)
  lessons    the same for TimeEdit classes
  morning    a summary of today at `morningAt` (events, open tasks, birthdays)
  bdays      a birthday one week ahead (sent with the morning slot)
  partner    the other person added an event/task/birthday for you or for both
  money      the other person added an expense or a repayment

settings/push-state remembers what was already sent and which items/expenses
were already known, so nothing is sent twice.

  FIREBASE_SERVICE_ACCOUNT   the full JSON of a Firebase service-account key
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import firebase_admin
from firebase_admin import credentials, firestore, messaging
from google.cloud.firestore_v1.field_path import FieldPath

TZ = ZoneInfo("Europe/Brussels")
PEOPLE = {"ewoud": "Ewoud", "helen": "Helen"}
DEFAULTS = {"events": True, "before": 30, "lessons": False, "morning": True, "morningAt": "08:00",
            "bdays": True, "partner": True, "money": True}
DOW = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
ANNIVERSARY = {"id": "anniv-2023", "type": "anniversary", "title": "Ewoud & Helen", "date": "2023-09-11",
               "yearKnown": True, "who": "both"}
MORNING_WINDOW = timedelta(hours=3)  # a late run still sends the summary, but not at night


def euro(cents):
    s = f"{(cents or 0) / 100:,.2f}"
    return "€ " + s.replace(",", " ").replace(".", ",").replace(" ", ".")


def nice(d):
    return f"{DOW[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"


def parse_day(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def parse_time(s):
    try:
        h, m = str(s).split(":")[:2]
        return int(h), int(m)
    except (TypeError, ValueError):
        return None


def for_person(item, person):
    return item.get("who") in (person, "both")


def occurs_on(item, d):
    """Same rules as the app: one-off on its date, weekly repeats, birthdays every year."""
    start = parse_day(item.get("date"))
    if not start:
        return False
    if item.get("type") in ("birthday", "anniversary"):
        md = (start.month, start.day)
        leap = d.year % 4 == 0 and (d.year % 100 != 0 or d.year % 400 == 0)
        if md == (2, 29) and not leap:
            md = (2, 28)
        return (d.month, d.day) == md and (not item.get("yearKnown", True) or d >= start)
    if item.get("repeat") == "weekly":
        until = parse_day(item.get("until"))
        return d >= start and (not until or d <= until) and (d - start).days % 7 == 0
    return d == start


def task_done(item, d):
    if item.get("repeat") == "weekly":
        return d.isoformat() in (item.get("doneDates") or [])
    return bool(item.get("done"))


def bday_label(item, d):
    if item.get("type") == "anniversary":
        years = d.year - int(item["date"][:4])
        return f"{years} jaar samen" if years > 0 else "Samen sinds vandaag"
    age = d.year - int(item["date"][:4]) if item.get("yearKnown", True) else None
    return item.get("title", "") + (f" ({age})" if age and age > 0 else "")


class Sender:
    def __init__(self, db, dry):
        self.db, self.dry, self.sent = db, dry, []

    def send(self, user, title, body, tag, url="./"):
        tokens = list((user.get("tokens") or {}).keys())
        self.sent.append((user.get("name"), title, body))
        print(f"-> {user.get('name')}: {title} | {body}")
        if self.dry or not tokens:
            return
        msgs = [messaging.Message(
            token=t,
            data={"title": title, "body": body, "tag": tag, "url": url},
            webpush=messaging.WebpushConfig(headers={"Urgency": "high", "TTL": "21600"}),
        ) for t in tokens]
        res = messaging.send_each(msgs)
        dead = []
        for t, r in zip(tokens, res.responses):
            if not r.success:
                code = type(r.exception).__name__
                print(f"   failed for a device: {code} {r.exception}", file=sys.stderr)
                if code in ("UnregisteredError", "SenderIdMismatchError") or "not a valid FCM registration token" in str(r.exception):
                    dead.append(t)
        if dead:
            self.db.document(f"settings/push-{user['uid']}").update(
                {FieldPath("tokens", t).to_api_repr(): firestore.DELETE_FIELD for t in dead})


def load_users(db):
    users = []
    for doc in db.collection("settings").stream():
        if not doc.id.startswith("push-"):
            continue
        v = doc.to_dict() or {}
        person = v.get("person") or (v.get("name") or "").lower()
        if person not in PEOPLE:
            continue
        prefs = dict(DEFAULTS)
        prefs.update(v.get("prefs") or {})
        users.append({"uid": doc.id[5:], "name": v.get("name") or PEOPLE[person], "person": person,
                      "tokens": v.get("tokens") or {}, "prefs": prefs})
    return users


def run(db, now, dry=False):
    users = [u for u in load_users(db) if u["tokens"] or dry]
    state_ref = db.document("settings/push-state")
    state = (state_ref.get().to_dict() or {}) if not dry else {}
    sent_keys = dict(state.get("sent") or {})
    items = [dict(d.to_dict() or {}, id=d.id) for d in db.collection("items").stream()]
    expenses = [dict(d.to_dict() or {}, id=d.id) for d in db.collection("expenses").stream()]
    own_items = {i["id"]: i for i in items if i.get("source") not in ("timeedit", "gcal")
                 and not i["id"].startswith(("te-", "gc-"))}
    first_run = "known_items" not in state
    known_items = set(state.get("known_items") or [])
    known_exp = set(state.get("known_expenses") or [])
    out = Sender(db, dry)
    today = now.date()

    def once(key, fn):
        if key in sent_keys:
            return
        fn()
        sent_keys[key] = now.isoformat()

    for u in users:
        p, person = u["prefs"], u["person"]

        # 1. Reminders before events (and tasks with a time).
        if p.get("events"):
            lead = timedelta(minutes=int(p.get("before") or 30))
            for i in items:
                if i.get("type") not in ("event", "task") or not for_person(i, person):
                    continue
                if i.get("source") == "timeedit" and not p.get("lessons"):
                    continue
                hm = parse_time(i.get("start"))
                if not hm:
                    continue
                for d in (today, today + timedelta(days=1)):
                    if not occurs_on(i, d) or (i.get("type") == "task" and task_done(i, d)):
                        continue
                    start = datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=TZ)
                    if not (start - lead <= now < start):
                        continue
                    mins = max(1, round((start - now).total_seconds() / 60))
                    when = f"over {mins} min" if mins < 60 else f"over {mins // 60} uur" + (f" {mins % 60} min" if mins % 60 else "")
                    body = when + (f" · {i['location']}" if i.get("location") else "")
                    if i.get("type") == "task":
                        body = "Taak · " + body
                    once(f"ev:{u['uid']}:{i['id']}:{d}:{i.get('start')}",
                         lambda i=i, body=body: out.send(u, f"{i.get('start')} {i.get('title', '')}", body, f"ev-{i['id']}"))

        # 2. Morning: summary of today and birthdays a week ahead.
        mt = parse_time(p.get("morningAt")) or (8, 0)
        slot = datetime(today.year, today.month, today.day, mt[0], mt[1], tzinfo=TZ)
        in_slot = slot <= now < slot + MORNING_WINDOW
        if in_slot and p.get("morning"):
            def summary():
                todays = [i for i in items + [ANNIVERSARY] if for_person(i, person) and occurs_on(i, today)]
                if not p.get("lessons"):
                    lessons = [i for i in todays if i.get("source") == "timeedit"]
                    todays = [i for i in todays if i.get("source") != "timeedit"]
                else:
                    lessons = []
                bdays = [i for i in todays if i.get("type") in ("birthday", "anniversary")]
                events = sorted([i for i in todays if i.get("type") == "event"], key=lambda i: i.get("start") or "")
                tasks = [i for i in todays if i.get("type") == "task" and not task_done(i, today)]
                if not (bdays or events or tasks or lessons):
                    return
                lines = []
                for b in bdays:
                    lines.append(("🎉 " if b.get("type") == "anniversary" else "🎂 Jarig: ") + bday_label(b, today))
                for e in events:
                    lines.append((e.get("start") + " " if e.get("start") else "") + e.get("title", ""))
                if lessons:
                    lines.append(f"{len(lessons)} les" + ("sen" if len(lessons) > 1 else "") + f", eerste om {min(l.get('start') or '' for l in lessons)}")
                for t in tasks:
                    lines.append("☐ " + (t.get("start") + " " if t.get("start") else "") + t.get("title", ""))
                parts = []
                if events or lessons:
                    n = len(events) + len(lessons)
                    parts.append(f"{n} afspra{'ken' if n != 1 else 'ak'}")
                if tasks:
                    parts.append(f"{len(tasks)} ta{'ken' if len(tasks) != 1 else 'ak'}")
                title = "Vandaag" + (": " + " en ".join(parts) if parts else "")
                out.send(u, title, "\n".join(lines[:8]) + ("\n…" if len(lines) > 8 else ""), "morning")
            once(f"am:{u['uid']}:{today}", summary)

        if in_slot and p.get("bdays"):
            ahead = today + timedelta(days=7)
            for i in items + [ANNIVERSARY]:
                if i.get("type") in ("birthday", "anniversary") and for_person(i, person) and occurs_on(i, ahead):
                    if i.get("type") == "anniversary":
                        title, body = "Over een week: " + bday_label(i, ahead), f"Op {nice(ahead)} vieren jullie het."
                    else:
                        title, body = "Over een week jarig: " + bday_label(i, ahead), f"Op {nice(ahead)}. Tijd voor een cadeau?"
                    once(f"bd7:{u['uid']}:{i['id']}:{ahead}", lambda title=title, body=body, i=i: out.send(u, title, body, f"bd-{i['id']}"))

        # 3. The other person added something.
        if not first_run and p.get("partner"):
            for iid, i in own_items.items():
                if iid in known_items or not for_person(i, person):
                    continue
                by = i.get("addedBy") or ""
                if not by or by.lower() == u["name"].lower():
                    continue
                d = parse_day(i.get("date"))
                kind = {"task": "een taak", "birthday": "een verjaardag"}.get(i.get("type"), "een afspraak")
                body = i.get("title", "")
                if i.get("type") == "birthday" and d:
                    body = f"{body} · {d.day} {MONTHS[d.month - 1]}"
                elif d:
                    body = f"{body} · {nice(d)}" + (f" om {i['start']}" if i.get("start") else "")
                    if i.get("repeat") == "weekly":
                        body += " (elke week)"
                once(f"add:{u['uid']}:{iid}", lambda by=by, kind=kind, body=body, iid=iid: out.send(u, f"{by} voegde {kind} toe", body, f"add-{iid}"))

        if not first_run and p.get("money"):
            for e in expenses:
                if e["id"] in known_exp:
                    continue
                by = e.get("addedBy") or ""
                if not by or by.lower() == u["name"].lower():
                    continue
                amount, payer = e.get("amount") or 0, e.get("paidBy")
                if e.get("kind") == "settle":
                    title = f"{PEOPLE.get(payer, by)} betaalde {euro(amount)} terug"
                    body = "Toegevoegd door " + by
                else:
                    title = f"{PEOPLE.get(payer, by)} betaalde {euro(amount)}" + (f" voor {e['desc']}" if e.get("desc") else "")
                    share = amount if e.get("split") == "full" else round(amount / 2)
                    body = (f"Jij moet {euro(share)} terugbetalen" if payer != person
                            else f"{PEOPLE[other(person)]} moet jou {euro(share)} terugbetalen")
                once(f"exp:{u['uid']}:{e['id']}", lambda title=title, body=body, e=e: out.send(u, title, body, f"exp-{e['id']}"))

    # Remember what is known now; forget sent keys older than 3 days.
    cutoff = (now - timedelta(days=3)).isoformat()
    sent_keys = {k: v for k, v in sent_keys.items() if v >= cutoff}
    if not dry:
        state_ref.set({"sent": sent_keys, "known_items": sorted(own_items), "known_expenses": sorted(e["id"] for e in expenses),
                       "last_run": now.isoformat()})
    if first_run:
        print("First run: stored the current items and expenses; only new ones will be announced.")
    return out.sent


def other(p):
    return "helen" if p == "ewoud" else "ewoud"


def main():
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    if not raw:
        sys.exit("FIREBASE_SERVICE_ACCOUNT is missing.")
    firebase_admin.initialize_app(credentials.Certificate(json.loads(raw)))
    sent = run(firestore.client(), datetime.now(TZ))
    print(f"Done: {len(sent)} notification(s).")


if __name__ == "__main__":
    main()
