import unittest

import add_people as ap
import people_sync as ps


class Phones(unittest.TestCase):
    def test_how_people_type_numbers_in_a_shared_sheet(self):
        cases = {"0177/2805952": "+491772805952", "17670832441": "+4917670832441",
                 "4917664827442": "+4917664827442", "0049157-75058122": "+4915775058122",
                 "0041/78/9236157": "+41789236157", "18059010130": "+18059010130", "1724639125": "+491724639125",
                 "8122/5337": "+4981225337", "08122/15192": "+49812215192"}
        for raw, want in cases.items():
            self.assertEqual(ap.phone(raw), want, raw)


class Plan(unittest.TestCase):
    def test_existing_gets_married_name_and_missing_fields_new_ones_are_created(self):
        people = [{"resourceName": "people/1", "names": [{"givenName": "Sarah", "familyName": "Teyssen",
                                                          "displayName": "Sarah Teyssen"}]}]
        rows = [{"given": "Sarah", "family": "Meinhart", "maiden": "Teyssen", "email": "s@x.com",
                 "phone": "18059010130", "facebook": "https://www.facebook.com/s", "linkedin": ""},
                {"given": "Tom", "family": "Horseman", "maiden": "", "email": "t@x.com", "phone": "",
                 "facebook": "", "linkedin": "https://www.linkedin.com/in/tom/edit/birthday/"},
                {"given": "Only", "family": "Name", "maiden": "", "email": "", "phone": "",
                 "facebook": "", "linkedin": ""}]
        creates, updates, lines = ap.plan(rows, people)
        self.assertEqual([c["names"][0]["familyName"] for c in creates], ["Horseman"])
        self.assertEqual(creates[0]["urls"][0]["value"], "https://www.linkedin.com/in/tom/")
        sarah = updates[0]["set"]
        self.assertEqual(sarah["names"]["familyName"], "Meinhart")
        self.assertEqual(sarah["nicknames"][0]["value"], "geb. Teyssen")
        self.assertEqual(sarah["phoneNumbers"][0]["value"], "+18059010130")
        self.assertEqual(updates[0]["fp"], ps.person_fingerprint(people[0]))
        self.assertEqual(len(lines), 2)

    def test_a_newer_name_on_the_card_is_kept(self):
        people = [{"resourceName": "people/1", "names": [{"givenName": "Stephan", "familyName": "Siebold-Weber",
                                                          "displayName": "Stephan Siebold-Weber"}],
                   "emailAddresses": [{"value": "st@x.com"}]}]
        rows = [{"given": "Stephan", "family": "Siebold", "maiden": "", "email": "st@x.com", "phone": "",
                 "facebook": "", "linkedin": ""}]
        _, updates, _ = ap.plan(rows, people)
        self.assertNotIn("names", updates[0]["set"])


if __name__ == "__main__":
    unittest.main()


class OldList(unittest.TestCase):
    def test_landline_and_address_marked_as_old(self):
        rows = [{"given": "Martin", "family": "Adelberger", "maiden": "", "email": "", "phone": "",
                 "landline": "8122/5337", "street": "Danzigerstr. 5", "city": "85435 Erding",
                 "facebook": "", "linkedin": ""}]
        creates, _, _ = ap.plan(rows, [])
        body = creates[0]
        self.assertEqual(body["phoneNumbers"], [{"value": "+4981225337", "type": "home (2009)"}])
        self.assertEqual(body["addresses"][0], {"streetAddress": "Danzigerstr. 5", "postalCode": "85435",
                                                "city": "Erding", "countryCode": "DE", "type": "home (2009)"})


class Siblings(unittest.TestCase):
    def test_parents_landline_does_not_link_a_sibling(self):
        people = [{"resourceName": "people/1", "names": [{"givenName": "Julia", "familyName": "Stein"}],
                   "phoneNumbers": [{"value": "+49812212345"}]}]
        row = {"given": "Carina", "family": "Stein", "maiden": "", "email": "", "phone": "", "landline": "08122/12345"}
        self.assertIsNone(ap.match(row, people))
        row["given"] = "Julia"
        self.assertIs(ap.match(row, people), people[0])
