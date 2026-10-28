"""Deterministic real-pressure policy checks; never change OS memory pressure."""
import unittest

from guard_mac_build import GIB, hold_reason


class PressurePolicyTests(unittest.TestCase):
    def test_normal_does_not_impose_an_rss_or_free_ram_budget(self):
        self.assertIsNone(hold_reason(dict(memory_pressure_level=1, disk_free_bytes=6 * GIB,
                                          group_rss_bytes=20 * GIB), 5 * GIB))

    def test_os_warning_and_critical_are_distinct(self):
        base = dict(disk_free_bytes=6 * GIB, group_rss_bytes=0)
        self.assertEqual(hold_reason({**base, "memory_pressure_level": 2}, 5 * GIB), "memory-pressure-warning")
        self.assertEqual(hold_reason({**base, "memory_pressure_level": 4}, 5 * GIB), "memory-pressure-critical")

    def test_disk_near_exhaustion_stops_even_without_memory_pressure(self):
        self.assertEqual(hold_reason(dict(memory_pressure_level=1, disk_free_bytes=4 * GIB,
                                          group_rss_bytes=0), 5 * GIB), "disk-reserve-low")


if __name__ == "__main__":
    unittest.main()
