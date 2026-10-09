import unittest
from unittest.mock import Mock, patch
from buffering import buffer_plan


class BufferingTests(unittest.TestCase):
    def plan(self, memory, disk, count=500):
        with patch('buffering.psutil.virtual_memory', return_value=Mock(available=memory)), patch(
                'buffering.shutil.disk_usage', return_value=Mock(free=disk)):
            return buffer_plan(8, count, 10 * 1024**2)

    def test_large_machine_caps_disk_buffer_at_one_gib(self):
        plan = self.plan(16 * 1024**3, 100 * 1024**3)
        self.assertEqual(plan.workers, 8)
        self.assertEqual(plan.slots, 102)

    def test_low_memory_reduces_download_concurrency(self):
        plan = self.plan(512 * 1024**2, 10 * 1024**3)
        self.assertEqual(plan.workers, 1)

    def test_low_disk_reduces_prefetch_window(self):
        plan = self.plan(16 * 1024**3, 800 * 1024**2)
        self.assertEqual(plan.slots, 2)
        self.assertEqual(plan.workers, 2)

    def test_insufficient_resources_fail_before_transfer(self):
        with self.assertRaises(RuntimeError):
            self.plan(256 * 1024**2, 10 * 1024**3)
        with self.assertRaises(RuntimeError):
            self.plan(16 * 1024**3, 512 * 1024**2)
