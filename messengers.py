"""Signal and Telegram as sources, in the same row shape as WhatsApp:
key (+number), channel, sent, last_sent (YYYY-MM-DD), push_name.

Signal Desktop (macOS): its database is SQLCipher-encrypted. The key is in
config.json, itself encrypted with the "Signal Safe Storage" password from
the login keychain (Electron safeStorage: PBKDF2-SHA1 'saltysalt' 1003
rounds, AES-128-CBC, IV of 16 spaces, 'v10' prefix). Decrypting needs the
macOS `security` and `openssl` tools; reading needs the `sqlcipher` CLI
(brew install sqlcipher). Only numbers, profile names and the last-active
date are read, never messages. push_name is the name the person set in
their own Signal profile.

Telegram: the official export from Telegram Desktop (Settings -> Advanced ->
Export Telegram data -> Contacts list, JSON) gives result.json with the
names and numbers saved in Telegram.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

SIGNAL_DIR = Path.home() / "Library/Application Support/Signal"
SIGNAL_QUERY = ("SELECT e164, profileName, profileFamilyName, profileFullName, active_at "
                "FROM conversations WHERE type = 'private' AND e164 IS NOT NULL;")


def run(cmd: list, stdin: bytes = b"") -> bytes:
    return subprocess.run(cmd, input=stdin, capture_output=True, check=True).stdout


def signal_key(config: dict, runner=run) -> str:
    """The SQLCipher key as hex, from config.json's key or encryptedKey."""
    if config.get("key"):
        return config["key"]
    blob = bytes.fromhex(config["encryptedKey"])
    if not blob.startswith(b"v10"):
        raise ValueError("unknown Signal key format")
    password = runner(["security", "find-generic-password", "-ws", "Signal Safe Storage"]).strip()
    aes = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
    return runner(["openssl", "enc", "-d", "-aes-128-cbc", "-K", aes.hex(), "-iv", "20" * 16],
                  blob[3:]).decode().strip()


def signal_rows(base: Path = SIGNAL_DIR, runner=run) -> list:
    if not shutil.which("sqlcipher") and runner is run:
        raise SystemExit("Signal needs the sqlcipher tool: brew install sqlcipher")
    key = signal_key(json.loads((base / "config.json").read_text()), runner)
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ("", "-wal", "-shm"):
            src = base / "sql" / f"db.sqlite{suffix}"
            if src.exists():
                shutil.copy2(src, Path(tmp) / src.name)
        script = f"PRAGMA key = \"x'{key}'\";\n.headers on\n.mode csv\n{SIGNAL_QUERY}\n".encode()
        out = runner(["sqlcipher", str(Path(tmp) / "db.sqlite")], script).decode()
    rows = []
    for r in csv.DictReader(io.StringIO(out.split("\n", 1)[1] if out.startswith("ok") else out)):
        name = (r.get("profileFullName") or " ".join(
            x for x in (r.get("profileName"), r.get("profileFamilyName")) if x) or "").strip()
        active = int(r.get("active_at") or 0)
        day = dt.datetime.fromtimestamp(active / 1000, dt.timezone.utc).date().isoformat() if active else ""
        rows.append({"key": r["e164"], "channel": "signal", "sent": 1 if active else 0, "last_sent": day,
                     "push_name": name})
    return rows


def telegram_rows(result_json: Path) -> list:
    data = json.loads(Path(result_json).read_text(encoding="utf-8"))
    rows = []
    for c in (data.get("contacts") or {}).get("list", []):
        number = "".join(ch for ch in c.get("phone_number", "") if ch.isdigit() or ch == "+")
        if not number:
            continue
        name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
        rows.append({"key": number if number.startswith("+") else "+" + number, "channel": "telegram",
                     "sent": 0, "last_sent": (c.get("date") or "")[:10], "push_name": name})
    return rows


def write(path: Path, rows: list) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["key", "channel", "sent", "last_sent", "push_name"])
        w.writeheader()
        w.writerows(rows)
    return path
