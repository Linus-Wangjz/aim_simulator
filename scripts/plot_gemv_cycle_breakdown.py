#!/usr/bin/env python3
"""Plot stacked GEMV cycle breakdowns from actual issued DRAM commands."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from aim_analysis.commands import (
    CONSTRAINT_LABELS,
    STACK_LABELS,
    TRANSITION_CONSTRAINT_MAP,
    command_component,
    transition_constraint,
)
from aim_analysis.ramulator import (
    GemvRamulatorRunner,
    RamulatorArtifactStore,
    command_trace_files,
    display_path,
    dram_label,
    dram_name,
    dram_sort_key,
    parse_command_trace,
    parse_nccd_values,
    read_result_stats,
    repo_root,
    result_cycles,
)
from aim_analysis.runtime import configure_matplotlib_cache
from aim_analysis.workloads import GEMV_WORKLOADS

def ensure_matplotlib_available() -> None:
    configure_matplotlib_cache()
    try:
        import matplotlib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError("matplotlib is not installed. Rerun with --no-plot to only generate CSV/results.") from exc


def select_critical_command_trace(trace_prefix: Path) -> Path:
    candidates: list[tuple[int, int, Path]] = []
    for path in command_trace_files(trace_prefix):
        try:
            issued = parse_command_trace(path)
        except ValueError:
            continue
        candidates.append((issued[-1].clock, len(issued), path))

    if not candidates:
        raise RuntimeError(f"No issued commands found for command trace prefix {trace_prefix}")
    return max(candidates)[2]


def parse_result(result_path: Path, command_trace_path: Path) -> tuple[dict[str, int], list[dict[str, str]]]:
    stats = {"memory_system_cycles": result_cycles(read_result_stats(result_path))}
    for label in STACK_LABELS:
        stats[label] = 0

    issued = parse_command_trace(command_trace_path)
    stats["issued_commands"] = len(issued)
    transitions: dict[str, dict[str, str | int]] = {}
    for previous, current in zip(issued, issued[1:]):
        delta = current.clock - previous.clock
        if delta < 0:
            raise RuntimeError(f"Command trace is not monotonic: {command_trace_path}")
        stats[command_component(current.command)] += delta

        transition = f"{previous.command}->{current.command}"
        item = transitions.setdefault(transition, {"transition": transition, "count": 0, "cycles": 0})
        item["count"] = int(item["count"]) + 1
        item["cycles"] = int(item["cycles"]) + delta

    component_sum = sum(stats[label] for label in STACK_LABELS)
    stats["Other"] += max(stats["memory_system_cycles"] - component_sum, 0)
    stats["component_sum"] = sum(stats[label] for label in STACK_LABELS)
    transition_rows = [
        {
            "transition": str(item["transition"]),
            "constraint": transition_constraint(
                str(item["transition"]).split("->", 1)[0],
                str(item["transition"]).split("->", 1)[1],
            ),
            "count": str(item["count"]),
            "cycles": str(item["cycles"]),
        }
        for item in transitions.values()
    ]
    return stats, transition_rows


def build_rows(args: argparse.Namespace) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    root = repo_root()
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
    transition_rows: list[dict[str, str]] = []

    for workload in GEMV_WORKLOADS:
        runs: list[tuple[str, int | None]] = [("gddr6", None)]
        runs.extend(("lpddr4", nccd) for nccd in args.lpddr4_nccd_values)

        for dram, nccd in runs:
            artifacts = runner.ensure(workload, dram, nccd)
            command_trace = select_critical_command_trace(artifacts.command_trace_prefix)
            stats, transitions = parse_result(artifacts.result, command_trace)
            memory_name = dram_name(dram, nccd)
            row = {
                "size": str(workload.size),
                "workload": workload.name,
                "dram": memory_name,
                "lpddr4_nccd": "" if nccd is None else str(nccd),
                "memory_system_cycles": str(stats["memory_system_cycles"]),
                "issued_commands": str(stats["issued_commands"]),
                "component_sum": str(stats["component_sum"]),
                "Other": str(stats["Other"]),
                "output": display_path(artifacts.result, root),
                "command_trace": display_path(command_trace, root),
                "trace_channel": command_trace.name.rsplit(".ch", 1)[-1],
            }
            for label in STACK_LABELS:
                row[label] = str(stats[label])
            rows.append(row)

            for transition in transitions:
                transition_rows.append(
                    {
                        "size": str(workload.size),
                        "workload": workload.name,
                        "dram": memory_name,
                        "lpddr4_nccd": "" if nccd is None else str(nccd),
                        "trace_channel": command_trace.name.rsplit(".ch", 1)[-1],
                        **transition,
                    }
                )

    return rows, transition_rows


def write_csv(rows: list[dict[str, str]], csv_path: Path) -> None:
    fieldnames = [
        "size",
        "workload",
        "dram",
        "lpddr4_nccd",
        "memory_system_cycles",
        "issued_commands",
        "WR_GB",
        "WR_BIAS",
        "ACT_PRE",
        "MAC_ABK",
        "RD_MAC",
        "TMOD",
        "Other",
        "component_sum",
        "output",
        "command_trace",
        "trace_channel",
    ]
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_transition_csv(rows: list[dict[str, str]], csv_path: Path) -> None:
    fieldnames = [
        "size",
        "workload",
        "dram",
        "lpddr4_nccd",
        "trace_channel",
        "transition",
        "constraint",
        "count",
        "cycles",
    ]
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_constraint_rows(rows: list[dict[str, str]], transition_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    row_by_key = {
        (row["size"], row["workload"], row["dram"]): row
        for row in rows
    }
    accum: dict[tuple[str, str, str], dict[str, int]] = {
        key: {label: 0 for label in CONSTRAINT_LABELS}
        for key in row_by_key
    }

    for transition in transition_rows:
        key = (transition["size"], transition["workload"], transition["dram"])
        label = transition["constraint"]
        if label not in CONSTRAINT_LABELS:
            label = "Other"
        accum[key][label] += int(transition["cycles"])

    constraint_rows: list[dict[str, str]] = []
    for key in sorted(row_by_key, key=lambda item: (int(item[0]), dram_sort_key(item[2]))):
        base = row_by_key[key]
        values = accum[key]
        transition_sum = sum(values[label] for label in CONSTRAINT_LABELS)
        values["Other"] += max(int(base["memory_system_cycles"]) - transition_sum, 0)
        component_sum = sum(values[label] for label in CONSTRAINT_LABELS)
        row = {
            "size": base["size"],
            "workload": base["workload"],
            "dram": base["dram"],
            "lpddr4_nccd": base["lpddr4_nccd"],
            "memory_system_cycles": base["memory_system_cycles"],
            "component_sum": str(component_sum),
        }
        for label in CONSTRAINT_LABELS:
            row[label] = str(values[label])
        constraint_rows.append(row)

    return constraint_rows


def write_constraint_csv(rows: list[dict[str, str]], csv_path: Path) -> None:
    fieldnames = [
        "size",
        "workload",
        "dram",
        "lpddr4_nccd",
        "memory_system_cycles",
        *CONSTRAINT_LABELS,
        "component_sum",
    ]
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_constraint_map_csv(csv_path: Path) -> None:
    fieldnames = ["preceding", "following", "transition", "constraint"]
    rows = [
        {
            "preceding": preceding,
            "following": following,
            "transition": f"{preceding}->{following}",
            "constraint": constraint,
        }
        for (preceding, following), constraint in sorted(TRANSITION_CONSTRAINT_MAP.items())
    ]
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def plot_inputs(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[float], list[str], list[float], list[str]]:
    ordered = sorted(rows, key=lambda row: (int(row["size"]), dram_sort_key(row["dram"])))
    sizes = sorted({int(row["size"]) for row in ordered})
    drams = sorted({row["dram"] for row in ordered}, key=dram_sort_key)
    bar_spacing = 0.56
    group_stride = max(3.7, (len(drams) - 1) * bar_spacing + 1.8)
    size_to_base = {size: idx * group_stride for idx, size in enumerate(sizes)}
    dram_offsets = {
        dram: (idx - (len(drams) - 1) / 2) * bar_spacing
        for idx, dram in enumerate(drams)
    }
    x_positions: list[float] = []
    x_labels: list[str] = []
    for row in ordered:
        dram = row["dram"]
        offset = dram_offsets[dram]
        x_positions.append(size_to_base[int(row["size"])] + offset)
        x_labels.append(dram_label(dram))

    group_positions = [size_to_base[size] for size in sizes]
    group_labels = [str(size) for size in sizes]
    return ordered, x_positions, x_labels, group_positions, group_labels


def add_group_dividers(ax, group_positions: list[float]) -> None:
    for left_group, right_group in zip(group_positions, group_positions[1:]):
        ax.axvline((left_group + right_group) / 2, color="#D8D8D8", linewidth=0.8, zorder=0)


def style_grouped_axis(ax, x_positions: list[float], x_labels: list[str], group_positions: list[float], group_labels: list[str]) -> None:
    ax.set_xticks(x_positions)
    ax.set_xticklabels(x_labels)
    ax.tick_params(axis="x", length=0, pad=6)
    for group_x, group_label in zip(group_positions, group_labels):
        ax.text(
            group_x,
            -0.22,
            group_label,
            ha="center",
            va="top",
            transform=ax.get_xaxis_transform(),
            fontsize=10,
            fontweight="semibold",
        )
    add_group_dividers(ax, group_positions)
    ax.set_xlabel("matrix size")
    ax.xaxis.set_label_coords(0.5, -0.32)


def component_colors() -> dict[str, str]:
    return {
        "WR_GB": "#4C78A8",
        "WR_BIAS": "#F58518",
        "ACT_PRE": "#E45756",
        "MAC_ABK": "#54A24B",
        "RD_MAC": "#B279A2",
        "TMOD": "#72B7B2",
        "Other": "#BAB0AC",
    }


def draw_stacked_bars(ax, ordered: list[dict[str, str]], x_positions: list[float], labels: list[str], colors: dict[str, str], *, percent: bool) -> None:
    totals = [max(int(row["memory_system_cycles"]), 1) for row in ordered]
    bottoms = [0.0] * len(ordered)
    for label in labels:
        if percent:
            values = [100.0 * int(row[label]) / total for row, total in zip(ordered, totals)]
        else:
            values = [int(row[label]) for row in ordered]
        ax.bar(x_positions, values, bottom=bottoms, label=label, color=colors[label], width=0.46, edgecolor="white", linewidth=0.5)
        bottoms = [base + value for base, value in zip(bottoms, values)]


def style_panel(ax) -> None:
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def write_cycle_combined_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    configure_matplotlib_cache()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = component_colors()
    ordered, x_positions, x_labels, group_positions, group_labels = plot_inputs(rows)

    nccd_text = ",".join(str(value) for value in lpddr4_nccd_values)
    fig, (ax_abs, ax_pct) = plt.subplots(2, 1, figsize=(12.8, 8.7), sharex=True, gridspec_kw={"height_ratios": [1.35, 1.0]})

    draw_stacked_bars(ax_abs, ordered, x_positions, STACK_LABELS, colors, percent=False)
    ax_abs.set_ylabel("cycles")
    ax_abs.set_title("Absolute cycles", loc="left", fontsize=11, fontweight="semibold")
    add_group_dividers(ax_abs, group_positions)
    style_panel(ax_abs)

    draw_stacked_bars(ax_pct, ordered, x_positions, STACK_LABELS, colors, percent=True)
    ax_pct.set_ylabel("share of memory_system_cycles (%)")
    ax_pct.set_ylim(0, 100)
    ax_pct.set_title("Percentage", loc="left", fontsize=11, fontweight="semibold")
    style_grouped_axis(ax_pct, x_positions, x_labels, group_positions, group_labels)
    style_panel(ax_pct)

    handles, labels = ax_abs.get_legend_handles_labels()
    fig.suptitle(f"GEMV Cycle Breakdown from Issued Commands: GDDR6 vs LPDDR4 nCCD={nccd_text}", y=0.985)
    fig.legend(handles, labels, ncols=7, loc="upper center", bbox_to_anchor=(0.5, 0.945), frameon=False)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.18, top=0.88, hspace=0.20)
    fig.savefig(plot_path, dpi=200)
    plt.close(fig)



def constraint_colors() -> dict[str, str]:
    return {
        "nMODCH": "#4C78A8",
        "nCCD/nCCDS": "#54A24B",
        "nCCD/nBL": "#F58518",
        "nRCDRDMAC": "#E45756",
        "nRP/nRPab": "#72B7B2",
        "nRFCab": "#B279A2",
        "nRTW": "#FF9DA6",
        "CAS sync": "#9D755D",
        "ACT split": "#8CD17D",
        "Issue gap": "#D4A6C8",
        "Other": "#BAB0AC",
    }


def write_constraint_combined_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    configure_matplotlib_cache()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = constraint_colors()
    ordered, x_positions, x_labels, group_positions, group_labels = plot_inputs(rows)

    nccd_text = ",".join(str(value) for value in lpddr4_nccd_values)
    fig, (ax_abs, ax_pct) = plt.subplots(2, 1, figsize=(13.8, 9.4), sharex=True, gridspec_kw={"height_ratios": [1.35, 1.0]})

    draw_stacked_bars(ax_abs, ordered, x_positions, CONSTRAINT_LABELS, colors, percent=False)
    ax_abs.set_ylabel("cycles")
    ax_abs.set_title("Absolute cycles", loc="left", fontsize=11, fontweight="semibold")
    add_group_dividers(ax_abs, group_positions)
    style_panel(ax_abs)

    draw_stacked_bars(ax_pct, ordered, x_positions, CONSTRAINT_LABELS, colors, percent=True)
    ax_pct.set_ylabel("share of memory_system_cycles (%)")
    ax_pct.set_ylim(0, 100)
    ax_pct.set_title("Percentage", loc="left", fontsize=11, fontweight="semibold")
    style_grouped_axis(ax_pct, x_positions, x_labels, group_positions, group_labels)
    style_panel(ax_pct)

    handles, labels = ax_abs.get_legend_handles_labels()
    fig.suptitle(f"GEMV Timing-Constraint Attribution: GDDR6 vs LPDDR4 nCCD={nccd_text}", y=0.985)
    fig.legend(handles, labels, ncols=6, loc="upper center", bbox_to_anchor=(0.5, 0.945), frameon=False)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.18, top=0.83, hspace=0.20)
    fig.savefig(plot_path, dpi=200)
    plt.close(fig)


def write_constraint_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = constraint_colors()

    ordered, x_positions, x_labels, group_positions, group_labels = plot_inputs(rows)

    fig, ax = plt.subplots(figsize=(13.4, 6.1))
    bottoms = [0] * len(ordered)
    for label in CONSTRAINT_LABELS:
        values = [int(row[label]) for row in ordered]
        ax.bar(x_positions, values, bottom=bottoms, label=label, color=colors[label], width=0.46, edgecolor="white", linewidth=0.5)
        bottoms = [base + value for base, value in zip(bottoms, values)]

    nccd_text = ",".join(str(value) for value in lpddr4_nccd_values)
    ax.set_title(f"GEMV Timing-Constraint Attribution: GDDR6 vs LPDDR4 nCCD={nccd_text}")
    ax.set_ylabel("cycles")
    style_grouped_axis(ax, x_positions, x_labels, group_positions, group_labels)
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(ncols=6, loc="upper center", bbox_to_anchor=(0.5, 1.17), frameon=False)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.20, top=0.82)
    fig.savefig(plot_path, dpi=200)
    plt.close(fig)


def write_constraint_percent_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = constraint_colors()
    ordered, x_positions, x_labels, group_positions, group_labels = plot_inputs(rows)
    totals = [max(int(row["memory_system_cycles"]), 1) for row in ordered]

    fig, ax = plt.subplots(figsize=(13.4, 6.1))
    bottoms = [0.0] * len(ordered)
    for label in CONSTRAINT_LABELS:
        values = [100.0 * int(row[label]) / total for row, total in zip(ordered, totals)]
        ax.bar(x_positions, values, bottom=bottoms, label=label, color=colors[label], width=0.46, edgecolor="white", linewidth=0.5)
        bottoms = [base + value for base, value in zip(bottoms, values)]

    nccd_text = ",".join(str(value) for value in lpddr4_nccd_values)
    ax.set_title(f"GEMV Timing-Constraint Attribution Percent: GDDR6 vs LPDDR4 nCCD={nccd_text}")
    ax.set_ylabel("share of memory_system_cycles (%)")
    ax.set_ylim(0, 100)
    style_grouped_axis(ax, x_positions, x_labels, group_positions, group_labels)
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(ncols=6, loc="upper center", bbox_to_anchor=(0.5, 1.17), frameon=False)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.20, top=0.82)
    fig.savefig(plot_path, dpi=200)
    plt.close(fig)


def main() -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ramulator", type=Path, default=root / "build/ramulator2")
    parser.add_argument("--lpddr4-yaml", type=Path, default=root / "test/example_LPDDR4.yaml")
    parser.add_argument("--gddr6-yaml", type=Path, default=root / "test/example_GDDR6.yaml")
    parser.add_argument("--lpddr4-nccd-values", default="2,6", help="Comma-separated LPDDR4 nCCD values to sweep.")
    parser.add_argument("--lpddr4-nccd", type=int, dest="lpddr4_nccd_single", help="Run only one LPDDR4 nCCD value.")
    parser.add_argument("--output-dir", type=Path, default=root / "output/gemv_cycle_breakdown")
    parser.add_argument(
        "--ramulator-output-dir",
        type=Path,
        default=root / "output/ramulator",
        help="Shared raw Ramulator artifact cache (results, resolved timing, and command traces).",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Reuse a complete shared raw artifact instead of rerunning Ramulator.",
    )
    parser.add_argument("--no-plot", action="store_true", help="Skip matplotlib plot generation.")
    args = parser.parse_args()
    args.lpddr4_nccd_values = (
        [args.lpddr4_nccd_single]
        if args.lpddr4_nccd_single is not None
        else parse_nccd_values(args.lpddr4_nccd_values)
    )

    if not args.no_plot:
        ensure_matplotlib_available()

    rows, transition_rows = build_rows(args)
    constraint_rows = build_constraint_rows(rows, transition_rows)
    csv_path = args.output_dir / "gemv_cycle_breakdown.csv"
    transition_csv_path = args.output_dir / "gemv_cycle_transition_breakdown.csv"
    constraint_csv_path = args.output_dir / "gemv_cycle_constraint_breakdown.csv"
    constraint_map_csv_path = args.output_dir / "gemv_cycle_constraint_map.csv"
    write_csv(rows, csv_path)
    write_transition_csv(transition_rows, transition_csv_path)
    write_constraint_csv(constraint_rows, constraint_csv_path)
    write_constraint_map_csv(constraint_map_csv_path)
    print(f"Wrote {csv_path}")
    print(f"Wrote {transition_csv_path}")
    print(f"Wrote {constraint_csv_path}")
    print(f"Wrote {constraint_map_csv_path}")

    if not args.no_plot:
        cycle_plot_path = args.output_dir / "gemv_cycle_breakdown_cycles_and_percent.png"
        constraint_plot_path = args.output_dir / "gemv_cycle_breakdown_constraints_and_percent.png"
        write_cycle_combined_plot(rows, cycle_plot_path, args.lpddr4_nccd_values)
        write_constraint_combined_plot(constraint_rows, constraint_plot_path, args.lpddr4_nccd_values)
        print(f"Wrote {cycle_plot_path}")
        print(f"Wrote {constraint_plot_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
