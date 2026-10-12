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
    base = dict(apply=False, merge=False, activity=None, whatsapp=None, linkedin=None, names=None, reach=None, group_labels=None)
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


class Merge(unittest.TestCase):
    def test_merges_a_nickname_pair_into_the_fuller_card(self):
        work = Path(tempfile.mkdtemp())
        people = [
            person("people/1", "Dennis", "Herr", emails=["d@x.de"], phones=["+49 176 1111111"],
                   memberships=[{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}}]),
            person("people/2", "Dennis-Florian", "Herr", emails=["dfh@y.de"], addresses=[{"city": "Berlin"}],
                   memberships=[{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/9"}}]),
            person("people/3", "Clara", "Lang", phones=["+49 30 1234567"]),
            person("people/4", "Paul", "Lang", phones=["+49 30 1234567"]),  # shared landline: two people
        ]
        api = Api(people)
        api.groups["contactGroups/9"] = "Friends"
        calls = []
        orig = api.call

        def call(method, path, body=None, params=None):
            calls.append((method, path))
            if path.endswith(":updateContact"):
                for f in params["updatePersonFields"].split(","):
                    api.people[path[1:-len(":updateContact")]][f] = body[f]
                return {}
            return orig(method, path, body, params)
        api.call = call
        dry = pipeline.run(api, None, pipeline.DEFAULT_CONFIG, args(merge=True), work)
        self.assertEqual((dry["groups"], dry["merged"]), (1, 0))
        self.assertEqual(len(api.people), 4)
        pipeline.run(api, None, pipeline.DEFAULT_CONFIG, args(merge=True, apply=True), work)
        self.assertEqual(sorted(api.people), ["people/1", "people/3", "people/4"])
        kept = api.people["people/1"]
        self.assertEqual(kept["names"][0]["givenName"], "Dennis-Florian")
        self.assertEqual(sorted(e["value"] for e in kept["emailAddresses"]), ["d@x.de", "dfh@y.de"])
        self.assertEqual(kept["addresses"], [{"city": "Berlin"}])
        self.assertIn("Friends", labels_of(api, kept))


class WhatsAppRescue(unittest.TestCase):
    def test_name_only_card_gets_the_number_instead_of_being_deleted(self):
        people = [person("people/1", "Dora", "Weiss"), person("people/2", "Emil", "Brandt")]
        rows = [{"key": "+4917012345678", "push_name": "Dora Weiss"}, {"key": "+4917099999999", "push_name": "emil"}]
        out = pipeline.rescue_by_whatsapp(people, rows, set())
        self.assertEqual([(u["name"], u["set"]["phoneNumbers"][0]["value"]) for u in out],
                         [("Dora Weiss", "+4917012345678")])


class Incomplete(unittest.TestCase):
    def test_businesses_and_titled_names_count_as_complete(self):
        import csv
        people = [person("people/1", "Anna"),
                  {"resourceName": "people/2", "organizations": [{"name": "Pizza Italia"}],
                   "phoneNumbers": [{"value": "+4981221"}]},
                  {"resourceName": "people/3", "names": [{"honorificPrefix": "Dr", "familyName": "Dörr"}],
                   "phoneNumbers": [{"value": "+4981222"}]}]
        out = Path(tempfile.mkdtemp()) / "incomplete.csv"
        self.assertEqual(pipeline.write_incomplete(out, people, {"deletes": [], "updates": []}), 1)
        with out.open() as f:
            self.assertEqual([r["resourceName"] for r in csv.DictReader(f)], ["people/1"])


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

    def test_stops_at_the_cutoff_newest_first(self):
        fetched = []

        class G:
            def call(self, method, path, body=None, params=None):
                if path == "/users/me/messages":
                    assert ("labelIds", "SENT") in params and not any(k == "q" for k, _ in params)
                    return {"messages": [{"id": "new"}, {"id": "old"}, {"id": "older"}]}
                fetched.append(path)
                return {"internalDate": "1700000000000" if path.endswith("/new") else "1500000000000",
                        "payload": {"headers": [{"name": "To", "value": "a@x.de"}]}}
        rows = pipeline.gmail_activity(G(), "2020-01-01")
        self.assertEqual([r["key"] for r in rows], ["a@x.de"])
        self.assertEqual(len(fetched), 2)  # stopped at the first message before the cutoff


if __name__ == "__main__":
    unittest.main()
