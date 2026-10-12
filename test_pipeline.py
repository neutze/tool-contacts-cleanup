import argparse
import tempfile
import unittest
from pathlib import Path

import people_sync as ps
import pipeline
from test_people_sync import FakeApi, labels_of


def person(rn, given="", family="", emails=(), phones=(), urls=(), **extra):
    p = {"resourceName": rn, "etag": "e" + rn}
    if given or family:
        p["names"] = [{"givenName": given, "familyName": family, "displayName": f"{given} {family}".strip()}]
    for field, vals in (("emailAddresses", emails), ("phoneNumbers", phones), ("urls", urls)):
        if vals:
            p[field] = [{"value": v} for v in vals]
    p.update(extra)
    return p


class Api(FakeApi):
    def call(self, method, path, body=None, params=None):
        if path == "/contactGroups" and method == "GET":
            return {"contactGroups": [{"resourceName": k, "name": v, "formattedName": v} for k, v in self.groups.items()]}
        if path.endswith(":updateContactPhoto"):
            return {}
        return super().call(method, path, body, params)


def args(**kw):
    base = dict(apply=False, activity=None, whatsapp=None, linkedin=None, names=None, reach=None, group_labels=None)
    base.update(kw)
    return argparse.Namespace(**base)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.people = [
            person("people/1", "Anna", "Schmidt", emails=["anna@gmail.com"], phones=["0151 2345678"]),
            person("people/2", "Google", "Deutschland", urls=["http://www.google.com/profiles/1"]),
            person("people/3", "Dora", "Weiss", urls=["http://www.google.com/profiles/2"]),
            person("people/4", emails=["noreply@shop.example"]),
            person("people/5", addresses=[{"city": "Berlin"}]),  # nothing but an address: not blank
            person("people/6", "Ben", "Okafor", emails=["ben@tum.de"],
                   memberships=[{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}}]),
        ]
        self.cfg = dict(pipeline.DEFAULT_CONFIG, labels=[["domain", "tum.de", "TUM"]])

    def test_dry_run_plans_without_writing(self):
        api = Api(self.people)
        summary = pipeline.run(api, None, self.cfg, args(), self.work)
        self.assertEqual(summary["delete"], 2)  # brand page, no-reply
        self.assertEqual(len(api.people), 6)
        self.assertTrue((self.work / "sync_report.txt").exists())

    def test_apply(self):
        api = Api(self.people)
        pipeline.run(api, None, self.cfg, args(apply=True), self.work)
        self.assertEqual(sorted(api.people), ["people/1", "people/3", "people/5", "people/6"])
        self.assertEqual(api.people["people/3"]["urls"], [])
        self.assertEqual(api.people["people/1"]["phoneNumbers"][0]["value"], "+491512345678")
        self.assertEqual(api.people["people/1"]["emailAddresses"][0].get("type"), "home")
        self.assertIn("TUM", labels_of(api, api.people["people/6"]))
        self.assertTrue((self.work / "undo.json").exists())

    def test_second_run_finds_nothing_left(self):
        api = Api(self.people)
        pipeline.run(api, None, self.cfg, args(apply=True), self.work)
        summary = pipeline.run(api, None, self.cfg, args(), self.work)
        self.assertEqual((summary["delete"], summary["update"], summary["skipped"]), (0, 0, 0))

    def test_rows_fingerprint_like_the_api(self):
        rows = pipeline.to_rows(self.people, {})
        import contacts_fix
        self.assertEqual([contacts_fix.row_fingerprint(r) for r in rows],
                         [ps.person_fingerprint(p) for p in self.people])


class Gmail(unittest.TestCase):
    def test_sent_headers_become_activity(self):
        class G:
            def call(self, method, path, body=None, params=None):
                if path == "/users/me/messages":
                    return {"messages": [{"id": "a"}, {"id": "b"}]}
                return {"internalDate": "1700000000000" if path.endswith("/a") else "1600000000000",
                        "payload": {"headers": [{"name": "To", "value": "Anna <ANNA@x.de>, b@y.de"}]}}
        rows = {r["key"]: r for r in pipeline.gmail_activity(G(), "2020-01-01")}
        self.assertEqual(rows["anna@x.de"]["sent"], 2)
        self.assertEqual(rows["anna@x.de"]["last_sent"], "2023-11-14")


if __name__ == "__main__":
    unittest.main()
