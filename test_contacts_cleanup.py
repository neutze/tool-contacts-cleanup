import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

import contacts_cleanup as cc
import whatsapp_activity as wa

SAMPLE = Path(__file__).parent / "sample" / "contacts.csv"
ACTIVITY = Path(__file__).parent / "sample" / "activity.csv"


def read(path):
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(*extra):
    out = Path(tempfile.mkdtemp())
    assert cc.main([str(SAMPLE), "-o", str(out), *extra]) == 0
    rows = read(out / "review.csv")
    return out, rows


class PhoneKey(unittest.TestCase):
    def test_international_and_national_forms_match(self):
        self.assertEqual(cc.phone_key("+49 151 2345678"), cc.phone_key("0151 2345678"))
        self.assertEqual(cc.phone_key("0049 160 9876543"), cc.phone_key("+49 160 9876543"))

    def test_short_numbers_are_ignored(self):
        self.assertEqual(cc.phone_key("112"), "")


class NoReply(unittest.TestCase):
    def test_sending_domains_count(self):
        for e in ("noreply@shop.example", "repo@noreply.github.com", "reply+abc@reply.github.com",
                  "do-not-reply@bank.example"):
            self.assertTrue(cc.is_noreply(e), e)
        for e in ("anna@gmail.com", "info@feierreisen.de", "replyguy@example.com"):
            self.assertFalse(cc.is_noreply(e), e)


class Flags(unittest.TestCase):
    def setUp(self):
        _, self.rows = run()

    def action(self, row_no):
        return self.rows[row_no - 2]["suggested_action"]

    def test_blank_and_noreply_rows_are_delete_candidates(self):
        self.assertEqual(self.action(8), "delete-candidate")  # noreply@
        self.assertEqual(self.action(9), "delete-candidate")  # blank

    def test_import_batch_is_flagged_for_review(self):
        for r in (5, 6, 7):
            self.assertEqual(self.action(r), "review")
            self.assertIn("import_label", self.rows[r - 2]["cleanup_flags"])

    def test_duplicates_grouped_by_phone_and_name(self):
        anna = {self.rows[0]["dup_group"], self.rows[1]["dup_group"]}
        carla = {self.rows[8]["dup_group"], self.rows[9]["dup_group"]}
        self.assertEqual(len(anna), 1)
        self.assertEqual(len(carla), 1)
        self.assertNotEqual(anna, carla)
        self.assertEqual(self.action(4), "keep")  # Ben

    def test_google_plus_leftover_with_a_real_name_is_kept(self):
        self.assertIn("dead_profile", self.rows[10]["cleanup_flags"])  # Dora Weiss
        self.assertEqual(self.action(12), "keep")

    def test_google_plus_leftover_without_a_real_name_is_delete_candidate(self):
        self.assertEqual(self.action(14), "delete-candidate")  # last name only
        self.assertEqual(self.action(15), "delete-candidate")  # digits in the name

    def test_google_plus_brand_page_is_delete_candidate(self):
        c = cc.parse({"First Name": "Google", "Last Name": "Deutschland",
                      "Website 1 - Value": "http://www.google.com/profiles/1"}, 2, "t")
        cc.flag(c)
        self.assertIn("brand_page", c.flags)
        self.assertEqual(c.action, "delete-candidate")

    def test_name_without_email_or_phone_is_kept_when_the_name_is_real(self):
        self.assertIn("no_contact", self.rows[11]["cleanup_flags"])  # Emil Brandt
        self.assertEqual(self.action(13), "keep")


class RealName(unittest.TestCase):
    def test_accepts(self):
        for first, last in [("Anna", "Schmidt"), ("Jean-Luc", "Picard"), ("Sean", "O'Brien"),
                            ("José", "García Márquez"), ("anna", "schmidt"), ("Eva", "Maier (Huber)"),
                            ("Dr.", "Who"), ("Zoë", "Ünal")]:
            self.assertTrue(cc.real_name(first, last), (first, last))

    def test_rejects(self):
        for first, last in [("", "Schmidt"), ("Anna", ""), ("J.", "Smith"), ("Anna", "S"),
                            ("Max2000", "Power"), ("Max", "+49 151"), ("►Max", "Power"), ("Max", "Power!")]:
            self.assertFalse(cc.real_name(first, last), (first, last))


class OddNames(unittest.TestCase):
    def test_handles_are_delete_candidates_real_names_are_not(self):
        for name, odd in (("Ali W33d", True), ("Airbnb2", True), ("Rg", True), ("Anna 🌴", True),
                          ("Anna Schmidt", False), ("Zoë Ünal", False), ("Taxi", False)):
            c = cc.parse({"First Name": name, "Phone 1 - Value": "+491511111111"}, 2, "t")
            cc.flag(c)
            self.assertEqual("odd_name" in c.flags, odd, name)


class Duplicates(unittest.TestCase):
    def groups(self, *rows):
        contacts = [cc.parse(r, i + 2, "t") for i, r in enumerate(rows)]
        for c in contacts:
            cc.flag(c)
        return [[c.name for c in g] for g in cc.find_duplicates(contacts)]

    def test_shared_landline_is_not_one_person(self):
        self.assertEqual(self.groups({"First Name": "Clara", "Last Name": "Lang", "Phone 1 - Value": "+49 30 1234567"},
                                     {"First Name": "Paul", "Last Name": "Lang", "Phone 1 - Value": "+49301234567"}), [])

    def test_same_phone_with_a_longer_name_is(self):
        self.assertEqual(len(self.groups({"First Name": "Eva", "Last Name": "Roth", "Phone 1 - Value": "+49 89 1572301"},
                                         {"First Name": "Eva", "Last Name": "Roth (Attorney)",
                                          "Phone 1 - Value": "+49 89 1572301"})), 1)

    def test_nickname_pair(self):
        self.assertEqual(self.groups({"First Name": "Dimi", "Last Name": "Papas"},
                                     {"First Name": "Dimitris", "Last Name": "Papas"},
                                     {"First Name": "Dina", "Last Name": "Papas"}), [["Dimi Papas", "Dimitris Papas"]])


class Activity(unittest.TestCase):
    def setUp(self):
        self.out, self.rows = run("--activity", str(ACTIVITY), "--active-since", "2023-10-12")

    def action(self, row_no):
        return self.rows[row_no - 2]["suggested_action"]

    def test_recent_mail_rescues_a_review_row(self):
        self.assertEqual(self.action(5), "keep")
        self.assertEqual(self.rows[3]["last_sent"], "2026-05-01")

    def test_email_only_without_recent_mail_is_stale(self):
        for r in (6, 7):  # old mail, no mail
            self.assertEqual(self.action(r), "delete-candidate")
            self.assertIn("stale", self.rows[r - 2]["cleanup_flags"])
        self.assertNotIn("active", self.rows[4]["cleanup_flags"])

    def test_messenger_activity_alone_makes_nothing_stale(self):
        c = cc.Contact(0, "t", {}, emails=["info@cafe.de"])
        cc.flag(c)
        cc.apply_activity([c], {"491701234567": ("2026-05-01", {"whatsapp"})}, "2023-01-01")
        self.assertNotIn("stale", c.flags)

    def test_a_business_is_not_email_only(self):
        c = cc.Contact(0, "t", {}, emails=["info@cafe.de"], org="Café Härtl")
        cc.flag(c)
        self.assertNotIn("email_only", c.flags)

    def test_zero_sent_does_not_count(self):
        self.assertEqual(self.action(8), "delete-candidate")

    def test_active_duplicate_still_goes_to_merge_review(self):
        self.assertEqual(self.action(2), "merge-review")
        self.assertEqual(self.action(3), "merge-review")  # phone match across +49 / 0 forms
        self.assertEqual(self.rows[1]["sent_via"], "whatsapp")

    def test_report_lists_rescued_rows(self):
        self.assertIn("1 rows were flagged but are kept", (self.out / "report.md").read_text())


class WhatsApp(unittest.TestCase):
    def test_reads_sent_counts_from_a_snapshot(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "ChatStorage.sqlite"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INT, ZISFROMME INT,
                                     ZMESSAGEDATE REAL, ZTEXT TEXT);
            INSERT INTO ZWACHATSESSION VALUES (1, '4915123456789@s.whatsapp.net'),
                                              (2, '4916000000000@s.whatsapp.net'),
                                              (3, '1203634@g.us'), (4, '99887766@lid');
            INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0, 'hi'), (2, 1, 1, 86400, 'x'), (3, 1, 0, 999999, 'y'),
                                          (4, 2, 0, 50, 'spam'), (5, 3, 1, 50, 'group');
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '4915123456789@s.whatsapp.net', 'Anna Schmidt'),
                                                  (2, '4917000000001@s.whatsapp.net', 'Fritz Fischer');
        """)
        con.commit()
        con.close()
        found, hidden = wa.rows(db)
        self.assertEqual(found, [
            {"key": "+4915123456789", "channel": "whatsapp", "sent": 2, "last_sent": "2001-01-02",
             "push_name": "Anna Schmidt"},
            {"key": "+4917000000001", "channel": "whatsapp", "sent": 0, "last_sent": "",
             "push_name": "Fritz Fischer"},  # only shares a group: name, no activity
        ])
        self.assertEqual(hidden, 1)
        self.assertIsNone(wa.group_rows(db))  # no group tables in this database

    def test_reads_group_members(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "ChatStorage.sqlite"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INT, ZMEMBERJID TEXT);
            INSERT INTO ZWACHATSESSION VALUES (1, '120363@g.us', 'Volleyball'), (2, '4915100000@s.whatsapp.net', 'Anna');
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 1, '4915123456789@s.whatsapp.net'), (2, 1, '998877@lid'),
                                              (3, 2, '4915100000@s.whatsapp.net');
        """)
        con.commit()
        con.close()
        self.assertEqual(wa.group_rows(db), [("Volleyball", "+4915123456789")])

    def test_hidden_ids_become_numbers_through_the_address_book(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "ChatStorage.sqlite"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INT, ZISFROMME INT, ZMESSAGEDATE REAL);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INT, ZMEMBERJID TEXT);
            CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPUSHNAME TEXT);
            CREATE TABLE ZWAPROFILEPICTUREITEM (Z_PK INTEGER PRIMARY KEY, ZJID TEXT, ZPATH TEXT);
            INSERT INTO ZWACHATSESSION VALUES (1, '4915123456789@s.whatsapp.net', 'Anna'), (2, '998877@lid', 'Anna'),
                                              (3, '555555@lid', 'Stranger'), (4, '120363@g.us', 'Volleyball');
            INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0), (2, 2, 1, 86400), (3, 3, 1, 50);
            INSERT INTO ZWAGROUPMEMBER VALUES (1, 4, '998877@lid'), (2, 4, '555555@lid');
            INSERT INTO ZWAPROFILEPUSHNAME VALUES (1, '998877@lid', 'Anna Schmidt');
            INSERT INTO ZWAPROFILEPICTUREITEM VALUES (1, '998877@lid', 'Media/Profile/a.jpg');
        """)
        con.commit()
        con.close()
        book = sqlite3.connect(tmp / "ContactsV2.sqlite")
        book.executescript("""
            CREATE TABLE ZWAADDRESSBOOKCONTACT (Z_PK INTEGER PRIMARY KEY, ZLID TEXT, ZWHATSAPPID TEXT);
            INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (1, '998877@lid', '4915123456789@s.whatsapp.net');
        """)
        book.commit()
        book.close()
        prof = tmp / "Media" / "Profile"
        prof.mkdir(parents=True)
        (prof / "a.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 10)
        (prof / "998877-111.thumb").write_bytes(b"\xff\xd8\xff" + b"0" * 90)
        found, hidden = wa.rows(db)
        self.assertEqual(found, [{"key": "+4915123456789", "channel": "whatsapp", "sent": 2,
                                  "last_sent": "2001-01-02", "push_name": "Anna Schmidt"}])
        self.assertEqual(hidden, 1)  # the stranger's id is in no address book
        self.assertEqual(wa.group_rows(db), [("Volleyball", "+4915123456789")])
        self.assertEqual({k: f.name for k, f in wa.photo_files(db).items()}, {"+4915123456789": "998877-111.thumb"})


class WhatsAppZip(unittest.TestCase):
    def test_zip_holds_csvs_and_largest_photo_per_number(self):
        import zipfile
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "ChatStorage.sqlite"
        con = sqlite3.connect(db)
        con.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INT, ZISFROMME INT, ZMESSAGEDATE REAL);
            INSERT INTO ZWACHATSESSION VALUES (1, '4915123456789@s.whatsapp.net', 'Anna');
            INSERT INTO ZWAMESSAGE VALUES (1, 1, 1, 0);
        """)
        con.commit()
        con.close()
        prof = tmp / "Media" / "Profile"
        prof.mkdir(parents=True)
        jpeg = b"\xff\xd8\xff\xe0" + b"0" * 50
        (prof / "4915123456789-111.thumb").write_bytes(jpeg)
        (prof / "4915123456789-222.jpg").write_bytes(jpeg + b"1" * 100)
        (prof / "4915123456789-333.jpg").write_bytes(b"not an image" * 50)
        (prof / "readme.txt").write_text("x")
        out = tmp / "wa.zip"
        assert wa.main(["--db", str(db), "--zip", str(out)]) == 0
        with zipfile.ZipFile(out) as z:
            names = set(z.namelist())
            self.assertEqual(names, {"whatsapp_activity.csv", "whatsapp_groups.csv", "photos/+4915123456789.jpg"})
            self.assertEqual(len(z.read("photos/+4915123456789.jpg")), len(jpeg) + 100)


class Cleaned(unittest.TestCase):
    def test_drop_label_and_flag(self):
        out, _ = run("--drop-label", "Imported on 3/14/21", "--drop-flag", "empty", "--drop-flag", "noreply")
        rows = read(out / "cleaned.csv")
        self.assertEqual(len(rows), 13)
        self.assertNotIn("cleanup_flags", rows[0])  # original columns only

    def test_no_cleaned_csv_without_drop_options(self):
        out, _ = run()
        self.assertFalse((out / "cleaned.csv").exists())
        self.assertIn("Imported on 3/14/21", (out / "report.md").read_text())


if __name__ == "__main__":
    unittest.main()
