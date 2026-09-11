"""Regression checks for selectable DRAM IDD power tables."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from aim_analysis.models import dram_power_for_impl  # noqa: E402


class DRAMPowerModelTest(unittest.TestCase):
    def test_lpddr4x_micron_table_is_selectable_and_distinct(self):
        winbond = dram_power_for_impl("LPDDR4X")
        micron = dram_power_for_impl("LPDDR4X_MICRON")

        self.assertEqual(set(micron), set(winbond))
        self.assertTrue(all(value > 0.0 for value in micron.values()))
        self.assertNotEqual(micron, winbond)


if __name__ == "__main__":
    unittest.main()
