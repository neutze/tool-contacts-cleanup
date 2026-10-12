#!/usr/bin/env python3
"""Apply a cleanup plan to Google Contacts through the People API, changing
only what differs.

  python3 people_sync.py plan.json --client-secret client_secret.json          # dry run
  python3 people_sync.py plan.json --client-secret client_secret.json --apply  # do it

plan.json comes from contacts_fix.py. It names contacts by a fingerprint of
what the CSV export showed (name, emails, phones, links), because the export
carries no contact ids. Each contact is fetched live and matched on the same
fingerprint, so:

  - a contact you changed since the export no longer matches and is left alone
  - identical twins are only touched when the live count equals the export count
  - only the fields the plan changes are written; photos, stars, birthdays,
    addresses and ids stay as they are
  - labels are only ever added, never removed

Every run writes sync_report.txt. --apply also writes undo.json first: the
full live record of every contact it is about to delete or change. Google
Contacts' own Settings -> Undo changes covers 30 days on top of that.

Setup, once: a Google Cloud project with the People API enabled and an OAuth
client of type "Desktop app"; download its JSON as client_secret.json. The
first run opens a browser to log in; the token is kept in
~/.config/contacts-sync/token.json (readable only by you).

Photos: --photos DIR (a folder of +<number>.jpg, e.g. from
whatsapp_activity.py --zip) sets a contact's photo from the file for one of
its numbers, only where the contact has no photo of its own yet. A number
that belongs to two contacts is skipped. No plan.json needed.

Running somewhere without a browser (a server, a cloud session): log in in
two steps instead. --login-url prints a link; open it, approve, and the
browser lands on a page that fails to load. Copy that page's address and pass
it to --login-code. After that the saved token is used as usual.

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from pathlib import Path

API = "https://people.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/contacts"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
TOKEN_PATH = Path(os.environ.get("CONTACTS_SYNC_TOKEN", Path.home() / ".config/contacts-sync/token.json"))
PASTE_REDIRECT = "http://127.0.0.1:8765"  # nothing listens; the code is read from the pasted address
READ_FIELDS = "names,emailAddresses,phoneNumbers,urls,organizations,biographies,memberships,metadata"
FULL_FIELDS = READ_FIELDS + ",addresses,birthdays,nicknames,relations,events,userDefined,photos"
KNOWN_TYPES = {"home", "work", "other", "mobile", "main", "homefax", "workfax", "otherfax", "pager",
               "workmobile", "workpager", "googlevoice", "homepage", "blog", "profile"}


# ---- fingerprint: shared with contacts_fix.py, so both sides agree ----------

def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


def phone_key(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("00"):
        digits = digits[2:]
    return digits[-9:] if len(digits) >= 6 else ""


def fingerprint(given: str, middle: str, family: str, emails, phones, urls) -> str:
    name = fold(" ".join(p for p in (given, middle, family) if (p or "").strip()))
    parts = [name,
             ",".join(sorted({e.strip().lower() for e in emails if e.strip()})),
             ",".join(sorted({phone_key(p) for p in phones} - {""})),
             ",".join(sorted({u.strip() for u in urls if u.strip()}))]
    return "|".join(parts)


def person_fingerprint(p: dict) -> str:
    n = (p.get("names") or [{}])[0]
    return fingerprint(n.get("givenName", ""), n.get("middleName", ""), n.get("familyName", ""),
                       [e.get("value", "") for e in p.get("emailAddresses", [])],
                       [x.get("value", "") for x in p.get("phoneNumbers", [])],
                       [u.get("value", "") for u in p.get("urls", [])])


# ---- OAuth for an installed app, loopback redirect + PKCE -------------------

def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def _save_token(tok: dict) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(tok))
    os.chmod(TOKEN_PATH, 0o600)


def _client(client_secret: Path) -> dict:
    client = json.loads(client_secret.read_text())
    return client.get("installed") or client.get("web") or client


def login_url(client_secret: Path, scope: str = SCOPE) -> str:
    """Step 1 of the paste flow: the link to open, with the PKCE secret kept next to the token."""
    client = _client(client_secret)
    verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(16)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    pending = TOKEN_PATH.with_name("pending.json")
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({"verifier": verifier, "state": state}))
    os.chmod(pending, 0o600)
    return client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": PASTE_REDIRECT, "response_type": "code",
        "scope": scope, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent"})


def login_code(client_secret: Path, pasted: str) -> None:
    """Step 2: trade the code in the pasted address for a token."""
    client = _client(client_secret)
    pending = TOKEN_PATH.with_name("pending.json")
    saved = json.loads(pending.read_text())
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(pasted.strip()).query).items()}
    if q.get("state") != saved["state"] or "code" not in q:
        sys.exit(f"That address doesn't belong to the last --login-url ({q.get('error', 'state mismatch')}).")
    tok = _post_form(client["token_uri"], {
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code": q["code"],
        "code_verifier": saved["verifier"], "redirect_uri": PASTE_REDIRECT, "grant_type": "authorization_code"})
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600)
    _save_token(tok)
    pending.unlink()


def access_token(client_secret: Path, scope: str = SCOPE) -> str:
    client = json.loads(client_secret.read_text())
    client = client.get("installed") or client.get("web") or client
    if TOKEN_PATH.exists():
        tok = json.loads(TOKEN_PATH.read_text())
        if not set(scope.split()) <= set(tok.get("scope", scope).split()):
            tok = {}  # this run needs a scope the saved login didn't grant: log in again
        if tok.get("expires_at", 0) > time.time() + 60:
            return tok["access_token"]
        if tok.get("refresh_token"):
            try:
                new = _post_form(client["token_uri"], {
                    "client_id": client["client_id"], "client_secret": client["client_secret"],
                    "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
                tok.update(new, expires_at=time.time() + new.get("expires_in", 3600))
                _save_token(tok)
                return tok["access_token"]
            except urllib.error.HTTPError:
                pass  # refresh token revoked or expired: log in again

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Logged in. You can close this tab and go back to the terminal.")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": scope, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent"})
    print(f"Opening the browser to log in. If it doesn't open, visit:\n{url}")
    webbrowser.open(url)
    while "code" not in got and "error" not in got:
        server.handle_request()
    server.server_close()
    if got.get("state") != state or "code" not in got:
        sys.exit(f"Login failed: {got.get('error', 'state mismatch')}")
    tok = _post_form(client["token_uri"], {
        "client_id": client["client_id"], "client_secret": client["client_secret"], "code": got["code"],
        "code_verifier": verifier, "redirect_uri": redirect, "grant_type": "authorization_code"})
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600)
    _save_token(tok)
    return tok["access_token"]


class Api:
    def __init__(self, token: str, base: str = API):
        self.token, self.base = token, base

    def call(self, method: str, path: str, body=None, params=None) -> dict:
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                # Quotas are per minute; Gmail reports an exceeded one as 403, not 429.
                limited = e.code in (429, 500, 502, 503) or (e.code == 403 and "uota" in text or "ateLimit" in text)
                if limited and attempt < 5:
                    time.sleep(min(2 ** attempt * 5, 65))
                    continue
                raise SystemExit(f"{method} {path} failed: {e.code} {text[:500]}")
        return {}


# ---- the sync ---------------------------------------------------------------

def fetch_all(api, fields: str = READ_FIELDS) -> list:
    people, token = [], None
    while True:
        params = {"personFields": fields, "pageSize": 1000, "sortOrder": "FIRST_NAME_ASCENDING"}
        if token:
            params["pageToken"] = token
        page = api.call("GET", "/people/me/connections", params=params)
        people += page.get("connections", [])
        token = page.get("nextPageToken")
        if not token:
            return people


def typed(items: list) -> list:
    out = []
    for it in items:
        entry = {"value": it["value"]}
        t = (it.get("type") or "").lstrip("* ").replace("‎", "").strip()
        if t:
            entry["type"] = t.lower() if t.lower().replace(" ", "") in KNOWN_TYPES else t
        out.append(entry)
    return out


def strip_meta(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in ("metadata", "displayName", "displayNameLastFirst",
                                                    "unstructuredName", "formattedType")}


def build_update(person: dict, changes: dict) -> tuple:
    """The person body and update mask for the fields the plan changes."""
    body, mask = {"etag": person["etag"]}, []
    if "names" in changes:
        base = strip_meta((person.get("names") or [{}])[0])
        for key, val in changes["names"].items():
            if val:
                base[key] = val
            else:
                base.pop(key, None)
        body["names"] = [base]
        mask.append("names")
    for field in ("emailAddresses", "phoneNumbers", "urls"):
        if field in changes:
            body[field] = typed(changes[field])
            mask.append(field)
    if "organizations" in changes:
        base = strip_meta((person.get("organizations") or [{}])[0])
        for key, val in changes["organizations"].items():
            if val:
                base[key] = val
            else:
                base.pop(key, None)
        body["organizations"] = [base] if any(base.get(k) for k in ("name", "title", "department")) else []
        mask.append("organizations")
    for field in ("addresses", "birthdays", "nicknames"):
        if field in changes:  # whole list, already merged with what the contact has
            body[field] = [strip_meta(x) for x in changes[field]]
            mask.append(field)
    if "biographies" in changes:
        text = changes["biographies"]
        body["biographies"] = [{"value": text, "contentType": "TEXT_PLAIN"}] if text else []
        mask.append("biographies")
    return body, ",".join(mask)


def match(plan: dict, people: list) -> tuple:
    """Pair plan entries with live contacts. Returns (deletes, updates, skipped)
    where deletes is a list of people, updates a list of (person, entry)."""
    live = defaultdict(list)
    for p in people:
        live[person_fingerprint(p)].append(p)
    counts = plan.get("export_counts", {})

    def take(entries):
        by_fp = defaultdict(list)
        for e in entries:
            by_fp[e["fp"]].append(e)
        ok, skipped = [], []
        for fp, group in by_fp.items():
            found = live.get(fp, [])
            expected = counts.get(fp, len(group))
            if not found:
                skipped += [(e, "not found: changed or deleted since the export") for e in group]
            elif len(found) != expected or len(group) != expected:
                skipped += [(e, f"{len(found)} live contacts look like this, the export had {expected}")
                            for e in group]
            else:
                ok += list(zip(found, group))
        return ok, skipped

    deletes, skip_d = take(plan.get("deletes", []))
    updates, skip_u = take(plan.get("updates", []))
    return [p for p, _ in deletes], updates, skip_d + skip_u


def ensure_groups(api, names: set, apply: bool) -> dict:
    groups = api.call("GET", "/contactGroups", params={"pageSize": 1000}).get("contactGroups", [])
    by_name = {g.get("formattedName") or g.get("name"): g["resourceName"] for g in groups}
    for n in sorted(names - set(by_name)):
        if apply:
            by_name[n] = api.call("POST", "/contactGroups", body={"contactGroup": {"name": n}})["resourceName"]
        else:
            by_name[n] = f"(new label {n})"
    return by_name


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def run(plan: dict, api, apply: bool, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    people = fetch_all(api)
    deletes, updates, skipped = match(plan, people)

    bodies = defaultdict(dict)  # mask -> {resourceName: body}
    label_adds = defaultdict(list)
    lines = []
    for person, entry in updates:
        if entry.get("set"):
            body, mask = build_update(person, entry["set"])
            bodies[mask][person["resourceName"]] = body
        for label in entry.get("add_labels", []):
            label_adds[label].append(person["resourceName"])
        lines.append(f"UPDATE {entry['name'] or '(no name)'}: {', '.join(sorted(entry.get('set', {})))}"
                     + (f" +labels {', '.join(entry['add_labels'])}" if entry.get("add_labels") else ""))
    for p in deletes:
        n = (p.get("names") or [{}])[0].get("displayName") or \
            ((p.get("emailAddresses") or [{}])[0].get("value")) or "(blank)"
        lines.insert(0, f"DELETE {n}")
    groups = ensure_groups(api, set(label_adds), apply) if label_adds else {}

    summary = {"live_contacts": len(people), "delete": len(deletes),
               "update": sum(len(b) for b in bodies.values()),
               "label": sum(len(v) for v in label_adds.values()), "skipped": len(skipped),
               "new_labels": sorted(n for n, rn in groups.items() if rn.startswith("(new"))}
    report = [f"{'APPLIED' if apply else 'DRY RUN'}: " + json.dumps(summary), ""] + lines + [""] + \
             [f"SKIPPED {e['name'] or '(no name)'} ({e.get('action', '')}): {why}" for e, why in skipped]
    (out / "sync_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    if apply:
        touched = {p["resourceName"] for p in deletes} | {p["resourceName"] for p, _ in updates}
        full = [p for p in fetch_all(api, FULL_FIELDS) if p["resourceName"] in touched]
        (out / "undo.json").write_text(json.dumps(full, ensure_ascii=False, indent=1), encoding="utf-8")
        failed = []
        for mask, contacts in bodies.items():
            items = list(contacts.items())
            for part in chunks(items, 200):
                result = api.call("POST", "/people:batchUpdateContacts", body={
                    "contacts": dict(part), "updateMask": mask, "readMask": "names"})
                # The batch answers 200 even when single contacts fail; each carries its own status.
                for rn, res in (result.get("updateResult") or {}).items():
                    if res.get("httpStatusCode", 200) != 200 or res.get("status", {}).get("code"):
                        failed.append(f"FAILED {rn}: {res.get('status', {}).get('message', res.get('httpStatusCode'))}")
        if failed:
            summary["failed"] = len(failed)
            with (out / "sync_report.txt").open("a", encoding="utf-8") as f:
                f.write("\n".join(failed) + "\n")
        for label, members in label_adds.items():
            for part in chunks(members, 1000):
                api.call("POST", f"/{groups[label]}/members:modify", body={"resourceNamesToAdd": part})
        for part in chunks([p["resourceName"] for p in deletes], 500):
            api.call("POST", "/people:batchDeleteContacts", body={"resourceNames": part})
    return summary


def photo_matches(people: list, photos: Path) -> tuple:
    """(person, file) for contacts without a photo of their own that one photo
    file's number belongs to, and the reasons the others were skipped."""
    files = {phone_key(f.stem): f for f in photos.glob("*.jpg") if phone_key(f.stem)}
    owners = defaultdict(set)
    for p in people:
        for x in p.get("phoneNumbers", []):
            if phone_key(x.get("value", "")):
                owners[phone_key(x["value"])].add(p["resourceName"])
    matches, skipped = [], []
    for p in people:
        keys = [k for k in dict.fromkeys(phone_key(x.get("value", "")) for x in p.get("phoneNumbers", []))
                if k in files]
        if not keys:
            continue
        name = (p.get("names") or [{}])[0].get("displayName", "(no name)")
        if any(not ph.get("default") for ph in p.get("photos", [])):
            skipped.append((name, "already has a photo"))
        elif len(owners[keys[0]]) > 1:
            skipped.append((name, "number shared with another contact"))
        else:
            matches.append((p, files[keys[0]]))
    return matches, skipped


def run_photos(api, photos: Path, apply: bool, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    people = fetch_all(api, "names,phoneNumbers,photos")
    matches, skipped = photo_matches(people, photos)
    lines = [f"PHOTO {(p.get('names') or [{}])[0].get('displayName', '(no name)')} <- {f.name}" for p, f in matches]
    lines += [f"SKIPPED {n}: {why}" for n, why in skipped]
    summary = {"live_contacts": len(people), "photo": len(matches), "skipped": len(skipped)}
    (out / "photo_report.txt").write_text(f"{'APPLIED' if apply else 'DRY RUN'}: {json.dumps(summary)}\n\n"
                                          + "\n".join(lines) + "\n", encoding="utf-8")
    if apply:
        for p, f in matches:
            api.call("PATCH", f"/{p['resourceName']}:updateContactPhoto",
                     body={"photoBytes": base64.b64encode(f.read_bytes()).decode(), "personFields": "photos"})
    return summary


def gravatar(people: list, out: Path, fetch=None) -> Path:
    """Public Gravatar pictures (by SHA-256 of the address) for contacts with an
    email and no photo, saved as <email>.jpg for run_photos_by_email."""
    out.mkdir(parents=True, exist_ok=True)

    def default_fetch(url):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.read()
        except urllib.error.HTTPError:
            return b""
    fetch = fetch or default_fetch
    for p in people:
        if any(not ph.get("default") for ph in p.get("photos", [])):
            continue
        for e in p.get("emailAddresses", []):
            addr = e["value"].strip().lower()
            image = fetch(f"https://gravatar.com/avatar/{hashlib.sha256(addr.encode()).hexdigest()}?d=404&s=512")
            if image:
                (out / f"{addr}.jpg").write_bytes(image)
                break
    return out


def run_photos_by_email(api, photos: Path, apply: bool, out: Path) -> dict:
    """Like run_photos, keyed by email address instead of number."""
    out.mkdir(parents=True, exist_ok=True)
    files = {f.stem.lower(): f for f in photos.glob("*.jpg")}
    people = fetch_all(api, "names,emailAddresses,photos")
    lines, done = [], 0
    for p in people:
        if any(not ph.get("default") for ph in p.get("photos", [])):
            continue
        f = next((files[e["value"].strip().lower()] for e in p.get("emailAddresses", [])
                  if e["value"].strip().lower() in files), None)
        if f:
            lines.append(f"PHOTO {(p.get('names') or [{}])[0].get('displayName', '')} <- {f.name}")
            if apply:
                api.call("PATCH", f"/{p['resourceName']}:updateContactPhoto",
                         body={"photoBytes": base64.b64encode(f.read_bytes()).decode()})
            done += 1
    (out / "photo_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"photo": done}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=Path, nargs="?", help="plan.json from contacts_fix.py")
    ap.add_argument("--client-secret", type=Path, required=True, help="OAuth client JSON (Desktop app)")
    ap.add_argument("--apply", action="store_true", help="write the changes; without it nothing is changed")
    ap.add_argument("-o", "--out", type=Path, default=Path("sync_out"))
    ap.add_argument("--login-url", action="store_true", help="print a login link (no-browser login, step 1)")
    ap.add_argument("--login-code", metavar="ADDRESS", help="the address the login ended on (step 2)")
    ap.add_argument("--photos", type=Path, metavar="DIR", help="set photos from DIR/+<number>.jpg instead of a plan")
    args = ap.parse_args(argv)

    if args.login_url:
        print(login_url(args.client_secret))
        return 0
    if args.login_code:
        login_code(args.client_secret, args.login_code)
        print(f"Logged in; token saved to {TOKEN_PATH}")
        return 0
    if args.photos:
        summary = run_photos(Api(access_token(args.client_secret)), args.photos, args.apply, args.out)
        print(("Applied. " if args.apply else "Dry run, nothing changed. ") + json.dumps(summary))
        print(f"Details: {args.out / 'photo_report.txt'}")
        return 0
    if not args.plan:
        ap.error("plan.json is required")

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    summary = run(plan, Api(access_token(args.client_secret)), args.apply, args.out)
    print(("Applied. " if args.apply else "Dry run, nothing changed. ") + json.dumps(summary))
    print(f"Details: {args.out / 'sync_report.txt'}")
    if args.apply:
        print(f"Before-state of every touched contact: {args.out / 'undo.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
