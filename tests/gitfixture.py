"""Throwaway git setup for update-checker tests.

Builds, in a temporary directory:

- ``remote``: a bare repository standing in for the kit's upstream;
- ``clone``: a clone of it containing copies of install.py, kit/ and VERSION, committed
  and tracking ``origin/main``;
- ``target``: an empty project that is a git repository.

GIT_CEILING_DIRECTORIES is set while the fixture is alive so git never discovers a
repository outside the temporary directory, and every repository gets a local identity,
so nothing depends on the developer's machine. No test touches the network.
"""

import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRANCH = "main"
# Variables that would point git at another repository (set, for example, inside git hooks).
GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR")


def git_available():
    return shutil.which("git") is not None


def clean_env():
    env = dict(os.environ)
    for name in GIT_LOCATION_VARS:
        env.pop(name, None)
    return env


def copy_kit_source(dest):
    """Copy install.py, kit/ and VERSION from this repository into dest."""
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(os.path.join(REPO, "install.py"), dest)
    shutil.copy2(os.path.join(REPO, "VERSION"), dest)
    shutil.copytree(os.path.join(REPO, "kit"), os.path.join(dest, "kit"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


class GitFixture(object):

    def __init__(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self._ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = os.path.dirname(self.root)
        self.remote = os.path.join(self.root, "remote.git")
        self.clone = os.path.join(self.root, "clone")
        self.work = os.path.join(self.root, "work")
        self.target = os.path.join(self.root, "target")
        try:
            self._build()
        except Exception:
            self.cleanup()
            raise

    # -- helpers -------------------------------------------------------------------------

    def git(self, cwd, *args):
        """Run git in cwd, raise on failure, return stripped stdout."""
        result = subprocess.run(["git"] + list(args), cwd=cwd, env=clean_env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)
        if result.returncode != 0:
            raise RuntimeError("git %s failed in %s: %s" % (" ".join(args), cwd, result.stderr))
        return result.stdout.strip()

    def configure(self, repo):
        for key, value in (("user.name", "specloop tests"), ("user.email", "tests@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            self.git(repo, "config", key, value)

    def init_repo(self, path, bare=False):
        os.makedirs(path, exist_ok=True)
        self.git(path, "init", "-q", *(["--bare"] if bare else []))
        self.git(path, "symbolic-ref", "HEAD", "refs/heads/" + BRANCH)
        if not bare:
            self.configure(path)

    def clone_remote(self, dest):
        self.git(self.root, "clone", "-q", self.remote, dest)
        self.configure(dest)

    def _build(self):
        self.init_repo(self.remote, bare=True)
        seed = os.path.join(self.root, "seed")
        self.init_repo(seed)
        copy_kit_source(seed)
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-q", "-m", "Initial kit")
        self.git(seed, "remote", "add", "origin", self.remote)
        self.git(seed, "push", "-q", "origin", BRANCH)
        shutil.rmtree(seed, onerror=_force_remove)
        self.clone_remote(self.clone)
        self.init_repo(self.target)

    def head(self, repo=None):
        return self.git(repo or self.clone, "rev-parse", "HEAD")

    def read_version(self, repo=None):
        with open(os.path.join(repo or self.clone, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()

    def _commit_version(self, repo, version):
        with open(os.path.join(repo, "VERSION"), "w", encoding="utf-8", newline="\n") as f:
            f.write(version + "\n")
        self.git(repo, "commit", "-q", "-am", "Release " + version)
        return self.head(repo)

    def bump_remote_version(self, version):
        """Commit a new VERSION in the bare repository through a second clone; return the commit."""
        if not os.path.isdir(self.work):
            self.clone_remote(self.work)
        else:
            self.git(self.work, "pull", "-q", "--ff-only")
        commit = self._commit_version(self.work, version)
        self.git(self.work, "push", "-q", "origin", BRANCH)
        return commit

    def bump_clone_version(self, version):
        """Commit a new VERSION in the source clone only (not pushed); return the commit."""
        return self._commit_version(self.clone, version)

    def run_install(self, *args, **kwargs):
        """Run the clone's install.py (or the one in `source`) with this Python; return the result."""
        source = kwargs.pop("source", self.clone)
        target = kwargs.pop("target", self.target)
        return subprocess.run([sys.executable, os.path.join(source, "install.py"), target] + list(args),
                              env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, **kwargs)

    def cleanup(self):
        if self._ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self._ceiling
        shutil.rmtree(self.root, onerror=_force_remove)


def _force_remove(func, path, _exc_info):
    """Git marks object files read-only, which shutil.rmtree cannot delete on Windows."""
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass
