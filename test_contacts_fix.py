import csv
import tempfile
import unittest
from pathlib import Path

import contacts_cleanup as cc
import contacts_fix as cf

HERE = Path(__file__).parent / "sample"


def read(path):
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Fix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp())
        assert cc.main([str(HERE / "contacts.csv"), "-o", str(tmp / "triage")]) == 0
        cls.out = tmp / "fix"
        assert cf.main([str(tmp / "triage" / "review.csv"), "-o", str(cls.out),
                        "--drop-email-domain", "okafor.net",
                        "--names", str(HERE / "fix" / "names.csv"),
                        "--linkedin", str(HERE / "fix" / "linkedin.csv"),
                        "--label-map", str(HERE / "fix" / "label_map.csv")]) == 0
        cls.all = read(cls.out / "import.csv")
        cls.rows = {r["First Name"] + " " + r["Last Name"]: r for r in cls.all}
        cls.changes = read(cls.out / "changes.csv")

    def test_delete_candidates_are_left_out(self):
        emails = [r["E-mail 1 - Value"] for r in self.all]
        self.assertNotIn("noreply@shop.example", emails)
        self.assertNotIn(" Mueller", self.rows)
        self.assertFalse(any(not (r["First Name"] or r["E-mail 1 - Value"] or r["Phone 1 - Value"])
                             for r in self.all))  # the blank row

    def test_review_columns_are_not_imported(self):
        self.assertNotIn("suggested_action", next(iter(self.rows.values())))

    def test_dead_google_plus_link_removed(self):
        self.assertEqual(self.rows["Dora Weiss"]["Website 1 - Value"], "")
        self.assertEqual(self.rows["Dora Weiss"]["Website 1 - Label"], "")

    def test_country_code_added_only_where_missing(self):
        phones = [r["Phone 1 - Value"] for r in self.all if r["First Name"] == "Carla"]
        self.assertEqual(sorted(phones), ["+49 160 9876543", "+491609876543"])

    def test_names_typed_one_way_are_recased_mixed_ones_kept(self):
        self.assertIn("Hans Meier", self.rows)
        self.assertIn("ADAC Pannenhilfe", self.rows)

    def test_dropped_domain_removes_address_not_contact(self):
        ben = self.rows["Ben Okafor"]
        self.assertEqual(ben["E-mail 2 - Value"], "")
        self.assertEqual(ben["E-mail 1 - Value"], "ben.okafor@example-corp.com")

    def test_label_matched_before_address_is_dropped(self):
        self.assertIn("Okafor", self.rows["Ben Okafor"]["Labels"])
        self.assertIn("Example Corp", self.rows["Ben Okafor"]["Labels"])

    def test_names_sheet_fills_and_deletes(self):
        self.assertIn("Fritz Fischer", self.rows)
        self.assertFalse(any(r["First Name"] == "Gina" for r in self.rows.values()))

    def test_linkedin_fills_empty_fields_and_adds_url(self):
        emil = self.rows["Emil Brandt"]
        self.assertEqual(emil["Organization Name"], "Brandt GmbH")
        self.assertEqual(emil["E-mail 1 - Value"], "emil@brandt.example")
        self.assertIn("linkedin.com/in/emilbrandt", emil["Website 1 - Value"])
        self.assertIn("LinkedIn", emil["Labels"])

    def test_ambiguous_linkedin_name_is_skipped(self):
        for r in self.all:
            if r["First Name"] == "Carla":
                self.assertEqual(r["Organization Name"], "")

    def test_every_change_is_logged(self):
        whys = {c["why"] for c in self.changes}
        for w in ("dead Google+ link", "country code added", "capitalisation", "old student address",
                  "names sheet", "LinkedIn", "label", "marked delete in names sheet"):
            self.assertIn(w, whys)


class Suggestions(unittest.TestCase):
    def test_sheet_carries_whatsapp_and_gmail_suggestions_without_applying_them(self):
        tmp = Path(tempfile.mkdtemp())
        assert cc.main([str(HERE / "contacts.csv"), "-o", str(tmp / "triage")]) == 0
        (tmp / "wa.csv").write_text("key,channel,sent,last_sent,push_name\n"
                                    "+491701112223,whatsapp,0,,Fritz Fischer\n", encoding="utf-8")
        (tmp / "mail.csv").write_text("cleanup_row,first_name,suggested_last_name,suggested_company,confidence,evidence\n"
                                      "17,Gina,Gruber,,high,signature 2019-04\n", encoding="utf-8")
        assert cf.main([str(tmp / "triage" / "review.csv"), "-o", str(tmp / "fix"),
                        "--whatsapp", str(tmp / "wa.csv"), "--name-suggestions", str(tmp / "mail.csv")]) == 0
        sheet = {r["first_name"]: r for r in read(tmp / "fix" / "first_name_only.csv")}
        self.assertEqual(sheet["Fritz"]["suggested_last_name"], "Fischer")
        self.assertEqual(sheet["Fritz"]["suggested_from"], "whatsapp profile name")
        self.assertEqual(sheet["Gina"]["suggested_last_name"], "Gruber")
        self.assertTrue(sheet["Gina"]["suggested_from"].startswith("gmail, high"))
        imported = read(tmp / "fix" / "import.csv")
        self.assertFalse(any(r["Last Name"] in ("Fischer", "Gruber") for r in imported))


class FindAndTidy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        rows = [
            "First Name,Last Name,Notes,Labels,E-mail 1 - Label,E-mail 1 - Value,Phone 1 - Label,Phone 1 - Value,"
            "Phone 2 - Label,Phone 2 - Value",
            "Dora,Weiss,,* myContacts,,,,,,",
            "Emil,Brandt,,* myContacts,,,,,,",
            "Anna,Schmidt,uid: 123sex: femaleprofile_url: http://facebook.com/x pic_big: http://x,* myContacts,"
            "Work,anna@old-employer.example,Mobile,+49 151 2345678,\u200eold number,+49 151 9999999",
        ]
        (self.tmp / "c.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        assert cc.main([str(self.tmp / "c.csv"), "-o", str(self.tmp / "t")]) == 0
        (self.tmp / "wa.csv").write_text("key,channel,sent,last_sent,push_name\n"
                                         "+4917012345678,whatsapp,0,,Dora Weiss\n"
                                         "+4917099999999,whatsapp,0,,Emil Brandt\n"
                                         "+4917088888888,whatsapp,0,,Emil Brandt\n", encoding="utf-8")
        (self.tmp / "found.csv").write_text("cleanup_row,name,email,last_seen,evidence\n"
                                            "3,Emil Brandt,emil@brandt.example,2022-01-01,From header\n",
                                            encoding="utf-8")
        (self.tmp / "groups.csv").write_text("group,key\nVolleyball,+491512345678\n", encoding="utf-8")

    def fix(self, *extra):
        out = self.tmp / "f"
        assert cf.main([str(self.tmp / "t" / "review.csv"), "-o", str(out), "--whatsapp", str(self.tmp / "wa.csv"),
                        "--found-emails", str(self.tmp / "found.csv"),
                        "--whatsapp-groups", str(self.tmp / "groups.csv"),
                        "--old-email-domain", "old-employer.example", *extra]) == 0
        return out

    def test_reach_suggestions_unique_whatsapp_name_and_gmail(self):
        sug = {r["name"]: r for r in read(self.fix() / "reach_suggestions.csv")}
        self.assertEqual(sug["Dora Weiss"]["phone"], "+4917012345678")
        self.assertEqual(sug["Emil Brandt"]["phone"], "")  # two numbers carry that name: ambiguous
        self.assertEqual(sug["Emil Brandt"]["email"], "emil@brandt.example")

    def test_applied_reach_rows_are_added(self):
        out = self.fix()
        sheet = read(out / "reach_suggestions.csv")
        for r in sheet:
            r["apply"] = "y" if r["name"] == "Dora Weiss" else ""
        cf.write(self.tmp / "reach.csv", list(sheet[0]), sheet)
        out = self.fix("--reach", str(self.tmp / "reach.csv"))
        dora = next(r for r in read(out / "import.csv") if r["First Name"] == "Dora")
        self.assertEqual(dora["Phone 1 - Value"], "+4917012345678")

    def test_tidy_ups(self):
        anna = next(r for r in read(self.fix() / "import.csv") if r["First Name"] == "Anna")
        self.assertEqual(anna["Notes"], "")
        self.assertEqual(anna["Phone 2 - Value"], "")
        self.assertEqual(anna["E-mail 1 - Label"], "old")
        self.assertEqual(anna["E-mail 1 - Value"], "anna@old-employer.example")

    def test_group_labels_only_where_a_label_is_chosen(self):
        out = self.fix()
        self.assertEqual(read(out / "groups.csv"), [{"group": "Volleyball", "contacts": "1", "label": ""}])
        anna = next(r for r in read(out / "import.csv") if r["First Name"] == "Anna")
        self.assertNotIn("Volleyball", anna["Labels"])
        (self.tmp / "gl.csv").write_text("group,contacts,label\nVolleyball,1,Sport\n", encoding="utf-8")
        anna = next(r for r in read(self.fix("--group-labels", str(self.tmp / "gl.csv")) / "import.csv")
                    if r["First Name"] == "Anna")
        self.assertIn("Sport", anna["Labels"])


class Units(unittest.TestCase):
    def test_last_name_from_profile_name(self):
        self.assertEqual(cf.last_name_from("Anna Schmidt", "Anna"), "Schmidt")
        self.assertEqual(cf.last_name_from("anna von Weber", "Anna"), "von Weber")
        self.assertEqual(cf.last_name_from("Anna", "Anna"), "")
        self.assertEqual(cf.last_name_from("Schmidt Anna", "Anna"), "")
        self.assertEqual(cf.last_name_from("Anna 2024", "Anna"), "")

    def test_normalize_phone(self):
        self.assertEqual(cf.normalize_phone("0151 2345678", "49"), "+491512345678")
        self.assertEqual(cf.normalize_phone("0044 7700 900123", "49"), "+447700900123")
        self.assertEqual(cf.normalize_phone("+1 (647) 863-1234", "49"), "+1 (647) 863-1234")
        self.assertEqual(cf.normalize_phone("112", "49"), "112")

    def test_recase(self):
        self.assertEqual(cf.recase("anna-lena"), "Anna-Lena")
        self.assertEqual(cf.recase("ludwig van beethoven"), "Ludwig van Beethoven")
        self.assertEqual(cf.recase("McDonald"), "McDonald")


class EmailAsName(unittest.TestCase):
    def test_address_in_name_field_counts_as_no_name(self):
        self.assertEqual(cc.contact_name({"First Name": "max@example.com"}), "")


if __name__ == "__main__":
    unittest.main()
