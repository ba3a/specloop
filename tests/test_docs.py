import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class DocsTest(unittest.TestCase):

    def test_every_default_config_key_is_documented(self):
        defaults = json.loads(read("kit", "specloop", "defaults.json"))
        readme = read("README.md")
        keys = [k for k in defaults if k != "feature"] + ["feature." + k for k in defaults["feature"]]
        missing = [k for k in keys if "`%s`" % k not in readme]
        self.assertEqual([], missing, "config keys missing from README.md")

    def test_version_is_0_3_0(self):
        self.assertEqual("0.3.0", read("VERSION").strip())

    def test_readme_documents_local_config_and_updates(self):
        readme = read("README.md")
        for needle in ("config.local.json", "## Updates", "`/sl-update`", "/sl-update check", "/sl-update skip",
                       "/sl-update never", "update.json", "update.lock", "`CI`", "SPECLOOP_DISABLED",
                       "intervalHours", "timeoutSec", "safe point", "major", "fork", "--keep-config",
                       "install.py <project>", "`off`", "`notify`", "`auto`"):
            self.assertIn(needle, readme)

    def test_guide_describes_installed_update_files(self):
        guide = read("CLAUDE.md")
        for needle in ("config.local.json", "update.json", "update.lock", "manifest.json", "source",
                       "kit/commands/update.md"):
            self.assertIn(needle, guide)

    def test_multi_chat_behaviour_is_documented(self):
        readme, guide = read("README.md"), read("CLAUDE.md")
        for needle in ("{attempt_dir}", "{review_file}", "lateReviews", "commitBase", "{run_dir}/review.md",
                       "## Several chats in one project", "git worktree", "waits on its own background work"):
            self.assertIn(needle, readme)
        for needle in ("state.lock", "run/attempts/", "update_state", "background_tasks",
                       "waits on its own background work"):
            self.assertIn(needle, guide)
        self.assertNotIn("Nothing in this repository runs inside this repository", guide)


if __name__ == "__main__":
    unittest.main()
