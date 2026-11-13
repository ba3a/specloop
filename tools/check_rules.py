#!/usr/bin/env python3
"""Check the machine-checkable rules of CLAUDE.md on the files tracked by git.

Standard library only. Prints one line `path:line: rule: message` per violation and exits 1 if there are any.

Rules:
  py38-grammar     files under kit/, install.py, tools/ and tests/ parse with Python 3.8 grammar
  import-allowlist imports in kit/, install.py and tools/ are standard-library modules (allowlist below) or local
  hardcoded-prefix no "/sl-" in the string literals of kit/specloop/engine.py (docstrings excepted) or in
                   kit/commands/*.md: command names go through the prefix
  line-endings     .sh files use LF, .cmd files use CRLF

Usage: python tools/check_rules.py [repository-root]
"""

import ast
import os
import subprocess
import sys
from typing import Iterator, List, Optional, Set, Tuple

# (path, line, rule, message); path uses forward slashes and is relative to the repository root.
Violation = Tuple[str, int, str, str]

PY_GRAMMAR_ROOTS = ("kit/", "install.py", "tools/", "tests/")
IMPORT_ROOTS = ("kit/", "install.py", "tools/")
ENGINE = "kit/specloop/engine.py"
PREFIX_MARKER = "/sl-"

# Top-level standard-library modules of Python 3.8. `sys.stdlib_module_names` needs Python 3.10, so the list is kept
# here. Add a module only if it exists in the standard library of Python 3.8.
STDLIB_3_8 = frozenset("""
__future__ abc argparse array ast asyncio atexit base64 bdb binascii bisect builtins bz2 calendar cmath cmd code
codecs collections colorsys concurrent configparser contextlib contextvars copy copyreg csv ctypes dataclasses
datetime decimal difflib dis doctest email encodings enum errno faulthandler fcntl filecmp fileinput fnmatch
fractions ftplib functools gc getopt getpass gettext glob gzip hashlib heapq hmac html http imaplib imp importlib
inspect io ipaddress itertools json keyword linecache locale logging lzma mimetypes mmap msvcrt multiprocessing
netrc numbers operator os pathlib pickle pkgutil platform plistlib poplib posix posixpath pprint pwd queue quopri
random re reprlib resource runpy sched secrets select selectors shelve shlex shutil signal site smtplib socket
socketserver sqlite3 ssl stat statistics string stringprep struct subprocess sys sysconfig tarfile tempfile
textwrap threading time timeit token tokenize traceback types typing unicodedata unittest urllib uu uuid venv
warnings weakref winreg xml zipfile zipimport zlib
math cgi cgitb compileall copyreg cProfile curses dbm grp imghdr lib2to3 mailbox marshal modulefinder ntpath optparse
pdb pstats profile pty py_compile pydoc pyclbr readline symtable sunau syslog tabnanny telnetlib termios trace
tracemalloc tty turtle wave webbrowser wsgiref xmlrpc zipapp _thread
""".split())


def starts_with_any(path: str, roots: Tuple[str, ...]) -> bool:
    return any(path == root or (root.endswith("/") and path.startswith(root)) for root in roots)


def tracked_files(root: str) -> List[str]:
    """Paths of the tracked files that exist in the working tree, relative to `root`, with forward slashes."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         check=True).stdout
    paths = [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]
    return sorted(p for p in paths if os.path.isfile(os.path.join(root, p)))


def read_bytes(root: str, rel: str) -> bytes:
    with open(os.path.join(root, rel), "rb") as f:
        return f.read()


def parse_py(rel: str, data: bytes) -> Tuple[Optional[ast.AST], List[Violation]]:
    """Parse `data` with Python 3.8 grammar. Returns the tree, or None and the violation."""
    try:
        return ast.parse(data, filename=rel, feature_version=(3, 8)), []
    except SyntaxError as e:
        return None, [(rel, e.lineno or 1, "py38-grammar", "not valid Python 3.8: %s" % e.msg)]
    except (ValueError, MemoryError, RecursionError) as e:
        return None, [(rel, 1, "py38-grammar", "cannot be parsed: %s" % e)]


def imported_modules(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    """(line, top-level module name) of every absolute import in the tree."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module.split(".")[0]


def local_modules(rel: str, tracked: List[str]) -> Set[str]:
    """Names of the tracked Python modules next to `rel` and in the repository root: in-kit imports such as `engine`."""
    here = os.path.dirname(rel)
    names = set()
    for path in tracked:
        if path.endswith(".py") and os.path.dirname(path) in (here, ""):
            names.add(os.path.basename(path)[:-3])
    return names


def check_imports(rel: str, tree: ast.AST, local: Set[str]) -> List[Violation]:
    found = []
    for line, name in imported_modules(tree):
        if name not in STDLIB_3_8 and name not in local:
            found.append((rel, line, "import-allowlist",
                          "'%s' is not in the standard-library allowlist of tools/check_rules.py" % name))
    return found


def docstring_nodes(tree: ast.AST) -> Set[int]:
    """ids of the string Constant nodes that are docstrings of the module, a class or a function."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def check_engine_prefix(rel: str, tree: ast.AST) -> List[Violation]:
    """Hard-coded command names in string literals. Comments are not in the AST, docstrings are skipped on purpose:
    they may explain the prefix."""
    docstrings = docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings \
                and PREFIX_MARKER in node.value:
            found.append((rel, node.lineno, "hardcoded-prefix",
                          "hard-coded '/sl-' in a string literal; use cmd_name(config, ...)"))
    return found


def check_command_template(rel: str, data: bytes) -> List[Violation]:
    found = []
    text = data.decode("utf-8", "replace")
    for number, line in enumerate(text.splitlines(), 1):
        if PREFIX_MARKER in line:
            found.append((rel, number, "hardcoded-prefix", "hard-coded '/sl-'; use /{{prefix}}"))
    return found


def check_line_endings(rel: str, data: bytes) -> List[Violation]:
    """.sh files must be LF only, .cmd files CRLF only."""
    found = []
    lines = data.split(b"\n")
    if rel.endswith(".sh"):
        for number, line in enumerate(lines, 1):
            if line.endswith(b"\r"):
                found.append((rel, number, "line-endings", "CRLF in a .sh file; use LF"))
                break
    elif rel.endswith(".cmd"):
        for number, line in enumerate(lines[:-1], 1):
            if not line.endswith(b"\r"):
                found.append((rel, number, "line-endings", "LF without CR in a .cmd file; use CRLF"))
                break
    return found


def check_repo(root: str) -> List[Violation]:
    tracked = tracked_files(root)
    found = []  # type: List[Violation]
    trees = {}  # type: Dict[str, ast.AST]
    for rel in tracked:
        if rel.endswith(".py") and starts_with_any(rel, PY_GRAMMAR_ROOTS):
            tree, problems = parse_py(rel, read_bytes(root, rel))
            found.extend(problems)
            if tree is not None:
                trees[rel] = tree
    for rel, tree in trees.items():
        if starts_with_any(rel, IMPORT_ROOTS):
            found.extend(check_imports(rel, tree, local_modules(rel, tracked)))
        if rel == ENGINE:
            found.extend(check_engine_prefix(rel, tree))
    for rel in tracked:
        if rel.startswith("kit/commands/") and rel.endswith(".md") and rel.count("/") == 2:
            found.extend(check_command_template(rel, read_bytes(root, rel)))
        elif rel.endswith((".sh", ".cmd")):
            found.extend(check_line_endings(rel, read_bytes(root, rel)))
    return sorted(found)


def main(argv: List[str]) -> int:
    root = argv[1] if len(argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        found = check_repo(root)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = (getattr(e, "stderr", None) or b"").decode("utf-8", "replace").strip() or str(e)
        print("check_rules: cannot list tracked files in %s: %s" % (root, detail), file=sys.stderr)
        return 2
    for path, line, rule, message in found:
        print("%s:%d: %s: %s" % (path, line, rule, message))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
