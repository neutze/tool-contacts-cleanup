#!/usr/bin/env python3
"""Turn a triaged review.csv into a clean Google CSV to re-import.

Takes review.csv from contacts_cleanup.py and writes into --out:

  import.csv          every row that is not a delete candidate, fixed, in the
                      original Google CSV columns (plus any it had to add)
  changes.csv         one line per change: row, field, old, new, why
  first_name_only.csv contacts that are only a first name, to fill in and
                      hand back with --names. With --whatsapp and/or
                      --name-suggestions it carries suggested last names;
                      nothing is applied until you copy one into last_name.

Fixes:
  - dead Google+ profile links removed
  - phone numbers without a country code given one (--country, default 49);
    the same number twice in one contact kept once
  - person names typed entirely in lower or entirely in upper case
    recapitalised; a mixed name such as "ADAC Pannenhilfe" is left alone
  - email addresses at --drop-email-domain removed (old student accounts);
    the contact stays
  - --names: last name / company filled in from the reviewed sheet, or the
    row dropped where its delete column says y
  - --linkedin: company, position, email and profile URL added from a
    LinkedIn Connections.csv, matched on full name, only where the field is
    empty; ambiguous names are skipped
  - --label-map: labels added by email domain or company
  - notes that are an old Facebook-sync dump (uid: ... profile_url: ...) cleared
  - phone numbers labelled "old number" removed
  - work addresses at --old-email-domain relabelled "old"

Finding what is missing (all suggestions; nothing applied until you say so):
  reach_suggestions.csv  for contacts with a name but no phone and no email:
                         a phone from a WhatsApp profile name that matches the
                         full name exactly (--whatsapp), an email found in
                         Gmail (--found-emails). Set apply to y and pass the
                         file back with --reach.
  groups.csv             with --whatsapp-groups: each WhatsApp group and how
                         many of your contacts are in it. Fill in label and
                         pass it back with --group-labels.

  plan.json              the same result as a list of deletes and per-field
                         updates, for people_sync.py to apply to the live
                         account without a re-import (keeps photos, stars, ids).

import.csv is the fallback: re-importing it means deleting everything in
Google Contacts first. Keep the backup; Settings -> Undo changes covers 30 days.

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from contacts_cleanup import DEAD_PROFILE, MULTI, fold, phone_key, real_name, split_multi
from people_sync import fingerprint

REVIEW_COLS = ("cleanup_row", "cleanup_source", "cleanup_flags", "dup_group", "suggested_action",
               "last_sent", "sent_via")
DEFAULT_DROP_DOMAINS = ("mytum.de", "in.tum.de", "net.in.tum.de", "fs.tum.de", "whu.edu")
PARTICLES = {"von", "van", "der", "den", "de", "zu", "zur", "da", "di", "du", "del", "dos", "la", "le", "ten", "ter"}
NAME_COLS = ("First Name", "Middle Name", "Last Name")
FACEBOOK_NOTE = re.compile(r"^\s*uid:\s*\d+.*profile_url:", re.S)
OLD_LABEL = re.compile(r"\bold\b", re.I)


class Row:
    def __init__(self, data: dict, log: list):
        self.d = data
        self.id = data.get("cleanup_row", "")
        self.log = log

    def set(self, col: str, new: str, why: str) -> None:
        old = self.d.get(col, "")
        if old != new:
            self.d[col] = new
            self.log.append({"row": self.id, "field": col, "old": old, "new": new, "why": why})

    def slots(self, kind: str) -> list:
        """Numbered column groups of one kind, e.g. 'E-mail' -> [1, 2, ...] present in the row."""
        pat = re.compile(rf"^{re.escape(kind)} (\d+) - Value$")
        return sorted(int(m.group(1)) for c in self.d if c and (m := pat.match(c)))

    def values(self, kind: str) -> list:
        return [v for n in self.slots(kind) for v in split_multi(self.d.get(f"{kind} {n} - Value", ""))]

    def add(self, kind: str, label: str, value: str, why: str) -> None:
        """Put value into the first empty slot of this kind, adding a slot if every one is taken."""
        slots = self.slots(kind)
        free = next((n for n in slots if not self.d.get(f"{kind} {n} - Value", "").strip()), None)
        n = free or (max(slots, default=0) + 1)
        self.d.setdefault(f"{kind} {n} - Label", "")
        self.set(f"{kind} {n} - Label", label, why)
        self.set(f"{kind} {n} - Value", value, why)

    def filter_values(self, kind: str, keep, why: str) -> None:
        for n in self.slots(kind):
            col = f"{kind} {n} - Value"
            vals = split_multi(self.d.get(col, ""))
            kept = [v for v in vals if keep(v)]
            if len(kept) != len(vals):
                self.set(col, MULTI.join(kept), why)
                if not kept:
                    self.set(f"{kind} {n} - Label", "", why)

    def map_values(self, kind: str, fn, why: str) -> None:
        for n in self.slots(kind):
            col = f"{kind} {n} - Value"
            vals = split_multi(self.d.get(col, ""))
            if vals:
                self.set(col, MULTI.join(fn(v) for v in vals), why)

    def drop_labelled(self, kind: str, label_pattern, why: str) -> None:
        """Clear every slot of this kind whose label matches, e.g. Phone labelled 'old number'."""
        for n in self.slots(kind):
            label = self.d.get(f"{kind} {n} - Label", "").replace("\u200e", "")
            if label_pattern.search(label) and self.d.get(f"{kind} {n} - Value", "").strip():
                self.set(f"{kind} {n} - Value", "", why)
                self.set(f"{kind} {n} - Label", "", why)

    def relabel(self, kind: str, match, label: str, why: str) -> None:
        for n in self.slots(kind):
            vals = split_multi(self.d.get(f"{kind} {n} - Value", ""))
            if vals and all(match(v) for v in vals):
                self.set(f"{kind} {n} - Label", label, why)

    def dedupe_phones(self) -> None:
        seen = set()

        def first_time(p):
            key = re.sub(r"\D", "", p)[-9:]
            if key in seen:
                return False
            seen.add(key)
            return True
        self.filter_values("Phone", first_time, "same number twice")


def normalize_phone(raw: str, country: str) -> str:
    """0151 2345678 and 0049 151 2345678 become +491512345678. A number that
    already starts with + is left alone, and so is one with no prefix at all:
    it could be local to anywhere."""
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+"):
        return raw.strip()
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0") and len(digits) > 5:
        return "+" + country + digits[1:]
    return raw.strip()


def recase(name: str) -> str:
    """Only touch a name written all in lower or all in upper case."""
    letters = [c for c in name if c.isalpha()]
    if not letters or not (name.islower() or (name.isupper() and len(letters) > 2)):
        return name
    words = name.lower().split(" ")
    return " ".join(w if i and w in PARTICLES else w.title() for i, w in enumerate(words))


def read_csv(path: Path) -> tuple:
    with path.open(newline="", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    # LinkedIn puts a few lines of notes above the header
    start = next((i for i, l in enumerate(lines) if l.startswith("First Name,")), 0)
    reader = csv.DictReader(lines[start:])
    return reader.fieldnames or [], list(reader)


def load_linkedin(path: Path) -> dict:
    _, rows = read_csv(path)
    by_name = defaultdict(list)
    for r in rows:
        name = fold(f"{r.get('First Name', '')} {r.get('Last Name', '')}")
        if " " in name:
            by_name[name].append(r)
    return {k: v[0] for k, v in by_name.items() if len(v) == 1}


def load_push_names(path: Path) -> dict:
    """phone key -> the name the person set on their own WhatsApp profile."""
    return {phone_key(r["key"]): r["push_name"].strip()
            for r in read_csv(path)[1] if r.get("push_name", "").strip() and phone_key(r["key"])}


def last_name_from(full: str, first: str) -> str:
    """'Anna Schmidt' with first name Anna -> 'Schmidt'. Only when the profile
    name starts with the contact's first name and has more after it."""
    words = full.split()
    if len(words) >= 2 and fold(words[0]) == fold(first):
        rest = " ".join(words[1:])
        return rest if not re.search(r"[\d@]", rest) else ""
    return ""


def suggestions_for(data: dict, push: dict, mail: dict) -> dict:
    first = data.get("First Name", "").strip()
    wa_name = next((push[k] for k in (phone_key(p) for p in Row(data, []).values("Phone")) if k in push), "")
    found = mail.get(data.get("cleanup_row", ""), {})
    if found.get("suggested_last_name"):
        last, source = found["suggested_last_name"], f"gmail, {found.get('confidence', '')}: {found.get('evidence', '')}"
    elif last_name_from(wa_name, first):
        last, source = last_name_from(wa_name, first), "whatsapp profile name"
    else:
        last, source = "", ""
    return {"whatsapp_name": wa_name, "suggested_last_name": last,
            "suggested_company": found.get("suggested_company", ""), "suggested_from": source}


def load_reverse_push(path: Path) -> dict:
    """folded full name -> phone, only for names that belong to exactly one number."""
    by_name = defaultdict(set)
    for r in read_csv(path)[1]:
        name = fold(r.get("push_name", ""))
        if " " in name and phone_key(r["key"]):
            by_name[name].add(r["key"].strip())
    return {k: next(iter(v)) for k, v in by_name.items() if len(v) == 1}


def load_groups(path: Path) -> dict:
    """phone key -> set of WhatsApp group names."""
    groups = defaultdict(set)
    for r in read_csv(path)[1]:
        if phone_key(r["key"]) and r.get("group", "").strip():
            groups[phone_key(r["key"])].add(r["group"].strip())
    return groups


# Which People API field a changed CSV column belongs to
CATEGORY = [(re.compile(r"^(First|Middle|Last) Name$"), "names"),
            (re.compile(r"^E-mail \d+ - "), "emailAddresses"),
            (re.compile(r"^Phone \d+ - "), "phoneNumbers"),
            (re.compile(r"^Website \d+ - "), "urls"),
            (re.compile(r"^Organization (Name|Title)$"), "organizations"),
            (re.compile(r"^Notes$"), "biographies")]


def row_fingerprint(data: dict) -> str:
    r = Row(data, [])
    return fingerprint(data.get("First Name", ""), data.get("Middle Name", ""), data.get("Last Name", ""),
                       r.values("E-mail"), r.values("Phone"), r.values("Website"))


def slot_items(row: "Row", kind: str) -> list:
    return [{"value": v, "type": row.d.get(f"{kind} {n} - Label", "")}
            for n in row.slots(kind) for v in split_multi(row.d.get(f"{kind} {n} - Value", ""))]


def plan_update(row: "Row", entries: list) -> dict:
    """The People API fields to write for this row, from the columns that changed."""
    cats = {cat for e in entries for pat, cat in CATEGORY if pat.match(e["field"])}
    d, out = row.d, {}
    if "names" in cats:
        out["names"] = {"givenName": d.get("First Name", ""), "middleName": d.get("Middle Name", ""),
                        "familyName": d.get("Last Name", "")}
    for cat, kind in (("emailAddresses", "E-mail"), ("phoneNumbers", "Phone"), ("urls", "Website")):
        if cat in cats:
            out[cat] = slot_items(row, kind)
    if "organizations" in cats:
        out["organizations"] = {"name": d.get("Organization Name", ""), "title": d.get("Organization Title", "")}
    if "biographies" in cats:
        out["biographies"] = d.get("Notes", "")
    return out


def load_label_map(path: Path) -> list:
    """Lines of kind,pattern,label. kind is domain (matches the address's
    domain or any parent of it) or org (case-insensitive substring)."""
    _, rows = read_csv(path)
    return [(r["kind"].strip(), r["pattern"].strip().lower(), r["label"].strip()) for r in rows]


def labels_for(row: Row, rules: list) -> list:
    domains = {e.lower().split("@")[-1] for e in row.values("E-mail") if "@" in e}
    org = row.d.get("Organization Name", "").lower()
    out = []
    for kind, pattern, label in rules:
        hit = (kind == "domain" and any(d == pattern or d.endswith("." + pattern) for d in domains)) or \
              (kind == "org" and pattern in org)
        if hit and label not in out:
            out.append(label)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("review", type=Path, help="review.csv from contacts_cleanup.py")
    ap.add_argument("-o", "--out", type=Path, default=Path("fix_out"))
    ap.add_argument("--country", default="49", help="country code for numbers starting with 0 (default: 49)")
    ap.add_argument("--drop-email-domain", action="append", metavar="DOMAIN",
                    help=f"remove addresses at this domain (repeatable; default: {', '.join(DEFAULT_DROP_DOMAINS)})")
    ap.add_argument("--names", type=Path, help="first_name_only.csv with last_name / company / delete filled in")
    ap.add_argument("--linkedin", type=Path, help="LinkedIn Connections.csv")
    ap.add_argument("--label-map", type=Path, help="CSV of kind,pattern,label")
    ap.add_argument("--whatsapp", type=Path, help="whatsapp_activity.csv, for profile names")
    ap.add_argument("--name-suggestions", type=Path,
                    help="CSV of cleanup_row,suggested_last_name,suggested_company,confidence,evidence")
    ap.add_argument("--found-emails", type=Path, help="CSV of cleanup_row,name,email,last_seen,evidence")
    ap.add_argument("--reach", type=Path, help="reach_suggestions.csv with apply = y on the rows to add")
    ap.add_argument("--whatsapp-groups", type=Path, help="whatsapp_groups.csv (group,key)")
    ap.add_argument("--group-labels", type=Path, help="groups.csv with a label filled in per group")
    ap.add_argument("--old-email-domain", action="append", default=[], metavar="DOMAIN",
                    help="relabel addresses at this domain as old (repeatable), e.g. a former employer")
    args = ap.parse_args(argv)

    header, rows = read_csv(args.review)
    if "suggested_action" not in header:
        print("Expected review.csv from contacts_cleanup.py.", file=sys.stderr)
        return 1
    drop_domains = tuple(d.lower() for d in (args.drop_email_domain or DEFAULT_DROP_DOMAINS))
    names = {}
    if args.names:
        names = {r["cleanup_row"]: r for r in read_csv(args.names)[1]}
    linkedin = load_linkedin(args.linkedin) if args.linkedin else {}
    rules = load_label_map(args.label_map) if args.label_map else []
    push = load_push_names(args.whatsapp) if args.whatsapp else {}
    mail = {r["cleanup_row"]: r for r in read_csv(args.name_suggestions)[1]} if args.name_suggestions else {}
    reverse_push = load_reverse_push(args.whatsapp) if args.whatsapp else {}
    found_emails = {r["cleanup_row"]: r for r in read_csv(args.found_emails)[1]} if args.found_emails else {}
    reach = defaultdict(list)
    if args.reach:
        for r in read_csv(args.reach)[1]:
            if r.get("apply", "").strip().lower() in ("y", "yes", "x"):
                reach[r["cleanup_row"]].append(r)
    groups = load_groups(args.whatsapp_groups) if args.whatsapp_groups else {}
    group_labels = {}
    if args.group_labels:
        group_labels = {r["group"]: r["label"].strip() for r in read_csv(args.group_labels)[1]
                        if r.get("label", "").strip()}
    old_domains = tuple(d.lower() for d in args.old_email_domain)
    reach_rows, group_count = [], defaultdict(int)

    log, out_rows, first_only = [], [], []
    plan = {"version": 1, "export_counts": defaultdict(int), "deletes": [], "updates": []}
    for data in rows:
        plan["export_counts"][row_fingerprint(data)] += 1
    for data in rows:
        row = Row(data, log)
        fp, start = row_fingerprint(data), len(log)
        shown = " ".join(x for x in (data.get("First Name", ""), data.get("Last Name", "")) if x.strip()) \
            or (row.values("E-mail") or row.values("Phone") or [""])[0]
        if data["suggested_action"] == "delete-candidate":
            plan["deletes"].append({"fp": fp, "name": shown, "action": data.get("cleanup_flags", "")})
            continue
        review = names.get(row.id)
        if review and review.get("delete", "").strip().lower() in ("y", "yes", "x"):
            log.append({"row": row.id, "field": "(row)", "old": data.get("First Name", ""), "new": "",
                        "why": "marked delete in names sheet"})
            plan["deletes"].append({"fp": fp, "name": shown, "action": "names sheet"})
            continue

        labels = labels_for(row, rules)  # before student addresses go, so TUM / WHU still match
        row.filter_values("Website", lambda w: not DEAD_PROFILE.match(w), "dead Google+ link")
        row.filter_values("E-mail", lambda e: not e.lower().split("@")[-1].endswith(drop_domains),
                          "old student address")
        row.drop_labelled("Phone", OLD_LABEL, "labelled old number")
        row.map_values("Phone", lambda p: normalize_phone(p, args.country), "country code added")
        row.dedupe_phones()
        if FACEBOOK_NOTE.match(data.get("Notes", "")):
            row.set("Notes", "", "old Facebook-sync note")
        if old_domains:
            row.relabel("E-mail", lambda e: e.lower().split("@")[-1].endswith(old_domains), "old",
                        "former employer address")
        for col in NAME_COLS:
            if col in data:
                row.set(col, data[col].strip(), "stray spaces")
        full = f"{data.get('First Name', '')} {data.get('Last Name', '')}"
        # Only when the whole name is typed one way: "ADAC Pannenhilfe" keeps its acronym.
        if real_name(data.get("First Name", ""), data.get("Last Name", "")) and (full.islower() or full.isupper()):
            for col in ("First Name", "Last Name"):
                row.set(col, recase(data[col]), "capitalisation")

        if review:
            for src, col in (("last_name", "Last Name"), ("company", "Organization Name")):
                if review.get(src, "").strip():
                    row.set(col, review[src].strip(), "names sheet")

        person = linkedin.get(fold(f"{data.get('First Name', '')} {data.get('Last Name', '')}"))
        if person:
            if not data.get("Organization Name", "").strip() and person.get("Company"):
                row.set("Organization Name", person["Company"], "LinkedIn")
            if not data.get("Organization Title", "").strip() and person.get("Position"):
                row.set("Organization Title", person["Position"], "LinkedIn")
            email = (person.get("Email Address") or "").strip().lower()
            if email and email not in (e.lower() for e in row.values("E-mail")):
                row.add("E-mail", "LinkedIn", email, "LinkedIn")
            url = (person.get("URL") or "").strip()
            if url and url not in row.values("Website"):
                row.add("Website", "LinkedIn", url, "LinkedIn")
            rules_hit = ["LinkedIn"]
        else:
            rules_hit = []

        for r in reach.get(row.id, []):
            if r.get("phone", "").strip():
                row.add("Phone", "Mobile", r["phone"].strip(), f"found: {r.get('source', '')}")
            if r.get("email", "").strip():
                row.add("E-mail", "Other", r["email"].strip(), f"found: {r.get('source', '')}")

        member_of = set()
        for k in {phone_key(p) for p in row.values("Phone")} - {""}:
            member_of |= groups.get(k, set())
        for g in member_of:
            group_count[g] += 1
        rules_hit += [group_labels[g] for g in sorted(member_of) if g in group_labels]

        if not row.values("Phone") and not row.values("E-mail") and real_name(
                data.get("First Name", ""), data.get("Last Name", "")):
            name = f"{data['First Name']} {data['Last Name']}"
            phone = reverse_push.get(fold(name), "")
            found = found_emails.get(row.id, {})
            if phone or found.get("email"):
                reach_rows.append({"cleanup_row": row.id, "name": name, "phone": phone,
                                   "email": found.get("email", ""),
                                   "source": "; ".join(x for x in (
                                       "whatsapp profile name" if phone else "",
                                       f"gmail, last seen {found.get('last_seen', '')}" if found.get("email") else "")
                                       if x),
                                   "apply": ""})

        new_labels = [l for l in dict.fromkeys(labels + rules_hit)
                      if l not in [x.lstrip("* ").strip() for x in split_multi(data.get("Labels", ""))]]
        if new_labels:
            row.set("Labels", MULTI.join(split_multi(data.get("Labels", "")) + new_labels), "label")

        entries = log[start:]
        update = plan_update(row, entries)
        old_labels = set(split_multi(next((e["old"] for e in entries if e["field"] == "Labels"), data.get("Labels", ""))))
        added_labels = [l for l in split_multi(data.get("Labels", "")) if l not in old_labels]
        if update or added_labels:
            plan["updates"].append({"fp": fp, "name": shown, "set": update, "add_labels": added_labels})

        first, last = data.get("First Name", "").strip(), data.get("Last Name", "").strip()
        # "Charles Njofack" in First Name is a full name in the wrong field, not a first name only
        if first and " " not in first and not last and not data.get("Organization Name", "").strip():
            first_only.append(data)
        out_rows.append(data)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    google_cols = [c for c in header if c not in REVIEW_COLS]
    added = [c for r in out_rows for c in r if c and c not in header]
    google_cols += list(dict.fromkeys(added))
    write(out / "import.csv", google_cols, out_rows)
    write(out / "changes.csv", ["row", "field", "old", "new", "why"], log)
    (out / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    sheet = [{"cleanup_row": r["cleanup_row"], "first_name": r.get("First Name", ""),
              "emails": "; ".join(Row(r, []).values("E-mail")), "phones": "; ".join(Row(r, []).values("Phone")),
              "notes": r.get("Notes", ""), **suggestions_for(r, push, mail),
              "last_name": "", "company": "", "delete": ""} for r in first_only]
    write(out / "first_name_only.csv",
          ["cleanup_row", "first_name", "emails", "phones", "notes", "whatsapp_name", "suggested_last_name",
           "suggested_company", "suggested_from", "last_name", "company", "delete"], sheet)
    suggested = sum(1 for r in sheet if r["suggested_last_name"])
    write(out / "reach_suggestions.csv", ["cleanup_row", "name", "phone", "email", "source", "apply"], reach_rows)
    if groups:
        write(out / "groups.csv", ["group", "contacts", "label"],
              [{"group": g, "contacts": n, "label": group_labels.get(g, "")}
               for g, n in sorted(group_count.items(), key=lambda kv: -kv[1])])

    why = defaultdict(set)
    for c in log:
        why[c["why"]].add(c["row"])
    print(f"import.csv: {len(out_rows)} of {len(rows)} rows; "
          f"plan.json: {len(plan['deletes'])} deletes, {len(plan['updates'])} updates")
    for k, v in sorted(why.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(v):4} rows  {k}")
    print(f"first_name_only.csv: {len(sheet)} rows to fill in, {suggested} with a suggested last name")
    print(f"reach_suggestions.csv: {len(reach_rows)} name-only contacts with a phone or email found")
    if groups:
        print(f"groups.csv: {len(group_count)} WhatsApp groups with at least one of your contacts")
    return 0


def write(path: Path, header: list, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
