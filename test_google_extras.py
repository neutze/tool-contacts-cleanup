import tempfile
import unittest
from pathlib import Path

import google_extras as ge


def result(complete=True, unconfirmed=False, lines=("Hauptstr. 1",), postal="80331", city="München"):
    return {"result": {"verdict": {"addressComplete": complete, "hasUnconfirmedComponents": unconfirmed,
                                   "validationGranularity": "PREMISE"},
                       "address": {"postalAddress": {"addressLines": list(lines), "postalCode": postal,
                                                     "locality": city, "regionCode": "DE"}}}}


class OtherContacts(unittest.TestCase):
    def test_only_recently_mailed_and_not_already_a_contact(self):
        others = [{"resourceName": "otherContacts/1", "emailAddresses": [{"value": "New@x.de"}]},
                  {"resourceName": "otherContacts/2", "emailAddresses": [{"value": "old@x.de"}]},
                  {"resourceName": "otherContacts/3", "emailAddresses": [{"value": "anna@x.de"}]},
                  {"resourceName": "otherContacts/4", "names": [{"displayName": "no email"}]}]
        people = [{"emailAddresses": [{"value": "anna@x.de"}]}]
        activity = {"new@x.de": "2026-01-01", "old@x.de": "2015-01-01", "anna@x.de": "2026-01-01"}
        keep, rest = ge.worth_keeping(others, people, activity, "2023-10-12")
        self.assertEqual([o["resourceName"] for o, _ in keep], ["otherContacts/1"])
        self.assertEqual(len(rest), 3)

    def test_copy_is_dry_by_default(self):
        calls = []

        class Api:
            def call(self, *a, **k):
                calls.append(a)
        keep = [({"resourceName": "otherContacts/1", "emailAddresses": [{"value": "n@x.de"}]}, "2026-01-01")]
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(ge.copy_others(Api(), keep, False, tmp), {"other_contacts_to_copy": 1})
        self.assertEqual(calls, [])
        ge.copy_others(Api(), keep, True, tmp)
        self.assertEqual(calls, [("POST", "/otherContacts/1:copyOtherContactToMyContactsGroup")])


class Addresses(unittest.TestCase):
    def test_only_confirmed_addresses_are_written(self):
        self.assertEqual(ge.checked({"type": "home"}, result()),
                         {"streetAddress": "Hauptstr. 1", "postalCode": "80331", "city": "München",
                          "countryCode": "DE", "type": "home"})
        self.assertEqual(ge.checked({}, result(complete=False)), {})
        self.assertEqual(ge.checked({}, result(unconfirmed=True)), {})

    def test_plan_changes_only_what_differs_and_skips_city_only(self):
        people = [
            {"resourceName": "people/1", "names": [{"displayName": "A"}],
             "addresses": [{"streetAddress": "hauptstrasse 1", "city": "muenchen", "formattedValue": "x"}]},
            {"resourceName": "people/2", "names": [{"displayName": "B"}],
             "addresses": [{"streetAddress": "Hauptstr. 1", "postalCode": "80331", "city": "München",
                            "countryCode": "DE"}]},
            {"resourceName": "people/3", "names": [{"displayName": "C"}], "addresses": [{"city": "Berlin"}]},
        ]
        sent = []
        updates, lines = ge.address_plan(people, "k", post=lambda body: sent.append(body) or result())
        self.assertEqual([u["name"] for u in updates], ["A"])
        self.assertEqual(updates[0]["set"]["addresses"][0]["postalCode"], "80331")
        self.assertEqual(len(sent), 2)  # Berlin alone is never sent


if __name__ == "__main__":
    unittest.main()
