import os
import unittest
from unittest.mock import patch

from app.services.capital_dll_environment import ensure_dll_directory_on_path


class CapitalDllEnvironmentTests(unittest.TestCase):
    def test_dll_directory_is_kept_once_on_path(self):
        with patch.dict(os.environ, {"PATH": r"C:\Windows;C:\Capital;C:\Capital"}, clear=False):
            ensure_dll_directory_on_path(r"C:\Capital")
            entries = os.environ["PATH"].split(os.pathsep)
        target = os.path.normcase(os.path.abspath(r"C:\Capital"))
        matches = [entry for entry in entries if os.path.normcase(os.path.abspath(entry)) == target]
        self.assertEqual(len(matches), 1)
        self.assertEqual(os.path.normcase(os.path.abspath(entries[0])), target)


if __name__ == "__main__":
    unittest.main()
