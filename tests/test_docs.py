import glob
import json
import os
import re
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

README_MAX_LINES = 80
GROUPS = ("Getting started", "Concepts", "Features", "Reference")
NESTED_BLOCKS = ("feature", "largeSpecWarning", "update")
FENCE = re.compile(r"^\s*(```|~~~)")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$")


def read(root, *parts):
    with open(os.path.join(root, *parts), encoding="utf-8") as f:
        return f.read()


def exists(root, *parts):
    return os.path.exists(os.path.join(root, *parts))


def doc_pages(root):
    """Topic pages: every docs/*.md except the index."""
    paths = sorted(glob.glob(os.path.join(root, "docs", "*.md")))
    return [os.path.basename(p) for p in paths if os.path.basename(p) != "README.md"]


def doc_files(root):
    return ["docs/README.md"] + ["docs/" + p for p in doc_pages(root)] if exists(root, "docs", "README.md") \
        else ["docs/" + p for p in doc_pages(root)]


def docs_text(root):
    return "\n".join(read(root, f) for f in doc_files(root))


def outside_fences(text):
    """Lines of `text` that are not inside a fenced code block."""
    inside = False
    for line in text.splitlines():
        if FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            yield line


def slug(heading):
    """GitHub's heading anchor: lowercase, keep letters, digits, spaces, - and _."""
    text = heading.strip().rstrip("#").strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def heading_slugs(text):
    seen, slugs = {}, set()
    for line in outside_fences(text):
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if not m:
            continue
        base = slug(m.group(1))
        n = seen.get(base, 0)
        seen[base] = n + 1
        slugs.add(base if n == 0 else "%s-%d" % (base, n))
    return slugs


def link_targets(text):
    """Link destinations outside fenced code blocks and inline code spans."""
    targets = []
    for line in outside_fences(text):
        line = re.sub(r"`[^`]*`", "", line)
        targets.extend(LINK.findall(line))
    return targets


def split_target(target):
    path, _, anchor = target.partition("#")
    return path, anchor


def is_external(target):
    return re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) is not None


# Each check returns a list of problems; an empty list means the rule holds.

def check_readme_length(root):
    count = len(read(root, "README.md").splitlines())
    return [] if count <= README_MAX_LINES else ["README.md has %d lines (max %d)" % (count, README_MAX_LINES)]


def config_keys(root):
    defaults = json.loads(read(root, "kit", "specloop", "defaults.json"))
    keys = []
    for k, v in defaults.items():
        keys.append(k)
        if k in NESTED_BLOCKS and isinstance(v, dict):
            keys.extend("%s.%s" % (k, sub) for sub in v)
    return defaults, keys


def table_rows(text):
    return [line for line in outside_fences(text) if line.lstrip().startswith("|")]


def check_config_keys(root):
    defaults, keys = config_keys(root)
    problems = []
    all_rows = "\n".join(r for f in doc_files(root) for r in table_rows(read(root, f)))
    for key in keys:
        if "`%s`" % key not in all_rows:
            problems.append("config key %s has no table row under docs/" % key)
    configuration = "\n".join(table_rows(read(root, "docs", "configuration.md"))) \
        if exists(root, "docs", "configuration.md") else ""
    for key in defaults:
        if "`%s`" % key not in configuration:
            problems.append("top-level key %s has no table row in docs/configuration.md" % key)
    return problems


def check_commands(root):
    commands = read(root, "docs", "commands.md") if exists(root, "docs", "commands.md") else ""
    names = sorted(os.path.splitext(os.path.basename(p))[0]
                   for p in glob.glob(os.path.join(root, "kit", "commands", "*.md")))
    return ["command /sl-%s is not in docs/commands.md" % n for n in names if "/sl-%s" % n not in commands]


def check_phase_files(root):
    text = docs_text(root)
    names = sorted(os.path.basename(p) for p in glob.glob(os.path.join(root, "kit", "specloop", "phases", "*.md")))
    return ["phase file %s is not mentioned under docs/" % n for n in names if n not in text]


def check_index(root):
    problems = []
    if not exists(root, "docs", "README.md"):
        return ["docs/README.md is missing"]
    index = read(root, "docs", "README.md")
    headings = [slug(m) for m in re.findall(r"^##\s+(.*)$", "\n".join(outside_fences(index)), re.M)]
    for group in GROUPS:
        if slug(group) not in headings:
            problems.append("docs/README.md has no '## %s' heading" % group)
    index_paths = [split_target(t)[0] for t in link_targets(index)]
    pages = doc_pages(root)
    for page in pages:
        listed = index_paths.count(page)
        if listed != 1:
            problems.append("docs/%s is listed %d times in docs/README.md (expected once)" % (page, listed))
        text = read(root, "docs", page)
        first = text.splitlines()[0] if text.strip() else ""
        if not first.startswith("[Documentation](README.md)"):
            problems.append("docs/%s does not start with a link to the index" % page)
        paths = [split_target(t)[0] for t in link_targets(text) if not is_external(t)]
        if "README.md" not in paths:
            problems.append("docs/%s does not link to README.md (the index)" % page)
        if not any(p.endswith(".md") and p not in ("README.md", page) and "/" not in p for p in paths):
            problems.append("docs/%s links to no other topic page" % page)
        if "see-also" not in heading_slugs(text):
            problems.append("docs/%s has no '## See also' section" % page)
    for path in index_paths:
        if path.endswith(".md") and path not in pages and "/" not in path and path != "README.md":
            problems.append("docs/README.md lists %s, which does not exist" % path)
    return problems


def check_links(root):
    problems = []
    files = ["README.md", "CHANGELOG.md"] + doc_files(root)
    for rel in files:
        if not exists(root, rel):
            problems.append("%s is missing" % rel)
            continue
        text = read(root, rel)
        for target in link_targets(text):
            if is_external(target):
                continue
            path, anchor = split_target(target)
            dest = os.path.normpath(os.path.join(root, os.path.dirname(rel), path)) if path else \
                os.path.join(root, rel)
            if not os.path.exists(dest):
                problems.append("%s links to missing file %s" % (rel, target))
                continue
            if anchor and os.path.isfile(dest):
                with open(dest, encoding="utf-8") as f:
                    if anchor not in heading_slugs(f.read()):
                        problems.append("%s links to missing anchor %s" % (rel, target))
    return problems


def check_changelog(root):
    lines = read(root, "CHANGELOG.md").splitlines()
    sections = [line for line in lines if line.startswith("## ")]
    version = read(root, "VERSION").strip()
    problems = []
    if not sections or sections[0] != "## [Unreleased]":
        problems.append("the first release section of CHANGELOG.md must be '## [Unreleased]'")
    released = []
    for line in sections[1:]:
        m = RELEASE_HEADING.match(line)
        if not m:
            problems.append("bad release heading: %s" % line)
        else:
            released.append(m.group(1))
    if version not in released:
        problems.append("CHANGELOG.md has no '## [%s] - YYYY-MM-DD' heading for VERSION" % version)
    return problems


class Tree:
    """A throwaway repository holding the smallest layout that satisfies every check."""

    FILES = {
        "VERSION": "1.2.3\n",
        "README.md": "# t\n\n[Docs](docs/README.md) [Changes](CHANGELOG.md) [License](LICENSE)\n",
        "LICENSE": "x\n",
        "CHANGELOG.md": "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-01-02\n",
        "kit/specloop/defaults.json": json.dumps({"maxIterations": 1, "update": {"mode": "off"}}),
        "kit/specloop/phases/plan.md": "plan\n",
        "kit/commands/go.md": "go\n",
        "docs/README.md": "# Docs\n\n## Getting started\n\n- [One](one.md) one\n\n## Concepts\n\n## Features\n"
                          "\n## Reference\n\n- [Two](two.md) two\n- [Config](configuration.md) config\n"
                          "- [Commands](commands.md) commands\n",
        "docs/one.md": "[Documentation](README.md) › One\n\n# One\n\nSee [Two](two.md#two).\n\n## See also\n\n"
                       "- [Two](two.md)\n",
        "docs/two.md": "[Documentation](README.md) › Two\n\n# Two\n\nphases: plan.md\n\n## See also\n\n"
                       "- [One](one.md)\n",
        "docs/configuration.md": "[Documentation](README.md) › Configuration\n\n# Configuration\n\n"
                                 "| Key | Meaning |\n|---|---|\n| `maxIterations` | n |\n| `update` | see [One](one.md) |\n"
                                 "| `update.mode` | m |\n\n## See also\n\n- [One](one.md)\n",
        "docs/commands.md": "[Documentation](README.md) › Commands\n\n# Commands\n\n`/sl-go`\n\n## See also\n\n"
                            "- [One](one.md)\n",
    }

    def __init__(self, test):
        self.root = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.root, True)
        for rel, text in self.FILES.items():
            self.write(rel, text)

    def write(self, rel, text):
        path = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def replace(self, rel, old, new):
        text = read(self.root, *rel.split("/"))
        assert old in text, (rel, old)
        self.write(rel, text.replace(old, new))


class DocsRulesTest(unittest.TestCase):
    """Each rule holds for the real repository (below) and fails when it is broken (here)."""

    def test_the_minimal_tree_passes_every_check(self):
        t = Tree(self)
        for check in (check_readme_length, check_config_keys, check_commands, check_phase_files, check_index,
                      check_links, check_changelog):
            self.assertEqual([], check(t.root), check.__name__)

    def test_slug_follows_github_rules(self):
        self.assertEqual("personal-configuration-configlocaljson", slug("Personal configuration: config.local.json"))
        self.assertEqual("sl-update", slug("`/sl-update`"))
        self.assertEqual("large-specs-and-plans", slug("Large specs and plans"))
        self.assertEqual("snake_case-ok", slug("snake_case-ok"))
        self.assertEqual({"a", "a-1", "b"}, heading_slugs("# A\n\n## A\n\n### B ###\n"))

    def test_links_in_code_and_external_links_are_ignored(self):
        text = "[a](x.md)\n```\n[b](nope.md)\n```\n`[c](nope.md)` [d](https://example.com/nope.md)\n"
        self.assertEqual(["x.md", "https://example.com/nope.md"], link_targets(text))
        t = Tree(self)
        t.write("docs/one.md", read(t.root, "docs", "one.md") + "\n```\n[b](nope.md#x)\n```\n"
                               "[web](https://example.com/nope.md#x)\n")
        self.assertEqual([], check_links(t.root))

    def test_readme_line_limit_fails_when_exceeded(self):
        t = Tree(self)
        t.write("README.md", "line\n" * (README_MAX_LINES + 1))
        self.assertEqual(1, len(check_readme_length(t.root)))
        t.write("README.md", "line\n" * README_MAX_LINES)
        self.assertEqual([], check_readme_length(t.root))

    def test_a_config_key_missing_from_the_docs_is_reported(self):
        t = Tree(self)
        t.replace("docs/configuration.md", "| `update.mode` | m |\n", "")
        self.assertEqual(["config key update.mode has no table row under docs/"], check_config_keys(t.root))

    def test_a_key_in_prose_is_not_documented(self):
        t = Tree(self)
        t.replace("docs/configuration.md", "| `maxIterations` | n |\n", "`maxIterations` is a key.\n")
        self.assertTrue(any("maxIterations" in p for p in check_config_keys(t.root)))

    def test_a_top_level_key_documented_only_on_another_page_is_reported(self):
        t = Tree(self)
        t.replace("docs/configuration.md", "| `maxIterations` | n |\n", "")
        t.replace("docs/one.md", "## See also", "| `maxIterations` | n |\n\n## See also")
        self.assertEqual(["top-level key maxIterations has no table row in docs/configuration.md"],
                         check_config_keys(t.root))

    def test_a_command_missing_from_commands_md_is_reported(self):
        t = Tree(self)
        t.write("kit/commands/pause.md", "pause\n")
        self.assertEqual(["command /sl-pause is not in docs/commands.md"], check_commands(t.root))

    def test_a_phase_file_not_mentioned_is_reported(self):
        t = Tree(self)
        t.write("kit/specloop/phases/review.md", "review\n")
        self.assertEqual(["phase file review.md is not mentioned under docs/"], check_phase_files(t.root))

    def test_a_page_missing_from_the_index_is_reported(self):
        t = Tree(self)
        t.write("docs/three.md", "[Documentation](README.md) › Three\n\n## See also\n\n- [One](one.md)\n")
        self.assertEqual(["docs/three.md is listed 0 times in docs/README.md (expected once)"],
                         check_index(t.root))

    def test_a_page_listed_twice_is_reported(self):
        t = Tree(self)
        t.replace("docs/README.md", "- [One](one.md) one\n", "- [One](one.md) one\n- [One again](one.md) one\n")
        self.assertEqual(["docs/one.md is listed 2 times in docs/README.md (expected once)"], check_index(t.root))

    def test_a_missing_group_heading_is_reported(self):
        t = Tree(self)
        t.replace("docs/README.md", "## Concepts\n", "")
        self.assertEqual(["docs/README.md has no '## Concepts' heading"], check_index(t.root))

    def test_a_page_without_a_back_link_is_reported(self):
        t = Tree(self)
        t.replace("docs/two.md", "[Documentation](README.md) › Two\n", "Two\n")
        problems = check_index(t.root)
        self.assertIn("docs/two.md does not start with a link to the index", problems)
        self.assertIn("docs/two.md does not link to README.md (the index)", problems)

    def test_a_page_without_see_also_or_topic_link_is_reported(self):
        t = Tree(self)
        t.write("docs/two.md", "[Documentation](README.md) › Two\n\n# Two\n\nSee [here](#two).\n")
        problems = check_index(t.root)
        self.assertIn("docs/two.md links to no other topic page", problems)
        self.assertIn("docs/two.md has no '## See also' section", problems)

    def test_a_broken_file_link_is_reported(self):
        t = Tree(self)
        t.replace("docs/one.md", "(two.md#two)", "(missing.md)")
        self.assertEqual(["docs/one.md links to missing file missing.md"], check_links(t.root))

    def test_a_broken_anchor_is_reported(self):
        t = Tree(self)
        t.replace("docs/one.md", "(two.md#two)", "(two.md#nowhere)")
        self.assertEqual(["docs/one.md links to missing anchor two.md#nowhere"], check_links(t.root))

    def test_a_broken_anchor_in_the_same_file_is_reported(self):
        t = Tree(self)
        t.replace("docs/one.md", "(two.md#two)", "(#nowhere)")
        self.assertEqual(["docs/one.md links to missing anchor #nowhere"], check_links(t.root))

    def test_a_broken_link_in_the_readme_or_changelog_is_reported(self):
        t = Tree(self)
        t.replace("README.md", "(LICENSE)", "(LICENSES)")
        t.replace("CHANGELOG.md", "# Changelog", "# Changelog\n\n[x](docs/gone.md)")
        self.assertEqual(["README.md links to missing file LICENSES",
                          "CHANGELOG.md links to missing file docs/gone.md"], check_links(t.root))

    def test_a_version_without_a_changelog_entry_is_reported(self):
        t = Tree(self)
        t.write("VERSION", "1.2.4\n")
        self.assertEqual(["CHANGELOG.md has no '## [1.2.4] - YYYY-MM-DD' heading for VERSION"],
                         check_changelog(t.root))

    def test_unreleased_must_come_first(self):
        t = Tree(self)
        t.write("CHANGELOG.md", "# Changelog\n\n## [1.2.3] - 2026-01-02\n\n## [Unreleased]\n")
        problems = check_changelog(t.root)
        self.assertIn("the first release section of CHANGELOG.md must be '## [Unreleased]'", problems)

    def test_a_release_heading_needs_a_date(self):
        t = Tree(self)
        t.write("CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n## [1.2.3]\n")
        self.assertTrue(check_changelog(t.root))


class DocsTest(unittest.TestCase):
    """The real repository."""

    def test_readme_has_at_most_80_lines(self):
        self.assertEqual([], check_readme_length(ROOT))

    def test_every_default_config_key_is_documented(self):
        self.assertEqual([], check_config_keys(ROOT))

    def test_every_command_template_is_in_commands_md(self):
        self.assertEqual([], check_commands(ROOT))

    def test_every_phase_file_is_mentioned_in_docs(self):
        self.assertEqual([], check_phase_files(ROOT))

    def test_docs_pages_are_indexed_linked_back_and_cross_linked(self):
        self.assertEqual([], check_index(ROOT))

    def test_relative_links_and_anchors_resolve(self):
        self.assertEqual([], check_links(ROOT))

    def test_changelog_has_unreleased_first_and_the_current_version(self):
        self.assertEqual([], check_changelog(ROOT))

    def test_changelog_is_not_part_of_the_installed_kit(self):
        self.assertFalse(exists(ROOT, "kit", "CHANGELOG.md"))
        self.assertFalse(exists(ROOT, "kit", "docs"))

    def test_docs_cover_updates_and_local_config(self):
        docs = docs_text(ROOT)
        for needle in ("config.local.json", "`/sl-update`", "/sl-update check", "/sl-update skip",
                       "/sl-update never", "update.json", "update.lock", "`CI`", "SPECLOOP_DISABLED",
                       "intervalHours", "timeoutSec", "safe point", "major", "fork", "--keep-config",
                       "install.py <project>", "`off`", "`notify`", "`auto`"):
            self.assertIn(needle, docs)

    def test_multi_chat_behaviour_is_documented(self):
        docs = docs_text(ROOT)
        for needle in ("{attempt_dir}", "{review_file}", "lateReviews", "commitBase", "{run_dir}/review.md",
                       "Several chats in one project", "git worktree", "waits on its own background work"):
            self.assertIn(needle, docs)

    def test_guide_describes_installed_update_files(self):
        guide = read(ROOT, "CLAUDE.md")
        for needle in ("config.local.json", "update.json", "update.lock", "manifest.json", "source",
                       "kit/commands/update.md"):
            self.assertIn(needle, guide)

    def test_guide_describes_the_engine_rules(self):
        guide = read(ROOT, "CLAUDE.md")
        for needle in ("state.lock", "run/attempts/", "update_state", "background_tasks",
                       "waits on its own background work"):
            self.assertIn(needle, guide)
        self.assertNotIn("Nothing in this repository runs inside this repository", guide)

    def test_guide_describes_the_documentation_layout_and_rules(self):
        guide = read(ROOT, "CLAUDE.md")
        for needle in ("docs/", "docs/README.md", "CHANGELOG.md", "Unreleased", "80 lines", "## See also",
                       "configuration.md", "commands.md"):
            self.assertIn(needle, guide)
        self.assertNotIn("Update `README.md` whenever", guide)


if __name__ == "__main__":
    unittest.main()
