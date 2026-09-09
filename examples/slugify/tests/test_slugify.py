import unittest

from slugify import slugify


class SlugifyTest(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_strips_leading_and_trailing_separators(self):
        self.assertEqual(slugify("--hello--"), "hello")

    # There is no test that "a  b" collapses to "a-b" rather than "a--b".
    # The suite is green. The collapse_dropped mutant survives. That is the
    # gap this tool exists to show.
