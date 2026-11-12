import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SECTION = """## Website landing page (ba3a.dev)

`site/landing.md` is this project's landing page on ba3a.dev. The site fetches it from `main`
and republishes within about 15 minutes of a push; nothing else has to change. It goes stale
fast, so keep it true.

- When a change alters what a user sees or types (commands, flags, output, install steps,
  supported platforms, features, limits, license, version or size claims), update the matching
  section of `site/landing.md` in the same commit.
- Every command, number and output sample on the page must match the code. Re-run a sample
  when you touch it.
- Keep the section markers (`<div class="section-marker" id="name"></div>`) and their names
  (hero, problem, audience, how-it-works, timeline, spec-example, features, compare, dogfood,
  quick-start, faq, final-cta). The theme lays the sections out; do not add layout markup.
- Links and images must resolve: a broken one fails the ba3a.dev build.
- Leave `site/brand/` and `site/card.png` alone unless asked.
"""


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8", newline="") as handle:
        return handle.read().replace("\r\n", "\n")


class LandingGuidanceTest(unittest.TestCase):
    def test_claude_md_ends_with_the_landing_section(self):
        self.assertTrue(read("CLAUDE.md").endswith("\n" + SECTION))

    def test_claude_md_has_the_landing_heading_once(self):
        self.assertEqual(1, read("CLAUDE.md").count("## Website landing page (ba3a.dev)"))

    def test_pull_request_template_asks_whether_the_landing_page_is_still_true(self):
        template = read(".github", "pull_request_template.md")
        self.assertRegex(template, re.compile(
            r"^- \[ \] Landing page \(`site/landing\.md`\) still true\?$", re.MULTILINE))


if __name__ == "__main__":
    unittest.main()
