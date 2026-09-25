import unittest

from login_security import safe_login_destination


class LoginDestinationTests(unittest.TestCase):
    def test_keeps_local_dashboard_and_filters(self):
        self.assertEqual(safe_login_destination("/admin?date=2026-09-25&status=all"),
                         "/admin?date=2026-09-25&status=all")

    def test_converts_trusted_https_bookmarks_to_relative(self):
        self.assertEqual(safe_login_destination(
            "https://reservations.sienaatl.com/admin?date=2026-09-25",
            "https://reservations.sienaatl.com"), "/admin?date=2026-09-25")

    def test_rejects_external_and_ambiguous_destinations(self):
        for value in (None, "", "https://example.com", "//example.com", "///example.com",
                      "https://reservations.sienaatl.com@example.com/",
                      "http://reservations.sienaatl.com/admin", "javascript:alert(1)",
                      "/\\example.com", "/%5cexample.com", "/%2fexample.com",
                      "/%252fexample.com", "\t//example.com", "/admin%0d%0aLocation:x",
                      "https://[invalid/", "admin"):
            with self.subTest(value=value):
                self.assertEqual(safe_login_destination(value, "https://reservations.sienaatl.com"),
                                 "/admin")


if __name__ == "__main__":
    unittest.main()
