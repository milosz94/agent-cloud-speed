"""The off-clock success oracle (M1): flags an infra error / default page that the <500 liveness predicate
still counts as serving, WITHOUT rejecting a legitimate app 4xx. Pure-logic test of _looks_like_non_app."""
import unittest
from autorun import _looks_like_non_app


class TestSuccessOracle(unittest.TestCase):
    def test_flags_infra_and_default_pages(self):
        self.assertIsNotNone(_looks_like_non_app("<html><body>welcome to nginx! thank you</body></html>"))
        self.assertIsNotNone(_looks_like_non_app("<error><code>accessdenied</code><message>...</message></error>"))
        self.assertIsNotNone(_looks_like_non_app("<html><title>error</title>the request could not be satisfied.</html>"))
        self.assertIsNotNone(_looks_like_non_app("<html><head><title>502 bad gateway</title></head></html>"))
        self.assertIsNotNone(_looks_like_non_app("apache2 default page: it works"))

    def test_does_NOT_flag_legit_app_responses(self):
        # a real app root, and legitimate app 4xx bodies, must pass (not infra/default pages)
        self.assertIsNone(_looks_like_non_app('{"error":"unauthorized","code":401}'))       # API 401
        self.assertIsNone(_looks_like_non_app("<!doctype html><title>umami</title><div id=app></div>"))
        self.assertIsNone(_looks_like_non_app("please provide a query parameter"))            # Isso-style 400
        self.assertIsNone(_looks_like_non_app("<html><body>my dashboard</body></html>"))
        # a page that merely mentions access in prose is not an S3 error (needs the <error> structure)
        self.assertIsNone(_looks_like_non_app("access your account settings here"))


if __name__ == "__main__":
    unittest.main()
