#!/usr/bin/env python3
"""Sweep LPDDR4 nCCD for GEMV traces and plot LPDDR4/GDDR6 cycle ratio."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import sys
import tempfile
from pathlib import Path


WORKLOADS = (
    {
        "name": "gemv_256x256",
        "lpddr4_trace": "test/gemv_256x256.trace",
        "gddr6_trace": "test/gemv_256x256.trace",
    },
    {
        "name": "gemv_512x512",
        "lpddr4_trace": "test/gemv_512x512_lpddr4.trace",
        "gddr6_trace": "test/gemv_512x512_gddr6.trace",
    },
    {
        "name": "gemv_1024x1024",
        "lpddr4_trace": "test/gemv_1024x1024_lpddr4.trace",
        "gddr6_trace": "test/gemv_1024x1024_gddr6.trace",
    },
    # {
    #     "name": "gemv_2048x2048_accum",
    #     "lpddr4_trace": "test/gemv_2048x2048_accum_lpddr4.trace",
    #     "gddr6_trace": "test/gemv_2048x2048_accum_gddr6.trace",
    # },
    {
        "name": "gemv_2048x2048_gpr_psum",
        "lpddr4_trace": "test/gemv_2048x2048_gpr_psum_lpddr4.trace",
        "gddr6_trace": "test/gemv_2048x2048_gpr_psum_gddr6.trace",
    },
)

ACTIVE_RE = re.compile(r"CH(\d+)_active_cycles:\s+(\d+)")
MEM_CYCLES_RE = re.compile(r"memory_system_cycles:\s+(\d+)")
NCCD_RE = re.compile(r"^\s*nCCD:\s*(\d+)\s*$", re.MULTILINE)

METRIC_LABELS = {
    "channel_active": "max CHx_active_cycles",
    "memory_system": "memory_system_cycles",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def parse_nccd_values(raw: str) -> list[int]:
    values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not values:
        raise ValueError("--nccd-values must contain at least one integer")
    if values != sorted(set(values)):
        raise ValueError("--nccd-values must be unique and sorted ascending")
    return values


def ensure_matplotlib_available() -> None:
    try:
        import matplotlib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "matplotlib is not installed. Install it, or rerun with --no-plot "
            "to only generate CSV/raw simulation outputs."
        ) from exc


def write_lpddr4_yaml_with_nccd(base_yaml: Path, nccd: int, dst: Path) -> None:
    lines = base_yaml.read_text().splitlines(keepends=True)
    output = []
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("nCCD:"):
            continue
        output.append(line)
        if not inserted and "preset:" in stripped and "LPDDR4_AiM_timing" in stripped:
            indent = re.match(r"^(\s*)", line).group(1)
            output.append(f"{indent}nCCD: {nccd}\n")
            inserted = True
    if not inserted:
        raise RuntimeError(f"Could not find LPDDR4_AiM_timing preset in {base_yaml}")
    dst.write_text("".join(output))

    match = NCCD_RE.search(dst.read_text())
    if not match or int(match.group(1)) != nccd:
        raise RuntimeError(f"nCCD override verification failed for {dst}")


def run_ramulator(ramulator: Path, config: Path, trace: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ramulator), "-f", str(config), "-t", str(trace)]
    with output.open("w") as fh:
        proc = subprocess.run(cmd, cwd=repo_root(), stdout=fh, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\nSee {output}")


def parse_result(path: Path) -> dict[str, int]:
    text = path.read_text()
    active = [int(match.group(2)) for match in ACTIVE_RE.finditer(text)]
    if not active:
        raise RuntimeError(f"No CH*_active_cycles entries found in {path}")
    mem_match = MEM_CYCLES_RE.search(text)
    if not mem_match:
        raise RuntimeError(f"No memory_system_cycles entry found in {path}")
    return {
        "active_max": max(active),
        "active_sum": sum(active),
        "memory_system_cycles": int(mem_match.group(1)),
    }


def selected_metric(stats: dict[str, int], metric: str) -> int:
    if metric == "channel_active":
        return stats["active_max"]
    if metric == "memory_system":
        return stats["memory_system_cycles"]
    raise ValueError(f"unknown metric: {metric}")


def validate_override_effect(rows: list[dict[str, str]], metric_label: str) -> None:
    by_workload: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_workload.setdefault(row["workload"], []).append(row)

    for workload, workload_rows in by_workload.items():
        ordered = sorted(workload_rows, key=lambda row: int(row["lpddr4_nccd"]))
        metric_values = [int(row["lpddr4_metric_cycles"]) for row in ordered]
        nccd_values = [int(row["lpddr4_nccd"]) for row in ordered]
        if any(b <= a for a, b in zip(metric_values, metric_values[1:])):
            raise RuntimeError(
                f"nCCD override did not produce strictly increasing {metric_label} "
                f"for {workload}: {list(zip(nccd_values, metric_values))}"
            )
        print(f"[check] nCCD override affects {workload} {metric_label}: {list(zip(nccd_values, metric_values))}")


def write_plot(rows: list[dict[str, str]], out_path: Path, metric_label: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    by_workload: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_workload.setdefault(row["workload"], []).append(row)

    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    for workload, workload_rows in by_workload.items():
        ordered = sorted(workload_rows, key=lambda row: float(row["nccd_ratio"]))
        xs = [float(row["nccd_ratio"]) for row in ordered]
        ys = [float(row["metric_ratio"]) for row in ordered]
        ax.plot(xs, ys, marker="o", linewidth=2.0, label=workload)

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
    parser.add_argument("--nccd-values", default="2,4,6,8")
    parser.add_argument("--gddr6-nccd", type=int, default=2)
    parser.add_argument(
        "--metric",
        choices=sorted(METRIC_LABELS),
        default="channel_active",
        help="Cycle metric for ratio/plot: channel_active uses max CHx_active_cycles; memory_system uses memory_system_cycles.",
    )
    parser.add_argument("--no-plot", action="store_true", help="Skip matplotlib plot generation.")
    args = parser.parse_args()
    metric_label = METRIC_LABELS[args.metric]

    nccd_values = parse_nccd_values(args.nccd_values)
    if not args.no_plot:
        ensure_matplotlib_available()
    if not args.ramulator.exists():
        raise FileNotFoundError(f"ramulator binary not found: {args.ramulator}")

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="lpddr4_nccd_", dir=root / "output") as tmp:
        tmp_dir = Path(tmp)
        for workload in WORKLOADS:
            name = workload["name"]
            lpddr4_trace = root / workload["lpddr4_trace"]
            gddr6_trace = root / workload["gddr6_trace"]

            gddr6_out = out_dir / f"output_{name}_gddr6.result"
            print(f"[GDDR6] {name}: {gddr6_trace}")
            run_ramulator(args.ramulator, args.gddr6_yaml, gddr6_trace, gddr6_out)
            gddr6_stats = parse_result(gddr6_out)
            gddr6_metric_cycles = selected_metric(gddr6_stats, args.metric)

            for nccd in nccd_values:
                lpddr4_yaml = tmp_dir / f"example_LPDDR4_nCCD{nccd}.yaml"
                write_lpddr4_yaml_with_nccd(args.lpddr4_yaml, nccd, lpddr4_yaml)
                lpddr4_out = out_dir / f"output_{name}_lpddr4_nCCD{nccd}.result"
                print(f"[LPDDR4] {name}: nCCD={nccd}, {lpddr4_trace}")
                run_ramulator(args.ramulator, lpddr4_yaml, lpddr4_trace, lpddr4_out)
                lpddr4_stats = parse_result(lpddr4_out)
                lpddr4_metric_cycles = selected_metric(lpddr4_stats, args.metric)

                rows.append({
                    "workload": name,
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
                    "lpddr4_output": display_path(lpddr4_out, root),
                    "gddr6_output": display_path(gddr6_out, root),
                })

    validate_override_effect(rows, metric_label)

    csv_path = out_dir / "nccd_sweep_gemv.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {csv_path}")
    if not args.no_plot:
        plot_path = out_dir / "nccd_sweep_gemv.png"
        write_plot(rows, plot_path, metric_label)
        print(f"Wrote {plot_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
