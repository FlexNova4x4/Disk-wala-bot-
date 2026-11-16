import unittest

from diskwala_utils import parse_diskwala_link, safe_filename


class LinkValidationTests(unittest.TestCase):
    def test_valid_www_link(self):
        self.assertEqual(
            parse_diskwala_link("https://www.diskwala.com/app/6af93cc0b61fd14efb04cbe4"),
            (
                "6af93cc0b61fd14efb04cbe4",
                "https://www.diskwala.com/app/6af93cc0b61fd14efb04cbe4",
            ),
        )

    def test_valid_apex_link_with_query(self):
        parsed = parse_diskwala_link("https://diskwala.com/app/abcdef123456?utm_source=x")
        self.assertEqual(parsed[0], "abcdef123456")

    def test_rejects_terabox(self):
        self.assertIsNone(parse_diskwala_link("https://1024tera.com/s/abc123"))

    def test_rejects_host_spoof(self):
        self.assertIsNone(parse_diskwala_link("https://diskwala.com.evil.test/app/abcdef123456"))

    def test_rejects_http(self):
        self.assertIsNone(parse_diskwala_link("http://diskwala.com/app/abcdef123456"))

    def test_rejects_prose(self):
        self.assertIsNone(parse_diskwala_link("Here https://diskwala.com/app/abcdef123456"))

    def test_rejects_bad_port(self):
        self.assertIsNone(parse_diskwala_link("https://diskwala.com:99999/app/abcdef123456"))

    def test_filename_sanitized(self):
        self.assertEqual(safe_filename("../bad:name.mp4"), "bad_name.mp4")


if __name__ == "__main__":
    unittest.main()
