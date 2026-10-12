import json
import tempfile
import unittest
from pathlib import Path

import messengers as m


class Signal(unittest.TestCase):
    def test_encrypted_key_is_decrypted_with_the_keychain_password(self):
        calls = []

        def runner(cmd, stdin=b""):
            calls.append((cmd[0], stdin))
            if cmd[0] == "security":
                return b"pw\n"
            return b"ab" * 32 + b"\n"
        key = m.signal_key({"encryptedKey": (b"v10" + b"cipher").hex()}, runner)
        self.assertEqual(key, "ab" * 32)
        self.assertEqual(calls[1], ("openssl", b"cipher"))

    def test_rows_from_the_database(self):
        base = Path(tempfile.mkdtemp())
        (base / "sql").mkdir()
        (base / "sql" / "db.sqlite").write_bytes(b"x")
        (base / "config.json").write_text(json.dumps({"key": "00"}))

        def runner(cmd, stdin=b""):
            assert b"PRAGMA key" in stdin
            return (b"ok\ne164,profileName,profileFamilyName,profileFullName,active_at\n"
                    b"+491511111111,Anna,M\xc3\xbcller,,1760000000000\n+491522222222,Dimi,,,0\n")
        rows = m.signal_rows(base, runner)
        self.assertEqual(rows[0], {"key": "+491511111111", "channel": "signal", "sent": 1,
                                   "last_sent": "2025-10-09", "push_name": "Anna Müller"})
        self.assertEqual(rows[1]["push_name"], "Dimi")


class SignalAvatar(unittest.TestCase):
    def test_plain_and_encrypted_avatars(self):
        import base64
        import hashlib
        import hmac
        base = Path(tempfile.mkdtemp())
        (base / "attachments.noindex" / "ab").mkdir(parents=True)
        (base / "attachments.noindex" / "ab" / "plain").write_bytes(b"\xff\xd8\xffJPEG")
        self.assertEqual(m.signal_avatar(base, {"path": "ab/plain"}), b"\xff\xd8\xffJPEG")
        key = bytes(range(64))
        iv, body = b"i" * 16, b"ciphertext"
        tag = hmac.new(key[32:], iv + body, hashlib.sha256).digest()
        (base / "attachments.noindex" / "ab" / "enc").write_bytes(iv + body + tag)
        runner = lambda cmd, stdin=b"": b"\xff\xd8\xffJPEG" + b"\x00" * 20  # openssl stand-in
        meta = {"path": "ab/enc", "localKey": base64.b64encode(key).decode(), "size": 7}
        self.assertEqual(m.signal_avatar(base, meta, runner), b"\xff\xd8\xffJPEG")
        meta["localKey"] = base64.b64encode(bytes(64)).decode()  # wrong key: MAC fails, nothing used
        self.assertEqual(m.signal_avatar(base, meta, runner), b"")


class Telegram(unittest.TestCase):
    def test_export_contacts(self):
        f = Path(tempfile.mkdtemp()) / "result.json"
        f.write_text(json.dumps({"contacts": {"list": [
            {"first_name": "Anna", "last_name": "Müller", "phone_number": "+49 151 1111111", "date": "2024-05-01T10:00:00"},
            {"first_name": "Ghost", "last_name": "", "phone_number": "", "date": "2024-05-01T10:00:00"}]}}))
        self.assertEqual(m.telegram_rows(f), [{"key": "+491511111111", "channel": "telegram", "sent": 0,
                                               "last_sent": "2024-05-01", "push_name": "Anna Müller"}])


if __name__ == "__main__":
    unittest.main()
