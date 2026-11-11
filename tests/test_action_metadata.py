import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ActionMetadataTests(unittest.TestCase):
    def test_descriptions_contain_no_expressions(self):
        """GitHub evaluates expressions in action metadata and rejects contexts such as needs."""
        for action in sorted(ROOT.glob("*/action.yml")):
            for number, line in enumerate(action.read_text().splitlines(), 1):
                if re.match(r"\s*description:", line):
                    with self.subTest(action=action.parent.name, line=number):
                        self.assertNotIn("${{", line)


if __name__ == "__main__":
    unittest.main()
