#!/usr/bin/env python3
"""Keeps Iris's people and Google Contacts in step (Iris's spec: Sync People
with Google Contacts). Runs on the Mac as the job de.neutze.iris.contacts.

  python3 iris_sync.py --client-secret ~/.config/contacts-sync/client_secret.json \
      --pictures ~/Pictures/_inbox [--apply]

Each run:
  1. writes <pictures>/.iris/contacts.vcf: every contact that is a person, with
     only its name, birthday and Google id, as vCard 4.0;
  2. writes <pictures>/.iris/contacts-wishes.json: the day and month of the
     birthday wishes you sent on WhatsApp, with the birthday Google has. The
     words are matched inside WhatsApp's database; no message text is selected;
  3. sends, with --apply, every yes in <pictures>/.iris/contacts-answers.jsonl
     not yet in contacts-sent.jsonl, after reading the contact live. Google is
     only added to; the one overwrite is a yes on Iris's "Iris's wins" card,
     sent only while Google still has the date that card showed. Without
     --apply it prints what it would send.

Nothing here deletes a contact or a birthday. Standard library only.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import people_sync as ps
import whatsapp_activity as wa

EXPORT_FIELDS = "names,birthdays,organizations,metadata"
LIVE_FIELDS = "names,birthdays,metadata"
WISH_WORDS = ("geburtstag", "purzeltag", "birthday", "bday", "b-day", "hbd", "alles gute")
WISH_QUERY = """
SELECT s.ZCONTACTJID, m.ZMESSAGEDATE
FROM ZWACHATSESSION s
JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
WHERE m.ZISFROMME = 1
  AND (s.ZCONTACTJID LIKE '%@s.whatsapp.net' OR s.ZCONTACTJID LIKE '%@lid')
  AND ({})
""".format(" OR ".join("lower(m.ZTEXT) LIKE ?" for _ in WISH_WORDS))
KINDS_TO_SEND = {"add-birthday", "create", "overwrite", "wish"}
UNDO = Path(__file__).parent / "work" / "iris-undo.jsonl"


# ---- birthdays ---------------------------------------------------------------

def birthday_of(person: dict) -> dict:
    """The contact's birthday as {year?, month, day}, or {} when it has none
    with a day and month."""
    entries = sorted(person.get("birthdays", []), key=lambda b: not b.get("metadata", {}).get("primary"))
    for b in entries:
        d = b.get("date") or {}
        if d.get("month") and d.get("day"):
            return {k: d[k] for k in ("year", "month", "day") if d.get(k)}
    return {}


def iso(date: dict) -> str:
    """{1985, 3, 12} -> 1985-03-12; without a year, --03-12; {} -> ''."""
    if not date:
        return ""
    head = f"{date['year']:04d}" if date.get("year") else "-"
    return f"{head}-{date['month']:02d}-{date['day']:02d}"


def parse_iso(text: str) -> dict:
    """1985-03-12 or --03-12 as Google's date; anything else, {}."""
    m = re.fullmatch(r"(\d{4}|-)-(\d{2})-(\d{2})", (text or "").strip())
    if not m:
        return {}
    out = {"month": int(m.group(2)), "day": int(m.group(3))}
    if m.group(1) != "-":
        out["year"] = int(m.group(1))
    try:
        dt.date(out.get("year", 2000), out["month"], out["day"])
    except ValueError:
        return {}
    return out


# ---- the export --------------------------------------------------------------

def is_person(p: dict) -> bool:
    n = (p.get("names") or [{}])[0]
    return bool((n.get("givenName") or "").strip() or (n.get("familyName") or "").strip())


def full_name(p: dict) -> str:
    n = (p.get("names") or [{}])[0]
    return (n.get("displayName") or " ".join(
        x for x in (n.get("givenName"), n.get("middleName"), n.get("familyName")) if x)).strip()


def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def vcard(people: list) -> str:
    """vCard 4.0 with only FN, N, BDAY and UID; companies are left out."""
    out = []
    for p in sorted((p for p in people if is_person(p)), key=lambda p: ps.fold(full_name(p))):
        n = (p.get("names") or [{}])[0]
        lines = ["BEGIN:VCARD", "VERSION:4.0", f"UID:{p['resourceName']}", f"FN:{_esc(full_name(p))}",
                 "N:" + ";".join(_esc(n.get(k, "")) for k in ("familyName", "givenName", "middleName")) + ";;"]
        bd = birthday_of(p)
        if bd:
            lines.append("BDAY:" + (f"{bd['year']:04d}" if bd.get("year") else "--") + f"{bd['month']:02d}{bd['day']:02d}")
        out += lines + ["END:VCARD"]
    return "\r\n".join(out) + "\r\n" if out else ""


def write_atomic(path: Path, text: str) -> None:
    """Into a temporary file beside it, then moved over it, so a reader never sees half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ---- WhatsApp wishes ---------------------------------------------------------

def wish_rows(db: Path) -> list:
    """(number, local date) of each birthday wish you sent in a one-to-one chat."""
    lids = wa.lid_numbers(db)
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(wa.snapshot(db, Path(tmp)))
        try:
            rows = con.execute(WISH_QUERY, [f"%{w}%" for w in WISH_WORDS]).fetchall()
        finally:
            con.close()
    out = []
    for jid, seconds in rows:
        number = wa.number(jid, lids)
        if number and seconds is not None:
            out.append((number, (wa.APPLE_EPOCH + dt.timedelta(seconds=seconds)).astimezone().date()))
    return out


def wishes(rows: list, people: list) -> list:
    """One suggestion per contact whose number belongs to it alone and whose
    wishes all fall on one day of the year, with the birthday Google has
    (google, '' for none), so Iris can show both. A wish on Google's own
    day is no suggestion."""
    owners = collections.defaultdict(set)
    for p in people:
        if is_person(p):
            for ph in p.get("phoneNumbers", []):
                key = ps.phone_key(ph.get("value", ""))
                if key:
                    owners[key].add(p["resourceName"])
    by_rn = {p["resourceName"]: p for p in people}
    days = collections.defaultdict(lambda: collections.defaultdict(set))
    for number, date in rows:
        rns = owners.get(ps.phone_key(number), set())
        if len(rns) == 1:
            days[next(iter(rns))][(date.month, date.day)].add(date.year)
    out = []
    for rn, found in sorted(days.items()):
        if len(found) != 1:
            continue
        (month, day), years = next(iter(found.items()))
        have = birthday_of(by_rn[rn])
        if have and (have["month"], have["day"]) == (month, day):
            continue
        out.append({"uid": rn, "name": full_name(by_rn[rn]), "month": month, "day": day, "years": sorted(years),
                    "google": iso(have)})
    return out


# ---- the answers -------------------------------------------------------------

def read_jsonl(path: Path) -> list:
    """Every complete line; a last line still being written (no newline) waits for the next run."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")[:-1]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def append_jsonl(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def to_send(answers: list, sent: list) -> list:
    done = {s.get("answer") for s in sent}
    return [a for a in answers if a.get("answer") == "yes" and a.get("kind") in KINDS_TO_SEND
            and a.get("id") and a["id"] not in done]


def decide(answer: dict, live: dict | None, people: list) -> tuple:
    """What to send for one yes, after reading the contact live:
    ("create", body) / ("update", birthday) / ("skipped", why)."""
    want = parse_iso(answer.get("birthday", ""))
    kind = answer["kind"]
    if kind == "create":
        name = (answer.get("name") or "").strip()
        if not name:
            return "skipped", "no name"
        if any(ps.fold(full_name(p)) == ps.fold(name) for p in people if is_person(p)):
            return "skipped", "a contact of that name exists now"
        given, _, family = name.rpartition(" ")
        names = {"givenName": given, "familyName": family} if given else {"givenName": family}
        body = {"names": [names]}
        if want and want.get("year"):
            body["birthdays"] = [{"date": want}]
        return "create", body
    if live is None:
        return "skipped", "the contact is gone"
    if not want:
        return "skipped", "no birthday on the answer"
    have = birthday_of(live)
    if kind == "overwrite":
        if iso(have) != answer.get("was"):
            return "skipped", f"Google has {iso(have) or 'no birthday'} now"
        return "update", want
    if not have:
        return "update", want
    # Google's day and month with the year Iris knows: the year is only added.
    if kind == "add-birthday" and not have.get("year") and want.get("year") \
            and (have["month"], have["day"]) == (want["month"], want["day"]):
        return "update", want
    return "skipped", f"Google has {iso(have)} now"


def send(api, pictures: Path, people: list, apply: bool, log=print) -> dict:
    iris = pictures / ".iris"
    pending = to_send(read_jsonl(iris / "contacts-answers.jsonl"), read_jsonl(iris / "contacts-sent.jsonl"))
    counts = collections.Counter()
    for answer in pending:
        live = None
        if answer["kind"] != "create":
            live = next((p for p in people if p["resourceName"] == answer.get("uid")), None)
            if live is not None:
                live = api.call("GET", f"/{live['resourceName']}", params={"personFields": LIVE_FIELDS})
        what, detail = decide(answer, live, people)
        label = answer.get("name") or answer.get("uid", "")
        if what == "skipped":
            result = {"result": "skipped", "why": detail}
        elif not apply:
            log(f"would {what} {label}: {iso(detail) if what == 'update' else iso(parse_iso(answer.get('birthday', '')))}")
            counts["would send"] += 1
            continue
        else:
            result = {"result": "sent"}
            try:
                UNDO.parent.mkdir(parents=True, exist_ok=True)
                if what == "create":
                    made = api.call("POST", "/people:createContact", body=detail, params={"personFields": "names"})
                    result["uid"] = made.get("resourceName", "")
                    append_jsonl(UNDO, {"at": now(), "created": result["uid"]})
                else:
                    append_jsonl(UNDO, {"at": now(), "before": live})
                    api.call("PATCH", f"/{live['resourceName']}:updateContact",
                             body={"etag": live["etag"], "birthdays": [{"date": detail}]},
                             params={"updatePersonFields": "birthdays"})
            except SystemExit as e:  # people_sync.Api ends a failed call this way
                result = {"result": "failed", "why": str(e)[:300]}
        counts[result["result"]] += 1
        if apply or result["result"] == "skipped":
            append_jsonl(iris / "contacts-sent.jsonl", {"answer": answer["id"], "at": now(), **result})
        log(f"{result['result']} {label}" + (f": {result['why']}" if result.get("why") else ""))
    return dict(counts)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---- the run -----------------------------------------------------------------

def run(api, pictures: Path, apply: bool, whatsapp_db: Path | None, log=print) -> dict:
    people = ps.fetch_all(api, EXPORT_FIELDS + ",phoneNumbers")
    iris = pictures / ".iris"
    write_atomic(iris / "contacts.vcf", vcard(people))
    summary = {"contacts": sum(1 for p in people if is_person(p)),
               "with birthday": sum(1 for p in people if is_person(p) and birthday_of(p))}
    if whatsapp_db is not None:
        try:
            found = wishes(wish_rows(whatsapp_db), people)
            write_atomic(iris / "contacts-wishes.json", json.dumps(found, ensure_ascii=False, indent=1) + "\n")
            summary["wishes"] = len(found)
        except (sqlite3.Error, OSError) as e:
            summary["wishes"] = f"not read ({type(e).__name__}; Full Disk Access?)"
    summary.update(send(api, pictures, people, apply, log))
    return summary


def job_token(secret: Path, force_refresh: bool = False) -> str:
    """The saved login, refreshed if needed; never the browser. A login that has
    ended stops the job with the one command that renews it."""
    tok = json.loads(ps.TOKEN_PATH.read_text()) if ps.TOKEN_PATH.exists() else {}
    if not force_refresh and tok.get("expires_at", 0) > dt.datetime.now().timestamp() + 60:
        return tok["access_token"]
    if tok.get("refresh_token"):
        client = json.loads(secret.read_text())
        client = client.get("installed") or client.get("web") or client
        try:
            new = ps._post_form(client["token_uri"], {
                "client_id": client["client_id"], "client_secret": client["client_secret"],
                "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
            tok.update(new, expires_at=dt.datetime.now().timestamp() + new.get("expires_in", 3600))
            ps._save_token(tok)
            return tok["access_token"]
        except OSError:
            pass
    sys.exit("The Google login has ended. Log in again in a terminal: "
             "cd ~/Repositories/tool-contacts-cleanup && python3 iris_sync.py --pictures ~/Pictures/_inbox")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-secret", type=Path, default=Path.home() / ".config/contacts-sync/client_secret.json")
    ap.add_argument("--pictures", type=Path, required=True, help="the picture inbox; files go into its .iris/")
    ap.add_argument("--whatsapp-db", type=Path, default=wa.DEFAULT_DB)
    ap.add_argument("--no-whatsapp", action="store_true")
    ap.add_argument("--apply", action="store_true", help="send the answers marked yes to Google")
    args = ap.parse_args(argv)
    if not (args.pictures / ".iris").is_dir():
        sys.exit(f"{args.pictures}/.iris does not exist: is the picture inbox there?")
    secret = args.client_secret.expanduser()
    token = ps.access_token if sys.stdin.isatty() else job_token  # a job never waits on a browser
    api = ps.Api(token(secret), refresh=lambda: token(secret, force_refresh=True))
    db = None if args.no_whatsapp or not args.whatsapp_db.exists() else args.whatsapp_db
    summary = run(api, args.pictures, args.apply, db)
    print(("APPLIED: " if args.apply else "PREVIEW: ") + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
