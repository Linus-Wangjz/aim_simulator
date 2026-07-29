"""Regression tests for trace-based DRAM command-lifetime accounting."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from cellar_power_calculator import trace_based_activity_cycles


class TraceBasedActivityCyclesTest(unittest.TestCase):
    def activity_cycles(self, dram_impl: str, content: str, end_cycle: int, nras: int, nrc: int):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "commands"
            Path(f"{prefix}.ch0").write_text(content)
            return trace_based_activity_cycles(prefix, dram_impl, end_cycle, {"nRAS": nras, "nRC": nrc})

    def test_all_bank_act_runs_until_prea(self):
        cycles = self.activity_cycles(
            "LPDDR4X",
            "10, ACT8-2, 0, 0, -1, -1, 0, 0\n"
            "20, MAC8, 0, 0, -1, -1, 0, 0\n"
            "70, PREA, 0, 0, -1, -1, 0, 0\n",
            100,
            nras=12,
            nrc=20,
        )

        # Dynamic ACT/PRE energy uses the fixed IDD0 timing window, while the
        # ACT -> PREA lifetime contributes only to active standby energy.
        self.assertEqual(cycles["ACT_cycles"], 8 * 12)
        self.assertEqual(cycles["PRE_cycles"], 8 * (20 - 12))
        self.assertEqual(cycles["ACT_STBY_cycles"], 70 - 10)

    def test_bank_group_act_runs_until_pre4(self):
        cycles = self.activity_cycles(
            "GDDR6",
            "10, ACT4, 0, 1, -1, 0, 0\n"
            "20, MAC, 0, 1, 0, 0, 0\n"
            "50, PRE4, 0, 1, -1, 0, 0\n",
            60,
            nras=7,
            nrc=18,
        )

        # GDDR6 preserves its existing interval-based behavior because its
        # configured power table has no IDD0/IDD2N rails for the split.
        self.assertEqual(cycles["ACT_cycles"], 4 * (20 - 10))
        self.assertEqual(cycles["PRE_cycles"], 4 * (60 - 50))

    def test_lpddr_act_energy_does_not_expand_without_pre(self):
        cycles = self.activity_cycles(
            "LPDDR4X",
            "10, ACT8-2, 0, 0, -1, -1, 0, 0\n"
            "20, MAC8, 0, 0, -1, -1, 0, 0\n",
            40,
            nras=9,
            nrc=24,
        )

        self.assertEqual(cycles["ACT_cycles"], 8 * 9)
        self.assertEqual(cycles["PRE_cycles"], 0)


if __name__ == "__main__":
    unittest.main()
