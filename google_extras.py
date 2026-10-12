"""Two extra Google sources for the pipeline.

Other contacts: the addresses Gmail collected on its own. The People API
can list them (contacts.other.readonly) and copy one into My Contacts, but
not delete them. Worth keeping are the ones you've written to recently and
that aren't in My Contacts yet; the rest are cleared by hand in Google
Contacts -> Other contacts.

Addresses: Google's Address Validation API (an API key with billing) turns
each postal address into a checked, uniformly formatted one. Only addresses
Google confirms in full are written; the rest are listed.
"""
from __future__ import annotations

import json
import re
import urllib.request

import people_sync as ps
from contacts_cleanup import is_noreply

ROLE = re.compile(r"^(info|service|kundenservice|kunde|kunden|support|hello|hallo|contact|kontakt|office|mail|team|"
                  r"people|jobs|career|karriere|bewerbung|anfrage|anfragen|booking|buchung|reservierung|rechnung|"
                  r"billing|sales|vertrieb|order|orders|shop|reiseportal|post|empfang|rezeption|praxis|verwaltung|"
                  r"standesamt|buergeramt|presse|newsletter|admin|webmaster)([._-].*)?$", re.I)
RELAY = re.compile(r"[0-9a-f]{16,}", re.I)


def wanted_address(email: str, name: str, own_domains: tuple) -> bool:
    """A person you'd want as a contact: not yourself, not a robot, not a
    relay alias, and not a nameless shared mailbox."""
    local, _, domain = email.lower().partition("@")
    if domain in own_domains or any(domain.endswith("." + d) for d in own_domains):
        return False
    if is_noreply(email) or RELAY.search(local):
        return False
    return bool(name.strip()) or not ROLE.match(local)

OTHER_SCOPE = "https://www.googleapis.com/auth/contacts.other.readonly"
VALIDATE = "https://addressvalidation.googleapis.com/v1:validateAddress"


# ---- Other contacts ---------------------------------------------------------

def other_contacts(api) -> list:
    out, token = [], None
    while True:
        params = {"readMask": "names,emailAddresses,phoneNumbers", "pageSize": 1000}
        if token:
            params["pageToken"] = token
        page = api.call("GET", "/otherContacts", params=params)
        out += page.get("otherContacts", [])
        token = page.get("nextPageToken")
        if not token:
            return out


def worth_keeping(others: list, people: list, activity: dict, since: str, own_domains=()) -> tuple:
    """(to copy, everything else). activity: lowercased address -> last sent date."""
    known = {e["value"].strip().lower() for p in people for e in p.get("emailAddresses", [])}
    keep, rest = [], []
    for o in others:
        emails = [e["value"].strip().lower() for e in o.get("emailAddresses", [])]
        name = (o.get("names") or [{}])[0].get("displayName", "")
        last = max((activity.get(e, "") for e in emails), default="")
        if emails and not set(emails) & known and last >= since \
                and all(wanted_address(e, name, tuple(own_domains)) for e in emails):
            keep.append((o, last))
        else:
            rest.append(o)
    return keep, rest


def rescue_by_name(people: list, others: list, own_domains=()) -> list:
    """Plan updates giving a contact with no phone and no email the address of
    the one Other contact that carries exactly its full name."""
    by_name = {}
    for o in others:
        name = ps.fold((o.get("names") or [{}])[0].get("displayName", ""))
        emails = [e["value"] for e in o.get("emailAddresses", [])
                  if wanted_address(e["value"], name, tuple(own_domains))]
        if " " in name and emails:
            by_name.setdefault(name, []).append(emails)
    updates = []
    for p in people:
        if p.get("phoneNumbers") or p.get("emailAddresses"):
            continue
        n = (p.get("names") or [{}])[0]
        found = by_name.get(ps.fold(f"{n.get('givenName', '')} {n.get('familyName', '')}"), [])
        if len(found) == 1:
            updates.append({"fp": ps.person_fingerprint(p), "name": n.get("displayName", ""),
                            "set": {"emailAddresses": [{"value": e} for e in found[0]]}, "add_labels": []})
    return updates


def attach_to_existing(people: list, others: list, own_domains=()) -> list:
    """Plan updates adding an Other contact's email to the one card it clearly
    belongs to: its name is that card's full name or first name plus the last
    name's initial ("Gizem K."), or the address itself spells first and last
    name ("kocgizem@", "anna.schmidt@")."""
    known = {e["value"].strip().lower() for p in people for e in p.get("emailAddresses", [])}
    cards = []
    for p in people:
        n = (p.get("names") or [{}])[0]
        given, family = ps.fold(n.get("givenName", "")), ps.fold(n.get("familyName", ""))
        if given and family:
            cards.append((p, given, family.split()))
    hits = {}
    for o in others:
        name = ps.fold((o.get("names") or [{}])[0].get("displayName", "")).replace(".", "")
        for e in o.get("emailAddresses", []):
            addr = e["value"].strip().lower()
            if addr in known or not wanted_address(addr, name, tuple(own_domains)):
                continue
            local = re.sub(r"[^a-z]", "", ps.fold(addr.split("@")[0]))
            words = name.split()
            found = [p for p, given, fam in cards
                     if (len(words) >= 2 and words[0] == given
                         and (" ".join(words[1:]) == " ".join(fam) or (len(words[1]) == 1 and words[1] == fam[0][0])))
                     or (len(given) >= 3 and given in local and any(len(f) >= 3 and f in local for f in fam))]
            if len(found) == 1:
                hits.setdefault(found[0]["resourceName"], (found[0], []))[1].append(e["value"].strip())
    updates = []
    for p, emails in hits.values():
        have = [{"value": x["value"], **({"type": x["type"]} if x.get("type") else {})} for x in p.get("emailAddresses", [])]
        updates.append({"fp": ps.person_fingerprint(p), "name": (p.get("names") or [{}])[0].get("displayName", ""),
                        "set": {"emailAddresses": have + [{"value": v, "type": "home"} for v in dict.fromkeys(emails)]},
                        "add_labels": []})
    return updates


def copy_others(api, keep: list, apply: bool, out) -> dict:
    lines = [f"COPY {(o.get('names') or [{}])[0].get('displayName', '')} <{o['emailAddresses'][0]['value']}>"
             f" last mailed {last}" for o, last in keep]
    if apply:
        for o, _ in keep:
            api.call("POST", f"/{o['resourceName']}:copyOtherContactToMyContactsGroup",
                     body={"copyMask": "names,emailAddresses,phoneNumbers"})
    out.mkdir(parents=True, exist_ok=True)
    (out / "other_contacts_report.txt").write_text(
        f"{'APPLIED' if apply else 'DRY RUN'}: copy {len(keep)}\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return {"other_contacts_copied" if apply else "other_contacts_to_copy": len(keep)}


# ---- addresses --------------------------------------------------------------

def validate(address: dict, key: str, region: str = "DE", post=None) -> dict:
    """Google's verdict and the checked address for one People API address."""
    lines = [address[k] for k in ("streetAddress", "extendedAddress") if address.get(k)]
    lines.append(" ".join(x for x in (address.get("postalCode", ""), address.get("city", "")) if x))
    if not any(lines) and address.get("formattedValue"):
        lines = address["formattedValue"].splitlines()
    body = {"address": {"regionCode": (address.get("countryCode") or region).upper(),
                        "addressLines": [l for l in lines if l.strip()]}}
    if post:
        return post(body)
    req = urllib.request.Request(f"{VALIDATE}?key={key}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def checked(address: dict, result: dict) -> dict:
    """The People API address to write, or {} when Google isn't sure."""
    res = result.get("result", {})
    verdict = res.get("verdict", {})
    if not verdict.get("addressComplete") or verdict.get("hasUnconfirmedComponents") \
            or verdict.get("hasInferredComponents") and verdict.get("validationGranularity") not in ("PREMISE", "SUB_PREMISE"):
        return {}
    pa = res.get("address", {}).get("postalAddress", {})
    new = {"streetAddress": ", ".join(pa.get("addressLines", [])), "postalCode": pa.get("postalCode", ""),
           "city": pa.get("locality", ""), "countryCode": pa.get("regionCode", "")}
    if address.get("type"):
        new["type"] = address["type"]
    return {k: v for k, v in new.items() if v}


def address_plan(people: list, key: str, region: str = "DE", post=None) -> tuple:
    """(plan updates in people_sync format, lines for the report)."""
    updates, lines = [], []
    for p in people:
        own = [a for a in p.get("addresses", [])
               if a.get("metadata", {}).get("source", {}).get("type", "CONTACT") == "CONTACT"]
        if not own:
            continue
        new_list, changed = [], False
        for a in own:
            if not (a.get("streetAddress") or "").strip():
                new_list.append({k: v for k, v in a.items() if k not in ("metadata", "formattedType")})
                continue  # a city alone can't be checked to a door
            good = checked(a, validate(a, key, region, post))
            old = {k: a.get(k, "") for k in ("streetAddress", "postalCode", "city", "countryCode")}
            if good and {k: good.get(k, "") for k in old} != old:
                new_list.append(good)
                changed = True
                lines.append(f"ADDRESS {(p.get('names') or [{}])[0].get('displayName', '')}: "
                             f"{a.get('formattedValue', '').replace(chr(10), ', ')} -> "
                             f"{good.get('streetAddress')}, {good.get('postalCode')} {good.get('city')}, "
                             f"{good.get('countryCode')}")
            else:
                new_list.append({k: v for k, v in a.items() if k not in ("metadata", "formattedType")})
                if not good:
                    lines.append(f"UNSURE {(p.get('names') or [{}])[0].get('displayName', '')}: "
                                 f"{a.get('formattedValue', '').replace(chr(10), ', ')}")
        if changed:
            updates.append({"fp": ps.person_fingerprint(p), "name": (p.get("names") or [{}])[0].get("displayName", ""),
                            "set": {"addresses": new_list}, "add_labels": []})
    return updates, lines
