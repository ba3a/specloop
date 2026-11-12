import os
import re
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANDING = os.path.join(ROOT, "site", "landing.md")

SECTIONS = [
    "hero",
    "problem",
    "how-it-works",
    "timeline",
    "spec-example",
    "features",
    "dogfood",
    "quick-start",
    "faq",
    "final-cta",
]

FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
MARKER = re.compile(r'^<div class="section-marker" id="([^"]*)"></div>$', re.MULTILINE)
FENCED = re.compile(r"^```.*?^```$", re.DOTALL | re.MULTILINE)
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def parse_mapping(text):
    """The nested `key: value` subset of YAML that the front matter uses."""
    root = {}
    stack = [(-1, root)]
    for line in text.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, value = line.strip().partition(":")
        value = value.strip()
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value:
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            parent[key] = value
        else:
            parent[key] = {}
            stack.append((indent, parent[key]))
    return root


def read_landing():
    with open(LANDING, encoding="utf-8") as f:
        text = f.read()
    match = FRONT.match(text)
    if not match:
        raise AssertionError("site/landing.md starts with front matter")
    return parse_mapping(match.group(1)), text[match.end():]


class LandingTest(unittest.TestCase):
    def test_the_text_has_the_ten_section_markers_in_order(self):
        _, body = read_landing()
        self.assertEqual(MARKER.findall(body), SECTIONS)

    def test_front_matter_has_the_layout_and_the_accent_palette(self):
        data, _ = read_landing()
        layout = data["layout"]
        self.assertEqual(
            layout["visual"],
            {
                "hero": "pre",
                "problem": "blockquote",
                "timeline": "ul",
                "spec-example": "pre",
                "quick-start": "pre",
            },
        )
        self.assertEqual(layout["dogfood"], "gitlog")
        self.assertEqual(layout["when"], "time")
        self.assertEqual(layout["brand"], "4")
        self.assertEqual(
            list(layout["strings"]),
            ["terminal_title", "hero_terminal", "dogfood_terminal", "chat"],
        )
        self.assertEqual(
            data["accent"],
            {
                "light": {"accent": "#c2521c", "accent-text": "#ae4716", "accent-soft": "#f6e3d6"},
                "dark": {"accent": "#f08a50", "accent-text": "#f08a50", "accent-soft": "#2e1f17"},
            },
        )

    def test_every_relative_link_and_image_target_exists(self):
        _, body = read_landing()
        targets = LINK.findall(FENCED.sub("", body))
        self.assertTrue(targets)
        for target in targets:
            if re.match(r"(https?:|mailto:|#)", target):
                continue
            self.assertFalse(target.startswith("/"), target + " is a site address, not a repository path")
            path = os.path.normpath(os.path.join(os.path.dirname(LANDING), target.split("#")[0].split("?")[0]))
            self.assertTrue(os.path.exists(path), target + " does not exist")

    def test_in_page_links_lead_to_a_section_marker(self):
        _, body = read_landing()
        for anchor in re.findall(r"\]\(#([^)\s]+)\)", FENCED.sub("", body)):
            self.assertIn(anchor, SECTIONS)


ALLOWED_MARKERS = {
    "hero",
    "problem",
    "audience",
    "how-it-works",
    "timeline",
    "spec-example",
    "features",
    "compare",
    "dogfood",
    "quick-start",
    "faq",
    "final-cta",
}
REQUIRED_FILES = ("site/brand/logo.svg", "site/card.png")
ANY_MARKER = re.compile(r'<div class="section-marker" id="([^"]*)"></div>')


def problems(root):
    """What is wrong with the landing page under `root`, as a list of messages."""
    found = []
    path = os.path.join(root, "site", "landing.md")
    if not os.path.isfile(path):
        found.append("site/landing.md is missing")
    else:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        lines = text.split("\n")
        fences = [i for i, line in enumerate(lines) if line == "---"]
        if lines[0] != "---" or len(fences) < 2:
            found.append("site/landing.md has no front matter between two --- lines")
            body = text
        else:
            body = "\n".join(lines[fences[1] + 1:])
        seen = set()
        for marker in ANY_MARKER.findall(body):
            if marker not in ALLOWED_MARKERS:
                found.append("unknown section marker: " + marker)
            elif marker in seen:
                found.append("duplicate section marker: " + marker)
            seen.add(marker)
    for name in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(root, *name.split("/"))):
            found.append(name + " is missing")
    return found


def marker(name):
    return '<div class="section-marker" id="%s"></div>\n' % name


class LandingGuardTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "site", "brand"))
        for name in REQUIRED_FILES:
            with open(os.path.join(self.root, *name.split("/")), "w") as f:
                f.write("x")

    def write(self, text):
        with open(os.path.join(self.root, "site", "landing.md"), "w", encoding="utf-8") as f:
            f.write(text)

    def good(self, *names):
        return "---\nlayout: x\n---\n\n" + "\n".join(marker(n) for n in names)

    def test_the_real_repository_passes(self):
        self.assertEqual(problems(ROOT), [])

    def test_a_valid_page_passes_with_up_to_twelve_markers(self):
        self.write(self.good(*sorted(ALLOWED_MARKERS)))
        self.assertEqual(problems(self.root), [])

    def test_a_missing_landing_file_fails(self):
        self.assertEqual(problems(self.root), ["site/landing.md is missing"])

    def test_missing_front_matter_fails(self):
        self.write(marker("hero"))
        self.assertEqual(len(problems(self.root)), 1)
        self.assertIn("front matter", problems(self.root)[0])
        self.write("---\nlayout: x\n\n" + marker("hero"))
        self.assertIn("front matter", problems(self.root)[0])

    def test_an_unknown_marker_fails(self):
        self.write(self.good("hero", "pricing"))
        self.assertEqual(problems(self.root), ["unknown section marker: pricing"])

    def test_a_duplicate_marker_fails(self):
        self.write(self.good("hero", "faq", "hero"))
        self.assertEqual(problems(self.root), ["duplicate section marker: hero"])

    def test_a_missing_logo_or_card_fails(self):
        self.write(self.good("hero"))
        os.remove(os.path.join(self.root, "site", "brand", "logo.svg"))
        os.remove(os.path.join(self.root, "site", "card.png"))
        self.assertEqual(
            problems(self.root), ["site/brand/logo.svg is missing", "site/card.png is missing"]
        )


if __name__ == "__main__":
    unittest.main()
