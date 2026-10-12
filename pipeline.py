#!/usr/bin/env python3
"""Clean up Google Contacts end to end, straight through the People API.

  python3 pipeline.py --client-secret client_secret.json                 # dry run
  python3 pipeline.py --client-secret client_secret.json --apply         # write

Each run:
  1. reads your live contacts (no export, no Drive)
  2. triages them: contacts_cleanup.py rules, plus activity from Gmail
     (--gmail), WhatsApp (--whatsapp) and any --activity files
  3. plans deletes and per-field fixes: contacts_fix.py rules, labels from
     config.json, LinkedIn details (--linkedin Connections.csv), WhatsApp
     profile names and group labels
  4. adds the streamline pass: email / phone types (live_plan.py)
  5. dry run: writes work/sync_report.txt and work/streamline_review.csv;
     with --apply it writes the changes, after saving work/undo.json
  6. --whatsapp also sets WhatsApp profile photos on contacts that have none

Everything it writes stays in --work (default ./work). The contacts are the
source of truth; there is no input file to keep in sync.

Settings live in config.json (see config.example.json): country code, the
activity window, email domains to drop or mark old, and labels by domain or
company. contacts_fix.py's suggestion sheets (first_name_only.csv,
reach_suggestions.csv, groups.csv) land in work/fix; fill one in and pass it
back with --names / --reach / --group-labels on the next run.

Setup once: README.md. Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import json
import os
import shutil
import sys
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

import contacts_cleanup
import contacts_fix
import google_extras
import live_plan
import merge as merging
import messengers
import people_sync as ps
import whatsapp_activity as wa

GMAIL = "https://gmail.googleapis.com/gmail/v1"
SYSTEM_GROUPS = {"myContacts": "* myContacts", "starred": "* starred"}


# ---- live contacts -> Google CSV rows, so the CSV rules apply unchanged ----

def to_rows(people: list, group_names: dict) -> list:
    rows = []
    for p in people:
        n = (p.get("names") or [{}])[0]
        org = (p.get("organizations") or [{}])[0]
        row = {"First Name": n.get("givenName", ""), "Middle Name": n.get("middleName", ""),
               "Last Name": n.get("familyName", ""), "Name Prefix": n.get("honorificPrefix", ""),
               "Name Suffix": n.get("honorificSuffix", ""),
               "Nickname": (p.get("nicknames") or [{}])[0].get("value", ""),
               "Organization Name": org.get("name", ""), "Organization Title": org.get("title", ""),
               "Notes": (p.get("biographies") or [{}])[0].get("value", ""),
               "Birthday": "x" if p.get("birthdays") else "",
               "Labels": contacts_cleanup.MULTI.join(filter(None, (
                   group_names.get(m.get("contactGroupMembership", {}).get("contactGroupResourceName", ""), "")
                   for m in p.get("memberships", []))))}
        for field, kind in (("emailAddresses", "E-mail"), ("phoneNumbers", "Phone"), ("urls", "Website")):
            for i, x in enumerate(p.get(field, []), 1):
                row[f"{kind} {i} - Label"] = x.get("type", "")
                row[f"{kind} {i} - Value"] = x.get("value", "")
        for i, a in enumerate(p.get("addresses", []), 1):
            row[f"Address {i} - Formatted"] = a.get("formattedValue") or a.get("city", "") or "x"
        rows.append(row)
    return rows


def write_rows(path: Path, rows: list) -> None:
    header = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def group_names(api) -> dict:
    groups = api.call("GET", "/contactGroups", params={"pageSize": 1000}).get("contactGroups", [])
    return {g["resourceName"]: SYSTEM_GROUPS.get(g.get("name", ""), g.get("formattedName") or g.get("name", ""))
            for g in groups}


# ---- activity ---------------------------------------------------------------

def gmail_activity(api, since: str) -> list:
    """key,channel,sent,last_sent for every address you mailed since `since`,
    from message headers only. The gmail.metadata scope allows no search
    query, so this walks the SENT label newest first and stops at `since`."""
    seen, token = {}, None
    while True:
        params = [("labelIds", "SENT"), ("maxResults", 500)] + ([("pageToken", token)] if token else [])
        page = api.call("GET", "/users/me/messages", params=params)
        for m in page.get("messages", []):
            msg = api.call("GET", f"/users/me/messages/{m['id']}", params=[
                ("format", "metadata"), ("metadataHeaders", "To"), ("metadataHeaders", "Cc"),
                ("metadataHeaders", "Bcc")])
            day = dt.datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000,
                                            dt.timezone.utc).date().isoformat()
            if day < since:
                return rows_from(seen)
            headers = [h["value"] for h in msg.get("payload", {}).get("headers", [])]
            for _, addr in email.utils.getaddresses(headers):
                addr = addr.strip().lower()
                if "@" in addr:
                    sent, last = seen.get(addr, (0, ""))
                    seen[addr] = (sent + 1, max(last, day))
        token = page.get("nextPageToken")
        if not token:
            return rows_from(seen)


def rows_from(seen: dict) -> list:
    return [{"key": a, "channel": "gmail", "sent": s, "last_sent": d} for a, (s, d) in seen.items()]


def write_activity(path: Path, rows: list, extra=()) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["key", "channel", "sent", "last_sent", *extra], extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def whatsapp_inputs(source: Path, work: Path) -> tuple:
    """(activity csv, groups csv, photos dir) from the Mac's WhatsApp database,
    or from a zip made by whatsapp_activity.py --zip."""
    out = work / "whatsapp"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    if source.suffix == ".zip":
        with zipfile.ZipFile(source) as z:
            z.extractall(out)
    else:
        found, _ = wa.rows(source)
        write_activity(out / "whatsapp_activity.csv", found, extra=["push_name"])
        with (out / "whatsapp_groups.csv").open("w", newline="", encoding="utf-8") as f:
            g = csv.writer(f)
            g.writerow(["group", "key"])
            g.writerows(wa.group_rows(source) or [])
        (out / "photos").mkdir()
        for number, src in wa.photo_files(source).items():
            wa.shrink(src, out / "photos" / f"{number}.jpg")
    return out / "whatsapp_activity.csv", out / "whatsapp_groups.csv", out / "photos"


# ---- config -----------------------------------------------------------------

DEFAULT_CONFIG = {"country": "49", "region": "DE", "active_years": 3, "drop_email_domains": list(contacts_fix.DEFAULT_DROP_DOMAINS),
                  "old_email_domains": [], "labels": []}


def load_config(path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path:
        cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
    return cfg


def fix_args(cfg: dict, work: Path, args, wa_files) -> list:
    out = ["-o", str(work / "fix"), "--country", cfg["country"]]
    for d in cfg["drop_email_domains"]:
        out += ["--drop-email-domain", d]
    for d in cfg["old_email_domains"]:
        out += ["--old-email-domain", d]
    if cfg["labels"]:
        with (work / "label_map.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["kind", "pattern", "label"])
            w.writerows(cfg["labels"])
        out += ["--label-map", str(work / "label_map.csv")]
    for flag, value in (("--linkedin", args.linkedin), ("--names", args.names), ("--reach", args.reach),
                        ("--group-labels", args.group_labels)):
        if value:
            out += [flag, str(value)]
    if wa_files:
        out += ["--whatsapp", str(wa_files[0]), "--whatsapp-groups", str(wa_files[1])]
    return out


# ---- the run ----------------------------------------------------------------

def run(api, gmail_api, cfg: dict, args, work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    since = (dt.date.today() - dt.timedelta(days=int(cfg["active_years"] * 365))).isoformat()
    people = ps.fetch_all(api, ps.FULL_FIELDS)
    own = tuple(cfg.get("own_domains", []))
    others, rescued, rescue = [], set(), []
    wa_files = whatsapp_inputs(args.whatsapp, work) if args.whatsapp else None
    msg_rows = messenger_rows(args, work, wa_files)
    if getattr(args, "other_contacts", False):
        others = google_extras.other_contacts(api)
        rescue += google_extras.rescue_by_name(people, others, own)
        rescued_fps = {u["fp"] for u in rescue}
        rescue += [u for u in google_extras.attach_to_existing(people, others, own) if u["fp"] not in rescued_fps]
    if msg_rows:
        rescue += rescue_by_whatsapp(people, msg_rows, {u["fp"] for u in rescue})
    if rescue:
        # A name-only card that another source can reach gets that email or number before anything is judged.
        ps.run({"export_counts": {}, "deletes": [], "updates": rescue}, api, args.apply, work / "rescue")
        if args.apply:
            people = ps.fetch_all(api, ps.FULL_FIELDS)
        else:
            rescued = {u["fp"] for u in rescue}
    write_rows(work / "contacts.csv", to_rows(people, group_names(api)))

    activity = list(args.activity or [])
    mailed = []
    if gmail_api:
        mailed = gmail_activity(gmail_api, since)
        write_activity(work / "gmail_activity.csv", mailed)
        activity.append(work / "gmail_activity.csv")
    if msg_rows:
        activity.append(messengers.write(work / "messenger_activity.csv", msg_rows))

    triage = [str(work / "contacts.csv"), "-o", str(work / "triage"), "--active-since", since]
    if cfg.get("delete_unreachable"):
        triage.append("--delete-unreachable")
    for a in activity:
        triage += ["--activity", str(a)]
    contacts_cleanup.main(triage)
    if getattr(args, "merge", False):
        # Merging rewrites the kept cards, so it runs on its own; the next run plans the rest.
        groups, stale = merging.groups_from_triage(work / "triage" / "review.csv", people)
        summary = merging.run(api, groups, args.apply, work)
        summary["changed_since_triage"] = len(stale)
        return summary
    contacts_fix.main([str(work / "triage" / "review.csv")] + fix_args(cfg, work, args, wa_files))

    plan = json.loads((work / "fix" / "plan.json").read_text(encoding="utf-8"))
    plan["deletes"] = [d for d in plan["deletes"] if d["fp"] not in rescued]
    extra = live_plan.Plan(people)
    live_plan.streamline(extra)
    seen = {r["key"].strip().lower(): r["last_sent"] for r in mailed if r.get("last_sent")}
    for r in sorted(msg_rows, key=lambda r: r.get("last_sent", "")):  # newest date wins per number
        if r.get("last_sent"):
            seen[ps.phone_key(r["key"])] = r["last_sent"]
    if msg_rows:
        live_plan.names_from_whatsapp(extra, msg_rows)
    if args.linkedin:
        _, li_rows = contacts_fix.read_csv(args.linkedin)
        live_plan.from_linkedin(extra, li_rows)
    live_plan.complete_names(extra, msg_rows)
    live_plan.recency(extra, seen, since)
    extra.write(work / "streamline.json", work / "streamline_review.csv")
    stream = json.loads((work / "streamline.json").read_text(encoding="utf-8"))
    merge(plan, stream, prefer=("names", "organizations"))
    maps_key = getattr(args, "maps_key", None)
    if maps_key:
        updates, lines = google_extras.address_plan(people, maps_key, cfg.get("region", "DE"))
        (work / "address_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        merge(plan, {"updates": updates})
    (work / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = ps.run(plan, api, args.apply, work)
    summary["incomplete"] = write_incomplete(work / "incomplete.csv", people, plan)
    if getattr(args, "other_contacts", False):
        keep, rest = google_extras.worth_keeping(others, people, {r["key"]: r["last_sent"] for r in mailed},
                                                 since, own)
        summary.update(google_extras.copy_others(api, keep, args.apply, work))
        summary["other_contacts_to_clear_by_hand"] = len(rest)
    # Photo priority: WhatsApp, then Signal (LinkedIn and Telegram exports carry none).
    # Each pass only fills cards that still have no photo, so the earlier source wins.
    for source, folder in (("whatsapp", wa_files[2] if wa_files else None), ("signal", work / "signal_photos")):
        if folder and folder.is_dir():
            summary[f"photos_{source}"] = ps.run_photos(api, folder, args.apply, work / f"photos_{source}")
    return summary


def write_incomplete(path: Path, people: list, plan: dict) -> int:
    """Cards that will still lack a first name, a last name, or any way to
    reach them after this plan, for going through by hand."""
    deleted = {d["fp"] for d in plan["deletes"]}
    sets = {u["fp"]: u["set"] for u in plan["updates"]}
    rows = []
    for p in people:
        fp = ps.person_fingerprint(p)
        if fp in deleted:
            continue
        n = dict((p.get("names") or [{}])[0])
        n.update(sets.get(fp, {}).get("names", {}))
        phones = sets.get(fp, {}).get("phoneNumbers", p.get("phoneNumbers", []))
        emails = sets.get(fp, {}).get("emailAddresses", p.get("emailAddresses", []))
        missing = [w for w, ok in (("first name", n.get("givenName")), ("last name", n.get("familyName")),
                                   ("phone or email", phones or emails)) if not ok]
        if missing:
            rows.append({"resourceName": p["resourceName"], "name": n.get("displayName") or
                         f"{n.get('givenName', '')} {n.get('familyName', '')}".strip(),
                         "phones": "; ".join(x["value"] for x in phones), "emails": "; ".join(e["value"] for e in emails),
                         "company": ((p.get("organizations") or [{}])[0]).get("name", ""), "missing": ", ".join(missing)})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["resourceName", "name", "phones", "emails", "company", "missing"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def messenger_rows(args, work: Path, wa_files) -> list:
    """WhatsApp, Signal and Telegram rows, newest profile name per number first."""
    rows = live_plan.read_csv(wa_files[0]) if wa_files else []
    if getattr(args, "signal", False):
        rows += messengers.signal_rows(photos=work / "signal_photos")
    if getattr(args, "telegram", None):
        rows += messengers.telegram_rows(args.telegram)
    rows.sort(key=lambda r: r.get("last_sent", ""), reverse=True)
    if rows:
        messengers.write(work / "messengers.csv", rows)
    return rows


def rescue_by_whatsapp(people: list, wa_rows: list, skip: set) -> list:
    """A phone for a name-only card whose full name is exactly one WhatsApp profile name."""
    pl = live_plan.Plan([p for p in people if ps.person_fingerprint(p) not in skip])
    live_plan.whatsapp(pl, wa_rows)
    return [{"fp": ps.person_fingerprint(pl.people[rn]), "name": (pl.people[rn].get("names") or [{}])[0].get(
        "displayName", ""), "set": {"phoneNumbers": s["phoneNumbers"]}, "add_labels": []}
            for rn, s in pl.sets.items() if "phoneNumbers" in s]


def merge(plan: dict, extra: dict, prefer=()) -> None:
    """Fold the live updates into the fix plan: one entry per contact. The
    fix plan wins where both touch a field, except for `prefer` fields, which
    come from sources newer than the card (WhatsApp, LinkedIn)."""
    by_fp = {u["fp"]: u for u in plan["updates"]}
    deleted = {d["fp"] for d in plan["deletes"]}
    for u in extra["updates"]:
        if u["fp"] in deleted:
            continue
        if u["fp"] in by_fp:
            fix = by_fp[u["fp"]]["set"]
            by_fp[u["fp"]]["set"] = {**u["set"], **{k: v for k, v in fix.items() if k not in prefer or k not in u["set"]}}
        else:
            plan["updates"].append(u)
    for u in plan["updates"]:  # a list the fix plan rewrote still gets its types
        live_plan.fill_types(u["set"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-secret", type=Path, required=True, help="OAuth client JSON (Desktop app)")
    ap.add_argument("--config", type=Path, help="config.json (see config.example.json)")
    ap.add_argument("--work", type=Path, default=Path("work"), help="where everything this run writes goes")
    ap.add_argument("--apply", action="store_true", help="write the changes; without it nothing is changed")
    ap.add_argument("--gmail", action="store_true", help="use your sent mail as activity (gmail.metadata scope)")
    ap.add_argument("--whatsapp", type=Path, nargs="?", const=wa.DEFAULT_DB, metavar="DB_OR_ZIP",
                    help="use WhatsApp: the Mac's database (default) or a zip from whatsapp_activity.py --zip")
    ap.add_argument("--activity", type=Path, action="append", help="extra activity CSV (key,channel,sent,last_sent)")
    ap.add_argument("--linkedin", type=Path, help="LinkedIn Connections.csv")
    ap.add_argument("--signal", action="store_true", help="use Signal Desktop's profile names and activity (macOS)")
    ap.add_argument("--telegram", type=Path, metavar="RESULT_JSON",
                    help="Telegram Desktop export (Contacts list, JSON)")
    ap.add_argument("--names", type=Path, help="filled-in work/fix/first_name_only.csv")
    ap.add_argument("--reach", type=Path, help="filled-in work/fix/reach_suggestions.csv")
    ap.add_argument("--group-labels", type=Path, help="filled-in work/fix/groups.csv")
    ap.add_argument("--other-contacts", action="store_true",
                    help="copy Other contacts you've mailed recently into My Contacts (needs --gmail)")
    ap.add_argument("--maps-key", default=os.environ.get("MAPS_API_KEY"),
                    help="Address Validation API key (or MAPS_API_KEY): check and format addresses")
    ap.add_argument("--merge", action="store_true",
                    help="merge the duplicate groups from the triage instead (report: work/merge_report.txt)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.other_contacts and not args.gmail:
        ap.error("--other-contacts needs --gmail: only recently mailed ones are worth keeping")
    scope = " ".join([ps.SCOPE] + ([ps.GMAIL_SCOPE] if args.gmail else [])
                     + ([google_extras.OTHER_SCOPE] if args.other_contacts else []))
    token = ps.access_token(args.client_secret, scope)
    api = ps.Api(token)
    gmail_api = ps.Api(token, GMAIL) if args.gmail else None
    summary = run(api, gmail_api, cfg, args, args.work)
    print(("Applied. " if args.apply else "Dry run, nothing changed. ") + json.dumps(summary))
    print(f"Details: {args.work / 'sync_report.txt'}, {args.work / 'triage' / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
