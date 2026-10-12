import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

import contacts_cleanup as cc
import contacts_fix as cf
import people_sync as ps

HERE = Path(__file__).parent / "sample"


def person_from_row(i, row):
    """What the People API would return for one row of the sample export."""
    r = cf.Row(row, [])
    p = {"resourceName": f"people/c{i}", "etag": f"e{i}"}
    if any(row.get(k) for k in ("First Name", "Middle Name", "Last Name")):
        p["names"] = [{"givenName": row["First Name"], "middleName": row.get("Middle Name", ""),
                       "familyName": row["Last Name"], "displayName": "x", "metadata": {"primary": True}}]
    for field, kind in (("emailAddresses", "E-mail"), ("phoneNumbers", "Phone"), ("urls", "Website")):
        vals = [{"value": v, "type": t["type"].lower()} for t in cf.slot_items(r, kind) for v in [t["value"]]]
        if vals:
            p[field] = vals
    if row.get("Organization Name"):
        p["organizations"] = [{"name": row["Organization Name"], "metadata": {}}]
    if row.get("Notes"):
        p["biographies"] = [{"value": row["Notes"]}]
    return p


class FakeApi:
    def __init__(self, people):
        self.people = {p["resourceName"]: p for p in people}
        self.groups = {"contactGroups/myContacts": "myContacts"}
        self.calls = []

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path))
        if path == "/people/me/connections":
            return {"connections": [copy.deepcopy(p) for p in self.people.values()]}
        if path == "/contactGroups" and method == "GET":
            return {"contactGroups": [{"resourceName": k, "name": v} for k, v in self.groups.items()]}
        if path == "/contactGroups":
            rn = f"contactGroups/{len(self.groups)}"
            self.groups[rn] = body["contactGroup"]["name"]
            return {"resourceName": rn}
        if path == "/people:batchUpdateContacts":
            for rn, b in body["contacts"].items():
                assert b["etag"] == self.people[rn]["etag"]
                for f in body["updateMask"].split(","):
                    self.people[rn][f] = b[f]
            return {}
        if path.endswith("/members:modify"):
            for rn in body["resourceNamesToAdd"]:
                self.people[rn].setdefault("memberships", []).append(
                    {"contactGroupMembership": {"contactGroupResourceName": path[1:-len("/members:modify")]}})
            return {}
        if path == "/people:batchDeleteContacts":
            for rn in body["resourceNames"]:
                del self.people[rn]
            return {}
        raise AssertionError(path)


def labels_of(api, p):
    return [api.groups[m["contactGroupMembership"]["contactGroupResourceName"]] for m in p.get("memberships", [])]


def by_name(api, given):
    return [p for p in api.people.values() if (p.get("names") or [{}])[0].get("givenName") == given]


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        assert cc.main([str(HERE / "contacts.csv"), "-o", str(self.tmp / "t")]) == 0
        assert cf.main([str(self.tmp / "t" / "review.csv"), "-o", str(self.tmp / "f"),
                        "--label-map", str(HERE / "fix" / "label_map.csv")]) == 0
        self.plan = json.loads((self.tmp / "f" / "plan.json").read_text())
        with (HERE / "contacts.csv").open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.api = FakeApi([person_from_row(i, r) for i, r in enumerate(rows)])
        self.before = len(self.api.people)

    def test_dry_run_changes_nothing(self):
        summary = ps.run(self.plan, self.api, apply=False, out=self.tmp / "s")
        self.assertEqual(len(self.api.people), self.before)
        self.assertFalse([c for c in self.api.calls if c[0] == "POST"])
        self.assertGreater(summary["delete"], 0)
        self.assertIn("DRY RUN", (self.tmp / "s" / "sync_report.txt").read_text())

    def test_apply_deletes_updates_and_labels(self):
        summary = ps.run(self.plan, self.api, apply=True, out=self.tmp / "s")
        self.assertEqual(len(self.api.people), self.before - summary["delete"])
        self.assertFalse(by_name(self.api, "Max2000"))  # Google+ leftover without a real name
        dora = by_name(self.api, "Dora")[0]
        self.assertEqual(dora.get("urls"), [])  # dead link gone, name kept
        carla = [p["phoneNumbers"][0]["value"] for p in by_name(self.api, "Carla")]
        self.assertIn("+491609876543", carla)
        ben = by_name(self.api, "Ben")[0]
        self.assertIn("Example Corp", labels_of(self.api, ben))
        self.assertTrue((self.tmp / "s" / "undo.json").exists())

    def test_contact_changed_since_export_is_left_alone(self):
        dora = by_name(self.api, "Dora")[0]
        dora["phoneNumbers"] = [{"value": "+49 170 0000000"}]  # edited on the phone after the export
        ps.run(self.plan, self.api, apply=True, out=self.tmp / "s")
        self.assertTrue(by_name(self.api, "Dora")[0].get("urls"))
        self.assertIn("SKIPPED Dora Weiss", (self.tmp / "s" / "sync_report.txt").read_text())

    def test_twins_are_skipped_when_the_count_moved(self):
        noreply = next(p for p in self.api.people.values()
                       if (p.get("emailAddresses") or [{}])[0].get("value") == "noreply@shop.example")
        twin = copy.deepcopy(noreply)
        twin["resourceName"] = "people/twin"
        self.api.people[twin["resourceName"]] = twin
        ps.run(self.plan, self.api, apply=True, out=self.tmp / "s")
        self.assertIn(twin["resourceName"], self.api.people)
        self.assertIn(noreply["resourceName"], self.api.people)

    def test_update_keeps_other_name_parts(self):
        person = {"etag": "e", "names": [{"givenName": "hans", "familyName": "meier", "honorificPrefix": "Dr.",
                                          "displayName": "hans meier", "metadata": {}}]}
        body, mask = ps.build_update(person, {"names": {"givenName": "Hans", "middleName": "", "familyName": "Meier"}})
        self.assertEqual(mask, "names")
        self.assertEqual(body["names"], [{"givenName": "Hans", "familyName": "Meier", "honorificPrefix": "Dr."}])


class Fingerprint(unittest.TestCase):
    def test_csv_and_api_forms_agree(self):
        a = ps.fingerprint("Anna", "", "Schmidt", ["Anna@X.de"], ["0151 2345678"], [])
        b = ps.fingerprint("anna", None, "Schmidt ", ["anna@x.de"], ["+49 151 2345678"], [])
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()


class PasteLogin(unittest.TestCase):
    def test_url_then_code(self):
        import urllib.parse
        tmp = Path(tempfile.mkdtemp())
        secret = tmp / "client.json"
        secret.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "s",
                                                    "auth_uri": "https://auth", "token_uri": "https://tok"}}))
        old_path, old_post = ps.TOKEN_PATH, ps._post_form
        ps.TOKEN_PATH = tmp / "token.json"
        sent = {}
        ps._post_form = lambda url, data: sent.update(data) or {"access_token": "A", "refresh_token": "R",
                                                                 "expires_in": 3600}
        try:
            url = ps.login_url(secret)
            state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
            with self.assertRaises(SystemExit):
                ps.login_code(secret, "http://127.0.0.1:8765/?state=wrong&code=c")
            ps.login_code(secret, f"http://127.0.0.1:8765/?state={state}&code=c&scope=x")
            self.assertEqual(sent["code"], "c")
            self.assertEqual(sent["redirect_uri"], ps.PASTE_REDIRECT)
            self.assertEqual(ps.access_token(secret), "A")
            self.assertEqual(oct(ps.TOKEN_PATH.stat().st_mode & 0o777), "0o600")
        finally:
            ps.TOKEN_PATH, ps._post_form = old_path, old_post


class Photos(unittest.TestCase):
    def test_only_contacts_without_a_photo_and_unshared_numbers(self):
        tmp = Path(tempfile.mkdtemp())
        for n in ("+491511111111", "+491512222222", "+491513333333"):
            (tmp / f"{n}.jpg").write_bytes(b"\xff\xd8\xff" + n.encode())
        people = [
            {"resourceName": "people/a", "names": [{"displayName": "A"}],
             "phoneNumbers": [{"value": "0151 1111111"}], "photos": [{"default": True}]},
            {"resourceName": "people/b", "names": [{"displayName": "B"}],
             "phoneNumbers": [{"value": "+49 151 2222222"}], "photos": [{"url": "x"}]},
            {"resourceName": "people/c", "names": [{"displayName": "C"}], "phoneNumbers": [{"value": "+491513333333"}]},
            {"resourceName": "people/d", "names": [{"displayName": "D"}], "phoneNumbers": [{"value": "+491513333333"}]},
        ]
        matches, skipped = ps.photo_matches(people, tmp)
        self.assertEqual([(p["resourceName"], f.name) for p, f in matches], [("people/a", "+491511111111.jpg")])
        self.assertEqual(sorted(skipped), [("B", "already has a photo"), ("C", "number shared with another contact"),
                                           ("D", "number shared with another contact")])

    def test_apply_uploads_bytes(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "+491511111111.jpg").write_bytes(b"\xff\xd8\xffA")
        api = FakeApi([{"resourceName": "people/a", "names": [{"displayName": "A"}],
                        "phoneNumbers": [{"value": "0151 1111111"}]}])
        sent = []
        orig = api.call
        api.call = lambda m, path, body=None, params=None: sent.append((m, path, body)) or (
            orig(m, path, body, params) if m == "GET" else {})
        ps.run_photos(api, tmp, apply=True, out=tmp / "o")
        self.assertEqual(sent[-1][1], "/people/a:updateContactPhoto")
        self.assertEqual(sent[-1][2]["photoBytes"], "/9j/QQ==")


class Failures(unittest.TestCase):
    def test_per_contact_errors_are_reported(self):
        class Api(FakeApi):
            def call(self, method, path, body=None, params=None):
                if path == "/people:batchUpdateContacts":
                    return {"updateResult": {rn: {"httpStatusCode": 400, "status": {"code": 3, "message": "bad"}}
                                             for rn in body["contacts"]}}
                return super().call(method, path, body, params)
        api = Api([{"resourceName": "people/a", "etag": "e", "names": [{"givenName": "A"}]}])
        plan = {"export_counts": {}, "deletes": [], "updates": [
            {"fp": ps.person_fingerprint(api.people["people/a"]), "name": "A",
             "set": {"names": {"givenName": "B"}}, "add_labels": []}]}
        tmp = Path(tempfile.mkdtemp())
        summary = ps.run(plan, api, apply=True, out=tmp)
        self.assertEqual(summary["failed"], 1)
        self.assertIn("FAILED people/a: bad", (tmp / "sync_report.txt").read_text())


class Retry(unittest.TestCase):
    def test_gmail_quota_403_is_retried(self):
        import io
        import urllib.error
        calls = []

        def fake(req):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 403, "x", {}, io.BytesIO(b'{"message": "Quota exceeded"}'))
            return io.BytesIO(b'{"ok": 1}')

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        orig_open, orig_sleep = ps.urllib.request.urlopen, ps.time.sleep
        ps.urllib.request.urlopen = lambda req: Resp(fake(req).getvalue())
        ps.time.sleep = lambda s: None
        try:
            self.assertEqual(ps.Api("t").call("GET", "/x"), {"ok": 1})
            self.assertEqual(len(calls), 2)
        finally:
            ps.urllib.request.urlopen, ps.time.sleep = orig_open, orig_sleep


class Gravatar(unittest.TestCase):
    def test_only_cards_without_photo_and_known_pictures(self):
        import hashlib
        tmp = Path(tempfile.mkdtemp())
        known = hashlib.sha256(b"anna@x.de").hexdigest()
        people = [{"emailAddresses": [{"value": "Anna@x.de"}]},
                  {"emailAddresses": [{"value": "ben@x.de"}], "photos": [{"url": "u"}]},
                  {"emailAddresses": [{"value": "nobody@x.de"}]}]
        seen = []
        fetch = lambda url: seen.append(url) or (b"\xff\xd8\xff" if known in url else b"")
        ps.gravatar(people, tmp, fetch)
        self.assertEqual(sorted(f.name for f in tmp.iterdir()), ["anna@x.de.jpg"])
        self.assertEqual(len(seen), 2)  # ben has a photo: never asked
