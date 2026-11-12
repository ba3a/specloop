import hashlib
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Brand files and the card, pinned by size and sha256 of the original bytes.
FILES = [
    ("site/brand/logo.svg", 1984, "bd35efd1891768d955ffb48ffc2a83efbecd3a2c95abd440756cd3314d707ee5"),
    ("site/brand/logo-dark.svg", 1984, "dd226f5d4f77d1018ea0fade653118871c3a7dfb495bfaf77c8efdf75ae86601"),
    ("site/brand/favicon.svg", 1598, "bb1ae9f302ead813733c665d90529005315ee063e0870d36f33561551148a262"),
    ("site/brand/favicon.ico", 6957, "28cae57f2aab2d581f2ebee586e4861a8598740f68e5d27bf2423539e44a29ae"),
    ("site/brand/apple-touch-icon.png", 12267, "5909679cbe6d4377b407580e431e35dccfe77d37dafba11ff0baf851cb7e1047"),
    ("site/card.png", 116344, "c973d800505bd5ec8c44dd1379433aa1b3b86d8525b339960ef1d78207f4e6eb"),
]


def read(path):
    with open(os.path.join(ROOT, path), "rb") as f:
        return f.read()


class SiteBrandTest(unittest.TestCase):
    def test_files_are_byte_identical_to_the_originals(self):
        for path, size, sha256 in FILES:
            with self.subTest(path=path):
                data = read(path)
                self.assertEqual(len(data), size)
                self.assertEqual(hashlib.sha256(data).hexdigest(), sha256)

    def test_svg_brand_files_use_lf_line_endings_only(self):
        for path, _, _ in FILES:
            if path.endswith(".svg"):
                with self.subTest(path=path):
                    self.assertNotIn(b"\r", read(path))

    def test_brand_dir_holds_only_the_expected_files(self):
        expected = sorted(p.split("/")[-1] for p, _, _ in FILES if p.startswith("site/brand/"))
        self.assertEqual(sorted(os.listdir(os.path.join(ROOT, "site", "brand"))), expected)


if __name__ == "__main__":
    unittest.main()
