"""Workloads shared by GEMV analysis scripts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GemvWorkload:
    size: int
    name: str
    gddr6_trace: str
    lpddr4_trace: str

    def trace_for(self, dram: str) -> str:
        if dram == "gddr6":
            return self.gddr6_trace
        if dram == "lpddr4":
            return self.lpddr4_trace
        raise ValueError(f"Unsupported GEMV DRAM configuration: {dram}")


GEMV_WORKLOADS = (
    GemvWorkload(256, "gemv_256x256", "test/gemv_256x256.trace", "test/gemv_256x256.trace"),
    GemvWorkload(512, "gemv_512x512", "test/gemv_512x512_gddr6.trace", "test/gemv_512x512_lpddr4.trace"),
    GemvWorkload(1024, "gemv_1024x1024", "test/gemv_1024x1024_gddr6.trace", "test/gemv_1024x1024_lpddr4.trace"),
    GemvWorkload(
        2048,
        "gemv_2048x2048_gpr_psum",
        "test/gemv_2048x2048_gpr_psum_gddr6.trace",
        "test/gemv_2048x2048_gpr_psum_lpddr4.trace",
    ),
)
