#!/usr/bin/env python3
"""Offline triage for a Google Contacts CSV export.

Reads one or more Google CSV exports (My Contacts, Other contacts), never
touches the live account, and writes:

  report.md   what is in there: import-label clusters, auto-harvested rows,
              shared domains and notes, likely duplicates
  review.csv  every row with its flags, duplicate group and suggested action
  cleaned.csv only with --drop-label / --drop-flag: the export minus the
              dropped rows, same columns, ready to re-import

--activity takes CSVs of key,channel,sent,last_sent (key = phone or email,
sent = messages you sent, last_sent = YYYY-MM-DD), e.g. from
whatsapp_activity.py. A row you wrote to since --active-since is kept,
whatever else is flagged on it. The one exception to "activity only keeps":
with activity files given, an email-only row you have not written to since
--active-since is flagged stale and becomes a delete candidate.

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

MULTI = " ::: "
EMAIL_COL = re.compile(r"^E-mail \d+ - Value$")
PHONE_COL = re.compile(r"^Phone \d+ - Value$")
WEBSITE_COL = re.compile(r"^Website \d+ - Value$")
# Google+ circles were synced into Contacts as a name plus a profile link;
# the profiles died with Google+ in 2019.
# Google+ also synced the brand pages you followed; their names give them away.
BRAND_WORDS = re.compile(
    r"\b(google\+?|youtube|android|developers?|business|official|online|news|magazine?|blog|tv|apps?|"
    r"games?|community|store|shop|media|music|records|club|gmbh|inc|ltd|world|channel|studio|labs?|daily)\b",
    re.I)
DEAD_PROFILE = re.compile(r"^(https?://)?(www\.)?(google\.com/profiles/|plus\.google\.com/)", re.I)
NOREPLY = re.compile(
    r"(^|[._+-])(no-?reply|do-?not-?reply|notifications?|mailer-daemon|bounces?|"
    r"postmaster|newsletters?|alerts?)([._+-]|$)",
    re.I,
)
NOREPLY_DOMAIN = re.compile(r"^(no-?reply|reply|bounces?|notifications?|mailer)\.", re.I)
# Digits glued into a word ("W33d", "Airbnb2") or emoji: a handle, not a name.
ODD_NAME = re.compile(r"[A-Za-zÀ-ÿ]\d|\d[A-Za-zÀ-ÿ]|[\U0001F000-\U0001FAFF\u2600-\u27BF]")
IMPORT_LABEL = re.compile(r"import", re.I)
SYSTEM_LABELS = {"mycontacts", "my contacts", "starred", "friends", "family", "coworkers"}
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.de", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "web.de", "gmx.de", "gmx.net", "t-online.de",
    "aol.com", "protonmail.com", "proton.me",
}
# One name part: letter-words joined by space, hyphen, apostrophe or dot,
# optionally followed by a parenthetical such as a maiden name.
NAME_PART = re.compile(r"^[^\W\d_]+(?:[-'’. ]+[^\W\d_]+)*\.?(?:\s*\([^\W\d_][^()\d]*\))?$")
FLAGS = ("empty", "unreachable", "odd_name", "noreply", "dead_profile", "brand_page", "email_only", "no_contact", "real_name", "no_name", "no_phone",
         "no_email", "import_label", "active", "stale")


@dataclass
class Contact:
    idx: int
    source: str
    row: dict
    name: str = ""
    first: str = ""
    last: str = ""
    emails: list = field(default_factory=list)
    phones: list = field(default_factory=list)
    websites: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    org: str = ""
    notes: str = ""
    flags: list = field(default_factory=list)
    dup_group: int = 0
    dup_reasons: set = field(default_factory=set)
    other: bool = False  # anything else on the card: company, notes, address, birthday
    last_sent: str = ""
    channels: set = field(default_factory=set)

    @property
    def action(self) -> str:
        return self.base_action if "active" not in self.flags or self.base_action == "merge-review" else "keep"

    @property
    def base_action(self) -> str:
        """The action without the activity signal, so the report can show what activity rescued."""
        f = set(self.flags)
        if {"empty", "unreachable", "noreply", "stale", "brand_page", "odd_name"} & f or ("dead_profile" in f and "real_name" not in f):
            return "delete-candidate"
        if self.dup_group:
            return "merge-review"
        if "email_only" in f or ("no_contact" in f and "real_name" not in f):
            return "review"
        return "keep"

    def display(self) -> str:
        return self.name or (self.emails[0] if self.emails else "") or (self.phones[0] if self.phones else "") or "(blank)"


def split_multi(value: str) -> list:
    return [v.strip() for v in (value or "").split(MULTI) if v.strip()]


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


def phone_key(raw: str) -> str:
    """Digits only, international prefix dropped, last 9 digits kept so that
    +49 151 234 and 0151 234 land on the same key."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) < 6:
        return ""
    return digits[-9:]


def contact_name(row: dict) -> str:
    if row.get("Name", "").strip():
        return row["Name"].strip()
    parts = [row.get(k, "") for k in ("First Name", "Middle Name", "Last Name")]
    if not any(p.strip() for p in parts):
        parts = [row.get(k, "") for k in ("Given Name", "Additional Name", "Family Name")]
    name = " ".join(p.strip() for p in parts if p.strip())
    name = name or row.get("File As", "").strip() or row.get("Nickname", "").strip()
    # Gmail's auto-added contacts often carry the address itself as the name
    return "" if "@" in name else name


def parse(row: dict, idx: int, source: str) -> Contact:
    c = Contact(idx=idx, source=source, row=row)
    c.name = contact_name(row)
    c.first = (row.get("First Name") or row.get("Given Name") or "").strip()
    c.last = (row.get("Last Name") or row.get("Family Name") or "").strip()
    for col, val in row.items():
        if col is None:
            continue
        if EMAIL_COL.match(col):
            c.emails += [e.lower() for e in split_multi(val)]
        elif PHONE_COL.match(col):
            c.phones += split_multi(val)
        elif WEBSITE_COL.match(col):
            c.websites += split_multi(val)
    c.labels = [l.lstrip("* ").strip() for l in split_multi(row.get("Labels") or row.get("Group Membership") or "")]
    c.org = (row.get("Organization Name") or row.get("Organization 1 - Name") or "").strip()
    c.notes = (row.get("Notes") or "").strip()
    c.other = bool(c.org or c.notes or (row.get("Birthday") or "").strip() or any(
        (v or "").strip() for k, v in row.items() if k and re.match(r"^Address \d+ - ", k)))
    return c


def real_name(first: str, last: str) -> bool:
    """A first and a last name, each at least two letters, with no digits,
    handles or symbols. An initial alone does not count."""
    return all(NAME_PART.match(p) and sum(ch.isalpha() for ch in p.split("(")[0]) >= 2 for p in (first, last))


def is_noreply(email: str) -> bool:
    """no-reply in the local part, or a sending domain such as noreply.github.com / reply.github.com."""
    local, _, domain = email.partition("@")
    return bool(NOREPLY.search(local) or NOREPLY_DOMAIN.match(domain))


def flag(c: Contact) -> None:
    f = c.flags
    if not c.name and not c.emails and not c.phones and not c.websites and not c.other:
        f.append("empty")
    if any(is_noreply(e) for e in c.emails):
        f.append("noreply")
    if not c.emails and not c.phones and c.websites and all(DEAD_PROFILE.match(w) for w in c.websites):
        f.append("dead_profile")
    if c.name and (ODD_NAME.search(c.name) or len(re.sub(r"\W", "", c.name)) <= 2):
        f.append("odd_name")
    if "dead_profile" in f and BRAND_WORDS.search(c.name):
        f.append("brand_page")
    if not c.name and not c.phones and c.emails:
        f.append("email_only")
    if c.name and not c.emails and not c.phones and "dead_profile" not in f:
        f.append("no_contact")
    if real_name(c.first, c.last):
        f.append("real_name")
    if not c.name:
        f.append("no_name")
    if not c.phones:
        f.append("no_phone")
    if not c.emails:
        f.append("no_email")
    if any(IMPORT_LABEL.search(l) for l in c.labels):
        f.append("import_label")


def activity_key(raw: str) -> str:
    raw = raw.strip()
    return raw.lower() if "@" in raw else phone_key(raw)


def load_activity(paths: list) -> dict:
    """key -> (last_sent, channels) over every file, keeping the latest date."""
    seen = {}
    for p in paths:
        with Path(p).open(newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                key = activity_key(r.get("key", ""))
                if not key or int(r.get("sent") or 0) < 1:
                    continue
                last, channels = seen.get(key, ("", set()))
                seen[key] = (max(last, r.get("last_sent", "")), channels | {r.get("channel", "?")})
    return seen


def mark_unreachable(contacts: list) -> None:
    """--delete-unreachable: a card with no phone and no email can't be used to reach anyone."""
    for c in contacts:
        if not c.emails and not c.phones and "empty" not in c.flags:
            c.flags.append("unreachable")


def apply_activity(contacts: list, activity: dict, since: str) -> None:
    for c in contacts:
        for key in [e.lower() for e in c.emails] + [phone_key(p) for p in c.phones]:
            if key in activity:
                last, channels = activity[key]
                c.last_sent = max(c.last_sent, last)
                c.channels |= channels
        if c.last_sent and c.last_sent >= since:
            c.flags.append("active")
        elif "email_only" in c.flags:
            # Gmail used to save everyone you emailed; no recent mail means nobody you still write to.
            c.flags.append("stale")


def same_person_name(a: Contact, b: Contact) -> bool:
    """Could these two names belong to one person? Blank, equal, one inside
    the other ("Eva Roth (Attorney)"), or the same last name with one
    first name starting the other ("Dimi" / "Dimitris")."""
    x, y = fold(a.name), fold(b.name)
    if not x or not y or x == y or x in y or y in x:
        return True
    return nickname_pair(a, b)


def nickname_pair(a: Contact, b: Contact) -> bool:
    fa, fb, la, lb = fold(a.first), fold(b.first), fold(a.last), fold(b.last)
    if not (fa and fb and la and la == lb) or fa == fb:
        return False
    short, long_ = sorted((fa, fb), key=len)
    return len(short) >= 3 and long_.startswith(short)


def find_duplicates(contacts: list) -> list:
    parent = list(range(len(contacts)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    keys = defaultdict(list)
    for i, c in enumerate(contacts):
        for e in set(c.emails):
            keys[("email", e)].append(i)
        for p in {phone_key(p) for p in c.phones} - {""}:
            keys[("phone", p)].append(i)
        if c.name and " " in c.name:  # single first names collide too often
            keys[("name", fold(c.name))].append(i)

    by_last = defaultdict(list)
    for i, c in enumerate(contacts):
        if c.first and c.last:
            by_last[fold(c.last)].append(i)
    for members in by_last.values():
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                if nickname_pair(contacts[members[x]], contacts[members[y]]):
                    keys[("nickname", members[x], members[y])] += [members[x], members[y]]

    for (kind, *_), members in keys.items():
        if len(members) < 2:
            continue
        for m in members[1:]:
            # A shared landline is a household, not one person: two people at one home number keep separate cards.
            if kind == "phone" and not same_person_name(contacts[m], contacts[members[0]]):
                continue
            for k in (m, members[0]):
                contacts[k].dup_reasons.add(kind)
            parent[root(m)] = root(members[0])

    groups = defaultdict(list)
    for i, c in enumerate(contacts):
        if c.dup_reasons:
            groups[root(i)].append(c)
    ordered = sorted((g for g in groups.values() if len(g) > 1), key=lambda g: -len(g))
    for n, g in enumerate(ordered, 1):
        for c in g:
            c.dup_group = n
    return ordered


def md_table(header: list, rows: list) -> str:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows]
    return "\n".join(out)


def report(contacts: list, dups: list, top: int, since: str = "") -> str:
    n = len(contacts)
    flags = Counter(f for c in contacts for f in c.flags)
    actions = Counter(c.action for c in contacts)
    sources = Counter(c.source for c in contacts)

    labels = defaultdict(list)
    for c in contacts:
        for l in c.labels:
            if l.lower() not in SYSTEM_LABELS:
                labels[l].append(c)

    domains = Counter()
    for c in contacts:
        for d in {e.split("@")[-1] for e in c.emails if "@" in e}:
            if d not in FREEMAIL:
                domains[d] += 1

    notes = Counter(fold(c.notes) for c in contacts if c.notes)

    def profile(group):
        k = len(group)
        pct = lambda f: f"{100 * sum(f in c.flags for c in group) // k}%"
        return pct("no_name"), pct("no_phone"), pct("email_only")

    s = [f"# Contacts cleanup report\n", f"**{n} contacts** from " + ", ".join(f"`{k}` ({v})" for k, v in sources.items()) + ".\n"]

    s.append("## Suggested actions\n")
    s.append(md_table(["Action", "Count", "Meaning"], [
        ["delete-candidate", actions["delete-candidate"],
         "blank, a no-reply / notifications / newsletter address, a dead Google+ profile link "
         "and no proper first + last name or a brand name, or email-only and not written to recently (stale)"],
        ["merge-review", actions["merge-review"], "shares an email, phone or full name with another row: Merge & fix (Step 6)"],
        ["review", actions["review"],
         "email only (typical auto-harvested row), or no email and no phone and no proper first + last name"],
        ["keep", actions["keep"], "nothing suspicious, a proper first + last name, or you wrote to them recently"],
    ]) + "\n")

    s.append("## Flags\n")
    s.append(md_table(["Flag", "Count"], [[f, flags[f]] for f in FLAGS]) + "\n")

    if since:
        matched = [c for c in contacts if c.last_sent]
        rescued = [c for c in contacts if c.action == "keep" and c.base_action != "keep"]
        s.append(f"## Activity: you wrote to them since {since}\n")
        s.append(f"{len(matched)} rows matched an activity file, {flags['active']} of them recently. "
                 f"{len(rescued)} rows were flagged but are kept because you wrote to them.\n")
        if rescued:
            s.append(md_table(["Row", "Shown as", "Would have been", "Last sent", "Via"],
                              [[c.idx, c.display(), c.base_action, c.last_sent, ", ".join(sorted(c.channels))]
                               for c in rescued[:top]]) + "\n")

    s.append("## Label clusters (Step 3)\n")
    s.append("A label where most rows have no name or no phone is the mystery batch. "
             "Delete it in Google Contacts by opening the label, selecting all, and deleting (Step 7).\n")
    if labels:
        rows = sorted(labels.items(), key=lambda kv: -len(kv[1]))
        s.append(md_table(["Label", "Rows", "No name", "No phone", "Email only", "Import label?"],
                          [[l, len(g), *profile(g), "yes" if IMPORT_LABEL.search(l) else ""] for l, g in rows]) + "\n")
    else:
        s.append("_No user labels in this export._\n")

    s.append(f"## Company domains with the most rows (Step 5, top {top})\n")
    s.append("Free-mail domains are left out. One domain with many nameless rows usually means a mailing list or CRM import.\n")
    s.append(md_table(["Domain", "Rows"], domains.most_common(top)) + "\n" if domains else "_None._\n")

    shared = [(t, k) for t, k in notes.most_common(top) if k > 1]
    s.append("## Notes shared by more than one row (Step 5)\n")
    s.append(md_table(["Note", "Rows"], [[t[:80], k] for t, k in shared]) + "\n" if shared else "_None._\n")

    s.append(f"## Duplicate groups (Step 6): {len(dups)} groups\n")
    s.append("Google's Merge & fix catches most of these. Check the ones it misses here; a `name`-only match can be two different people.\n")
    for i, g in enumerate(dups[:top], 1):
        reasons = ", ".join(sorted(set().union(*(c.dup_reasons for c in g))))
        s.append(f"**Group {i}** ({len(g)} rows, matched on {reasons})\n")
        s.append(md_table(["Row", "Source", "Name", "Emails", "Phones"],
                          [[c.idx, c.source, c.name, "; ".join(c.emails), "; ".join(c.phones)] for c in g]) + "\n")
    if len(dups) > top:
        s.append(f"_{len(dups) - top} more groups in `review.csv` (column `dup_group`)._\n")

    junk = [c for c in contacts if c.action == "delete-candidate"]
    s.append(f"## Delete candidates (first {top} of {len(junk)})\n")
    s.append(md_table(["Row", "Source", "Shown as", "Flags"],
                      [[c.idx, c.source, c.display(), ", ".join(c.flags)] for c in junk[:top]]) + "\n" if junk else "_None._\n")
    return "\n".join(s)


def write_csv(path: Path, header: list, rows: list) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load(paths: list) -> tuple:
    contacts, headers = [], {}
    for p in paths:
        with Path(p).open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers[p] = reader.fieldnames or []
            for row in reader:
                contacts.append(parse(row, len(contacts) + 2, Path(p).name))
    return contacts, headers


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="+", help="Google CSV export(s); pass My Contacts and Other contacts together")
    ap.add_argument("-o", "--out", default="cleanup_out", help="output folder (default: cleanup_out)")
    ap.add_argument("--top", type=int, default=25, help="rows per report section (default: 25)")
    ap.add_argument("--drop-label", action="append", default=[], metavar="LABEL",
                    help="leave rows with this label out of cleaned.csv (repeatable, exact match)")
    ap.add_argument("--drop-flag", action="append", default=[], choices=FLAGS,
                    help="leave rows with this flag out of cleaned.csv (repeatable)")
    ap.add_argument("--delete-unreachable", action="store_true",
                    help="make every card without a phone and an email a delete candidate")
    ap.add_argument("--activity", action="append", default=[], metavar="CSV",
                    help="activity file(s): key,channel,sent,last_sent (repeatable)")
    ap.add_argument("--active-since", metavar="YYYY-MM-DD",
                    default=(dt.date.today() - dt.timedelta(days=3 * 365)).isoformat(),
                    help="activity on or after this date keeps a row (default: 3 years ago)")
    args = ap.parse_args(argv)

    contacts, headers = load(args.csv)
    if not contacts:
        print("No rows found.", file=sys.stderr)
        return 1
    for c in contacts:
        flag(c)
    if args.delete_unreachable:
        mark_unreachable(contacts)
    if args.activity:
        apply_activity(contacts, load_activity(args.activity), args.active_since)
    dups = find_duplicates(contacts)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report(contacts, dups, args.top, args.active_since if args.activity else ""), encoding="utf-8")

    first_header = next(iter(headers.values()))
    extra = ["cleanup_row", "cleanup_source", "cleanup_flags", "dup_group", "suggested_action", "last_sent", "sent_via"]
    review_rows = [{**c.row, "cleanup_row": c.idx, "cleanup_source": c.source, "cleanup_flags": " ".join(c.flags),
                    "dup_group": c.dup_group or "", "suggested_action": c.action,
                    "last_sent": c.last_sent, "sent_via": " ".join(sorted(c.channels))} for c in contacts]
    write_csv(out / "review.csv", extra + first_header, review_rows)

    if args.drop_label or args.drop_flag:
        if len({tuple(h) for h in headers.values()}) > 1:
            print("cleaned.csv needs inputs with identical columns; run once per export.", file=sys.stderr)
            return 2
        drop_labels = {l.lstrip("* ").strip() for l in args.drop_label}
        kept = [c for c in contacts
                if not (drop_labels & set(c.labels)) and not (set(args.drop_flag) & set(c.flags))]
        write_csv(out / "cleaned.csv", first_header, [c.row for c in kept])
        print(f"cleaned.csv: kept {len(kept)} of {len(contacts)} rows")

    print(f"{len(contacts)} contacts, {len(dups)} duplicate groups -> {out}/report.md, {out}/review.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
