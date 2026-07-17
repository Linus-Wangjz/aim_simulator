#!/usr/bin/env python3
"""Run GEMV traces and plot DRAM-only energy breakdowns.

The energy model is intentionally reused from cellar_power_calculator.py.  This
script only keeps DRAM-side components: ACT/PRE, RD, WR, PIM, ACT_STBY, and
PRE_STBY.  DQ, memory controller, global-buffer, and accelerator energy are not
included in the plotted totals.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import subprocess
import tempfile
from pathlib import Path

import cellar_power_calculator as cellar


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

DRAM_ENERGY_COMPONENTS = ["ACT/PRE", "RD", "WR", "PIM", "ACT_STBY", "PRE_STBY"]
DRAM_POWER_COLUMNS = {
    "ACT/PRE": "ACT_PRE_W",
    "RD": "RD_W",
    "WR": "WR_W",
    "PIM": "PIM_W",
    "ACT_STBY": "ACT_STBY_W",
    "PRE_STBY": "PRE_STBY_W",
}
LPDDR4_DRAM_RE = re.compile(r"^LPDDR4_nCCD(\d+)$")
LPDDR4X_DRAM_RE = re.compile(r"^LPDDR4X_nCCD(\d+)$")
NCCD_RE = re.compile(r"^\s*nCCD:\s*(\d+)\s*$", re.MULTILINE)


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


def matplotlib_available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def write_lpddr4_yaml_with_nccd(base_yaml: Path, nccd: int, dst: Path) -> None:
    lines = base_yaml.read_text().splitlines(keepends=True)
    output: list[str] = []
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


def run_ramulator(ramulator: Path, config: Path, trace: Path, output: Path, root: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(ramulator), "-f", str(config), "-t", str(trace)]
    with output.open("w") as fh:
        proc = subprocess.run(cmd, cwd=root, stdout=fh, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\nSee {output}")


def result_name(workload_name: str, dram: str, lpddr4_nccd: int | None = None) -> str:
    if dram == "lpddr4":
        if lpddr4_nccd is None:
            raise ValueError("LPDDR4 result names require an nCCD value")
        return f"output_{workload_name}_lpddr4_nCCD{lpddr4_nccd}.result"
    return f"output_{workload_name}_gddr6.result"


def dram_name(dram: str, nccd: int | None = None, show_nccd: bool = True) -> str:
    if dram == "gddr6":
        return "GDDR6"
    if dram in {"lpddr4", "lpddr4x"}:
        if nccd is None:
            raise ValueError("LPDDR4 names require nCCD")
        base = "LPDDR4" if dram == "lpddr4" else "LPDDR4X"
        return f"{base}_nCCD{nccd}" if show_nccd else base
    raise ValueError(f"unknown DRAM name: {dram}")


def dram_label(dram: str) -> str:
    if dram == "GDDR6":
        return "G6"
    if dram == "LPDDR4":
        return "LP4"
    if dram == "LPDDR4X":
        return "LP4X"
    match = LPDDR4_DRAM_RE.match(dram)
    if match:
        return f"LP4\nn{match.group(1)}"
    match = LPDDR4X_DRAM_RE.match(dram)
    if match:
        return f"LP4X\nn{match.group(1)}"
    return dram


def dram_sort_key(dram: str) -> tuple[int, int]:
    match = LPDDR4_DRAM_RE.match(dram)
    if dram == "GDDR6":
        return (0, 0)
    if dram == "LPDDR4":
        return (1, 0)
    if dram == "LPDDR4X":
        return (2, 0)
    if match:
        return (1, int(match.group(1)))
    match = LPDDR4X_DRAM_RE.match(dram)
    if match:
        return (2, int(match.group(1)))
    return (2, 0)


def set_cellar_channel_count(ch_per_dv: int) -> None:
    cellar.CH_PER_DV = float(ch_per_dv)
    cellar.ACCEL_CYCLE = {
        "EXP": cellar.CH_PER_DV * cellar.SB_RD_CYCLE + cellar.EXP_LANE_CYCLE + cellar.SB_WR_CYCLE,
        "VEC": cellar.CH_PER_DV * 2.00 * cellar.SB_RD_CYCLE + 1.00 + cellar.SB_WR_CYCLE,
    }


def dram_energy_from_result(
    result: Path,
    hidden_dim: int,
    dram_power_impl: str | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    stat = cellar.command_processor(str(result))
    # Non-DRAM calculator inputs are dummies here; only DRAM energy components
    # are consumed below.
    energy, _latency = cellar.power_calculator(
        stat,
        PCIE_bits=0,
        Head=1,
        HiddenDim=hidden_dim,
        Tokens=1,
        GQA=1,
        dram_power_impl=dram_power_impl,
    )
    return energy, stat


def append_energy_row(
    rows: list[dict[str, str]],
    workload: dict[str, object],
    dram: str,
    nccd: int | None,
    energy: dict[str, float],
    stat: dict[str, float],
    output: Path,
    trace: Path,
    root: Path,
) -> None:
    latency_ms = stat["latency"]
    total_dram_mj = sum(energy[comp] for comp in DRAM_ENERGY_COMPONENTS)
    row = {
        "size": str(workload["size"]),
        "workload": str(workload["name"]),
        "dram": dram,
        "lpddr4_nccd": "" if nccd is None else str(nccd),
        "memory_system_cycles": f"{stat['cycles']:.0f}",
        "gemv_latency_ms": f"{latency_ms:.12g}",
        "pim_tccd_cycles": f"{stat['pim_tccd_cycles']:.0f}",
        "pim_tccd_ns": f"{stat['pim_tccd_cycles'] * cellar.GIGA / cellar.FREQ:.12g}",
        "total_dram_mJ": f"{total_dram_mj:.12g}",
        "total_dram_W": f"{total_dram_mj / latency_ms:.12g}",
        "output": display_path(output, root),
        "trace": display_path(trace, root),
    }
    for comp in DRAM_ENERGY_COMPONENTS:
        row[comp] = f"{energy[comp]:.12g}"
        row[DRAM_POWER_COLUMNS[comp]] = f"{energy[comp] / latency_ms:.12g}"
    rows.append(row)


def build_rows(args: argparse.Namespace) -> list[dict[str, str]]:
    root = repo_root()
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    set_cellar_channel_count(args.ch_per_dv)

    rows: list[dict[str, str]] = []
    show_lpddr4_nccd = len(args.lpddr4_nccd_values) > 1
    with tempfile.TemporaryDirectory(prefix="lpddr4_energy_nccd_", dir=root / "output") as tmp:
        tmp_dir = Path(tmp)

        for workload in WORKLOADS:
            runs: list[tuple[str, int | None]] = [("gddr6", None)]
            runs.extend(("lpddr4", nccd) for nccd in args.lpddr4_nccd_values)

            for dram, nccd in runs:
                trace = root / workload[f"{dram}_trace"]
                output = out_dir / result_name(workload["name"], dram, nccd)

                if dram == "gddr6":
                    config = args.gddr6_yaml
                    label = "GDDR6"
                else:
                    config = tmp_dir / f"example_LPDDR4_nCCD{nccd}.yaml"
                    write_lpddr4_yaml_with_nccd(args.lpddr4_yaml, nccd, config)
                    label = f"LPDDR4 nCCD={nccd}"

                if args.reuse_existing and output.exists():
                    print(f"[reuse] {display_path(output, root)}")
                else:
                    print(f"[run] {label} {workload['name']}: {display_path(trace, root)}")
                    run_ramulator(args.ramulator, config, trace, output, root)

                energy, stat = dram_energy_from_result(output, workload["size"])
                append_energy_row(
                    rows,
                    workload,
                    dram_name(dram, nccd, show_nccd=show_lpddr4_nccd),
                    nccd,
                    energy,
                    stat,
                    output,
                    trace,
                    root,
                )

                if dram == "lpddr4":
                    lpddr4x_energy, lpddr4x_stat = dram_energy_from_result(
                        output,
                        workload["size"],
                        dram_power_impl="LPDDR4X",
                    )
                    append_energy_row(
                        rows,
                        workload,
                        dram_name("lpddr4x", nccd, show_nccd=show_lpddr4_nccd),
                        nccd,
                        lpddr4x_energy,
                        lpddr4x_stat,
                        output,
                        trace,
                        root,
                    )

    return rows


def write_csv(rows: list[dict[str, str]], csv_path: Path) -> None:
    fieldnames = [
        "size",
        "workload",
        "dram",
        "lpddr4_nccd",
        "memory_system_cycles",
        "gemv_latency_ms",
        "pim_tccd_cycles",
        "pim_tccd_ns",
        *DRAM_ENERGY_COMPONENTS,
        "total_dram_mJ",
        *DRAM_POWER_COLUMNS.values(),
        "total_dram_W",
        "output",
        "trace",
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
        x_positions.append(size_to_base[int(row["size"])] + dram_offsets[dram])
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


def component_colors() -> dict[str, str]:
    return {
        "ACT/PRE": "#E45756",
        "RD": "#4C78A8",
        "WR": "#F58518",
        "PIM": "#54A24B",
        "ACT_STBY": "#72B7B2",
        "PRE_STBY": "#B279A2",
    }


def component_value(row: dict[str, str], comp: str, metric: str) -> float:
    if metric == "energy":
        return float(row[comp])
    if metric == "power":
        return float(row[DRAM_POWER_COLUMNS[comp]])
    raise ValueError(f"unknown plot metric: {metric}")


def total_value(row: dict[str, str], metric: str) -> float:
    if metric == "energy":
        return float(row["total_dram_mJ"])
    if metric == "power":
        return float(row["total_dram_W"])
    raise ValueError(f"unknown plot metric: {metric}")


def metric_title(metric: str, lpddr4_nccd_values: list[int]) -> str:
    nccd_text = ",".join(str(value) for value in lpddr4_nccd_values)
    timing_text = f"LPDDR4 timing nCCD={nccd_text}"
    if metric == "energy":
        return f"GEMV DRAM Energy Breakdown: GDDR6 vs LPDDR4 vs LPDDR4X ({timing_text})"
    if metric == "power":
        return f"GEMV Average DRAM Power Breakdown: GDDR6 vs LPDDR4 vs LPDDR4X ({timing_text})"
    raise ValueError(f"unknown plot metric: {metric}")


def metric_ylabel(metric: str) -> str:
    if metric == "energy":
        return "DRAM energy (mJ)"
    if metric == "power":
        return "Average DRAM power (W)"
    raise ValueError(f"unknown plot metric: {metric}")


def write_stacked_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int], metric: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = component_colors()

    ordered, x_positions, x_labels, group_positions, group_labels = plot_inputs(rows)

    fig, ax = plt.subplots(figsize=(12.2, 5.9))
    bottoms = [0.0] * len(ordered)
    for comp in DRAM_ENERGY_COMPONENTS:
        values = [component_value(row, comp, metric) for row in ordered]
        ax.bar(
            x_positions,
            values,
            bottom=bottoms,
            label=comp,
            color=colors[comp],
            width=0.46,
            edgecolor="white",
            linewidth=0.5,
        )
        bottoms = [base + value for base, value in zip(bottoms, values)]

    ax.set_title(metric_title(metric, lpddr4_nccd_values))
    ax.set_ylabel(metric_ylabel(metric))
    style_grouped_axis(ax, x_positions, x_labels, group_positions, group_labels)
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(ncols=6, loc="upper center", bbox_to_anchor=(0.5, 1.13), frameon=False)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.20, top=0.86)
    fig.savefig(plot_path, dpi=200)
    plt.close(fig)


def write_energy_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    write_stacked_plot(rows, plot_path, lpddr4_nccd_values, "energy")


def write_power_plot(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    write_stacked_plot(rows, plot_path, lpddr4_nccd_values, "power")


def write_stacked_svg(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int], metric: str) -> None:
    colors = component_colors()

    ordered = sorted(rows, key=lambda row: (int(row["size"]), dram_sort_key(row["dram"])))
    sizes = sorted({int(row["size"]) for row in ordered})
    drams = sorted({row["dram"] for row in ordered}, key=dram_sort_key)
    rows_by_key = {(int(row["size"]), row["dram"]): row for row in ordered}

    width = 1360
    height = 720
    margin_left = 88
    margin_right = 32
    margin_top = 106
    margin_bottom = 124
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    group_gap = 52
    bar_gap = 12
    group_width = (plot_width - group_gap * (len(sizes) - 1)) / len(sizes)
    bar_width = (group_width - bar_gap * (len(drams) - 1)) / len(drams)
    max_total = max(total_value(row, metric) for row in ordered)
    y_max = max_total * 1.10 if max_total > 0 else 1.0

    def sx(group_idx: int, dram_idx: int) -> float:
        group_x = margin_left + group_idx * (group_width + group_gap)
        return group_x + dram_idx * (bar_width + bar_gap)

    def sy(value: float) -> float:
        return margin_top + plot_height - value / y_max * plot_height

    def esc(value: str) -> str:
        return html.escape(value, quote=True)

    svg: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        "text{font-family:Arial,Helvetica,sans-serif;fill:#1f1f1f}",
        ".title{font-size:22px;font-weight:700}",
        ".axis{font-size:13px}",
        ".tick{font-size:12px;fill:#666}",
        ".group{font-size:14px;font-weight:700}",
        ".legend{font-size:13px}",
        "</style>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="white"/>',
        f'<text class="title" x="{width / 2}" y="36" text-anchor="middle">{esc(metric_title(metric, lpddr4_nccd_values))}</text>',
        f'<text class="axis" x="{-(margin_top + plot_height / 2)}" y="22" transform="rotate(-90)" text-anchor="middle">{esc(metric_ylabel(metric))}</text>',
    ]

    legend_x = margin_left
    legend_y = 64
    for idx, comp in enumerate(DRAM_ENERGY_COMPONENTS):
        x = legend_x + idx * 160
        svg.append(f'<rect x="{x}" y="{legend_y - 11}" width="14" height="14" fill="{colors[comp]}"/>')
        svg.append(f'<text class="legend" x="{x + 20}" y="{legend_y + 1}">{esc(comp)}</text>')

    tick_count = 5
    for tick in range(tick_count + 1):
        value = y_max * tick / tick_count
        y = sy(value)
        svg.append(f'<line x1="{margin_left}" x2="{width - margin_right}" y1="{y:.2f}" y2="{y:.2f}" stroke="#e2e2e2" stroke-width="1"/>')
        svg.append(f'<text class="tick" x="{margin_left - 10}" y="{y + 4:.2f}" text-anchor="end">{value:.3g}</text>')

    svg.append(f'<line x1="{margin_left}" x2="{margin_left}" y1="{margin_top}" y2="{margin_top + plot_height}" stroke="#444" stroke-width="1"/>')
    svg.append(f'<line x1="{margin_left}" x2="{width - margin_right}" y1="{margin_top + plot_height}" y2="{margin_top + plot_height}" stroke="#444" stroke-width="1"/>')

    for group_idx, size in enumerate(sizes):
        group_x = margin_left + group_idx * (group_width + group_gap)
        if group_idx > 0:
            sep_x = group_x - group_gap / 2
            svg.append(f'<line x1="{sep_x:.2f}" x2="{sep_x:.2f}" y1="{margin_top}" y2="{margin_top + plot_height}" stroke="#d8d8d8" stroke-width="1"/>')

        for dram_idx, dram in enumerate(drams):
            row = rows_by_key[(size, dram)]
            x = sx(group_idx, dram_idx)
            bottom = 0.0
            for comp in DRAM_ENERGY_COMPONENTS:
                value = component_value(row, comp, metric)
                y = sy(bottom + value)
                h = max(value / y_max * plot_height, 0.0)
                svg.append(
                    f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="{h:.2f}" '
                    f'fill="{colors[comp]}" stroke="white" stroke-width="0.6"/>'
                )
                bottom += value

            center_x = x + bar_width / 2
            label = dram_label(dram).split("\n")
            svg.append(f'<text class="tick" x="{center_x:.2f}" y="{margin_top + plot_height + 22}" text-anchor="middle">{esc(label[0])}</text>')
            if len(label) > 1:
                svg.append(f'<text class="tick" x="{center_x:.2f}" y="{margin_top + plot_height + 38}" text-anchor="middle">{esc(label[1])}</text>')

        group_center = group_x + group_width / 2
        svg.append(f'<text class="group" x="{group_center:.2f}" y="{height - 40}" text-anchor="middle">{size}</text>')

    svg.append(f'<text class="axis" x="{width / 2}" y="{height - 12}" text-anchor="middle">matrix size</text>')
    svg.append("</svg>")
    plot_path.write_text("\n".join(svg))


def write_energy_svg(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    write_stacked_svg(rows, plot_path, lpddr4_nccd_values, "energy")


def write_power_svg(rows: list[dict[str, str]], plot_path: Path, lpddr4_nccd_values: list[int]) -> None:
    write_stacked_svg(rows, plot_path, lpddr4_nccd_values, "power")


def main() -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ramulator", type=Path, default=root / "build/ramulator2")
    parser.add_argument("--lpddr4-yaml", type=Path, default=root / "test/example_LPDDR4.yaml")
    parser.add_argument("--gddr6-yaml", type=Path, default=root / "test/example_GDDR6.yaml")
    parser.add_argument("--output-dir", type=Path, default=root / "output/gemv_dram_energy_breakdown")
    parser.add_argument("--nccd-values", default="6")
    parser.add_argument("--ch-per-dv", type=int, default=32)
    parser.add_argument("--reuse-existing", action="store_true", help="Reuse existing .result files instead of rerunning them.")
    parser.add_argument("--no-plot", action="store_true", help="Skip matplotlib plot generation.")
    args = parser.parse_args()

    args.lpddr4_nccd_values = parse_nccd_values(args.nccd_values)
    has_matplotlib = False if args.no_plot else matplotlib_available()
    if not args.ramulator.exists():
        raise FileNotFoundError(f"ramulator binary not found: {args.ramulator}")

    rows = build_rows(args)
    csv_path = args.output_dir / "gemv_dram_energy_breakdown.csv"
    write_csv(rows, csv_path)
    print(f"[csv] {display_path(csv_path, root)}")

    if not args.no_plot and has_matplotlib:
        energy_plot_path = args.output_dir / "gemv_dram_energy_breakdown_mj.png"
        power_plot_path = args.output_dir / "gemv_dram_power_breakdown_w.png"
        write_energy_plot(rows, energy_plot_path, args.lpddr4_nccd_values)
        write_power_plot(rows, power_plot_path, args.lpddr4_nccd_values)
        print(f"[plot] {display_path(energy_plot_path, root)}")
        print(f"[plot] {display_path(power_plot_path, root)}")
    elif not args.no_plot:
        energy_plot_path = args.output_dir / "gemv_dram_energy_breakdown_mj.svg"
        power_plot_path = args.output_dir / "gemv_dram_power_breakdown_w.svg"
        write_energy_svg(rows, energy_plot_path, args.lpddr4_nccd_values)
        write_power_svg(rows, power_plot_path, args.lpddr4_nccd_values)
        print(f"[plot] {display_path(energy_plot_path, root)} (SVG fallback; matplotlib is not installed)")
        print(f"[plot] {display_path(power_plot_path, root)} (SVG fallback; matplotlib is not installed)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
