import re
import unittest
from pathlib import Path
from jinja2 import Environment, FileSystemLoader


PROMPT_DIR = Path(__file__).resolve().parents[1] / "assets" / "prompts"


class PmkisanPromptValidationTests(unittest.TestCase):
    def test_registration_examples_and_tool_handoff_in_every_language(self):
        prompts = sorted(PROMPT_DIR.glob("voice_*.md"))
        self.assertTrue(prompts)
        for path in prompts:
            with self.subTest(language=path.stem):
                prompt = Environment(loader=FileSystemLoader(PROMPT_DIR)).get_template(path.name).render()
                rule = next(
                    line for line in prompt.splitlines()
                    if line.startswith("**PM-KISAN Registration Number Validation:**")
                )
                self.assertIn("Only for personal status or grievance requests", rule)
                self.assertIn("does not apply to scheme information or public release dates", rule)
                self.assertIn("trim surrounding whitespace", rule)
                self.assertIn("join the spoken letters and digits", rule)
                pattern = rule.split("`")[1]
                for example in ("TN110700729", "TN110698429"):
                    self.assertIsNotNone(re.fullmatch(pattern, example))
                    self.assertNotIn(example, prompt)
                    self.assertIsNotNone(re.fullmatch(pattern, f" {example.lower()} ".strip().upper()))
                for spoken in ("TN 110700729", "tn-110700729"):
                    normalized = re.sub(r"[\s-]", "", spoken).upper()
                    self.assertIsNotNone(re.fullmatch(pattern, normalized))
                self.assertIn("`TN000000000`", rule)
                self.assertNotIn("UP 123456789", prompt)
                self.assertNotIn("UP-123456789", prompt)
                self.assertIn("Never show or say a real registration number as an example", rule)
                for invalid in ("TN11069842", "TN1106984290", "TN11069842X", "12110698429"):
                    self.assertIsNone(re.fullmatch(pattern, invalid))
                self.assertIn("11 characters, not 11 digits", rule)
                self.assertIn("do not reject it or ask for the number again", rule)
                self.assertIn("call `initiate_pm_kisan_status_check(reg_no)`", rule)
                self.assertIn("do not claim the portal rejected it", rule)
                self.assertIn("Only report a missing record if the status tool", rule)


if __name__ == "__main__":
    unittest.main()
