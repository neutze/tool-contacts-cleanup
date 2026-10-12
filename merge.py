"""Merge duplicate contacts through the People API, which has no merge call.

For each group the fullest card is kept. It gets the union of everyone's
emails, phones and links, the longest name, and any company, notes, address
or birthday it lacks, plus the others' labels and a photo if it has none.
Then the other cards are deleted.

Groups come from the triage (review.csv, column dup_group) and are matched to
live contacts by fingerprint, like every other change: a card that changed
since the triage leaves its whole group alone.
"""
from __future__ import annotations

import base64
import csv
import json
import urllib.request
from collections import defaultdict
from pathlib import Path

import people_sync as ps

MERGE_FIELDS = ("names", "emailAddresses", "phoneNumbers", "urls", "organizations", "biographies",
                "addresses", "birthdays")


def custom_photo(p: dict) -> str:
    return next((x["url"] for x in p.get("photos", [])
                 if not x.get("default") and x.get("metadata", {}).get("source", {}).get("type") == "CONTACT"), "")


def own(p: dict, field: str) -> list:
    """Entries the contact itself holds; profile data from a Google account can't be written back."""
    return [x for x in p.get(field, []) if x.get("metadata", {}).get("source", {}).get("type", "CONTACT") == "CONTACT"]


def score(p: dict) -> tuple:
    return (sum(len(own(p, f)) for f in MERGE_FIELDS), bool(custom_photo(p)), len(p.get("memberships", [])))


def display(p: dict) -> str:
    return (p.get("names") or [{}])[0].get("displayName", "")


def merged(keep: dict, others: list) -> tuple:
    """(changes for people_sync.build_update, labels to add, photo url to copy)."""
    changes = {}
    everyone = [keep] + others

    best = max(everyone, key=lambda p: (len(display(p)), sum(ord(c) > 127 for c in display(p))))
    if best is not keep and own(best, "names"):
        n = own(best, "names")[0]
        changes["names"] = {k: n.get(k, "") for k in ("givenName", "middleName", "familyName")}

    for field, key in (("emailAddresses", lambda v: v.strip().lower()), ("phoneNumbers", ps.phone_key),
                       ("urls", lambda v: v.strip())):
        items = [{k: v for k, v in x.items() if k in ("value", "type")} for x in own(keep, field)]
        seen = {key(x["value"]) for x in items}
        for o in others:
            for x in own(o, field):
                if key(x["value"]) and key(x["value"]) not in seen:
                    seen.add(key(x["value"]))
                    items.append({k: v for k, v in x.items() if k in ("value", "type")})
        if len(items) > len(own(keep, field)):
            changes[field] = items

    for field in ("addresses", "birthdays"):
        if not own(keep, field):
            donor = next((o for o in others if own(o, field)), None)
            if donor:
                changes[field] = own(donor, field)
    if not own(keep, "organizations"):
        donor = next((o for o in others if own(o, "organizations")), None)
        if donor:
            org = own(donor, "organizations")[0]
            changes["organizations"] = {"name": org.get("name", ""), "title": org.get("title", "")}
    notes = [own(p, "biographies")[0]["value"] for p in everyone if own(p, "biographies")]
    if len(set(notes)) > 1:
        changes["biographies"] = "\n\n".join(dict.fromkeys(notes))

    have = {m.get("contactGroupMembership", {}).get("contactGroupResourceName") for m in keep.get("memberships", [])}
    labels = [m["contactGroupMembership"]["contactGroupResourceName"] for o in others
              for m in o.get("memberships", []) if m.get("contactGroupMembership")
              and m["contactGroupMembership"]["contactGroupResourceName"] not in have]
    photo = "" if custom_photo(keep) else next((custom_photo(o) for o in others if custom_photo(o)), "")
    return changes, list(dict.fromkeys(labels)), photo


def groups_from_triage(review: Path, people: list) -> tuple:
    """Live contacts per duplicate group, and the groups that no longer match."""
    with review.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    by_fp = defaultdict(list)
    for p in people:
        by_fp[ps.person_fingerprint(p)].append(p)
    import contacts_fix
    groups, stale = defaultdict(list), []
    for r in rows:
        if r.get("dup_group"):
            groups[r["dup_group"]].append(contacts_fix.row_fingerprint(r))
    out = []
    for g, fps in groups.items():
        found = [by_fp.get(fp, []) for fp in fps]
        if all(len(x) == 1 for x in found):
            out.append([x[0] for x in found])
        else:
            stale.append(g)
    return out, stale


def fetch_photo(url: str) -> bytes:
    with urllib.request.urlopen(url) as r:
        return r.read()


def run(api, groups: list, apply: bool, out: Path, photo_fetch=fetch_photo) -> dict:
    lines, done = [], 0
    for group in groups:
        keep = max(group, key=score)
        others = [p for p in group if p is not keep]
        changes, labels, photo = merged(keep, others)
        lines.append(f"MERGE into {display(keep) or keep['resourceName']}: "
                     + ", ".join(display(o) or o["resourceName"] for o in others)
                     + (f" | {', '.join(sorted(changes))}" if changes else "")
                     + (" +labels" if labels else "") + (" +photo" if photo else ""))
        if not apply:
            continue
        if changes:
            body, mask = ps.build_update(keep, changes)
            api.call("PATCH", f"/{keep['resourceName']}:updateContact",
                     params={"updatePersonFields": mask}, body=body)
        for label in labels:
            api.call("POST", f"/{label}/members:modify", body={"resourceNamesToAdd": [keep["resourceName"]]})
        if photo:
            api.call("PATCH", f"/{keep['resourceName']}:updateContactPhoto",
                     body={"photoBytes": base64.b64encode(photo_fetch(photo)).decode()})
        api.call("POST", "/people:batchDeleteContacts", body={"resourceNames": [o["resourceName"] for o in others]})
        done += 1
    summary = {"groups": len(groups), "merged": done}
    out.mkdir(parents=True, exist_ok=True)
    (out / "merge_report.txt").write_text(f"{'APPLIED' if apply else 'DRY RUN'}: {json.dumps(summary)}\n\n"
                                          + "\n".join(lines) + "\n", encoding="utf-8")
    return summary
