#!/usr/bin/env python3
"""Build a plan.json for people_sync.py from the live contacts instead of a
CSV export, for the enrichment pass once the cleanup has been applied.

  python3 live_plan.py live.json -o plan2.json [--birthdays birthdays_found.csv]
      [--signatures signature_found.csv] [--whatsapp whatsapp_activity.csv]

live.json is people_sync.fetch_all(api, FULL_FIELDS) saved as JSON.

  streamline   emails without a type become home (free-mail domain) or work;
               phones without a type become mobile (+4915/16/17) or other
  birthdays    high-confidence matches from Calendar, only where none is set
  signatures   phone added if new; company, title and address only where empty
  whatsapp     a phone for a name-only contact whose full name is exactly one
               WhatsApp profile name; a last name for a first-name-only contact
               whose profile name starts with that first name

Every change is listed in review.csv with its source, so the dry run can be
checked line by line. Only fields the plan names are ever written.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from people_sync import fold, person_fingerprint, phone_key

FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.de", "hotmail.com", "hotmail.de", "outlook.com",
            "outlook.de", "live.com", "live.de", "icloud.com", "me.com", "mac.com", "web.de", "gmx.de", "gmx.net",
            "gmx.at", "gmx.ch", "t-online.de", "aol.com", "aol.de", "protonmail.com", "proton.me", "freenet.de",
            "arcor.de", "mail.de", "posteo.de", "yandex.ru", "mail.ru", "msn.com"}


def read_csv(path: Path) -> list:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def items(p: dict, field: str) -> list:
    return [{k: v for k, v in x.items() if k in ("value", "type")} for x in p.get(field, [])]


class Plan:
    def __init__(self, people: list):
        self.people = {p["resourceName"]: p for p in people}
        self.sets = defaultdict(dict)
        self.log = []

    def change(self, rn: str, field: str, value, source: str, what: str) -> None:
        self.sets[rn][field] = value
        name = (self.people[rn].get("names") or [{}])[0].get("displayName", "(no name)")
        self.log.append({"name": name, "field": field, "change": what, "source": source})

    def current(self, rn: str, field: str) -> list:
        """What the plan would leave in this list field so far."""
        return copy.deepcopy(self.sets.get(rn, {}).get(field, items(self.people[rn], field)))

    def write(self, out: Path, review: Path) -> dict:
        fps = defaultdict(int)
        for p in self.people.values():
            fps[person_fingerprint(p)] += 1
        plan = {"version": 1, "export_counts": fps, "deletes": [], "updates": [
            {"fp": person_fingerprint(self.people[rn]), "rn": rn,
             "name": (self.people[rn].get("names") or [{}])[0].get("displayName", ""), "set": s, "add_labels": []}
            for rn, s in self.sets.items()]}
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        with review.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["name", "field", "change", "source"])
            w.writeheader()
            w.writerows(self.log)
        return {"contacts": len(self.sets), "changes": len(self.log)}


def default_type(field: str, value: str) -> str:
    if field == "emailAddresses":
        return "home" if value.lower().split("@")[-1] in FREEMAIL else "work"
    return "mobile" if re.sub(r"\D", "", value).startswith(("4915", "4916", "4917")) else "other"


def fill_types(changes: dict) -> None:
    """Give untyped emails and phones in a planned list their default type."""
    for field in ("emailAddresses", "phoneNumbers"):
        for x in changes.get(field, []):
            if not (x.get("type") or "").strip():
                x["type"] = default_type(field, x["value"])


def streamline(plan: Plan) -> None:
    for rn, p in plan.people.items():
        emails = items(p, "emailAddresses")
        if any(not e.get("type") for e in emails):
            for e in emails:
                if not e.get("type"):
                    e["type"] = "home" if e["value"].lower().split("@")[-1] in FREEMAIL else "work"
                    plan.log.append({"name": (p.get("names") or [{}])[0].get("displayName", ""),
                                     "field": "emailAddresses", "change": f"{e['value']} -> {e['type']}",
                                     "source": "streamline"})
            plan.sets[rn]["emailAddresses"] = emails
        phones = items(p, "phoneNumbers")
        if any(not x.get("type") for x in phones):
            for x in phones:
                if not x.get("type"):
                    x["type"] = "mobile" if re.sub(r"\D", "", x["value"]).startswith(("4915", "4916", "4917")) else "other"
                    plan.log.append({"name": (p.get("names") or [{}])[0].get("displayName", ""),
                                     "field": "phoneNumbers", "change": f"{x['value']} -> {x['type']}",
                                     "source": "streamline"})
            plan.sets[rn]["phoneNumbers"] = phones


def birthdays(plan: Plan, rows: list) -> None:
    for r in rows:
        rn = r["resourceName"]
        if r.get("confidence") != "high" or rn not in plan.people or plan.people[rn].get("birthdays"):
            continue
        date = {"month": int(r["month"]), "day": int(r["day"])}
        if r.get("year"):
            date["year"] = int(r["year"])
        plan.change(rn, "birthdays", [{"date": date}], f"calendar: {r.get('evidence', '')}",
                    "-".join(str(date.get(k, "")) for k in ("year", "month", "day")).lstrip("-"))


def signatures(plan: Plan, rows: list) -> None:
    for r in rows:
        rn = r["resourceName"]
        if rn not in plan.people:
            continue
        p, src = plan.people[rn], f"mail signature {r.get('mail_date', '')}"
        for number in (x.strip() for x in r.get("phone", "").split(";")):
            phones = plan.current(rn, "phoneNumbers")
            if phone_key(number) and phone_key(number) not in {phone_key(x["value"]) for x in phones}:
                phones.append({"value": number, "type": "work"})
                plan.change(rn, "phoneNumbers", phones, src, f"+ {number}")
        org = (p.get("organizations") or [{}])[0]
        new_org = {k: v for k, v in (("name", r.get("company", "")), ("title", r.get("title", ""))) if v and not org.get(k)}
        if new_org:
            plan.change(rn, "organizations", {**{k: org.get(k, "") for k in ("name", "title")}, **new_org}, src,
                        ", ".join(f"{k} {v}" for k, v in new_org.items()))
        if r.get("city") and not p.get("addresses"):
            addr = {k: v for k, v in (("streetAddress", r.get("street", "")), ("postalCode", r.get("postal_code", "")),
                                       ("city", r.get("city", "")), ("country", r.get("country", ""))) if v}
            addr["type"] = "work"
            plan.change(rn, "addresses", [addr], src, ", ".join(v for k, v in addr.items() if k != "type"))


def whatsapp(plan: Plan, rows: list) -> list:
    """Applies exact full-name matches; returns the looser ones for review."""
    by_name = defaultdict(set)
    for r in rows:
        if " " in fold(r.get("push_name", "")) and phone_key(r["key"]):
            by_name[fold(r["push_name"])].add(r["key"])
    by_phone = {phone_key(r["key"]): r.get("push_name", "").strip() for r in rows if r.get("push_name", "").strip()}
    taken = {phone_key(x["value"]) for p in plan.people.values() for x in p.get("phoneNumbers", [])}
    maybe = []
    for rn, p in plan.people.items():
        n = (p.get("names") or [{}])[0]
        full = fold(f"{n.get('givenName', '')} {n.get('familyName', '')}")
        if not p.get("phoneNumbers") and not p.get("emailAddresses") and n.get("givenName") and n.get("familyName"):
            nums = by_name.get(full, set())
            if len(nums) == 1 and phone_key(next(iter(nums))) not in taken:
                num = next(iter(nums))
                plan.change(rn, "phoneNumbers", [{"value": num, "type": "mobile"}], "whatsapp profile name", f"+ {num}")
        if n.get("givenName") and not n.get("familyName"):
            for x in p.get("phoneNumbers", []):
                push = by_phone.get(phone_key(x["value"]), "")
                words = push.split()
                if len(words) >= 2 and fold(words[0]) == fold(n["givenName"]) and not re.search(r"[\d@]", push):
                    maybe.append({"name": n.get("displayName", ""), "field": "familyName",
                                  "change": " ".join(words[1:]), "source": f"whatsapp profile name '{push}'"})
                    break
    return maybe


def recency(plan: Plan, seen: dict, since: str) -> None:
    """Newest first. seen maps an email (lowercased) or a phone key to the date
    it was last used (sent mail, a WhatsApp message, a dated list). On a card
    with several emails or numbers the most recently used one moves to the
    top, and one not used since `since` while a newer one is, is typed old."""
    for rn, p in plan.people.items():
        for field, key in (("emailAddresses", lambda v: v.strip().lower()), ("phoneNumbers", phone_key)):
            current = plan.current(rn, field)
            if len(current) < 2:
                continue
            dated = [(seen.get(key(x["value"]), ""), i, x) for i, x in enumerate(current)]
            ordered = [x for _, _, x in sorted(dated, key=lambda d: (d[0] == "", "" if d[0] == "" else
                                                                     "".join(chr(255 - ord(c)) for c in d[0]), d[1]))]
            newest = max((d for d, _, _ in dated), default="")
            changed = ordered != current
            for d, _, x in dated:
                if d and d < since <= newest and (x.get("type") or "") != "old":
                    x["type"] = "old"
                    changed = True
            if changed:
                plan.change(rn, field, ordered, "recency", ", ".join(x["value"] for x in ordered[:2]) + " first")


def names_from_whatsapp(plan: Plan, rows: list) -> None:
    """A WhatsApp profile name is set by the person today: when it is a proper
    full name with the card's first name and another last name (married, say),
    it wins."""
    from contacts_cleanup import real_name
    by_phone = {phone_key(r["key"]): r.get("push_name", "").strip() for r in rows if r.get("push_name", "").strip()}
    for rn, p in plan.people.items():
        n = (p.get("names") or [{}])[0]
        if not n.get("givenName") or " " in n["givenName"].strip():
            continue
        for x in p.get("phoneNumbers", []):
            last = new_last_name(by_phone.get(phone_key(x["value"]), ""), n["givenName"], n.get("familyName", ""))
            if last:
                plan.change(rn, "names", {"givenName": n["givenName"], "middleName": n.get("middleName", ""),
                                          "familyName": last}, "whatsapp profile name",
                            f"{n.get('familyName') or '(none)'} -> {last}")
                break


def new_last_name(full: str, given: str, family: str) -> str:
    """The last name from a proper full name that starts with the card's first
    name, when it differs from the card's (or the card has none). A nickname,
    a single word, emoji or digits give nothing."""
    from contacts_cleanup import real_name
    words = full.split()
    if len(words) < 2 or fold(words[0]) != fold(given):
        return ""
    last = " ".join(words[1:])
    if not real_name(words[0], last) or fold(last) == fold(family):
        return ""
    return last


def clean_name(text: str) -> str:
    """Letters, spaces, hyphens and apostrophes only: 'Dimi 🌴' -> 'Dimi'."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s'’-]|[\d_]", " ", text)).strip()


def complete_names(plan: Plan, push_rows: list = ()) -> None:
    """Everyone gets a first and a last name. Run after the proper sources
    (WhatsApp, LinkedIn) have had their say. A missing part comes from, in
    order: the card's nickname, the WhatsApp profile name."""
    by_phone = {phone_key(r["key"]): clean_name(r.get("push_name", "")) for r in push_rows
                if clean_name(r.get("push_name", ""))}
    for rn, p in plan.people.items():
        n = dict((p.get("names") or [{}])[0])
        n.update(plan.sets.get(rn, {}).get("names", {}))
        given, family = (n.get("givenName") or "").strip(), (n.get("familyName") or "").strip()
        if given and family or not (given or family):
            continue
        from contacts_cleanup import real_name
        words = given.split()
        if not family and len(words) >= 2 and real_name(words[0], " ".join(words[1:])):
            # "Charles Njofack" typed into the first-name field: split, don't invent
            plan.change(rn, "names", {"givenName": words[0], "middleName": n.get("middleName", ""),
                                      "familyName": " ".join(words[1:])}, "complete names",
                        f"{given} -> {words[0]} | {' '.join(words[1:])}")
            continue
        pushes = [by_phone.get(phone_key(x["value"]), "") for x in p.get("phoneNumbers", [])]
        # The company is not a last name: "Carlo" at CrossFit stays "Carlo", with CrossFit as company.
        hints = [clean_name(x.get("value", "")) for x in p.get("nicknames", [])] + [x for x in pushes if x]
        have = fold(given or family)
        fill = ""
        for h in hints:
            words = [w for w in h.split() if fold(w) != have]
            if words:
                fill = " ".join(words)
                break
        if not fill:
            continue
        new = {"givenName": given or fill, "middleName": n.get("middleName", ""), "familyName": family or fill}
        plan.change(rn, "names", new, "complete names", f"{given or '?'} {family or '?'} -> "
                    f"{new['givenName']} {new['familyName']}")


def from_linkedin(plan: Plan, rows: list) -> None:
    """LinkedIn is kept up to date by the person: current company and title
    replace the card's; a new last name is taken when the match is sure (same
    email, same first name). Name matches without an email only fill gaps."""
    by_email, by_name = {}, defaultdict(list)
    for r in rows:
        if r.get("Email Address", "").strip():
            by_email[r["Email Address"].strip().lower()] = r
        by_name[fold(f"{r.get('First Name', '')} {r.get('Last Name', '')}")].append(r)
    for rn, p in plan.people.items():
        n = (p.get("names") or [{}])[0]
        r = next((by_email[e["value"].strip().lower()] for e in p.get("emailAddresses", [])
                  if e["value"].strip().lower() in by_email), None)
        sure = r is not None
        if not r:
            found = by_name.get(fold(f"{n.get('givenName', '')} {n.get('familyName', '')}"), [])
            r = found[0] if len(found) == 1 else None
        if not r:
            continue
        org = (p.get("organizations") or [{}])[0]
        new = {"name": r.get("Company", "").strip(), "title": r.get("Position", "").strip()}
        if any(new.values()) and (new["name"], new["title"]) != (org.get("name", ""), org.get("title", "")):
            plan.change(rn, "organizations", {k: v or org.get(k, "") for k, v in new.items()}, "linkedin",
                        f"{new['title']} @ {new['name']}".strip(" @"))
        if sure:
            last = new_last_name(f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip(),
                                 n.get("givenName", ""), n.get("familyName", ""))
            if last:
                plan.change(rn, "names", {"givenName": n["givenName"], "middleName": n.get("middleName", ""),
                                          "familyName": last}, "linkedin", f"{n.get('familyName')} -> {last}")


def birthdays_by_name(plan: Plan, rows: list) -> list:
    """Birthdays from name,month,day[,year] rows (e.g. old Facebook / XING
    birthday emails) for cards without one. Only an exact full-name match to
    exactly one card counts; returns the rows that matched nothing."""
    cards = defaultdict(list)
    for rn, p in plan.people.items():
        n = (p.get("names") or [{}])[0]
        full = fold(f"{n.get('givenName', '')} {n.get('familyName', '')}")
        if " " in full:
            cards[full].append(rn)
    names = defaultdict(list)
    for r in rows:
        names[fold(r["name"])].append(r)
    unmatched = []
    for name, found in names.items():
        rns = cards.get(name, [])
        dates = {(r["month"], r["day"]) for r in found}
        if len(rns) != 1 or len(dates) != 1:
            unmatched += found
            continue
        rn, r = rns[0], found[0]
        if plan.people[rn].get("birthdays"):
            continue
        date = {"month": int(r["month"]), "day": int(r["day"]), **({"year": int(r["year"])} if r.get("year") else {})}
        plan.change(rn, "birthdays", [{"date": date}], f"{r.get('source', 'list')} birthday email",
                    "-".join(str(date[k]) for k in ("year", "month", "day") if k in date))
    return unmatched


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("live", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("plan2.json"))
    ap.add_argument("--birthdays", type=Path)
    ap.add_argument("--signatures", type=Path)
    ap.add_argument("--whatsapp", type=Path)
    args = ap.parse_args(argv)

    plan = Plan(json.loads(args.live.read_text(encoding="utf-8")))
    streamline(plan)
    if args.birthdays:
        birthdays(plan, read_csv(args.birthdays))
    if args.signatures:
        signatures(plan, read_csv(args.signatures))
    maybe = whatsapp(plan, read_csv(args.whatsapp)) if args.whatsapp else []
    review = args.out.with_name(args.out.stem + "_review.csv")
    summary = plan.write(args.out, review)
    if maybe:
        with args.out.with_name(args.out.stem + "_last_names.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["name", "field", "change", "source"])
            w.writeheader()
            w.writerows(maybe)
    print(f"{args.out}: {summary['changes']} changes on {summary['contacts']} contacts; "
          f"{len(maybe)} last names to confirm" + f"; review: {review}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
