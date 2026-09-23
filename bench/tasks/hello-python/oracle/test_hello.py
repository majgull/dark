import subprocess
import sys
import unittest


class HelloTest(unittest.TestCase):
    def test_output(self):
        out = subprocess.run([sys.executable, "hello.py"], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, "hello, dark\n")


if __name__ == "__main__":
    unittest.main()
