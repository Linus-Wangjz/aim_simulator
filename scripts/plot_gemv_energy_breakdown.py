#!/usr/bin/env python3
"""Run GEMV traces and plot energy breakdowns.

The energy model is intentionally reused from cellar_power_calculator.py.  This
script defaults to DRAM-only energy, but can also include trace-relevant
system-side estimates such as DQ IO, controller/PHY, and PNM module energy.
"""

from __future__ import annotations

import argparse
import csv
import html
import math
from pathlib import Path

import cellar_power_calculator as cellar
from aim_analysis.ramulator import (
    GemvRamulatorRunner,
    RamulatorArtifactStore,
    display_path,
    dram_label,
    dram_name,
    dram_sort_key,
    parse_nccd_values,
    repo_root,
)
from aim_analysis.runtime import configure_matplotlib_cache
from aim_analysis.workloads import GEMV_WORKLOADS, GemvWorkload

LEGACY_DRAM_ENERGY_COMPONENTS = ["ACT/PRE", "RD", "WR", "PIM", "ACT_STBY", "PRE_STBY"]
TRACE_BASED_DRAM_ENERGY_COMPONENTS = ["ACT", "PRE", "RD", "WR", "PIM", "ACT_STBY", "PRE_STBY"]
SYSTEM_COMPONENT_GROUPS = {
    "DQ_IO": ["DQ"],
    "CTRL_PHY": ["MEM_CTR"],
    "PNM_STT": ["GB_STT", "SB_STT", "IB_STT", "RED_STT", "EXP_STT", "VEC_STT"],
    "PNM_DYN": ["GB_RD", "GB_WR"],
    "PNM_CTRL": ["DV_CTR"],
    "PCIe": ["PCIe"],
}
CELLAR_LLM_OVERHEAD_GROUPS = {
    # These are synthetic Cellar LLM-side estimates for RMSNorm/Softmax/RotEmbed.
    # They are not trace-derived GEMV work, so they are opt-in for this script.
    "PNM_LLM_DYN": ["SB_DYN", "IB_DYN", "RV_DYN", "RED_DYN", "EXP_DYN", "VEC_DYN"],
}
def dram_energy_components(dram_energy_model: str) -> list[str]:
    if dram_energy_model == "legacy":
        return LEGACY_DRAM_ENERGY_COMPONENTS
    if dram_energy_model == "trace-based":
        return TRACE_BASED_DRAM_ENERGY_COMPONENTS
    raise ValueError(f"unknown DRAM energy model: {dram_energy_model}")


def energy_component_groups(
    scope: str,
    dram_energy_model: str,
    include_cellar_llm_overhead: bool = False,
) -> dict[str, list[str]]:
    dram_components = dram_energy_components(dram_energy_model)
    if scope == "dram":
        return {component: [component] for component in dram_components}
    if scope == "system":
        groups = {component: [component] for component in dram_components}
        groups.update(SYSTEM_COMPONENT_GROUPS)
        if include_cellar_llm_overhead:
            groups.update(CELLAR_LLM_OVERHEAD_GROUPS)
        return groups
    raise ValueError(f"unknown energy scope: {scope}")


def power_column(component: str) -> str:
    return f"{component.replace('/', '_')}_W"


def matplotlib_available() -> bool:
    configure_matplotlib_cache()
    try:
        import matplotlib  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


def set_cellar_channel_count(ch_per_dv: int) -> None:
    cellar.CH_PER_DV = float(ch_per_dv)
    cellar.ACCEL_CYCLE = {
        "EXP": cellar.CH_PER_DV * cellar.SB_RD_CYCLE + cellar.EXP_LANE_CYCLE + cellar.SB_WR_CYCLE,
        "VEC": cellar.CH_PER_DV * 2.00 * cellar.SB_RD_CYCLE + 1.00 + cellar.SB_WR_CYCLE,
    }


def dram_energy_from_result(
    result: Path,
    timing_export: Path,
    hidden_dim: int,
    pcie_bits: int,
    dram_energy_model: str,
    command_trace_prefix: Path | None = None,
    dram_power_impl: str | None = None,
) -> tuple[dict[str, float], dict[str, float]]:
    stat = cellar.read_run_statistics(str(result), timing_export)
    # Head/Tokens/GQA are placeholders for this GEMV script. By default the
    # plotting scope does not include Cellar's synthetic LLM-side dynamic terms.
    energy, _latency = cellar.calculate_energy_and_latency(
        stat,
        PCIE_bits=pcie_bits,
        Head=1,
        HiddenDim=hidden_dim,
        Tokens=1,
        GQA=1,
        dram_power_impl=dram_power_impl,
        dram_energy_model=dram_energy_model,
        command_trace_prefix=command_trace_prefix,
    )
    return energy, stat


def append_energy_row(
    rows: list[dict[str, str]],
    workload: GemvWorkload,
    dram: str,
    nccd: int | None,
    energy: dict[str, float],
    stat: dict[str, float],
    energy_scope: str,
    dram_energy_model: str,
    groups: dict[str, list[str]],
    pcie_bits: int,
    output: Path,
    timing_export: Path,
    command_trace_prefix: Path | None,
    trace: Path,
    root: Path,
) -> None:
    latency_ms = stat["latency"]
    component_energy = {
        component: sum(energy[source] for source in sources)
        for component, sources in groups.items()
    }
    total_mj = sum(component_energy.values())
    row = {
        "size": str(workload.size),
        "workload": workload.name,
        "dram": dram,
        "energy_scope": energy_scope,
        "dram_energy_model": dram_energy_model,
        "device_scope": f"1 device ({int(cellar.CH_PER_DV)} channels)",
        "pcie_bits": str(pcie_bits),
        "lpddr4_nccd": "" if nccd is None else str(nccd),
        "memory_system_cycles": f"{stat['cycles']:.0f}",
        "gemv_latency_ms": f"{latency_ms:.12g}",
        "pim_tccd_cycles": f"{stat['pim_tccd_cycles']:.0f}",
        "pim_tccd_ns": f"{stat['pim_tccd_ns']:.12g}",
        "total_mJ": f"{total_mj:.12g}",
        "total_W": f"{total_mj / latency_ms:.12g}",
        "output": display_path(output, root),
        "timing_export": display_path(timing_export, root),
        "command_trace_prefix": "" if command_trace_prefix is None else display_path(command_trace_prefix, root),
        "trace": display_path(trace, root),
    }
    for comp, value in component_energy.items():
        row[comp] = f"{value:.12g}"
        row[power_column(comp)] = f"{value / latency_ms:.12g}"
    rows.append(row)


def build_rows(args: argparse.Namespace) -> list[dict[str, str]]:
    root = repo_root()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    set_cellar_channel_count(args.ch_per_dv)
    groups = energy_component_groups(args.energy_scope, args.dram_energy_model, args.include_cellar_llm_overhead)
    runner = GemvRamulatorRunner(
        root,
        args.ramulator,
        args.gddr6_yaml,
        args.lpddr4_yaml,
        RamulatorArtifactStore(args.ramulator_output_dir),
        args.reuse_existing,
    )

    rows: list[dict[str, str]] = []
    show_lpddr4_nccd = len(args.lpddr4_nccd_values) > 1
    for workload in GEMV_WORKLOADS:
        runs: list[tuple[str, int | None]] = [("gddr6", None)]
        runs.extend(("lpddr4", nccd) for nccd in args.lpddr4_nccd_values)

        for dram, nccd in runs:
            artifacts = runner.ensure(workload, dram, nccd)
            trace = root / workload.trace_for(dram)
            command_trace_prefix = artifacts.command_trace_prefix
            if dram == "gddr6":
                energy, stat = dram_energy_from_result(
                    artifacts.result,
                    artifacts.timing,
                    workload.size,
                    args.pcie_bits,
                    args.dram_energy_model,
                    command_trace_prefix,
                )
                display_name = dram_name("gddr6")
            else:
                energy, stat = dram_energy_from_result(
                    artifacts.result,
                    artifacts.timing,
                    workload.size,
                    args.pcie_bits,
                    args.dram_energy_model,
                    command_trace_prefix,
                    dram_power_impl="LPDDR4X",
                )
                display_name = dram_name(
                    "lpddr4",
                    nccd,
                    power_impl="LPDDR4X",
                    show_nccd=show_lpddr4_nccd,
                )
            append_energy_row(
                rows,
                workload,
                display_name,
                nccd,
                energy,
                stat,
                args.energy_scope,
                args.dram_energy_model,
                groups,
                args.pcie_bits,
                artifacts.result,
                artifacts.timing,
                command_trace_prefix,
                trace,
                root,
            )

    return rows


def write_csv(rows: list[dict[str, str]], csv_path: Path, groups: dict[str, list[str]]) -> None:
    components = list(groups)
    fieldnames = [
        "size",
        "workload",
        "dram",
        "energy_scope",
        "dram_energy_model",
        "device_scope",
        "pcie_bits",
        "lpddr4_nccd",
        "memory_system_cycles",
        "gemv_latency_ms",
        "pim_tccd_cycles",
        "pim_tccd_ns",
        *components,
        "total_mJ",
        *(power_column(component) for component in components),
        "total_W",
        "output",
        "timing_export",
        "command_trace_prefix",
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
        "ACT": "#E45756",
        "PRE": "#FF9DA6",
        "RD": "#4C78A8",
        "WR": "#F58518",
        "PIM": "#54A24B",
        "ACT_STBY": "#72B7B2",
        "PRE_STBY": "#B279A2",
        "DQ_IO": "#F58518",
        "CTRL_PHY": "#E45756",
        "PNM_STT": "#72B7B2",
        "PNM_DYN": "#54A24B",
        "PNM_CTRL": "#B279A2",
        "PNM_LLM_DYN": "#9D755D",
        "PCIe": "#BAB0AC",
    }


def component_value(row: dict[str, str], comp: str, metric: str) -> float:
    if metric == "energy":
        return float(row[comp])
    if metric == "power":
        return float(row[power_column(comp)])
    raise ValueError(f"unknown plot metric: {metric}")


def total_value(row: dict[str, str], metric: str) -> float:
    if metric == "energy":
        return float(row["total_mJ"])
    if metric == "power":
        return float(row["total_W"])
    raise ValueError(f"unknown plot metric: {metric}")


def metric_title(metric: str, lpddr4_nccd_values: list[int], energy_scope: str, dram_energy_model: str) -> str:
    nccd_text = ",".join(str(value) for value in lpddr4_nccd_values)
    timing_text = f"LPDDR4X power, LPDDR4 timing nCCD={nccd_text}"
    scope_text = "DRAM" if energy_scope == "dram" else "System"
    model_text = "Trace-Based Power Model" if dram_energy_model == "trace-based" else "Command-count model"
    if metric == "energy":
        return f"GEMV {scope_text} Energy Breakdown: {model_text} ({timing_text})"
    if metric == "power":
        return f"GEMV Average {scope_text} Power Breakdown: {model_text} ({timing_text})"
    raise ValueError(f"unknown plot metric: {metric}")


def metric_ylabel(metric: str, energy_scope: str) -> str:
    scope_text = "DRAM" if energy_scope == "dram" else "system"
    if metric == "energy":
        return f"{scope_text} energy (mJ)"
    if metric == "power":
        return f"Average {scope_text} power (W)"
    raise ValueError(f"unknown plot metric: {metric}")


def write_stacked_plot(
    rows: list[dict[str, str]],
    plot_path: Path,
    lpddr4_nccd_values: list[int],
    metric: str,
    energy_scope: str,
    dram_energy_model: str,
    groups: dict[str, list[str]],
) -> None:
    configure_matplotlib_cache()
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = component_colors()

    ordered, x_positions, x_labels, group_positions, group_labels = plot_inputs(rows)

    legend_cols = min(len(groups), 6 if len(groups) <= 6 else 4)
    legend_rows = max(1, math.ceil(len(groups) / legend_cols))
    axes_top = max(0.70, 0.86 - 0.045 * (legend_rows - 1))

    fig, ax = plt.subplots(figsize=(12.2, 6.2 if legend_rows > 1 else 5.9))
    bottoms = [0.0] * len(ordered)
    for comp in groups:
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

    fig.suptitle(metric_title(metric, lpddr4_nccd_values, energy_scope, dram_energy_model), y=0.975, fontsize=13, fontweight="semibold")
    ax.set_ylabel(metric_ylabel(metric, energy_scope))
    style_grouped_axis(ax, x_positions, x_labels, group_positions, group_labels)
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        ncols=legend_cols,
        loc="upper center",
        bbox_to_anchor=(0.5, 0.93),
        frameon=False,
        columnspacing=1.25,
        handlelength=1.35,
        handletextpad=0.45,
        fontsize=9,
    )
    fig.subplots_adjust(bottom=0.20, top=axes_top)
    fig.savefig(plot_path, dpi=200)
    plt.close(fig)


def write_energy_plot(
    rows: list[dict[str, str]],
    plot_path: Path,
    lpddr4_nccd_values: list[int],
    energy_scope: str,
    dram_energy_model: str,
    groups: dict[str, list[str]],
) -> None:
    write_stacked_plot(rows, plot_path, lpddr4_nccd_values, "energy", energy_scope, dram_energy_model, groups)


def write_power_plot(
    rows: list[dict[str, str]],
    plot_path: Path,
    lpddr4_nccd_values: list[int],
    energy_scope: str,
    dram_energy_model: str,
    groups: dict[str, list[str]],
) -> None:
    write_stacked_plot(rows, plot_path, lpddr4_nccd_values, "power", energy_scope, dram_energy_model, groups)


def write_stacked_svg(
    rows: list[dict[str, str]],
    plot_path: Path,
    lpddr4_nccd_values: list[int],
    metric: str,
    energy_scope: str,
    dram_energy_model: str,
    groups: dict[str, list[str]],
) -> None:
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
        f'<text class="title" x="{width / 2}" y="36" text-anchor="middle">{esc(metric_title(metric, lpddr4_nccd_values, energy_scope, dram_energy_model))}</text>',
        f'<text class="axis" x="{-(margin_top + plot_height / 2)}" y="22" transform="rotate(-90)" text-anchor="middle">{esc(metric_ylabel(metric, energy_scope))}</text>',
    ]

    legend_x = margin_left
    legend_y = 64
    for idx, comp in enumerate(groups):
        col = idx % 7
        row = idx // 7
        x = legend_x + col * 170
        y = legend_y + row * 20
        svg.append(f'<rect x="{x}" y="{y - 11}" width="14" height="14" fill="{colors[comp]}"/>')
        svg.append(f'<text class="legend" x="{x + 20}" y="{y + 1}">{esc(comp)}</text>')

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
            for comp in groups:
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


def write_energy_svg(
    rows: list[dict[str, str]],
    plot_path: Path,
    lpddr4_nccd_values: list[int],
    energy_scope: str,
    dram_energy_model: str,
    groups: dict[str, list[str]],
) -> None:
    write_stacked_svg(rows, plot_path, lpddr4_nccd_values, "energy", energy_scope, dram_energy_model, groups)


def write_power_svg(
    rows: list[dict[str, str]],
    plot_path: Path,
    lpddr4_nccd_values: list[int],
    energy_scope: str,
    dram_energy_model: str,
    groups: dict[str, list[str]],
) -> None:
    write_stacked_svg(rows, plot_path, lpddr4_nccd_values, "power", energy_scope, dram_energy_model, groups)


def main() -> int:
    root = repo_root()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ramulator", type=Path, default=root / "build/ramulator2")
    parser.add_argument("--lpddr4-yaml", type=Path, default=root / "test/example_LPDDR4.yaml")
    parser.add_argument("--gddr6-yaml", type=Path, default=root / "test/example_GDDR6.yaml")
    parser.add_argument("--output-dir", type=Path, default=root / "output/gemv_dram_energy_breakdown")
    parser.add_argument(
        "--ramulator-output-dir",
        type=Path,
        default=root / "output/ramulator",
        help="Shared raw Ramulator artifact cache (results, resolved timing, and command traces).",
    )
    parser.add_argument("--nccd-values", default="2,6")
    parser.add_argument("--ch-per-dv", type=int, default=32)
    parser.add_argument(
        "--dram-energy-model",
        choices=cellar.DRAM_ENERGY_MODELS,
        default="legacy",
        help="legacy uses command counts; trace-based replays TraceRecorder command intervals for DRAM active, standby, and PIM energy.",
    )
    parser.add_argument(
        "--energy-scope",
        choices=["dram", "system"],
        default="dram",
        help="dram keeps only ACT/RD/WR/PIM/standby DRAM terms; system also includes DQ IO, controller/PHY, and trace-relevant PNM terms.",
    )
    parser.add_argument(
        "--pcie-bits",
        type=int,
        default=0,
        help="Optional PCIe traffic bits to include in --energy-scope system. GEMV traces do not encode host PCIe traffic, so the default is 0.",
    )
    parser.add_argument(
        "--include-cellar-llm-overhead",
        action="store_true",
        help="For --energy-scope system, also include Cellar's synthetic RMSNorm/Softmax/RotEmbed PNM dynamic estimates.",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Reuse a complete shared raw artifact instead of rerunning Ramulator.",
    )
    parser.add_argument("--no-plot", action="store_true", help="Skip matplotlib plot generation.")
    args = parser.parse_args()

    args.lpddr4_nccd_values = parse_nccd_values(args.nccd_values)
    has_matplotlib = False if args.no_plot else matplotlib_available()
    rows = build_rows(args)
    groups = energy_component_groups(args.energy_scope, args.dram_energy_model, args.include_cellar_llm_overhead)
    output_stem = f"gemv_{args.energy_scope}_energy_breakdown"
    power_stem = f"gemv_{args.energy_scope}_power_breakdown"
    if args.dram_energy_model != "legacy":
        output_stem = f"gemv_{args.energy_scope}_{args.dram_energy_model}_energy_breakdown"
        power_stem = f"gemv_{args.energy_scope}_{args.dram_energy_model}_power_breakdown"
    csv_path = args.output_dir / f"{output_stem}.csv"
    write_csv(rows, csv_path, groups)
    print(f"[csv] {display_path(csv_path, root)}")

    if not args.no_plot and has_matplotlib:
        energy_plot_path = args.output_dir / f"{output_stem}.png"
        power_plot_path = args.output_dir / f"{power_stem}.png"
        write_energy_plot(rows, energy_plot_path, args.lpddr4_nccd_values, args.energy_scope, args.dram_energy_model, groups)
        write_power_plot(rows, power_plot_path, args.lpddr4_nccd_values, args.energy_scope, args.dram_energy_model, groups)
        print(f"[plot] {display_path(energy_plot_path, root)}")
        print(f"[plot] {display_path(power_plot_path, root)}")
    elif not args.no_plot:
        energy_plot_path = args.output_dir / f"{output_stem}.svg"
        power_plot_path = args.output_dir / f"{power_stem}.svg"
        write_energy_svg(rows, energy_plot_path, args.lpddr4_nccd_values, args.energy_scope, args.dram_energy_model, groups)
        write_power_svg(rows, power_plot_path, args.lpddr4_nccd_values, args.energy_scope, args.dram_energy_model, groups)
        print(f"[plot] {display_path(energy_plot_path, root)} (SVG fallback; matplotlib is not installed)")
        print(f"[plot] {display_path(power_plot_path, root)} (SVG fallback; matplotlib is not installed)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
