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


if __name__ == "__main__":
    unittest.main()
