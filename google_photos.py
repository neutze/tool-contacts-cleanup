#!/usr/bin/env python3
"""Give Google Contacts a picture from your Google Photos library.

  python3 google_photos.py pick   --client-secret client_secret.json   # choose photos in Google Photos
  python3 google_photos.py assign --client-secret client_secret.json   # say who each face is
  python3 google_photos.py apply  --client-secret client_secret.json   # dry run
  python3 google_photos.py apply  --client-secret client_secret.json --apply

Google gives no app the face groups or their names, and since 2025 the Photos
Library API reads only what the app itself uploaded. What is left is the
Photos Picker API: you choose the photos, the app gets exactly those.

  pick    opens Google Photos; choose any number of photos and press Done.
          Each is downloaded, every face in it is cut out as a square, and a
          photo without a face is cut from its middle.
  assign  opens a local page with every face; type the contact's name next to
          it. Leave a face empty to skip it.
  apply   sets the picture, only on a card that has no picture of its own. A
          Google profile picture is the other person's, not the card's own, so
          such a card still gets one. A picture you set is never replaced.

Everything is kept in --work (default work/photos): picked/, crops/,
assignments.json and photo_report.txt. Run pick again to add more photos.

Setup, once: enable the Photos Picker API in the Cloud project of your OAuth
client. The first run opens a browser to log in; this login is kept in its
own file (photos_token.json) so the one for contacts and Gmail stays as it is.

Standard library only, plus face_crop.swift for the faces (macOS).
"""
from __future__ import annotations

import argparse
import base64
import html
import http.server
import json
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
from pathlib import Path

import people_sync as ps

PICKER = "https://photospicker.googleapis.com/v1"
PICKER_SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
SCOPES = f"{ps.SCOPE} {PICKER_SCOPE}"
PHOTOS_TOKEN = ps.TOKEN_PATH.with_name("photos_token.json")
SWIFT = Path(__file__).with_name("face_crop.swift")
PEOPLE_FIELDS = "names,emailAddresses,phoneNumbers,photos"
DOWNLOAD_SIZE = 1600  # longest side asked from Google Photos
PHOTO_SIZE = 720      # side of the square given to the contact


def token(client_secret: Path, force_refresh: bool = False) -> str:
    # Logging in for one more scope would replace the saved login and drop its
    # Gmail scopes, so this tool keeps a login of its own.
    ps.TOKEN_PATH = PHOTOS_TOKEN
    return ps.access_token(client_secret, SCOPES, force_refresh)


# ---- the rule ---------------------------------------------------------------

def has_own_photo(person: dict) -> bool:
    """True when the card carries a picture set on the card itself. The
    picture of the person's Google profile does not count."""
    return any(not ph.get("default") and ph.get("metadata", {}).get("source", {}).get("type") == "CONTACT"
               for ph in person.get("photos", []))


def display_name(person: dict) -> str:
    return (person.get("names") or [{}])[0].get("displayName", "") or "(no name)"


def contact_labels(people: list) -> dict:
    """label -> resourceName for every card that may get a picture. Cards that
    share a name are told apart by their first email or number."""
    free = [p for p in people if not has_own_photo(p)]
    names = Counter(display_name(p) for p in free)
    labels = {}
    for p in free:
        label = display_name(p)
        if names[label] > 1 or label == "(no name)":
            extra = next((x["value"] for f in ("emailAddresses", "phoneNumbers") for x in p.get(f, [])
                          if x.get("value")), p["resourceName"])
            label = f"{label} · {extra}"
        if label in labels:
            label = f"{label} · {p['resourceName']}"
        labels[label] = p["resourceName"]
    return labels


# ---- picking ----------------------------------------------------------------

def _seconds(text: str, default: float) -> float:
    try:
        return float((text or "").rstrip("s"))
    except ValueError:
        return default


def download(url: str, access_token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def pick(api, out: Path, fetch, open_url=webbrowser.open, sleep=time.sleep) -> list:
    """One picker session: wait until photos are chosen, save them to
    out/picked and list them in out/picked.json. Returns the new files."""
    picked_dir = out / "picked"
    picked_dir.mkdir(parents=True, exist_ok=True)
    index_path = out / "picked.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    known = {v["id"] for v in index.values()}

    session = api.call("POST", "/sessions", body={})
    try:
        print(f"Choose the photos in Google Photos, then press Done:\n{session['pickerUri']}")
        open_url(session["pickerUri"])
        waited, limit = 0.0, _seconds(session.get("pollingConfig", {}).get("timeoutIn"), 1800)
        while not session.get("mediaItemsSet"):
            pause = _seconds(session.get("pollingConfig", {}).get("pollInterval"), 5)
            if waited >= limit:
                raise SystemExit("No photos were chosen before the picker timed out.")
            sleep(pause)
            waited += pause
            session = api.call("GET", f"/sessions/{session['id']}")

        items, page = [], None
        while True:
            params = {"sessionId": session["id"], "pageSize": 100}
            if page:
                params["pageToken"] = page
            got = api.call("GET", "/mediaItems", params=params)
            items += got.get("mediaItems", [])
            page = got.get("nextPageToken")
            if not page:
                break

        new, number = [], max([int(Path(n).stem[1:]) for n in index] or [0])
        for item in items:
            if item.get("type", "PHOTO") != "PHOTO" or item["id"] in known:
                continue
            number += 1
            name = f"p{number:04d}.jpg"
            data = fetch(f"{item['mediaFile']['baseUrl']}=w{DOWNLOAD_SIZE}-h{DOWNLOAD_SIZE}")
            (picked_dir / name).write_bytes(data)
            index[name] = {"id": item["id"], "filename": item["mediaFile"].get("filename", ""),
                           "createTime": item.get("createTime", "")}
            known.add(item["id"])
            new.append(picked_dir / name)
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
        return new
    finally:
        api.call("DELETE", f"/sessions/{session['id']}")


# ---- cropping ---------------------------------------------------------------

def square_around(face, width: int, height: int, scale: float = 2.2) -> tuple:
    """(x, y, side) of the square cut for one face box (x, y, w, h): the face
    with room for hair and shoulders, kept inside the picture."""
    x, y, w, h = face
    side = int(min(max(w, h) * scale, width, height))
    cx, cy = x + w / 2, y + h / 2 - h * 0.1  # a face box ends at the eyebrows: lean up for the hair
    left = int(min(max(cx - side / 2, 0), width - side))
    top = int(min(max(cy - side / 2, 0), height - side))
    return left, top, side


def crop_jobs(found: dict, crops_dir: Path, size: int = PHOTO_SIZE, max_faces: int = 8) -> list:
    """The cuts for face_crop.swift: one per face that is not a bystander in
    the background (under 40 % of the largest face), or the middle of a photo
    without a face."""
    jobs = []
    for path in sorted(found):
        info, stem = found[path], Path(path).stem
        width, height = info["width"], info["height"]
        faces = sorted(info.get("faces", []), key=lambda f: -max(f[2], f[3]))
        faces = [f for f in faces if max(f[2], f[3]) >= 0.4 * max(faces[0][2], faces[0][3])][:max_faces]
        if not faces:
            side = min(width, height)
            cuts = [("noface", ((width - side) // 2, (height - side) // 2, side))]
        else:
            cuts = [(str(i + 1), square_around(f, width, height)) for i, f in enumerate(faces)]
        for tag, (x, y, side) in cuts:
            jobs.append({"src": path, "dst": str(crops_dir / f"{stem}_{tag}.jpg"),
                         "x": x, "y": y, "side": side, "size": min(size, side)})
    return jobs


def crop_faces(files: list, crops_dir: Path) -> list:
    if not files:
        return []
    crops_dir.mkdir(parents=True, exist_ok=True)
    try:
        found = subprocess.run(["swift", str(SWIFT), "faces", *map(str, files)],
                               capture_output=True, check=True).stdout
        jobs = crop_jobs(json.loads(found), crops_dir)
        subprocess.run(["swift", str(SWIFT), "crop"], input=json.dumps(jobs).encode(),
                       capture_output=True, check=True)
    except FileNotFoundError:
        raise SystemExit("Cutting out faces needs `swift` (macOS with the Xcode command line tools).")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"face_crop.swift failed: {e.stderr.decode(errors='replace')[:500]}")
    return [Path(j["dst"]) for j in jobs if Path(j["dst"]).exists()]


# ---- assigning --------------------------------------------------------------

def load_assignments(out: Path) -> dict:
    path = out / "assignments.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def set_assignment(assignments: dict, labels: dict, crop: str, label: str) -> str:
    """Record who is on one crop; an empty label clears it. Returns "" or why
    the label was not taken."""
    label = label.strip()
    if not label:
        assignments.pop(crop, None)
        return ""
    if label not in labels:
        return "no such contact without a photo"
    if any(c != crop and a["resourceName"] == labels[label] for c, a in assignments.items()):
        return "this contact already has another face assigned"
    assignments[crop] = {"resourceName": labels[label], "name": label}
    return ""


PAGE = """<!doctype html><meta charset="utf-8"><title>Who is this?</title>
<style>
 body{font:15px system-ui;margin:24px;background:#f4f4f2;color:#222}
 @media (prefers-color-scheme:dark){body{background:#1c1c1c;color:#eee}.card{background:#2a2a2a!important}}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}
 .card{background:#fff;border-radius:10px;padding:10px}
 img{width:100%%;border-radius:8px;display:block}
 input{width:100%%;box-sizing:border-box;margin-top:8px;padding:6px;font:inherit}
 .note{font-size:12px;min-height:16px;color:#b3261e}.ok{color:#2e7d32}
 button{font:inherit;padding:8px 18px;margin:16px 0}
</style>
<h1>Who is this?</h1>
<p>Type the contact's name next to each face. Only contacts without a photo of their own are offered.
Leave a face empty to skip it. Every entry is saved at once.</p>
<datalist id="contacts">%(options)s</datalist>
<div class="grid">%(cards)s</div>
<button onclick="fetch('done',{method:'POST'}).then(()=>document.body.innerHTML='<h1>Saved. You can close this tab.</h1>')">Done</button>
<script>
document.querySelectorAll('input').forEach(i=>i.addEventListener('change',async()=>{
  const r=await(await fetch('assign',{method:'POST',body:JSON.stringify({crop:i.dataset.crop,label:i.value})})).json();
  const n=i.nextElementSibling; n.textContent=r.error||(i.value?'saved':''); n.className='note'+(r.error?'':' ok');
}));
</script>"""


def page(crops: list, labels: dict, assignments: dict) -> str:
    cards = "".join(
        f'<div class="card"><img src="crop/{html.escape(c)}" loading="lazy">'
        f'<input list="contacts" data-crop="{html.escape(c)}" placeholder="{html.escape(c)}" '
        f'value="{html.escape(assignments.get(c, {}).get("name", ""))}"><div class="note"></div></div>'
        for c in crops)
    options = "".join(f'<option value="{html.escape(l)}">' for l in sorted(labels, key=str.lower))
    return PAGE % {"cards": cards, "options": options}


def serve_assign(out: Path, labels: dict, open_url=webbrowser.open) -> dict:
    """The local page; returns the assignments once Done is pressed."""
    crops_dir = out / "crops"
    crops = sorted(f.name for f in crops_dir.glob("*.jpg"))
    assignments = {c: a for c, a in load_assignments(out).items() if c in crops}
    key, state = secrets.token_urlsafe(16), {"done": False}  # the key keeps other web pages out

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, body: bytes, kind: str, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def route(self):
            parts = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).strip("/").split("/")
            return parts[1:] if parts and parts[0] == key else None

        def do_GET(self):
            route = self.route()
            if route == [""] or route == []:
                self.reply(page(crops, labels, assignments).encode(), "text/html; charset=utf-8")
            elif route and len(route) == 2 and route[0] == "crop" and route[1] in crops:
                self.reply((crops_dir / route[1]).read_bytes(), "image/jpeg")
            else:
                self.reply(b"not found", "text/plain", 404)

        def do_POST(self):
            route = self.route()
            if route == ["done"]:
                state["done"] = True
                self.reply(b"{}", "application/json")
            elif route == ["assign"]:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                error = "unknown face" if body.get("crop") not in crops else \
                    set_assignment(assignments, labels, body["crop"], body.get("label", ""))
                (out / "assignments.json").write_text(json.dumps(assignments, ensure_ascii=False, indent=1),
                                                      encoding="utf-8")
                self.reply(json.dumps({"error": error}).encode(), "application/json")
            else:
                self.reply(b"not found", "text/plain", 404)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/{key}/"
    print(f"{len(crops)} faces to assign. Press Done on the page when finished:\n{url}", flush=True)
    open_url(url)
    while not state["done"]:
        server.handle_request()
    server.server_close()
    return assignments


# ---- applying ---------------------------------------------------------------

def plan(assignments: dict, people: list, crops_dir: Path) -> tuple:
    """(person, file) for every assignment that still holds against the live
    contacts, and (name, why) for the ones that don't."""
    by_rn = {p["resourceName"]: p for p in people}
    matches, skipped, taken = [], [], set()
    for crop in sorted(assignments):
        a = assignments[crop]
        person, f = by_rn.get(a["resourceName"]), crops_dir / crop
        if person is None:
            skipped.append((a["name"], "contact no longer exists"))
        elif has_own_photo(person):
            skipped.append((a["name"], "already has a photo of its own"))
        elif a["resourceName"] in taken:
            skipped.append((a["name"], f"a second face ({crop}) for the same contact"))
        elif not f.exists():
            skipped.append((a["name"], f"{crop} is missing"))
        else:
            taken.add(a["resourceName"])
            matches.append((person, f))
    return matches, skipped


def run_apply(api, out: Path, apply: bool) -> dict:
    people = ps.fetch_all(api, PEOPLE_FIELDS)
    matches, skipped = plan(load_assignments(out), people, out / "crops")
    lines = [f"PHOTO {display_name(p)} <- {f.name}" for p, f in matches]
    lines += [f"SKIPPED {n}: {why}" for n, why in skipped]
    summary = {"live_contacts": len(people), "photo": len(matches), "skipped": len(skipped)}
    (out / "photo_report.txt").write_text(f"{'APPLIED' if apply else 'DRY RUN'}: {json.dumps(summary)}\n\n"
                                          + "\n".join(lines) + "\n", encoding="utf-8")
    if apply:
        for p, f in matches:
            api.call("PATCH", f"/{p['resourceName']}:updateContactPhoto",
                     body={"photoBytes": base64.b64encode(f.read_bytes()).decode(), "personFields": "photos"})
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=("pick", "assign", "apply"))
    ap.add_argument("--client-secret", type=Path, required=True, help="OAuth client JSON (Desktop app)")
    ap.add_argument("--work", type=Path, default=Path("work/photos"))
    ap.add_argument("--apply", action="store_true", help="with `apply`: set the photos; without it nothing is changed")
    args = ap.parse_args(argv)
    out = args.work
    out.mkdir(parents=True, exist_ok=True)

    def refresh():
        return token(args.client_secret, force_refresh=True)

    if args.step == "pick":
        picker = ps.Api(token(args.client_secret), base=PICKER, refresh=refresh)
        new = pick(picker, out, fetch=lambda url: download(url, picker.token))
        crops = crop_faces(new, out / "crops")
        print(f"{len(new)} photos saved, {len(crops)} faces cut out in {out / 'crops'}. Next: assign")
        return 0
    people_api = ps.Api(token(args.client_secret), refresh=refresh)
    if args.step == "assign":
        labels = contact_labels(ps.fetch_all(people_api, PEOPLE_FIELDS))
        assignments = serve_assign(out, labels)
        print(f"{len(assignments)} faces assigned. Next: apply")
        return 0
    summary = run_apply(people_api, out, args.apply)
    print(("Applied. " if args.apply else "Dry run, nothing changed. ") + json.dumps(summary))
    print(f"Details: {out / 'photo_report.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
