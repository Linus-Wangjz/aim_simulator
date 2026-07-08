#!/usr/bin/env python3
"""Plot stacked GEMV cycle breakdowns from actual issued DRAM commands."""

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
        "size": 256,
        "name": "gemv_256x256",
        "gddr6_trace": "test/gemv_256x256.trace",
        "lpddr4_trace": "test/gemv_256x256.trace",
    },
    {
        "size": 512,
        "name": "gemv_512x512",
        "gddr6_trace": "test/gemv_512x512_gddr6.trace",
        "lpddr4_trace": "test/gemv_512x512_lpddr4.trace",
    },
    {
        "size": 1024,
        "name": "gemv_1024x1024",
        "gddr6_trace": "test/gemv_1024x1024_gddr6.trace",
        "lpddr4_trace": "test/gemv_1024x1024_lpddr4.trace",
    },
    {
        "size": 2048,
        "name": "gemv_2048x2048_gpr_psum",
        "gddr6_trace": "test/gemv_2048x2048_gpr_psum_gddr6.trace",
        "lpddr4_trace": "test/gemv_2048x2048_gpr_psum_lpddr4.trace",
    },
)

STACK_LABELS = ["WR_GB", "WR_BIAS", "ACT_PRE", "MAC_ABK", "RD_MAC", "TMOD", "Other"]
CONSTRAINT_LABELS = [
    "nMODCH",
    "nCCD/nCCDS",
    "nCCD/nBL",
    "nRCDRDMAC",
    "nRP/nRPab",
    "nRFCab",
    "nRTW",
    "CAS sync",
    "ACT16 split",
    "Issue gap",
    "Other",
]

TRANSITION_CONSTRAINT_MAP = {
    ("TMOD", "WRGB"): "nMODCH",
    ("TMOD", "CASWRGB"): "nMODCH",
    ("TMOD", "CASWRMAC16"): "nMODCH",
    ("TMOD", "CASRDMAC16"): "nMODCH",
    ("TMOD", "RDMAC16"): "nMODCH",
    ("TMOD", "ACT16"): "nMODCH",
    ("TMOD", "ACT16-1"): "nMODCH",
    ("TMOD", "PREA"): "nMODCH",
    ("MAC16", "MAC16"): "nCCD/nCCDS",
    ("WRGB", "WRGB"): "nCCD/nBL",
    ("WRGB", "WRMAC16"): "nCCD/nBL",
    ("WRGB", "CASWRGB"): "nCCD/nBL",
    ("WRGB", "CASWRMAC16"): "nCCD/nBL",
    ("ACT16", "MAC16"): "nRCDRDMAC",
    ("ACT16-2", "MAC16"): "nRCDRDMAC",
    ("PREA", "ACT16"): "nRP/nRPab",
    ("PREA", "ACT16-1"): "nRP/nRPab",
    ("PREA", "REFab"): "nRP/nRPab",
    ("REFab", "ACT16-1"): "nRFCab",
    ("RDMAC16", "WRGB"): "nRTW",
    ("RDMAC16", "CASWRGB"): "nRTW",
    ("RDMAC16", "WRMAC16"): "nRTW",
    ("RDMAC16", "CASWRMAC16"): "nRTW",
    ("CASWRGB", "WRGB"): "CAS sync",
    ("CASWRMAC16", "WRMAC16"): "CAS sync",
    ("CASRDMAC16", "RDMAC16"): "CAS sync",
    ("ACT16-1", "ACT16-2"): "ACT16 split",
    ("WRMAC16", "TMOD"): "Issue gap",
    ("MAC16", "TMOD"): "Issue gap",
    ("WRGB", "TMOD"): "Issue gap",
    ("REFab", "TMOD"): "Issue gap",
}

COMMAND_TRACE_RE = re.compile(r"^\s*(\d+)\s*,\s*([^,]+)\s*,")
CHANNEL_TRACE_RE = re.compile(r"\.ch(\d+)$")
LPDDR4_DRAM_RE = re.compile(r"^LPDDR4_nCCD(\d+)$")

MEM_CYCLES_RE = re.compile(r"memory_system_cycles:\s+(\d+)")
NCCD_RE = re.compile(r"^\s*nCCD:\s*(\d+)\s*$", re.MULTILINE)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def ensure_matplotlib_available() -> None:
    try:
        import matplotlib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError("matplotlib is not installed. Rerun with --no-plot to only generate CSV/results.") from exc


def parse_nccd_values(values: str) -> list[int]:
    parsed: list[int] = []
    for value in values.split(","):
        value = value.strip()
        if not value:
            continue
        nccd = int(value)
        if nccd not in parsed:
            parsed.append(nccd)
    if not parsed:
        raise ValueError("At least one LPDDR4 nCCD value is required")
    return parsed


def write_yaml_with_trace_recorder(base_yaml: Path, dst: Path, trace_prefix: Path, nccd: int | None = None) -> None:
    lines = base_yaml.read_text().splitlines(keepends=True)
    output: list[str] = []
    inserted_nccd = nccd is None
    inserted_plugin = False
    for line in lines:
        stripped = line.strip()
        if nccd is not None and stripped.startswith("nCCD:"):
            continue
        output.append(line)
        if not inserted_nccd and "preset:" in stripped and "LPDDR4_AiM_timing" in stripped:
            indent = re.match(r"^(\s*)", line).group(1)
            output.append(f"{indent}nCCD: {nccd}\n")
            inserted_nccd = True
        if not inserted_plugin and stripped == "plugins:":
            indent = re.match(r"^(\s*)", line).group(1)
            output.append(f"{indent}  - ControllerPlugin:\n")
            output.append(f"{indent}      impl: TraceRecorder\n")
            output.append(f"{indent}      path: {trace_prefix}\n")
            inserted_plugin = True

    if not inserted_nccd:
        raise RuntimeError(f"Could not find LPDDR4_AiM_timing preset in {base_yaml}")
    if not inserted_plugin:
        raise RuntimeError(f"Could not find Controller plugins block in {base_yaml}")

    dst.write_text("".join(output))
    match = NCCD_RE.search(dst.read_text())
    if nccd is not None and (not match or int(match.group(1)) != nccd):
        raise RuntimeError(f"nCCD override verification failed for {dst}")


def run_ramulator(ramulator: Path, config: Path, trace: Path, output: Path, root: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ramulator), "-f", str(config), "-t", str(trace)]
    with output.open("w") as fh:
        proc = subprocess.run(cmd, cwd=root, stdout=fh, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\nSee {output}")


def command_component(command: str) -> str:
    if command in {"CASWRGB", "WRGB"}:
        return "WR_GB"
    if command in {"CASWRMAC16", "WRMAC16"}:
        return "WR_BIAS"
    if command in {"CASRDMAC16", "RDMAC16"}:
        return "RD_MAC"
    if command in {"MAC", "MAC16"}:
        return "MAC_ABK"
    if command == "TMOD":
        return "TMOD"
    if command.startswith("ACT") or command.startswith("PRE"):
        return "ACT_PRE"
    return "Other"


def transition_constraint(transition: str) -> str:
    try:
        preceding, following = transition.split("->", 1)
    except ValueError:
        return "Other"
    return TRANSITION_CONSTRAINT_MAP.get((preceding, following), "Other")


def parse_command_trace(path: Path) -> list[tuple[int, str]]:
    issued: list[tuple[int, str]] = []
    for line in path.read_text().splitlines():
        match = COMMAND_TRACE_RE.match(line)
        if not match:
            continue
        issued.append((int(match.group(1)), match.group(2).strip()))
    if not issued:
        raise RuntimeError(f"No issued commands found in command trace {path}")
    return issued


def command_trace_files(trace_prefix: Path) -> list[Path]:
    def channel_id(path: Path) -> int:
        match = CHANNEL_TRACE_RE.search(path.name)
        return int(match.group(1)) if match else -1

    return sorted(trace_prefix.parent.glob(f"{trace_prefix.name}.ch*"), key=channel_id)


def select_critical_command_trace(trace_prefix: Path) -> Path:
    candidates: list[tuple[int, int, Path]] = []
    for path in command_trace_files(trace_prefix):
        try:
            issued = parse_command_trace(path)
        except RuntimeError:
            continue
        candidates.append((issued[-1][0], len(issued), path))

    if not candidates:
        raise RuntimeError(f"No issued commands found for command trace prefix {trace_prefix}")
    return max(candidates)[2]


def parse_result(result_path: Path, command_trace_path: Path) -> tuple[dict[str, int], list[dict[str, str]]]:
    text = result_path.read_text()
    mem_match = MEM_CYCLES_RE.search(text)
    if not mem_match:
        raise RuntimeError(f"No memory_system_cycles entry found in {result_path}")

    stats = {"memory_system_cycles": int(mem_match.group(1))}
    for label in STACK_LABELS:
        stats[label] = 0

    issued = parse_command_trace(command_trace_path)
    stats["issued_commands"] = len(issued)
    transitions: dict[str, dict[str, str | int]] = {}
    for (prev_clk, _prev_cmd), (curr_clk, curr_cmd) in zip(issued, issued[1:]):
        delta = curr_clk - prev_clk
        if delta < 0:
            raise RuntimeError(f"Command trace is not monotonic: {command_trace_path}")
        stats[command_component(curr_cmd)] += delta

        transition = f"{_prev_cmd}->{curr_cmd}"
        item = transitions.setdefault(transition, {"transition": transition, "count": 0, "cycles": 0})
        item["count"] = int(item["count"]) + 1
        item["cycles"] = int(item["cycles"]) + delta

    component_sum = sum(stats[label] for label in STACK_LABELS)
    stats["Other"] += max(stats["memory_system_cycles"] - component_sum, 0)
    stats["component_sum"] = sum(stats[label] for label in STACK_LABELS)
    transition_rows = [
        {
            "transition": str(item["transition"]),
            "constraint": transition_constraint(str(item["transition"])),
            "count": str(item["count"]),
            "cycles": str(item["cycles"]),
        }
        for item in transitions.values()
    ]
    return stats, transition_rows


def result_name(workload_name: str, dram: str, lpddr4_nccd: int | None = None) -> str:
    if dram == "lpddr4":
        if lpddr4_nccd is None:
            raise ValueError("LPDDR4 result names require an nCCD value")
        return f"output_{workload_name}_lpddr4_nCCD{lpddr4_nccd}.result"
    return f"output_{workload_name}_gddr6.result"


def dram_label(dram: str) -> str:
    match = LPDDR4_DRAM_RE.match(dram)
    if match:
        return f"LP4\nn{match.group(1)}"
    return dram


def dram_sort_key(dram: str) -> tuple[int, int]:
    match = LPDDR4_DRAM_RE.match(dram)
    if dram == "GDDR6":
        return (0, 0)
    if match:
        return (1, int(match.group(1)))
    return (2, 0)


def build_rows(args: argparse.Namespace) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    root = repo_root()
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_dir = out_dir / "command_traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    transition_rows: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="lpddr4_nccd_", dir=root / "output") as tmp:
        tmp_dir = Path(tmp)

        for workload in WORKLOADS:
            runs: list[tuple[str, int | None]] = [("gddr6", None)]
            runs.extend(("lpddr4", nccd) for nccd in args.lpddr4_nccd_values)

            for dram, nccd in runs:
                trace = root / workload[f"{dram}_trace"]
                command_trace_prefix = trace_dir / result_name(workload["name"], dram, nccd).replace(".result", ".cmd")
                config_suffix = dram if nccd is None else f"{dram}_nCCD{nccd}"
                config = tmp_dir / f"{workload['name']}_{config_suffix}.yaml"
                if dram == "gddr6":
                    write_yaml_with_trace_recorder(args.gddr6_yaml, config, command_trace_prefix)
                else:
                    write_yaml_with_trace_recorder(args.lpddr4_yaml, config, command_trace_prefix, nccd)
                output = out_dir / result_name(workload["name"], dram, nccd)
                if args.reuse_existing and output.exists() and command_trace_files(command_trace_prefix):
                    print(f"[reuse] {display_path(output, root)}")
                else:
                    label = "GDDR6" if dram == "gddr6" else f"LPDDR4 nCCD={nccd}"
                    print(f"[run] {label} {workload['name']}: {display_path(trace, root)}")
                    run_ramulator(args.ramulator, config, trace, output, root)

                command_trace = select_critical_command_trace(command_trace_prefix)
                stats, transitions = parse_result(output, command_trace)
                dram_name = "GDDR6" if dram == "gddr6" else f"LPDDR4_nCCD{nccd}"
                row = {
                    "size": str(workload["size"]),
                    "workload": workload["name"],
                    "dram": dram_name,
                    "lpddr4_nccd": "" if nccd is None else str(nccd),
                    "memory_system_cycles": str(stats["memory_system_cycles"]),
                    "issued_commands": str(stats["issued_commands"]),
                    "component_sum": str(stats["component_sum"]),
                    "Other": str(stats["Other"]),
                    "output": display_path(output, root),
                    "command_trace": display_path(command_trace, root),
                    "trace_channel": command_trace.name.rsplit(".ch", 1)[-1],
                }
                for label in STACK_LABELS:
                    row[label] = str(stats[label])
                rows.append(row)

                for transition in transitions:
                    transition_rows.append(
                        {
                            "size": str(workload["size"]),
                            "workload": workload["name"],
                            "dram": dram_name,
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


def style_grouped_axis(ax, x_positions: list[float], x_labels: list[str], group_positions: list[float], group_labels: list[str]) -> None:
    ax.set_xticks(x_positions)
    ax.set_xticklabels(x_labels)
    ax.tick_params(axis="x", length=0, pad=6)
    for group_x, group_label in zip(group_positions, group_labels):
        ax.text(
            group_x,
            -0.13,
            group_label,
            ha="center",
            va="top",
            transform=ax.get_xaxis_transform(),
            fontsize=10,
            fontweight="semibold",
        )
    for left_group, right_group in zip(group_positions, group_positions[1:]):
        ax.axvline((left_group + right_group) / 2, color="#D8D8D8", linewidth=0.8, zorder=0)
    ax.set_xlabel("matrix size")
    ax.xaxis.set_label_coords(0.5, -0.19)


def write_cycle_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {
        "WR_GB": "#4C78A8",
        "WR_BIAS": "#F58518",
        "ACT_PRE": "#E45756",
        "MAC_ABK": "#54A24B",
        "RD_MAC": "#B279A2",
        "TMOD": "#72B7B2",
        "Other": "#BAB0AC",
    }

    ordered, x_positions, x_labels, group_positions, group_labels = plot_inputs(rows)

    fig, ax = plt.subplots(figsize=(12.2, 5.9))
    bottoms = [0] * len(ordered)
    for label in STACK_LABELS:
        values = [int(row[label]) for row in ordered]
        ax.bar(x_positions, values, bottom=bottoms, label=label, color=colors[label], width=0.46, edgecolor="white", linewidth=0.5)
        bottoms = [base + value for base, value in zip(bottoms, values)]

    nccd_text = ",".join(str(value) for value in lpddr4_nccd_values)
    ax.set_title(f"GEMV Cycle Breakdown from Issued Commands: GDDR6 vs LPDDR4 nCCD={nccd_text}")
    ax.set_ylabel("cycles")
    style_grouped_axis(ax, x_positions, x_labels, group_positions, group_labels)
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(ncols=7, loc="upper center", bbox_to_anchor=(0.5, 1.13), frameon=False)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.20, top=0.86)
    fig.savefig(plot_path, dpi=200)
    plt.close(fig)


def write_percent_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {
        "WR_GB": "#4C78A8",
        "WR_BIAS": "#F58518",
        "ACT_PRE": "#E45756",
        "MAC_ABK": "#54A24B",
        "RD_MAC": "#B279A2",
        "TMOD": "#72B7B2",
        "Other": "#BAB0AC",
    }

    ordered, x_positions, x_labels, group_positions, group_labels = plot_inputs(rows)
    totals = [max(int(row["memory_system_cycles"]), 1) for row in ordered]

    fig, ax = plt.subplots(figsize=(12.2, 5.9))
    bottoms = [0.0] * len(ordered)
    for label in STACK_LABELS:
        values = [100.0 * int(row[label]) / total for row, total in zip(ordered, totals)]
        ax.bar(x_positions, values, bottom=bottoms, label=label, color=colors[label], width=0.46, edgecolor="white", linewidth=0.5)
        bottoms = [base + value for base, value in zip(bottoms, values)]

    nccd_text = ",".join(str(value) for value in lpddr4_nccd_values)
    ax.set_title(f"GEMV Cycle Breakdown Percent from Issued Commands: GDDR6 vs LPDDR4 nCCD={nccd_text}")
    ax.set_ylabel("share of memory_system_cycles (%)")
    ax.set_ylim(0, 100)
    style_grouped_axis(ax, x_positions, x_labels, group_positions, group_labels)
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(ncols=7, loc="upper center", bbox_to_anchor=(0.5, 1.13), frameon=False)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.20, top=0.86)
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
        "ACT16 split": "#8CD17D",
        "Issue gap": "#D4A6C8",
        "Other": "#BAB0AC",
    }


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
    parser.add_argument("--lpddr4-nccd-values", default="2,4,6,8", help="Comma-separated LPDDR4 nCCD values to sweep.")
    parser.add_argument("--lpddr4-nccd", type=int, dest="lpddr4_nccd_single", help="Run only one LPDDR4 nCCD value.")
    parser.add_argument("--output-dir", type=Path, default=root / "output/gemv_cycle_breakdown")
    parser.add_argument("--reuse-existing", action="store_true", help="Parse existing result files instead of rerunning simulations.")
    parser.add_argument("--no-plot", action="store_true", help="Skip matplotlib plot generation.")
    args = parser.parse_args()
    args.lpddr4_nccd_values = (
        [args.lpddr4_nccd_single]
        if args.lpddr4_nccd_single is not None
        else parse_nccd_values(args.lpddr4_nccd_values)
    )

    if not args.ramulator.exists():
        raise FileNotFoundError(f"ramulator binary not found: {args.ramulator}")
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
        cycle_plot_path = args.output_dir / "gemv_cycle_breakdown_cycles.png"
        percent_plot_path = args.output_dir / "gemv_cycle_breakdown_percent.png"
        constraint_plot_path = args.output_dir / "gemv_cycle_breakdown_constraints.png"
        constraint_percent_plot_path = args.output_dir / "gemv_cycle_breakdown_constraints_percent.png"
        write_cycle_plot(rows, cycle_plot_path, args.lpddr4_nccd_values)
        write_percent_plot(rows, percent_plot_path, args.lpddr4_nccd_values)
        write_constraint_plot(constraint_rows, constraint_plot_path, args.lpddr4_nccd_values)
        write_constraint_percent_plot(constraint_rows, constraint_percent_plot_path, args.lpddr4_nccd_values)
        print(f"Wrote {cycle_plot_path}")
        print(f"Wrote {percent_plot_path}")
        print(f"Wrote {constraint_plot_path}")
        print(f"Wrote {constraint_percent_plot_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
