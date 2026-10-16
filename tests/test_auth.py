import time
import unittest

import auth

KEY = b"k" * 32


class SessionTest(unittest.TestCase):
    def test_round_trip(self):
        cookie = auth.sign_session("me@example.com", int(time.time()) + 60, KEY)
        self.assertEqual(auth.session_user(cookie, KEY), "me@example.com")

    def test_rejects_forged_expired_and_missing(self):
        good = auth.sign_session("me@example.com", int(time.time()) + 60, KEY)
        forged = good.replace("me@", "them@")
        expired = auth.sign_session("me@example.com", int(time.time()) - 1, KEY)
        for cookie in (forged, expired, "", None, "garbage", good + "x"):
            with self.subTest(cookie):
                self.assertIsNone(auth.session_user(cookie, KEY))
        self.assertIsNone(auth.session_user(good, b"other-key" * 4))


if __name__ == "__main__":
    unittest.main()
