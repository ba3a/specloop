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

    def test_version_is_0_2_1(self):
        self.assertEqual("0.2.1", read("VERSION").strip())

    def test_multi_chat_behaviour_is_documented(self):
        readme, guide = read("README.md"), read("CLAUDE.md")
        for needle in ("{attempt_dir}", "## Several chats in one project", "git worktree"):
            self.assertIn(needle, readme)
        for needle in ("state.lock", "run/attempts/", "update_state", "background_tasks"):
            self.assertIn(needle, guide)
        self.assertNotIn("Nothing in this repository runs inside this repository", guide)


if __name__ == "__main__":
    unittest.main()
