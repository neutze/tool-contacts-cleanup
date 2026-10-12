import json
import tempfile
import unittest
from pathlib import Path

import google_photos as gp


def person(i, name, own=False, profile=False, email=""):
    p = {"resourceName": f"people/c{i}", "etag": f"e{i}", "names": [{"displayName": name}], "photos": []}
    if own:
        p["photos"].append({"url": "u", "metadata": {"source": {"type": "CONTACT"}}})
    if profile:
        p["photos"].append({"url": "u", "metadata": {"source": {"type": "PROFILE"}}})
    if not own and not profile:
        p["photos"].append({"url": "u", "default": True, "metadata": {"source": {"type": "CONTACT"}}})
    if email:
        p["emailAddresses"] = [{"value": email}]
    return p


class FakeApi:
    def __init__(self, people=(), polls=1, pages=None):
        self.people, self.polls, self.pages = list(people), polls, pages or [[]]
        self.calls, self.token = [], "t"

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if path == "/people/me/connections":
            return {"connections": self.people}
        if (method, path) == ("POST", "/sessions"):
            return {"id": "s1", "pickerUri": "https://picker", "mediaItemsSet": False,
                    "pollingConfig": {"pollInterval": "2s", "timeoutIn": "10s"}}
        if (method, path) == ("GET", "/sessions/s1"):
            self.polls -= 1
            return {"id": "s1", "pickerUri": "https://picker", "mediaItemsSet": self.polls <= 0,
                    "pollingConfig": {"pollInterval": "2s", "timeoutIn": "10s"}}
        if path == "/mediaItems":
            n = int((params or {}).get("pageToken", 0))
            out = {"mediaItems": self.pages[n]}
            if n + 1 < len(self.pages):
                out["nextPageToken"] = str(n + 1)
            return out
        return {}


def item(i, kind="PHOTO"):
    return {"id": f"m{i}", "type": kind, "createTime": "2024-05-01T10:00:00Z",
            "mediaFile": {"baseUrl": f"https://lh3/{i}", "filename": f"IMG_{i}.jpg", "mimeType": "image/jpeg"}}


class OwnPhoto(unittest.TestCase):
    def test_only_a_photo_set_on_the_card_counts(self):
        self.assertTrue(gp.has_own_photo(person(1, "A", own=True)))
        self.assertFalse(gp.has_own_photo(person(2, "B", profile=True)))
        self.assertFalse(gp.has_own_photo(person(3, "C")))
        self.assertFalse(gp.has_own_photo({"resourceName": "people/c4"}))

    def test_labels_offer_cards_without_own_photo_and_tell_twins_apart(self):
        people = [person(1, "Ann Lee", own=True), person(2, "Bob Ray", profile=True),
                  person(3, "Cy Fox", email="cy@a.de"), person(4, "Cy Fox", email="cy@b.de"),
                  person(5, "Cy Fox", own=True)]
        self.assertEqual(gp.contact_labels(people), {
            "Bob Ray": "people/c2", "Cy Fox · cy@a.de": "people/c3", "Cy Fox · cy@b.de": "people/c4"})


class Crop(unittest.TestCase):
    def test_square_is_centred_on_the_face_with_room_around_it(self):
        x, y, side = gp.square_around((400, 300, 100, 100), 1600, 1200)
        self.assertEqual(side, 220)
        self.assertEqual((x, y), (340, 230))  # centre 450/350, leaning 10 px up

    def test_square_stays_inside_the_picture(self):
        self.assertEqual(gp.square_around((0, 0, 100, 100), 1600, 1200), (0, 0, 220))
        self.assertEqual(gp.square_around((1500, 1100, 100, 100), 1600, 1200), (1380, 980, 220))

    def test_a_face_filling_the_picture_gives_the_largest_square(self):
        self.assertEqual(gp.square_around((100, 100, 500, 600), 800, 1000), (0, 0, 800))

    def test_one_job_per_face_and_bystanders_dropped(self):
        found = {"/w/picked/p0001.jpg": {"width": 1600, "height": 1200, "faces": [
            [900, 300, 190, 200], [300, 300, 200, 200], [50, 50, 40, 40]]},
            "/w/picked/p0002.jpg": {"width": 1000, "height": 600, "faces": []}}
        jobs = gp.crop_jobs(found, Path("/w/crops"))
        self.assertEqual([Path(j["dst"]).name for j in jobs], ["p0001_1.jpg", "p0001_2.jpg", "p0002_noface.jpg"])
        self.assertEqual((jobs[2]["x"], jobs[2]["y"], jobs[2]["side"], jobs[2]["size"]), (200, 0, 600, 600))
        self.assertEqual(jobs[0]["size"], 440)  # never blown up beyond what the photo holds


class Pick(unittest.TestCase):
    def test_waits_saves_photos_and_closes_the_session(self):
        api = FakeApi(polls=2, pages=[[item(1), item(2, "VIDEO")], [item(3)]])
        opened, slept, fetched = [], [], []
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            new = gp.pick(api, out, fetch=lambda u: fetched.append(u) or b"jpg",
                          open_url=opened.append, sleep=slept.append)
            self.assertEqual([f.name for f in new], ["p0001.jpg", "p0002.jpg"])
            self.assertEqual(json.loads((out / "picked.json").read_text())["p0002.jpg"]["id"], "m3")
            # a second round keeps counting and skips what is already there
            api2 = FakeApi(polls=1, pages=[[item(3), item(4)]])
            again = gp.pick(api2, out, fetch=lambda u: b"jpg", open_url=opened.append, sleep=slept.append)
            self.assertEqual([f.name for f in again], ["p0003.jpg"])
        self.assertEqual(opened[0], "https://picker")
        self.assertEqual(slept[:2], [2.0, 2.0])
        self.assertEqual(fetched, ["https://lh3/1=w1600-h1600", "https://lh3/3=w1600-h1600"])
        self.assertEqual(api.calls[-1][:2], ("DELETE", "/sessions/s1"))

    def test_gives_up_when_nothing_is_chosen_and_still_closes_the_session(self):
        api = FakeApi(polls=99)
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            gp.pick(api, Path(d), fetch=lambda u: b"", open_url=lambda u: None, sleep=lambda s: None)
        self.assertEqual(api.calls[-1][:2], ("DELETE", "/sessions/s1"))


class Assign(unittest.TestCase):
    def test_set_clear_and_refuse(self):
        labels, a = {"Bob Ray": "people/c2", "Cy Fox": "people/c3"}, {}
        self.assertEqual(gp.set_assignment(a, labels, "p1_1.jpg", " Bob Ray "), "")
        self.assertEqual(a, {"p1_1.jpg": {"resourceName": "people/c2", "name": "Bob Ray"}})
        self.assertIn("no such contact", gp.set_assignment(a, labels, "p1_2.jpg", "Ann Lee"))
        self.assertIn("another face", gp.set_assignment(a, labels, "p1_2.jpg", "Bob Ray"))
        self.assertEqual(gp.set_assignment(a, labels, "p1_1.jpg", "Cy Fox"), "")  # change your mind
        self.assertEqual(gp.set_assignment(a, labels, "p1_1.jpg", ""), "")
        self.assertEqual(a, {})

    def test_page_escapes_names(self):
        text = gp.page(["p1_1.jpg"], {'A "<b>" & Co': "people/c1"}, {})
        self.assertIn("A &quot;&lt;b&gt;&quot; &amp; Co", text)
        self.assertNotIn("<b>", text)


class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        (self.out / "crops").mkdir()
        for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
            (self.out / "crops" / n).write_bytes(b"jpg")
        self.people = [person(1, "Ann Lee", own=True), person(2, "Bob Ray", profile=True), person(3, "Cy Fox")]
        (self.out / "assignments.json").write_text(json.dumps({
            "a.jpg": {"resourceName": "people/c1", "name": "Ann Lee"},   # set a photo herself since
            "b.jpg": {"resourceName": "people/c2", "name": "Bob Ray"},
            "c.jpg": {"resourceName": "people/c3", "name": "Cy Fox"},
            "d.jpg": {"resourceName": "people/c3", "name": "Cy Fox"},    # hand-edited double
            "e.jpg": {"resourceName": "people/c2", "name": "Bob Ray"},
            "f.jpg": {"resourceName": "people/c9", "name": "Gone"}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_writes_nothing_to_google(self):
        api = FakeApi(self.people)
        summary = gp.run_apply(api, self.out, apply=False)
        self.assertEqual(summary, {"live_contacts": 3, "photo": 2, "skipped": 4})
        self.assertEqual([c[0] for c in api.calls], ["GET"])
        report = (self.out / "photo_report.txt").read_text()
        self.assertIn("DRY RUN", report)
        self.assertIn("PHOTO Bob Ray <- b.jpg", report)
        self.assertIn("SKIPPED Ann Lee: already has a photo of its own", report)
        self.assertIn("SKIPPED Gone: contact no longer exists", report)

    def test_apply_sets_only_the_free_cards(self):
        api = FakeApi(self.people)
        gp.run_apply(api, self.out, apply=True)
        patches = [c for c in api.calls if c[0] == "PATCH"]
        self.assertEqual([c[1] for c in patches],
                         ["/people/c2:updateContactPhoto", "/people/c3:updateContactPhoto"])
        self.assertEqual(patches[0][2], {"photoBytes": "anBn", "personFields": "photos"})


if __name__ == "__main__":
    unittest.main()
