import os
import unittest
from pathlib import Path
import sys

tools_dir = Path(__file__).resolve().parent.parent / "tools"
if str(tools_dir) not in sys.path:
    sys.path.insert(0, str(tools_dir))

from rclone_installer import (
    find_rclone,
    is_winfsp_installed,
    _is_working_rclone,
    ensure_rclone,
)


class TestRcloneInstaller(unittest.TestCase):
    def test_find_rclone(self):
        rclone_path = find_rclone()
        if rclone_path:
            self.assertTrue(os.path.isfile(rclone_path))
            self.assertTrue(_is_working_rclone(rclone_path))

    def test_is_working_rclone_invalid(self):
        self.assertFalse(_is_working_rclone(""))
        self.assertFalse(_is_working_rclone("non_existent_file_path_12345.exe"))

    def test_ensure_rclone_returns_valid_path(self):
        rclone_path = ensure_rclone(ensure_mount_prereqs=False)
        self.assertTrue(os.path.isfile(rclone_path))
        self.assertTrue(_is_working_rclone(rclone_path))


if __name__ == "__main__":
    unittest.main()
