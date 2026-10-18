import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import iris_sync as iris


def person(rn, given, family="", birthday=None, phones=(), org=""):
    p = {"resourceName": rn, "etag": f"e-{rn}", "names": [{"givenName": given, "familyName": family,
                                                           "displayName": " ".join(x for x in (given, family) if x)}]}
    if not given and not family:
        p["names"] = [{"displayName": org}]
    if birthday:
        p["birthdays"] = [{"date": birthday, "metadata": {"primary": True}}]
    if phones:
        p["phoneNumbers"] = [{"value": v} for v in phones]
    if org:
        p["organizations"] = [{"name": org}]
    return p


class FakeApi:
    def __init__(self, people):
        self.people = {p["resourceName"]: p for p in people}
        self.calls = []

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if path == "/people/me/connections":
            return {"connections": [copy.deepcopy(p) for p in self.people.values()]}
        if method == "GET":
            return copy.deepcopy(self.people[path[1:]])
        if path == "/people:createContact":
            rn = f"people/new{len(self.people)}"
            self.people[rn] = {"resourceName": rn, "etag": "x", **body}
            return {"resourceName": rn}
        if path.endswith(":updateContact"):
            rn = path[1:-len(":updateContact")]
            assert body["etag"] == self.people[rn]["etag"]
            self.people[rn]["birthdays"] = body["birthdays"]
            return {}
        raise AssertionError(path)


class Birthdays(unittest.TestCase):
    def test_iso_both_ways(self):
        self.assertEqual(iris.iso({"year": 1985, "month": 3, "day": 12}), "1985-03-12")
        self.assertEqual(iris.iso({"month": 3, "day": 12}), "--03-12")
        self.assertEqual(iris.parse_iso("--03-12"), {"month": 3, "day": 12})
        self.assertEqual(iris.parse_iso("1985-02-30"), {})
        self.assertEqual(iris.parse_iso("1985"), {})

    def test_text_only_birthday_is_none(self):
        p = person("people/1", "Anna")
        p["birthdays"] = [{"text": "spring"}]
        self.assertEqual(iris.birthday_of(p), {})


class Export(unittest.TestCase):
    def test_only_people_with_name_birthday_and_id(self):
        text = iris.vcard([
            person("people/1", "Anna", "Müller", {"year": 1985, "month": 3, "day": 12}, phones=["+49 151 1"]),
            person("people/2", "Ben", "", {"month": 7, "day": 1}),
            person("people/3", "", "", org="ACME GmbH"),
            person("people/4", "Café; Bar", "O,Neil"),
        ])
        self.assertIn("UID:people/1\r\nFN:Anna Müller\r\nN:Müller;Anna;;;\r\nBDAY:19850312", text)
        self.assertIn("BDAY:--0701", text)
        self.assertNotIn("ACME", text)
        self.assertNotIn("151", text)  # no phone number leaves Google
        self.assertIn("FN:Café\\; Bar O\\,Neil", text)

    def test_written_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".iris" / "contacts.vcf"
            iris.write_atomic(target, "new")
            iris.write_atomic(target, "newer")
            self.assertEqual(target.read_text(), "newer")
            self.assertEqual([f.name for f in target.parent.iterdir()], ["contacts.vcf"])


class Wishes(unittest.TestCase):
    people = [person("people/1", "Anna", "M", phones=["0151 2345 6789"]),
              person("people/2", "Ben", "K", {"year": 1980, "month": 1, "day": 2}, phones=["+49 160 1111111"]),
              person("people/3", "Cem", "S", phones=["+49 170 2222222"]),
              person("people/4", "Dora", "A", phones=["+49 170 3333333"]),
              person("people/5", "Eva", "A", phones=["+49 170 3333333"])]

    def test_one_day_per_contact_without_birthday(self):
        rows = [("+4915123456789", dt.date(2023, 3, 12)), ("+4915123456789", dt.date(2024, 3, 12)),
                ("+491601111111", dt.date(2023, 5, 5)),      # Ben has a birthday: no suggestion
                ("+491702222222", dt.date(2023, 6, 1)), ("+491702222222", dt.date(2024, 6, 2)),  # two days
                ("+491703333333", dt.date(2023, 8, 8))]      # a shared number
        rows.append(("+491601111111", dt.date(2023, 1, 2)))  # on Ben's own day: no suggestion
        self.assertEqual(iris.wishes(rows[-1:], self.people), [])
        self.assertEqual(iris.wishes(rows[:-1], self.people),
                         [{"uid": "people/1", "name": "Anna M", "month": 3, "day": 12, "years": [2023, 2024], "google": ""},
                          ])  # Ben has a birthday in Google: it stands

    def test_query_never_selects_text(self):
        select = iris.WISH_QUERY.split("FROM")[0]
        self.assertNotIn("ZTEXT", select)


class AndroidWishes(unittest.TestCase):
    def make_db(self, path, with_map=True):
        import sqlite3
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, user TEXT, server TEXT, raw_string TEXT);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER);
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                                  timestamp INTEGER, text_data TEXT);
            INSERT INTO jid VALUES (1, '4915123456789', 's.whatsapp.net', NULL), (2, '123', 'g.us', NULL),
                                   (3, '999', 'lid', NULL), (4, '491702222222', 's.whatsapp.net', NULL);
            INSERT INTO chat VALUES (1, 1), (2, 2), (3, 3);
        """)
        if with_map:
            con.executescript("CREATE TABLE jid_map (lid_row_id INTEGER, jid_row_id INTEGER); INSERT INTO jid_map VALUES (3, 4);")
        noon = int(dt.datetime(2019, 3, 12, 12).timestamp() * 1000)
        con.executemany("INSERT INTO message (chat_row_id, from_me, timestamp, text_data) VALUES (?, ?, ?, ?)", [
            (1, 1, noon, "Alles Gute zum Geburtstag!"), (1, 0, noon, "Happy birthday back"),  # received: not a wish
            (1, 1, noon, "see you"), (2, 1, noon, "HBD"),  # a group
            (3, 1, noon, "happy bday")])
        con.commit()
        con.close()

    def test_sent_one_to_one_wishes_with_hidden_ids_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "msgstore.db"
            self.make_db(db)
            self.assertEqual(sorted(iris.android_wish_rows(db)),
                             [("+4915123456789", dt.date(2019, 3, 12)), ("+491702222222", dt.date(2019, 3, 12))])
            db2 = Path(tmp) / "old.db"
            self.make_db(db2, with_map=False)
            self.assertEqual(iris.android_wish_rows(db2), [("+4915123456789", dt.date(2019, 3, 12))])

    def test_rows_survive_the_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "android-wishes.json"
            rows = [("+491", dt.date(2019, 3, 12)), ("+491", dt.date(2019, 3, 12))]
            iris.save_rows(path, rows)
            self.assertEqual(iris.load_rows(path), rows[:1])
            self.assertEqual(iris.load_rows(Path(tmp) / "none.json"), [])

    def test_query_never_selects_text(self):
        select = iris.ANDROID_WISH_QUERY.split("FROM")[0]
        self.assertNotIn("text_data", select)

    def test_not_a_msgstore(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "x.db"
            sqlite3.connect(db).close()
            with self.assertRaises(sqlite3.DatabaseError):
                iris.android_wish_rows(db)


class Answers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pictures = Path(self.tmp.name)
        (self.pictures / ".iris").mkdir()
        self.undo = iris.UNDO
        iris.UNDO = self.pictures / "undo.jsonl"

    def tearDown(self):
        iris.UNDO = self.undo
        self.tmp.cleanup()

    def answer(self, **kw):
        with (self.pictures / ".iris/contacts-answers.jsonl").open("a") as f:
            f.write(json.dumps({"answer": "yes", **kw}) + "\n")

    def sent(self):
        return iris.read_jsonl(self.pictures / ".iris/contacts-sent.jsonl")

    def run_(self, api, apply=True):
        return iris.run(api, self.pictures, apply, None, log=lambda *_: None, android_rows=self.pictures / "none.json")

    def test_adds_a_missing_birthday_once(self):
        api = FakeApi([person("people/1", "Anna", "M")])
        self.answer(id="a1", kind="add-birthday", uid="people/1", birthday="1985-03-12")
        self.assertEqual(self.run_(api)["sent"], 1)
        self.assertEqual(api.people["people/1"]["birthdays"], [{"date": {"year": 1985, "month": 3, "day": 12}}])
        self.run_(api)
        self.assertEqual(len([c for c in api.calls if c[1].endswith(":updateContact")]), 1)
        self.assertIn('"before"', iris.UNDO.read_text())

    def test_never_overwrites_without_the_overwrite_card(self):
        api = FakeApi([person("people/1", "Anna", "M", {"year": 1984, "month": 3, "day": 12})])
        self.answer(id="a1", kind="add-birthday", uid="people/1", birthday="1985-03-12")
        self.run_(api)
        self.assertEqual(api.people["people/1"]["birthdays"][0]["date"]["year"], 1984)
        self.assertEqual(self.sent()[0]["why"], "Google has 1984-03-12 now")

    def test_adds_the_year_to_googles_day(self):
        api = FakeApi([person("people/1", "Anna", "M", {"month": 3, "day": 12})])
        self.answer(id="a1", kind="add-birthday", uid="people/1", birthday="1985-03-12")
        self.run_(api)
        self.assertEqual(api.people["people/1"]["birthdays"][0]["date"]["year"], 1985)

    def test_overwrite_only_while_google_has_what_the_card_showed(self):
        api = FakeApi([person("people/1", "Anna", "M", {"year": 1984, "month": 3, "day": 12})])
        self.answer(id="a1", kind="overwrite", uid="people/1", birthday="1985-03-12", was="1983-01-01")
        self.answer(id="a2", kind="overwrite", uid="people/1", birthday="1985-03-12", was="1984-03-12")
        self.run_(api)
        self.assertEqual([s["result"] for s in self.sent()], ["skipped", "sent"])
        self.assertEqual(api.people["people/1"]["birthdays"][0]["date"]["year"], 1985)

    def test_creates_a_person_unless_one_exists_now(self):
        api = FakeApi([person("people/1", "Ben", "Kühn")])
        self.answer(id="a1", kind="create", name="Anna Maria Müller", birthday="1985-03-12")
        self.answer(id="a2", kind="create", name="ben kuhn", birthday="")
        self.run_(api)
        new = [p for rn, p in api.people.items() if rn.startswith("people/new")]
        self.assertEqual(new[0]["names"], [{"givenName": "Anna Maria", "familyName": "Müller"}])
        self.assertEqual(self.sent()[1]["why"], "a contact of that name exists now")

    def test_a_wish_adds_day_and_month_only(self):
        api = FakeApi([person("people/1", "Anna", "M")])
        self.answer(id="a1", kind="wish", uid="people/1", birthday="--03-12")
        self.run_(api)
        self.assertEqual(api.people["people/1"]["birthdays"], [{"date": {"month": 3, "day": 12}}])

    def test_no_and_take_and_preview_send_nothing(self):
        api = FakeApi([person("people/1", "Anna", "M")])
        self.answer(id="a1", kind="add-birthday", uid="people/1", birthday="1985-03-12", answer="no")
        self.answer(id="a2", kind="take", uid="people/1", birthday="1985-03-12")
        self.answer(id="a3", kind="add-birthday", uid="people/1", birthday="1985-03-12")
        self.assertEqual(self.run_(api, apply=False), {"contacts": 1, "with birthday": 0, "would send": 1})
        self.assertNotIn("birthdays", api.people["people/1"])
        self.assertEqual(self.sent(), [])

    def test_a_half_written_last_line_waits(self):
        path = self.pictures / ".iris/contacts-answers.jsonl"
        path.write_text('{"id": "a1", "answer": "yes", "kind": "wish"}\n{"id": "a2", "ans')
        self.assertEqual([a["id"] for a in iris.read_jsonl(path)], ["a1"])

    def test_the_export_is_written_by_every_run(self):
        api = FakeApi([person("people/1", "Anna", "M", {"year": 1985, "month": 3, "day": 12})])
        self.run_(api, apply=False)
        self.assertIn("BDAY:19850312", (self.pictures / ".iris/contacts.vcf").read_text())

    def test_nothing_is_ever_deleted(self):
        src = Path(iris.__file__).read_text()
        for word in ("unlink", "rmtree", "batchDeleteContacts", "deleteContact", "os.remove"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
