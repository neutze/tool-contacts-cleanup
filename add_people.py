"""Add people from a list (a class list, a guest list) to Google Contacts.

Input is a CSV with the columns given, family, maiden, email, phone,
facebook, linkedin, and optionally landline, street, city and email_type /
phone_type (e.g. "home (2009)" to mark data from an old list). Any may be
empty. Someone already in your contacts,
matched by email, phone or full name (also under the maiden name), gets
what's missing: email, phone, links, the label, and their married name if
the list has one. Everyone else with an email or a phone becomes a new
contact. Rows with only a name are skipped.

  python3 add_people.py people.csv --label "Kollegstufe" --client-secret ... [--apply]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import people_sync as ps

GERMAN_MOBILE = ("15", "16", "17")


def phone(raw: str, country: str = "49") -> str:
    """Numbers as people type them into a shared sheet: 0176/…, 0049…, 4917…,
    or a German mobile with its leading 0 lost (17670…)."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if raw.strip().startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+" + country + digits[1:]
    if digits.startswith(country) and len(digits) >= 11:
        return "+" + digits
    if digits.startswith(GERMAN_MOBILE) and len(digits) in (10, 11):
        return "+" + country + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits  # North American number
    if country == "49" and 6 <= len(digits) <= 11:
        return "+" + country + digits  # a German area code that lost its 0, e.g. 8122/5337
    return "+" + digits


def clean_url(url: str) -> str:
    url = url.strip()
    url = re.sub(r"(linkedin\.com/in/[^/]+)/.*$", r"\1/", url)
    return url


def read(path: Path) -> list:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def match(row: dict, people: list):
    """Email, then mobile, then full name. A landline only counts together with
    the name: a parents' number is shared by siblings."""
    email = row.get("email", "").strip().lower()
    mobile = ps.phone_key(phone(row.get("phone", "")))
    landline = ps.phone_key(phone(row.get("landline", "")))
    names = {ps.fold(f"{row.get('given', '')} {row.get(k, '')}") for k in ("family", "maiden") if row.get(k)}

    def name_of(p):
        n = (p.get("names") or [{}])[0]
        return ps.fold(f"{n.get('givenName', '')} {n.get('familyName', '')}")

    def phones_of(p):
        return {ps.phone_key(x["value"]) for x in p.get("phoneNumbers", [])}

    for test in (lambda p: email and email in {e["value"].lower() for e in p.get("emailAddresses", [])},
                 lambda p: mobile and mobile in phones_of(p),
                 lambda p: name_of(p) in names):
        found = [p for p in people if test(p)]
        if len(found) == 1:
            return found[0]
    found = [p for p in people if landline and landline in phones_of(p) and name_of(p) in names]
    return found[0] if len(found) == 1 else None


def person_body(row: dict, country: str) -> dict:
    body = {"names": [{"givenName": row["given"], "familyName": row.get("family") or row.get("maiden", "")}]}
    if row.get("email"):
        body["emailAddresses"] = [{"value": row["email"].strip(), "type": row.get("email_type") or "home"}]
    phones = [{"value": phone(row[k], country), "type": row.get(f"{k}_type") or default}
              for k, default in (("phone", "mobile"), ("landline", "home (2009)")) if phone(row.get(k, ""), country)]
    if phones:
        body["phoneNumbers"] = phones
    if row.get("street") or row.get("city"):
        postal, _, city = (row.get("city") or "").strip().partition(" ")
        if not postal.isdigit():
            postal, city = "", row.get("city", "").strip()
        body["addresses"] = [{k: v for k, v in (("streetAddress", row.get("street", "").strip()), ("postalCode", postal),
                                                 ("city", city), ("countryCode", "DE"),
                                                 ("type", row.get("address_type") or "home (2009)")) if v}]
    urls = [{"value": clean_url(row[k]), "type": "profile"} for k in ("facebook", "linkedin")
            if row.get(k, "").startswith("http")]
    if urls:
        body["urls"] = urls
    if row.get("maiden") and row.get("family") and row["maiden"] != row["family"]:
        body["nicknames"] = [{"value": f"geb. {row['maiden']}", "type": "MAIDEN_NAME"}]
    return body


def plan(rows: list, people: list, country: str = "49") -> tuple:
    """(new contact bodies, people_sync updates, report lines)."""
    creates, updates, lines = [], [], []
    for row in rows:
        if not (row.get("email") or row.get("phone") or row.get("landline")):
            continue
        new = person_body(row, country)
        p = match(row, people)
        shown = f"{row['given']} {row.get('family') or row.get('maiden', '')}"
        if not p:
            creates.append(new)
            lines.append(f"CREATE {shown}: " + ", ".join(k for k in new if k != "names"))
            continue
        changes = {}
        n = (p.get("names") or [{}])[0]
        # Only a name change the list itself records (maiden -> married) beats your card;
        # otherwise your card may well be newer than the list.
        if row.get("maiden") and row.get("family") and ps.fold(n.get("familyName", "")) == ps.fold(row["maiden"]):
            changes["names"] = {"givenName": n.get("givenName", "") or row["given"],
                                "middleName": n.get("middleName", ""), "familyName": row["family"]}
            if "nicknames" in new:
                changes["nicknames"] = new["nicknames"]
        for field, key in (("emailAddresses", lambda v: v.lower()), ("phoneNumbers", ps.phone_key),
                           ("urls", lambda v: v.rstrip("/"))):
            have = [{k: v for k, v in x.items() if k in ("value", "type")} for x in p.get(field, [])]
            known = {key(x["value"]) for x in have}
            add = [x for x in new.get(field, []) if key(x["value"]) not in known]
            if add:
                changes[field] = have + add
        if "addresses" in new and not p.get("addresses"):
            changes["addresses"] = new["addresses"]
        if changes:
            updates.append({"fp": ps.person_fingerprint(p), "name": n.get("displayName", shown), "set": changes,
                            "add_labels": []})
        lines.append(f"UPDATE {n.get('displayName', shown)}: " + (", ".join(changes) or "label only"))
        updates_label = {"fp": ps.person_fingerprint(p), "name": n.get("displayName", shown), "set": {},
                         "add_labels": []}
        if not changes:
            updates.append(updates_label)
    return creates, updates, lines


def run(api, rows: list, label: str, apply: bool, out: Path, country: str = "49") -> dict:
    people = ps.fetch_all(api, ps.FULL_FIELDS)  # addresses too, or every card looks address-less
    creates, updates, lines = plan(rows, people, country)
    group = ps.ensure_groups(api, {label}, False)[label]
    members = {p_["resourceName"] for p_ in people for m in p_.get("memberships", [])
               if m.get("contactGroupMembership", {}).get("contactGroupResourceName") == group}
    by_fp = {ps.person_fingerprint(p_): p_["resourceName"] for p_ in people}
    for u in updates:
        u["add_labels"] = [] if by_fp.get(u["fp"]) in members else [label]
    updates = [u for u in updates if u["set"] or u["add_labels"]]
    out.mkdir(parents=True, exist_ok=True)
    summary = ps.run({"export_counts": {}, "deletes": [], "updates": updates}, api, apply, out)
    if apply:
        groups = ps.ensure_groups(api, {label}, True)
        new_rns = []
        for body in creates:
            body["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": groups[label]}}]
            new_rns.append(api.call("POST", "/people:createContact", body=body,
                                    params={"personFields": "names"})["resourceName"])
    (out / "add_report.txt").write_text(f"{'APPLIED' if apply else 'DRY RUN'}: create {len(creates)}, "
                                        f"update {len(updates)}\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return {"create": len(creates), "update": len(updates), "skipped": summary["skipped"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("people", type=Path)
    ap.add_argument("--label", required=True)
    ap.add_argument("--client-secret", type=Path, required=True)
    ap.add_argument("--country", default="49")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("-o", "--out", type=Path, default=Path("work/add"))
    args = ap.parse_args(argv)
    summary = run(ps.Api(ps.access_token(args.client_secret)), read(args.people), args.label, args.apply,
                  args.out, args.country)
    print(("Applied. " if args.apply else "Dry run, nothing changed. ") + json.dumps(summary))
    print(f"Details: {args.out / 'add_report.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
