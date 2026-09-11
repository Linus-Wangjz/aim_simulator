"""DRAM power models shared by Cellar and GEMV analysis scripts."""

from __future__ import annotations


PIM_TCCD_TIMING_KEY = {
    "GDDR6": "nCCDL",
    "LPDDR4": "nCCD",
    "LPDDR4X": "nCCD",
}
REQUIRED_TIMING_KEYS = ("tCK_ps", "nRAS", "nRC", "nBL")
PIM_POWER_SCALE = {"GDDR6": 3.00, "LPDDR4": 1.50, "LPDDR4X": 1.50}

GDDR6_DRAM_POWER = {
    "ACT_STBY": 527.5 / 2.00,
    "PRE_STBY": 366.3 / 2.00,
    "ACT": 132.6 / 2.00,
    "PRE": 132.6 / 2.00,
    "WR": 1106.3 / 2.00,
    "RD": 876.3 / 2.00,
}

LPDDR4_WINBOND_IDD = {
    "VDD": [1.8, 1.1, 1.1],
    "IDD0": [9.0, 53.0, 0.1],
    "IDD2N": [0.6, 31.0, 0.1],
    "IDD3N": [2.0, 34.5, 0.1],
    "IDD4R": [2.5, 287.0, 105.0],
    "IDD4W": [2.0, 265.0, 0.3],
}

LPDDR4X_WINBOND_IDD = {
    "VDD": [1.8, 1.1, 0.6],
    "IDD0": [9.0, 53.0, 0.1],
    "IDD2N": [0.6, 31.0, 0.1],
    "IDD3N": [2.0, 34.5, 0.1],
    "IDD4R": [2.5, 287.0, 85.0],
    "IDD4W": [2.0, 265.0, 0.3],
}

LPDDR4_MICRON_IDD = {
    "VDD": [1.8, 1.1, 1.1],
    "IDD0": [4.9, 32.0, 0.75],
    "IDD2N": [2.0, 16.0, 0.75],
    "IDD3N": [2.5, 20.0, 0.75],
    "IDD4R": [4.5, 270.0, 79.62],
    "IDD4W": [3.0, 210.0, 0.75],
}

LPDDR4X_MICRON_IDD = {
    "VDD": [1.8, 1.1, 0.6],
    "IDD0": [4.9, 32.0, 0.75],
    "IDD2N": [2.0, 16.0, 0.75],
    "IDD3N": [2.5, 20.0, 0.75],
    "IDD4R": [4.5, 270.0, 61.57],
    "IDD4W": [3.0, 210.0, 0.75],
}


def power_from_idd(idd: dict[str, list[float]]) -> dict[str, float]:
    vdd = idd["VDD"]
    required_currents = ("IDD0", "IDD2N", "IDD3N", "IDD4W", "IDD4R")
    invalid = [name for name in required_currents if len(idd[name]) != len(vdd)]
    if invalid:
        raise ValueError(f"IDD rail count does not match VDD for: {', '.join(invalid)}")

    def rail_sum(high: str, low: str | None = None) -> float:
        if low is None:
            return sum(voltage * current for voltage, current in zip(vdd, idd[high]))
        return sum(
            voltage * (high_current - low_current)
            for voltage, high_current, low_current in zip(vdd, idd[high], idd[low])
        )

    return {
        "ACT_STBY": rail_sum("IDD3N"),
        "PRE_STBY": rail_sum("IDD2N"),
        "ACT": rail_sum("IDD0", "IDD3N"),
        "PRE": rail_sum("IDD0", "IDD2N"),
        "RD": rail_sum("IDD4R", "IDD3N"),
        "WR": rail_sum("IDD4W", "IDD3N"),
    }


DRAM_POWER_BY_IMPL = {
    "GDDR6": GDDR6_DRAM_POWER,
    "LPDDR4": power_from_idd(LPDDR4_WINBOND_IDD),
    "LPDDR4X": power_from_idd(LPDDR4X_WINBOND_IDD),
    "LPDDR4_MICRON": power_from_idd(LPDDR4_MICRON_IDD),
    "LPDDR4X_MICRON": power_from_idd(LPDDR4X_MICRON_IDD),
}


def dram_power_for_impl(dram_impl: str) -> dict[str, float]:
    try:
        return DRAM_POWER_BY_IMPL[dram_impl]
    except KeyError as error:
        raise ValueError(f"Unknown DRAM impl '{dram_impl}'. Expected one of: {', '.join(DRAM_POWER_BY_IMPL)}") from error
