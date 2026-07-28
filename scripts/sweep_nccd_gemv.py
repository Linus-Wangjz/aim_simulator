#!/usr/bin/env python3
"""Sweep LPDDR4 nCCD for GEMV traces and plot LPDDR4/GDDR6 cycle ratios."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from aim_analysis.ramulator import (
    GemvRamulatorRunner,
    RamulatorArtifactStore,
    channel_active_cycles,
    display_path,
    parse_nccd_values,
    read_result_stats,
    repo_root,
    result_cycles,
)
from aim_analysis.runtime import configure_matplotlib_cache
from aim_analysis.workloads import GEMV_WORKLOADS


METRIC_LABELS = {
    "channel_active": "max CHx_active_cycles",
    "memory_system": "memory_system_cycles",
}


def ensure_matplotlib_available() -> None:
    configure_matplotlib_cache()
    try:
        import matplotlib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "matplotlib is not installed. Install it, or rerun with --no-plot "
            "to only generate the CSV."
        ) from exc


def parse_result(path: Path) -> dict[str, int]:
    stats = read_result_stats(path)
    active = channel_active_cycles(stats)
    return {
        "active_max": max(active),
        "active_sum": sum(active),
        "memory_system_cycles": result_cycles(stats),
    }


def selected_metric(stats: dict[str, int], metric: str) -> int:
    if metric == "channel_active":
        return stats["active_max"]
    if metric == "memory_system":
        return stats["memory_system_cycles"]
    raise ValueError(f"Unknown metric: {metric}")


def validate_override_effect(rows: list[dict[str, str]], metric_label: str) -> None:
    by_workload: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_workload.setdefault(row["workload"], []).append(row)

    for workload, workload_rows in by_workload.items():
        ordered = sorted(workload_rows, key=lambda row: int(row["lpddr4_nccd"]))
        metric_values = [int(row["lpddr4_metric_cycles"]) for row in ordered]
        nccd_values = [int(row["lpddr4_nccd"]) for row in ordered]
        if any(second <= first for first, second in zip(metric_values, metric_values[1:])):
            raise RuntimeError(
                f"nCCD override did not produce strictly increasing {metric_label} "
                f"for {workload}: {list(zip(nccd_values, metric_values))}"
            )
        print(f"[check] nCCD override affects {workload} {metric_label}: {list(zip(nccd_values, metric_values))}")


def write_plot(rows: list[dict[str, str]], out_path: Path, metric_label: str) -> None:
    configure_matplotlib_cache()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    by_workload: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_workload.setdefault(row["workload"], []).append(row)

    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    for workload, workload_rows in by_workload.items():
        ordered = sorted(workload_rows, key=lambda row: float(row["nccd_ratio"]))
        ax.plot(
            [float(row["nccd_ratio"]) for row in ordered],
            [float(row["metric_ratio"]) for row in ordered],
            marker="o",
            linewidth=2.0,
            label=workload,
        )

    ax.set_title(f"LPDDR4 nCCD Sweep on GEMV ({metric_label})")
    ax.set_xlabel("nCCD ratio = LPDDR4 nCCD / GDDR6 nCCD")
    ax.set_ylabel(f"{metric_label} ratio = LPDDR4 / GDDR6")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=200)
    plt.close(fig)


def main() -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ramulator", type=Path, default=root / "build/ramulator2")
    parser.add_argument("--lpddr4-yaml", type=Path, default=root / "test/example_LPDDR4.yaml")
    parser.add_argument("--gddr6-yaml", type=Path, default=root / "test/example_GDDR6.yaml")
    parser.add_argument("--output-dir", type=Path, default=root / "output/nccd_sweep_gemv")
    parser.add_argument(
        "--ramulator-output-dir",
        type=Path,
        default=root / "output/ramulator",
        help="Shared raw Ramulator artifact cache (results, resolved timing, and command traces).",
    )
    parser.add_argument("--nccd-values", default="2,4,6,8")
    parser.add_argument("--gddr6-nccd", type=int, default=2)
    parser.add_argument(
        "--metric",
        choices=sorted(METRIC_LABELS),
        default="channel_active",
        help="Cycle metric for ratio/plot.",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Reuse a complete shared raw artifact instead of rerunning Ramulator.",
    )
    parser.add_argument("--no-plot", action="store_true", help="Skip matplotlib plot generation.")
    args = parser.parse_args()

    nccd_values = parse_nccd_values(args.nccd_values)
    if not args.no_plot:
        ensure_matplotlib_available()
    metric_label = METRIC_LABELS[args.metric]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runner = GemvRamulatorRunner(
        root,
        args.ramulator,
        args.gddr6_yaml,
        args.lpddr4_yaml,
        RamulatorArtifactStore(args.ramulator_output_dir),
        args.reuse_existing,
    )

    rows: list[dict[str, str]] = []
    for workload in GEMV_WORKLOADS:
        gddr6_artifacts = runner.ensure(workload, "gddr6")
        gddr6_stats = parse_result(gddr6_artifacts.result)
        gddr6_metric_cycles = selected_metric(gddr6_stats, args.metric)

        for nccd in nccd_values:
            lpddr4_artifacts = runner.ensure(workload, "lpddr4", nccd)
            lpddr4_stats = parse_result(lpddr4_artifacts.result)
            lpddr4_metric_cycles = selected_metric(lpddr4_stats, args.metric)
            rows.append({
                "workload": workload.name,
                "metric": args.metric,
                "lpddr4_nccd": str(nccd),
                "gddr6_nccd": str(args.gddr6_nccd),
                "nccd_ratio": f"{nccd / args.gddr6_nccd:.6g}",
                "lpddr4_metric_cycles": str(lpddr4_metric_cycles),
                "gddr6_metric_cycles": str(gddr6_metric_cycles),
                "metric_ratio": f"{lpddr4_metric_cycles / gddr6_metric_cycles:.6g}",
                "lpddr4_active_max": str(lpddr4_stats["active_max"]),
                "gddr6_active_max": str(gddr6_stats["active_max"]),
                "lpddr4_memory_system_cycles": str(lpddr4_stats["memory_system_cycles"]),
                "gddr6_memory_system_cycles": str(gddr6_stats["memory_system_cycles"]),
                "lpddr4_output": display_path(lpddr4_artifacts.result, root),
                "gddr6_output": display_path(gddr6_artifacts.result, root),
            })

    validate_override_effect(rows, metric_label)
    csv_path = args.output_dir / "nccd_sweep_gemv.csv"
    with csv_path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"[csv] {display_path(csv_path, root)}")

    if not args.no_plot:
        plot_path = args.output_dir / "nccd_sweep_gemv.png"
        write_plot(rows, plot_path, metric_label)
        print(f"[plot] {display_path(plot_path, root)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
