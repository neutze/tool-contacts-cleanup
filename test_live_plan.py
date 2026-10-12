import unittest

import live_plan as lp


def plan(*people):
    return lp.Plan([dict(p, resourceName=f"people/{i}", etag="e") for i, p in enumerate(people)])


class Recency(unittest.TestCase):
    def test_newest_first_and_stale_marked_old(self):
        pl = plan({"names": [{"displayName": "A"}],
                   "emailAddresses": [{"value": "old@x.de", "type": "home"}, {"value": "new@y.de", "type": "home"}]})
        lp.recency(pl, {"old@x.de": "2012-01-01", "new@y.de": "2026-01-01"}, "2023-10-12")
        self.assertEqual(pl.sets["people/0"]["emailAddresses"],
                         [{"value": "new@y.de", "type": "home"}, {"value": "old@x.de", "type": "old"}])

    def test_nothing_known_nothing_changed(self):
        pl = plan({"phoneNumbers": [{"value": "+491511"}, {"value": "+491522"}]})
        lp.recency(pl, {}, "2023-10-12")
        self.assertEqual(dict(pl.sets), {})


class Names(unittest.TestCase):
    def test_married_name_from_whatsapp(self):
        pl = plan({"names": [{"givenName": "Anna", "familyName": "Schmidt", "displayName": "Anna Schmidt"}],
                   "phoneNumbers": [{"value": "+49 151 2345678"}]})
        lp.names_from_whatsapp(pl, [{"key": "+491512345678", "push_name": "Anna Müller"}])
        self.assertEqual(pl.sets["people/0"]["names"]["familyName"], "Müller")

    def test_nicknames_and_other_first_names_are_ignored(self):
        pl = plan({"names": [{"givenName": "Anna", "familyName": "Schmidt"}],
                   "phoneNumbers": [{"value": "+491512345678"}]})
        lp.names_from_whatsapp(pl, [{"key": "+491512345678", "push_name": "Annie 🌴"}])
        lp.names_from_whatsapp(pl, [{"key": "+491512345678", "push_name": "Mama Schmidt"}])
        self.assertEqual(dict(pl.sets), {})


class Truth(unittest.TestCase):
    def test_first_name_only_gets_last_name_from_proper_whatsapp_name(self):
        pl = plan({"names": [{"givenName": "Abdellah", "displayName": "Abdellah"}],
                   "phoneNumbers": [{"value": "+49 1514 0400135"}]})
        lp.names_from_whatsapp(pl, [{"key": "+4915140400135", "push_name": "Abdellah Benali"}])
        self.assertEqual(pl.sets["people/0"]["names"]["familyName"], "Benali")

    def test_new_last_name_rules(self):
        self.assertEqual(lp.new_last_name("Anna Müller", "Anna", "Schmidt"), "Müller")
        self.assertEqual(lp.new_last_name("Anna Schmidt", "Anna", "Schmidt"), "")
        self.assertEqual(lp.new_last_name("Ayse Mekikci", "Ayse", "Mekikci Aksoy"), "")  # shortened double name
        for nick in ("Anna", "Anna 🌴", "Anna2 Schmidt", "Annie Schmidt", "Anna S", "Mama"):
            self.assertEqual(lp.new_last_name(nick, "Anna", "Schmidt"), "", nick)

    def test_linkedin_job_wins_and_last_name_only_on_email_match(self):
        pl = plan({"names": [{"givenName": "Anna", "familyName": "Schmidt"}],
                   "emailAddresses": [{"value": "anna@x.de"}], "organizations": [{"name": "Old GmbH"}]},
                  {"names": [{"givenName": "Ben", "familyName": "Okafor"}]})
        rows = [{"First Name": "Anna", "Last Name": "Müller", "Email Address": "Anna@x.de", "Company": "New AG",
                 "Position": "CTO"},
                {"First Name": "Ben", "Last Name": "Okafor", "Email Address": "", "Company": "Acme", "Position": ""}]
        lp.from_linkedin(pl, rows)
        self.assertEqual(pl.sets["people/0"]["organizations"], {"name": "New AG", "title": "CTO"})
        self.assertEqual(pl.sets["people/0"]["names"]["familyName"], "Müller")
        self.assertEqual(pl.sets["people/1"]["organizations"]["name"], "Acme")
        self.assertNotIn("names", pl.sets["people/1"])


class CompleteNames(unittest.TestCase):
    def test_gaps_filled_from_nickname_then_whatsapp(self):
        pl = plan({"names": [{"givenName": "Dario"}], "nicknames": [{"value": "Mr. D"}]},
                  {"names": [{"givenName": "Pero"}], "phoneNumbers": [{"value": "+491769852"}]},
                  {"names": [{"givenName": "Taxi"}], "organizations": [{"name": "Isar Funk"}]},
                  {"names": [{"familyName": "Herr"}], "nicknames": [{"value": "Dennis"}]},
                  {"names": [{"givenName": "Solo"}]})
        lp.complete_names(pl, [{"key": "+491769852", "push_name": "Pero 🎸 Kovac"}])
        got = {rn: (s["names"]["givenName"], s["names"]["familyName"]) for rn, s in pl.sets.items()}
        self.assertEqual(got, {"people/0": ("Dario", "Mr D"), "people/1": ("Pero", "Kovac"),
                               "people/3": ("Dennis", "Herr")})  # a company never becomes a last name

    def test_full_name_in_the_first_name_field_is_split(self):
        pl = plan({"names": [{"givenName": "Charles Njofack"}]}, {"names": [{"givenName": "Walk The Cat 2"}]})
        lp.complete_names(pl)
        self.assertEqual(pl.sets["people/0"]["names"]["familyName"], "Njofack")
        self.assertNotIn("people/1", pl.sets)

    def test_proper_name_from_an_earlier_source_is_kept(self):
        pl = plan({"names": [{"givenName": "Abdellah"}], "nicknames": [{"value": "Abdi"}]})
        pl.sets["people/0"]["names"] = {"givenName": "Abdellah", "middleName": "", "familyName": "Benali"}
        lp.complete_names(pl)
        self.assertEqual(pl.sets["people/0"]["names"]["familyName"], "Benali")


class BirthdaysByName(unittest.TestCase):
    def test_exact_unique_match_only(self):
        pl = plan({"names": [{"givenName": "Anna", "familyName": "Schmidt", "displayName": "Anna Schmidt"}]},
                  {"names": [{"givenName": "Ben", "familyName": "Okafor"}], "birthdays": [{"date": {"month": 1, "day": 1}}]},
                  {"names": [{"givenName": "Max", "familyName": "Muster"}]},
                  {"names": [{"givenName": "Max", "familyName": "Muster"}]})
        rows = [{"name": "Anna Schmidt", "month": "5", "day": "1", "year": "1990", "source": "facebook"},
                {"name": "Ben Okafor", "month": "2", "day": "2", "year": "", "source": "facebook"},
                {"name": "Max Muster", "month": "3", "day": "3", "year": "", "source": "xing"},
                {"name": "Nobody Here", "month": "4", "day": "4", "year": "", "source": "xing"}]
        left = lp.birthdays_by_name(pl, rows)
        self.assertEqual(dict(pl.sets), {"people/0": {"birthdays": [{"date": {"month": 5, "day": 1, "year": 1990}}]}})
        self.assertEqual(sorted(r["name"] for r in left), ["Max Muster", "Nobody Here"])


if __name__ == "__main__":
    unittest.main()
