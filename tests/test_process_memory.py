import os
import json
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import Mock, patch

from app.services.process_memory import (
    ProcessMemory, current_process_memory, memory_limit_mb, memory_status,
    process_memory_for_pid,
)


class ProcessMemoryTests(unittest.TestCase):
    def test_memory_status_uses_private_usage_first(self):
        self.assertEqual(memory_status(ProcessMemory(100, 2050), 2048, 4096), "warning")
        self.assertEqual(memory_status(ProcessMemory(100, 4096), 2048, 4096), "backfill_stopped")

    def test_invalid_memory_limit_uses_default(self):
        with patch.dict(os.environ, {"TEST_MEMORY_LIMIT": "invalid"}):
            self.assertEqual(memory_limit_mb("TEST_MEMORY_LIMIT", 2048), 2048)

    @unittest.skipUnless(os.name == "nt", "Windows process counters are required")
    def test_current_and_pid_measurements_report_same_process(self):
        own = current_process_memory()
        by_pid = process_memory_for_pid(os.getpid())
        for field in ("private_usage_mb", "working_set_mb"):
            first, second = getattr(own, field), getattr(by_pid, field)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertGreater(first, 0)
            self.assertGreater(second, 0)
            # Memory can change between the two operating-system reads.
            self.assertLess(abs(first - second), 16)

    @unittest.skipUnless(os.name == "nt", "Windows process counters are required")
    def test_worker_handle_is_closed_when_memory_query_fails(self):
        kernel, psapi = Mock(), Mock()
        kernel.OpenProcess.return_value = 123
        psapi.GetProcessMemoryInfo.return_value = 0
        with patch("app.services.process_memory.ctypes.WinDLL", side_effect=[kernel, psapi]):
            self.assertEqual(process_memory_for_pid(os.getpid()), ProcessMemory(None, None))
        kernel.CloseHandle.assert_called_once_with(123)

    @unittest.skipUnless(os.name == "nt", "Windows process counters are required")
    def test_repeated_measurements_do_not_accumulate_pointer_types(self):
        # A fresh process excludes other tests' threads and allocator history.
        code = textwrap.dedent('''
            import ctypes, gc, json, os
            from app.services.process_memory import current_process_memory, process_memory_for_pid
            for _ in range(20):
                current_process_memory()
                process_memory_for_pid(os.getpid())
            gc.collect()
            before_types = len(ctypes._pointer_type_cache)
            before_memory = current_process_memory().private_usage_mb
            for _ in range(2000):
                current_process_memory()
                process_memory_for_pid(os.getpid())
            gc.collect()
            after_memory = current_process_memory().private_usage_mb
            print(json.dumps(dict(before_types=before_types,
                                  after_types=len(ctypes._pointer_type_cache),
                                  before_memory=before_memory, after_memory=after_memory)))
        ''')
        result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                                text=True, encoding="utf-8", timeout=30, check=True)
        measurements = json.loads(result.stdout)
        self.assertEqual(measurements["after_types"], measurements["before_types"])
        self.assertIsNotNone(measurements["before_memory"])
        self.assertIsNotNone(measurements["after_memory"])
        self.assertLess(measurements["after_memory"] - measurements["before_memory"], 8)


if __name__ == "__main__":
    unittest.main()
