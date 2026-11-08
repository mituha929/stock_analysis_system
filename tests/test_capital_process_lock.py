import tempfile
import unittest
from pathlib import Path

from app.services.capital_process_lock import CapitalProcessLock


class CapitalProcessLockTests(unittest.TestCase):
    def test_can_be_reentered_and_is_released_after_last_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = CapitalProcessLock(Path(directory) / "skcom.lock")
            lock.acquire(timeout_seconds=0)
            lock.acquire(timeout_seconds=0)
            lock.release()
            self.assertEqual(lock._depth, 1)
            lock.release()
            self.assertEqual(lock._depth, 0)


if __name__ == "__main__":
    unittest.main()
