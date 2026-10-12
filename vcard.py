"""Merge a vCard file (iCloud / iPhone export, Outlook, any .vcf) into Google
Contacts.

  python3 vcard.py contacts.vcf --client-secret ... [--label iPhone] [--apply]

Each card is matched to a Google contact by email, then mobile number, then
full name (add_people.match). A match gets what it lacks: emails, numbers,
a last name, company, birthday, address, and the vCard's photo if it has
none. Cards that match nothing and carry an email or a number are created.
Nothing on a Google card is overwritten; the vCard only fills gaps.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

import add_people
import people_sync as ps


def unfold(text: str) -> list:
    """RFC 6350 line unfolding: a line starting with a space continues the last."""
    lines = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def unescape(v: str) -> str:
    return v.replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n").replace("\\N", "\n").replace("\\\\", "\\")


def parse(text: str) -> list:
    cards, card = [], None
    for line in unfold(text):
        if line.upper() == "BEGIN:VCARD":
            card = {"emails": [], "phones": [], "addresses": []}
            continue
        if line.upper() == "END:VCARD":
            if card is not None:
                cards.append(card)
            card = None
            continue
        if card is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *params = head.split(";")
        name = name.split(".")[-1].upper()  # item1.EMAIL -> EMAIL
        types = [t.split("=", 1)[-1].lower() for p in params for t in p.split(",")
                 if p.upper().startswith("TYPE=") or "=" not in p]
        kind = next((t for t in types if t in ("cell", "iphone", "mobile", "home", "work", "main")), "")
        if name == "N":
            parts = (value.split(";") + [""] * 5)[:5]
            card["family"], card["given"] = unescape(parts[0]).strip(), unescape(parts[1]).strip()
            card["middle"] = unescape(parts[2]).strip()
        elif name == "FN":
            card["fn"] = unescape(value).strip()
        elif name == "NICKNAME":
            card["nickname"] = unescape(value).strip()
        elif name == "EMAIL" and value.strip():
            card["emails"].append((value.strip(), "work" if kind == "work" else "home"))
        elif name == "TEL" and value.strip():
            card["phones"].append((value.strip(), "mobile" if kind in ("cell", "iphone", "mobile") else kind or "other"))
        elif name == "ORG":
            card["org"] = unescape(value.split(";")[0]).strip()
        elif name == "TITLE":
            card["title"] = unescape(value).strip()
        elif name == "BDAY":
            m = re.match(r"(\d{4}|--)-?(\d{2})-?(\d{2})", value.strip())
            if m:
                card["bday"] = {"month": int(m.group(2)), "day": int(m.group(3)),
                                **({"year": int(m.group(1))} if m.group(1).isdigit() and m.group(1) != "1604" else {})}
        elif name == "ADR":
            parts = [unescape(x).strip() for x in (value.split(";") + [""] * 7)[:7]]
            if any(parts[2:]):
                card["addresses"].append({k: v for k, v in (("streetAddress", parts[2]), ("city", parts[3]),
                                                             ("region", parts[4]), ("postalCode", parts[5]),
                                                             ("country", parts[6]), ("type", kind or "home")) if v})
        elif name == "PHOTO" and ("b" in types or "base64" in head.lower() or "encoding=b" in head.lower()):
            try:
                card["photo"] = base64.b64decode(value.strip())
            except ValueError:
                pass
    return cards


def as_row(card: dict) -> dict:
    """add_people's row shape, for its matching."""
    return {"given": card.get("given", ""), "family": card.get("family", ""), "maiden": "",
            "email": card["emails"][0][0] if card["emails"] else "",
            "phone": next((v for v, t in card["phones"] if t == "mobile"), card["phones"][0][0] if card["phones"] else "")}


def plan(cards: list, people: list, country: str = "49") -> tuple:
    creates, updates, photos, lines = [], [], [], []
    for card in cards:
        if not (card["emails"] or card["phones"]) or not (card.get("given") or card.get("family") or card.get("org")):
            continue
        phones = [{"value": add_people.phone(v, country) or v, "type": t} for v, t in card["phones"]]
        emails = [{"value": v, "type": t} for v, t in card["emails"]]
        shown = card.get("fn") or f"{card.get('given', '')} {card.get('family', '')}".strip()
        p = add_people.match(as_row(card), people)
        if not p:
            body = {"names": [{"givenName": card.get("given") or card.get("org", ""), "familyName": card.get("family", "")}]}
            for field, val in (("emailAddresses", emails), ("phoneNumbers", phones), ("addresses", card["addresses"])):
                if val:
                    body[field] = val
            if card.get("org"):
                body["organizations"] = [{"name": card["org"], "title": card.get("title", "")}]
            if card.get("bday"):
                body["birthdays"] = [{"date": card["bday"]}]
            creates.append((body, card.get("photo", b"")))
            lines.append(f"CREATE {shown}")
            continue
        changes = {}
        n = (p.get("names") or [{}])[0]
        if card.get("family") and not n.get("familyName") and ps.fold(card.get("given", "")) == ps.fold(n.get("givenName", "")):
            changes["names"] = {"givenName": n.get("givenName", ""), "middleName": n.get("middleName", ""),
                                "familyName": card["family"]}
        for field, new, key in (("emailAddresses", emails, lambda v: v.lower()), ("phoneNumbers", phones, ps.phone_key)):
            have = [{k: v for k, v in x.items() if k in ("value", "type")} for x in p.get(field, [])]
            known = {key(x["value"]) for x in have}
            add = [x for x in new if key(x["value"]) and key(x["value"]) not in known]
            if add:
                changes[field] = have + add
        if card.get("org") and not p.get("organizations"):
            changes["organizations"] = {"name": card["org"], "title": card.get("title", "")}
        if card.get("bday") and not p.get("birthdays"):
            changes["birthdays"] = [{"date": card["bday"]}]
        if card["addresses"] and not p.get("addresses"):
            changes["addresses"] = card["addresses"]
        if changes:
            updates.append({"fp": ps.person_fingerprint(p), "name": n.get("displayName", shown), "set": changes,
                            "add_labels": []})
            lines.append(f"UPDATE {n.get('displayName', shown)}: {', '.join(changes)}")
        if card.get("photo") and not any(not x.get("default") for x in p.get("photos", [])):
            photos.append((p["resourceName"], card["photo"]))
            lines.append(f"PHOTO {n.get('displayName', shown)}")
    return creates, updates, photos, lines


def run(api, cards: list, apply: bool, out: Path, label: str = "", country: str = "49") -> dict:
    people = ps.fetch_all(api, ps.FULL_FIELDS)
    creates, updates, photos, lines = plan(cards, people, country)
    out.mkdir(parents=True, exist_ok=True)
    summary = ps.run({"export_counts": {}, "deletes": [], "updates": updates}, api, apply, out)
    if apply:
        group = ps.ensure_groups(api, {label}, True)[label] if label else ""
        for body, photo in creates:
            if group:
                body["memberships"] = [{"contactGroupMembership": {"contactGroupResourceName": group}}]
            rn = api.call("POST", "/people:createContact", body=body, params={"personFields": "names"})["resourceName"]
            if photo:
                photos.append((rn, photo))
        for rn, photo in photos:
            api.call("PATCH", f"/{rn}:updateContactPhoto", body={"photoBytes": base64.b64encode(photo).decode()})
    (out / "vcard_report.txt").write_text(f"{'APPLIED' if apply else 'DRY RUN'}: create {len(creates)}, update "
                                          f"{len(updates)}, photos {len(photos)}\n\n" + "\n".join(lines) + "\n",
                                          encoding="utf-8")
    return {"create": len(creates), "update": len(updates), "photos": len(photos), "skipped": summary["skipped"]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vcf", type=Path)
    ap.add_argument("--client-secret", type=Path, required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("-o", "--out", type=Path, default=Path("work/vcard"))
    args = ap.parse_args(argv)
    cards = parse(args.vcf.read_text(encoding="utf-8", errors="replace"))
    summary = run(ps.Api(ps.access_token(args.client_secret)), cards, args.apply, args.out, args.label)
    print(("Applied. " if args.apply else "Dry run, nothing changed. ") + json.dumps(summary))
    print(f"Details: {args.out / 'vcard_report.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
