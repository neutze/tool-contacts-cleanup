#!/usr/bin/env python3
"""Per-contact WhatsApp activity from the Mac app's local database.

Reads only the phone number, how many messages you sent, when you last sent
one, and the name the person set on their own WhatsApp profile (push name).
Message text is never selected. Works on a copy, so WhatsApp can stay open.
Writes activity.csv for contacts_cleanup.py --activity; contacts_fix.py
--whatsapp uses the push names to suggest missing last names.

Push names also come from people you only share a group with, so a number
can appear with sent = 0.

--groups also writes whatsapp_groups.csv: one line per group name and member
number, for contacts_fix.py --whatsapp-groups to turn groups into labels.

--zip PATH writes everything into one zip instead (whatsapp_activity.csv,
whatsapp_groups.csv and photos/+<number>.jpg, each profile photo WhatsApp has
cached, scaled to 512 px with macOS's sips). Nothing else is written.

Group chats are skipped. So are chats WhatsApp keys by a hidden id (@lid)
instead of a phone number; the count is printed.

If the database can't be opened, give your terminal Full Disk Access in
System Settings -> Privacy & Security.

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import shutil
import subprocess
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

DEFAULT_DB = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)

QUERY = """
SELECT s.ZCONTACTJID,
       SUM(CASE WHEN m.ZISFROMME = 1 THEN 1 ELSE 0 END),
       MAX(CASE WHEN m.ZISFROMME = 1 THEN m.ZMESSAGEDATE END)
FROM ZWACHATSESSION s
JOIN ZWAMESSAGE m ON m.ZCHATSESSION = s.Z_PK
WHERE s.ZCONTACTJID LIKE '%@s.whatsapp.net'
GROUP BY s.ZCONTACTJID
"""
PUSH_QUERY = "SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME WHERE ZJID LIKE '%@s.whatsapp.net'"
GROUP_QUERY = """
SELECT s.ZPARTNERNAME, g.ZMEMBERJID
FROM ZWAGROUPMEMBER g
JOIN ZWACHATSESSION s ON g.ZCHATSESSION = s.Z_PK
WHERE s.ZCONTACTJID LIKE '%@g.us' AND g.ZMEMBERJID LIKE '%@s.whatsapp.net'
"""


def has_columns(con, table: str, *cols) -> bool:
    found = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    return set(cols) <= found


def group_rows(db: Path):
    """(group name, +number) for every member of every group, or None when this
    WhatsApp version stores groups differently."""
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(snapshot(db, Path(tmp)))
        try:
            if not (has_columns(con, "ZWAGROUPMEMBER", "ZCHATSESSION", "ZMEMBERJID")
                    and has_columns(con, "ZWACHATSESSION", "ZPARTNERNAME")):
                return None
            result = con.execute(GROUP_QUERY).fetchall()
        finally:
            con.close()
    return sorted({((name or "").strip(), "+" + jid.split("@")[0]) for name, jid in result if (name or "").strip()})


def image_kind(path: Path) -> str:
    with path.open("rb") as f:
        head = f.read(8)
    return "jpg" if head[:3] == b"\xff\xd8\xff" else "png" if head[:8] == b"\x89PNG\r\n\x1a\n" else ""


def photo_files(db: Path) -> dict:
    """+number -> the largest cached profile picture for it. Uses the
    ZWAPROFILEPICTUREITEM table when this version has one, and also walks
    Media/Profile, because versions differ in which they keep."""
    base = db.parent
    paths = []
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(snapshot(db, Path(tmp)))
        try:
            if has_columns(con, "ZWAPROFILEPICTUREITEM", "ZJID", "ZPATH"):
                for jid, rel in con.execute("SELECT ZJID, ZPATH FROM ZWAPROFILEPICTUREITEM"):
                    if jid and rel and jid.endswith("@s.whatsapp.net"):
                        paths.append((jid.split("@")[0], base / rel))
        finally:
            con.close()
    profile_dir = base / "Media" / "Profile"
    if profile_dir.is_dir():
        for f in profile_dir.rglob("*"):
            m = re.match(r"^(\d{6,15})(?=[-@._])", f.name)
            if m and f.is_file():
                paths.append((m.group(1), f))
    best = {}
    for number, f in paths:
        if f.is_file() and image_kind(f) and (number not in best or f.stat().st_size > best[number].stat().st_size):
            best[number] = f
    return {"+" + n: f for n, f in best.items()}


def shrink(src: Path, dst: Path) -> None:
    """512 px JPEG via macOS sips; a plain copy where sips isn't there."""
    shutil.copy2(src, dst)
    if shutil.which("sips"):
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "512", str(dst), "--out", str(dst)],
                       check=False, capture_output=True)


def snapshot(db: Path, into: Path) -> Path:
    """Copy the database with its -wal and -shm files, so recent messages that
    are still only in the write-ahead log are included."""
    for suffix in ("", "-wal", "-shm"):
        src = db.with_name(db.name + suffix)
        if src.exists():
            shutil.copy2(src, into / src.name)
    return into / db.name


def rows(db: Path):
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(snapshot(db, Path(tmp)))
        try:
            hidden = con.execute(
                "SELECT COUNT(*) FROM ZWACHATSESSION WHERE ZCONTACTJID LIKE '%@lid'").fetchone()[0]
            result = con.execute(QUERY).fetchall()
            has_push = con.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'ZWAPROFILEPUSHNAME'").fetchone()
            push = dict(con.execute(PUSH_QUERY).fetchall()) if has_push else {}
        finally:
            con.close()
    out = {}
    for jid, sent, last in result:
        day = (APPLE_EPOCH + dt.timedelta(seconds=last)).date().isoformat() if sent else ""
        out[jid] = {"key": "+" + jid.split("@")[0], "channel": "whatsapp", "sent": sent or 0,
                    "last_sent": day, "push_name": ""}
    for jid, name in push.items():
        row = out.setdefault(jid, {"key": "+" + jid.split("@")[0], "channel": "whatsapp", "sent": 0,
                                   "last_sent": "", "push_name": ""})
        row["push_name"] = (name or "").strip()
    return [r for r in out.values() if r["sent"] or r["push_name"]], hidden


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="ChatStorage.sqlite (default: WhatsApp for Mac)")
    ap.add_argument("-o", "--out", type=Path, default=Path("whatsapp_activity.csv"))
    ap.add_argument("--groups", type=Path, metavar="CSV", help="also write group names and members here")
    ap.add_argument("--zip", type=Path, metavar="ZIP", help="write activity, groups and photos into this zip only")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"Not found: {args.db}\nIs WhatsApp for Mac installed, and does this terminal have Full Disk Access?",
              file=sys.stderr)
        return 1
    try:
        found, hidden = rows(args.db)
    except (sqlite3.Error, PermissionError) as e:
        print(f"Could not read {args.db}: {e}", file=sys.stderr)
        return 1

    if args.zip:
        return write_zip(args.db, args.zip, found, hidden)
    with args.out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["key", "channel", "sent", "last_sent", "push_name"])
        w.writeheader()
        w.writerows(sorted(found, key=lambda r: r["last_sent"], reverse=True))
    wrote = sum(1 for r in found if r["sent"])
    named = sum(1 for r in found if r["push_name"])
    print(f"{wrote} chats you wrote in, {named} profile names -> {args.out}  "
          f"({hidden} chats with a hidden number skipped)")

    if args.groups:
        members = group_rows(args.db)
        if members is None:
            print("This WhatsApp version stores groups differently; no group file written.", file=sys.stderr)
        else:
            with args.groups.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["group", "key"])
                w.writerows(members)
            print(f"{len({g for g, _ in members})} groups, {len(members)} memberships -> {args.groups}")
    return 0


def write_zip(db: Path, target: Path, found: list, hidden: int) -> int:
    members = group_rows(db) or []
    photos = photo_files(db)
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["key", "channel", "sent", "last_sent", "push_name"])
        w.writeheader()
        w.writerows(sorted(found, key=lambda r: r["last_sent"], reverse=True))
        z.writestr("whatsapp_activity.csv", buf.getvalue())
        buf = io.StringIO()
        g = csv.writer(buf)
        g.writerow(["group", "key"])
        g.writerows(members)
        z.writestr("whatsapp_groups.csv", buf.getvalue())
        for number, src in photos.items():
            dst = Path(tmp) / f"{number}.jpg"
            shrink(src, dst)
            z.write(dst, f"photos/{number}.jpg")
    print(f"{sum(1 for r in found if r['sent'])} chats you wrote in, {sum(1 for r in found if r['push_name'])} "
          f"profile names, {len({x for x, _ in members})} groups, {len(photos)} photos -> {target} "
          f"({hidden} chats with a hidden number skipped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
