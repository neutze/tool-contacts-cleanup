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
     words are matched inside WhatsApp's database; no message text is selected.
     --android-db <msgstore.db> once reads an Android phone's decrypted backup
     the same way and keeps each wish's number and date in work/android-wishes.json,
     which every later run adds to the Mac's wishes; --facebook <zips> and
     --gmail-list / --gmail-wishes do the same for Facebook's export and your
     sent mail, keeping name or address and date in work/;
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
import email.utils
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
ANDROID_WISH_QUERY = """
SELECT j.user || '@' || j.server, {mapped} m.timestamp
FROM message m
JOIN chat c ON m.chat_row_id = c._id
JOIN jid j ON c.jid_row_id = j._id
{join}
WHERE m.from_me = 1
  AND j.server IN ('s.whatsapp.net', 'lid')
  AND ({like})
""".replace("{like}", " OR ".join("lower(m.text_data) LIKE ?" for _ in WISH_WORDS))
ANDROID_WISHES = Path(__file__).parent / "work" / "android-wishes.json"
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


def android_wish_rows(db: Path) -> list:
    """(number, local date) of each birthday wish you sent in a one-to-one chat,
    from an Android phone's decrypted msgstore.db. A chat under a hidden id
    takes its number from jid_map where this version has it."""
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        tables = {n for n, in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"message", "chat", "jid"} <= tables:
            raise sqlite3.DatabaseError("not an Android msgstore.db (no message, chat and jid tables)")
        if "jid_map" in tables:
            query = ANDROID_WISH_QUERY.format(
                mapped="pj.user || '@' || pj.server,",
                join="LEFT JOIN jid_map jm ON jm.lid_row_id = j._id\nLEFT JOIN jid pj ON pj._id = jm.jid_row_id")
        else:
            query = ANDROID_WISH_QUERY.format(mapped="NULL,", join="")
        rows = con.execute(query, [f"%{w}%" for w in WISH_WORDS]).fetchall()
    finally:
        con.close()
    out = []
    for jid, phone_jid, millis in rows:
        number = wa.number(phone_jid or jid, {})
        if number and millis:
            out.append((number, dt.datetime.fromtimestamp(millis / 1000, dt.timezone.utc).astimezone().date()))
    return out


# ---- Facebook and Gmail wishes -------------------------------------------------

FACEBOOK_WISHES = Path(__file__).parent / "work" / "facebook-wishes.json"
GMAIL_WISHES = Path(__file__).parent / "work" / "gmail-wishes.json"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
TIMELINE = re.compile(r"(?:wrote|posted) on (.+?)['\u2019]s? timeline|(?:in|auf) (.+?) Chronik", re.IGNORECASE)


def is_wish(text) -> bool:
    """Whether a text holds one of the wish words; the text itself goes nowhere."""
    return isinstance(text, str) and any(w in _fb(text).lower() for w in WISH_WORDS)


def _fb(text: str) -> str:
    """Facebook's export writes UTF-8 as Latin-1 escapes ("JÃ¼rgen"); undo that where it applies."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _json_files(sources: list):
    """(path inside, parsed JSON) of every .json in the given zips and folders."""
    import zipfile
    for src in sources:
        if src.is_dir():
            for f in sorted(src.rglob("*.json")):
                yield f.relative_to(src).as_posix(), f.read_bytes()
        else:
            with zipfile.ZipFile(src) as z:
                for name in sorted(z.namelist()):
                    if name.endswith(".json"):
                        yield name, z.read(name)


def facebook_wish_rows(sources: list) -> list:
    """(name, local date) of each birthday wish you sent on Facebook, from its
    "Download your information" export (JSON, zips or unzipped folders): a
    Messenger message you sent in a chat with one other person, under that
    person's name, or a post of yours on someone's timeline, under the name in
    its title. You are the name found in most chats. Text is only matched."""
    threads, posts = [], []
    for name, raw in _json_files(sources):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("messages"), list) and "participants" in data:
            people = [_fb(x.get("name", "") if isinstance(x, dict) else str(x)) for x in data["participants"]]
            threads.append((people, data["messages"]))
        elif "/posts/" in f"/{name}" and isinstance(data, list):
            posts.extend(x for x in data if isinstance(x, dict))
    seen = collections.Counter(n for people, _ in threads for n in set(people) if n)
    me = seen.most_common(1)[0][0] if seen else ""
    out = []
    for people, messages in threads:
        others = [n for n in set(people) if n and n != me]
        if len(set(people)) != 2 or len(others) != 1:
            continue
        for m in messages:
            sender = _fb(str(m.get("sender_name", m.get("senderName", ""))))
            ms = m.get("timestamp_ms", m.get("timestamp"))
            if sender == me and isinstance(ms, (int, float)) and is_wish(m.get("content", m.get("text"))):
                out.append((others[0], dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).astimezone().date()))
    for post in posts:
        hit = TIMELINE.search(_fb(str(post.get("title", ""))))
        texts = [d.get("post") for d in post.get("data", []) if isinstance(d, dict)]
        if hit and isinstance(post.get("timestamp"), (int, float)) and any(is_wish(t) for t in texts):
            day = dt.datetime.fromtimestamp(post["timestamp"], dt.timezone.utc).astimezone().date()
            name = (hit.group(1) or hit.group(2)).strip()
            out.append((name, day))
            if hit.group(2) and name.endswith("s"):  # German genitive: "Annas Chronik"
                out.append((name[:-1], day))
    return out


def listed_wish_rows(text: str) -> list:
    """(address, date) from lines such as Gemini lists them ("anna@x.de,2019-03-12");
    a line without one address and one YYYY-MM-DD date is passed over."""
    out = []
    for line in text.splitlines():
        addrs = re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", line)
        dates = re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", line)
        if len(set(a.lower() for a in addrs)) == 1 and len(dates) == 1:
            try:
                out.append((addrs[0].lower(), dt.date(*map(int, dates[0]))))
            except ValueError:
                pass
    return out


def gmail_wish_rows(api, search: bool) -> list:
    """(address, local date) of each birthday wish you mailed to one address
    alone. With search (the gmail.readonly login) Gmail matches the words in
    the whole mail; else (gmail.metadata) only the subject can be matched,
    walking the SENT label. Either way only the headers come back, never a body."""
    out, token = [], None
    query = "in:sent (" + " OR ".join(f'"{w}"' for w in WISH_WORDS) + ")"
    headers = [("metadataHeaders", "To"), ("metadataHeaders", "Cc")] + ([] if search else [("metadataHeaders", "Subject")])
    while True:
        params = [("maxResults", 500)] + ([("q", query)] if search else [("labelIds", "SENT")])
        page = api.call("GET", "/users/me/messages", params=params + ([("pageToken", token)] if token else []))
        for m in page.get("messages", []):
            msg = api.call("GET", f"/users/me/messages/{m['id']}", params=[("format", "metadata")] + headers)
            hs = msg.get("payload", {}).get("headers", [])
            if not search and not is_wish(" ".join(h["value"] for h in hs if h["name"].lower() == "subject")):
                continue
            to = {a.strip().lower() for _, a in email.utils.getaddresses(
                [h["value"] for h in hs if h["name"].lower() in ("to", "cc")]) if "@" in a}
            if len(to) == 1 and msg.get("internalDate"):
                out.append((to.pop(), dt.datetime.fromtimestamp(int(msg["internalDate"]) / 1000,
                                                                 dt.timezone.utc).astimezone().date()))
        token = page.get("nextPageToken")
        if not token:
            return out


def save_rows(path: Path, rows: list, field: str = "number") -> None:
    unique = sorted({(n, d.isoformat()) for n, d in rows})
    write_atomic(path, json.dumps([{field: n, "date": d} for n, d in unique], ensure_ascii=False, indent=1) + "\n")


def load_rows(path: Path, field: str = "number") -> list:
    if not path.exists():
        return []
    return [(r[field], dt.date.fromisoformat(r["date"])) for r in json.loads(path.read_text(encoding="utf-8"))]


def wishes(rows: list, people: list, more: list = ()) -> list:
    """One suggestion per contact without a birthday in Google whose wishes all
    fall on one day of the year. `rows` are WhatsApp's (number, date); `more`
    are (kind, key, date, source) from the other sources, kind "phone",
    "email" or "name". A key counts only when it belongs to one contact alone.
    Google's birthday stands: a contact that has one gets no suggestion."""
    owners = collections.defaultdict(set)
    for p in people:
        if is_person(p):
            for ph in p.get("phoneNumbers", []):
                key = ps.phone_key(ph.get("value", ""))
                if key:
                    owners["phone", key].add(p["resourceName"])
            for em in p.get("emailAddresses", []):
                if "@" in em.get("value", ""):
                    owners["email", em["value"].strip().lower()].add(p["resourceName"])
            if full_name(p):
                owners["name", ps.fold(full_name(p))].add(p["resourceName"])
    by_rn = {p["resourceName"]: p for p in people}
    days = collections.defaultdict(lambda: collections.defaultdict(set))
    sources = collections.defaultdict(set)
    for kind, key, date, source in [("phone", n, d, "WhatsApp") for n, d in rows] + list(more):
        norm = ps.phone_key(key) if kind == "phone" else ps.fold(key) if kind == "name" else key.strip().lower()
        rns = owners.get((kind, norm), set())
        if len(rns) == 1:
            rn = next(iter(rns))
            days[rn][(date.month, date.day)].add(date.year)
            sources[rn].add(source)
    out = []
    for rn, found in sorted(days.items()):
        if len(found) != 1:
            continue
        (month, day), years = next(iter(found.items()))
        have = birthday_of(by_rn[rn])
        if have:
            continue
        out.append({"uid": rn, "name": full_name(by_rn[rn]), "month": month, "day": day, "years": sorted(years),
                    "google": iso(have), "sources": sorted(sources[rn])})
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

def run(api, pictures: Path, apply: bool, whatsapp_db: Path | None, log=print,
        android_rows: Path = ANDROID_WISHES, facebook_rows: Path = FACEBOOK_WISHES,
        gmail_rows: Path = GMAIL_WISHES) -> dict:
    people = ps.fetch_all(api, EXPORT_FIELDS + ",phoneNumbers,emailAddresses")
    iris = pictures / ".iris"
    write_atomic(iris / "contacts.vcf", vcard(people))
    summary = {"contacts": sum(1 for p in people if is_person(p)),
               "with birthday": sum(1 for p in people if is_person(p) and birthday_of(p))}
    phone = load_rows(android_rows)
    more = ([("name", n, d, "Facebook") for n, d in load_rows(facebook_rows, "name")]
            + [("email", a, d, "Gmail") for a, d in load_rows(gmail_rows, "email")])
    if whatsapp_db is not None or phone or more:
        try:
            mac = wish_rows(whatsapp_db) if whatsapp_db is not None else []
            found = wishes(sorted(set(mac + phone)), people, more)
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
    ap.add_argument("--android-db", type=Path, help="an Android phone's decrypted msgstore.db, read once into "
                    "work/android-wishes.json (number and date of each wish)")
    ap.add_argument("--facebook", type=Path, nargs="+", help="Facebook's JSON export (zips or folders), read once into "
                    "work/facebook-wishes.json (name and date of each wish)")
    ap.add_argument("--gmail-wishes", choices=["subject", "search"], help="read your sent mail once into "
                    "work/gmail-wishes.json (address and date of each wish): subject (gmail.metadata) or search "
                    "(gmail.readonly, the whole mail matched by Gmail); only headers come back")
    ap.add_argument("--gmail-list", type=Path, help="a text file of 'address,YYYY-MM-DD' lines (as Gemini lists your "
                    "sent wishes), added to work/gmail-wishes.json")
    ap.add_argument("--apply", action="store_true", help="send the answers marked yes to Google")
    args = ap.parse_args(argv)
    if not (args.pictures / ".iris").is_dir():
        sys.exit(f"{args.pictures}/.iris does not exist: is the picture inbox there?")
    if args.android_db is not None:
        rows = android_wish_rows(args.android_db.expanduser())
        save_rows(ANDROID_WISHES, rows)
        print(f"Android: {len(rows)} wishes in {len({n for n, _ in rows})} chats kept in {ANDROID_WISHES}")
    if args.facebook:
        rows = facebook_wish_rows([f.expanduser() for f in args.facebook])
        save_rows(FACEBOOK_WISHES, rows, "name")
        print(f"Facebook: {len(rows)} wishes to {len({n for n, _ in rows})} names kept in {FACEBOOK_WISHES}")
    if args.gmail_list:
        rows = load_rows(GMAIL_WISHES, "email") + listed_wish_rows(args.gmail_list.expanduser().read_text(encoding="utf-8"))
        save_rows(GMAIL_WISHES, rows, "email")
        print(f"Gmail: {len(set(rows))} wishes to {len({a for a, _ in rows})} addresses kept in {GMAIL_WISHES}")
    secret = args.client_secret.expanduser()
    if args.gmail_wishes:
        # Ask for what the saved login has plus what this needs, so a new login takes nothing away.
        had = json.loads(ps.TOKEN_PATH.read_text()).get("scope", "").split() if ps.TOKEN_PATH.exists() else []
        need = [ps.SCOPE, GMAIL_READONLY if args.gmail_wishes == "search" else ps.GMAIL_SCOPE]
        scope = " ".join(dict.fromkeys(had + need))
        gmail = ps.Api(ps.access_token(secret, scope), GMAIL_API)
        rows = load_rows(GMAIL_WISHES, "email") + gmail_wish_rows(gmail, args.gmail_wishes == "search")
        save_rows(GMAIL_WISHES, rows, "email")
        print(f"Gmail: {len(rows)} wishes to {len({a for a, _ in rows})} addresses kept in {GMAIL_WISHES}")
    token = ps.access_token if sys.stdin.isatty() else job_token  # a job never waits on a browser
    api = ps.Api(token(secret), refresh=lambda: token(secret, force_refresh=True))
    db = None if args.no_whatsapp or not args.whatsapp_db.exists() else args.whatsapp_db
    summary = run(api, args.pictures, args.apply, db)
    print(("APPLIED: " if args.apply else "PREVIEW: ") + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
