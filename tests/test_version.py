import unittest

from src.framecycler.core.version import (
    APP_NAME,
    __version__,
    get_about_text,
    get_application_version,
    get_build_info,
)


class TestVersion(unittest.TestCase):
    def test_build_info_has_expected_keys(self):
        info = get_build_info()
        for key in ("version", "commit", "commit_short", "branch", "dirty"):
            self.assertIn(key, info)

    def test_application_version_includes_commit(self):
        info = get_build_info()
        version = get_application_version()
        self.assertTrue(version.startswith(info["version"]))
        if info["commit_short"] != "unknown":
            self.assertIn(info["commit_short"], version)

    def test_about_text_includes_copyright_and_license(self):
        text = get_about_text()
        self.assertIn(get_build_info()["version"], text)
        self.assertIn("Copyright", text)
        self.assertIn("PolyForm Small Business License", text)
        self.assertIn("fewer than 10 total individuals", text)
        self.assertIn("500,000 USD", text)
        self.assertIn("commercial license", text)
        self.assertIn("Build commit:", text)


if __name__ == "__main__":
    unittest.main()
