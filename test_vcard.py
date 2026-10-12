import unittest

import people_sync as ps
import vcard

VCF = """BEGIN:VCARD
VERSION:3.0
N:Schmidt;Anna;;;
FN:Anna Schmidt
item1.EMAIL;type=INTERNET;type=HOME;type=pref:anna@x.de
TEL;type=CELL;type=VOICE;type=pref:0151 2345678
TEL;type=HOME:089 123456
ORG:New AG;
BDAY:1990-05-01
ADR;type=HOME:;;Hauptstr. 1;München;;80331;Deutschland
PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQ
 SkZJRg==
END:VCARD
BEGIN:VCARD
VERSION:3.0
N:Okafor;Ben;;;
FN:Ben Okafor
TEL;type=CELL:+44 7700 900123
BDAY:--03-04
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Nobody
END:VCARD
"""


class Parse(unittest.TestCase):
    def test_fields_folding_and_photo(self):
        a, b, c = vcard.parse(VCF)
        self.assertEqual((a["given"], a["family"], a["org"]), ("Anna", "Schmidt", "New AG"))
        self.assertEqual(a["emails"], [("anna@x.de", "home")])
        self.assertEqual(a["phones"], [("0151 2345678", "mobile"), ("089 123456", "home")])
        self.assertEqual(a["bday"], {"month": 5, "day": 1, "year": 1990})
        self.assertEqual(a["addresses"][0]["city"], "München")
        self.assertTrue(a["photo"].startswith(b"\xff\xd8\xff"))
        self.assertEqual(b["bday"], {"month": 3, "day": 4})
        self.assertEqual(c["emails"], [])


class IPhone(unittest.TestCase):
    def test_trailing_spaces_and_nameless_cards(self):
        cards = vcard.parse("BEGIN:VCARD \nVERSION:3.0 \nN:;;;; \nTEL;type=pref:+491728929331 \nEND:VCARD \n")
        self.assertEqual(cards[0]["phones"], [("+491728929331", "other")])
        self.assertEqual(vcard.plan(cards, [])[0], [])  # no name, no company: not created


class Plan(unittest.TestCase):
    def test_fills_gaps_creates_unknown_skips_empty(self):
        people = [{"resourceName": "people/1", "etag": "e",
                   "names": [{"givenName": "Anna", "familyName": "Schmidt", "displayName": "Anna Schmidt"}],
                   "phoneNumbers": [{"value": "+491512345678", "type": "mobile"}],
                   "organizations": [{"name": "Old GmbH"}]}]
        creates, updates, photos, lines = vcard.plan(vcard.parse(VCF), people)
        self.assertEqual([c[0]["names"][0]["givenName"] for c in creates], ["Ben"])
        anna = updates[0]["set"]
        self.assertEqual([e["value"] for e in anna["emailAddresses"]], ["anna@x.de"])
        self.assertEqual([x["value"] for x in anna["phoneNumbers"]], ["+491512345678", "+4989123456"])
        self.assertNotIn("organizations", anna)  # the Google card's company stays
        self.assertIn("birthdays", anna)
        self.assertEqual(photos[0][0], "people/1")
        self.assertEqual(updates[0]["fp"], ps.person_fingerprint(people[0]))


if __name__ == "__main__":
    unittest.main()
