# AiM Analysis Scripts

## Shared Modules

`scripts/aim_analysis/` contains definitions shared by all analysis entry
points:

- `commands.py`: GDDR6/LPDDR4 command sets, command classification, and
  command-transition timing-constraint attribution.
- `models.py`: GDDR6, LPDDR4, and LPDDR4X DRAM power models.
- `workloads.py`: GEMV size-to-trace mapping.
- `ramulator.py`: temporary YAML generation, reusable Ramulator artifacts,
  timing YAML parsing, command-trace parsing, and result-stat parsing.
- `runtime.py`: shared runtime setup, currently the matplotlib cache path.

## Shared Raw Cache

All three GEMV analysis scripts use the same raw Ramulator cache:

```text
output/ramulator/
  results/         # Ramulator stdout (.result)
  timing/          # DRAMTimingExporter YAML
  command_traces/  # TraceRecorder files, one .chN file per channel
```

Each cache entry is identified by workload, DRAM type, and LPDDR4 nCCD. A
cache entry is reusable only when its `.result`, timing YAML, and at least one
command trace all exist.

The GEMV scripts share these parameters:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `--ramulator PATH` | `build/ramulator2` | Ramulator executable used when a run is needed. |
| `--gddr6-yaml PATH` | `test/example_GDDR6.yaml` | Base GDDR6 configuration. |
| `--lpddr4-yaml PATH` | `test/example_LPDDR4.yaml` | Base LPDDR4 configuration. |
| `--ramulator-output-dir PATH` | `output/ramulator` | Shared raw-artifact cache root. |
| `--reuse-existing` | off | Use complete cached artifacts and do not rerun Ramulator for them. |
| `--no-plot` | off | Produce CSV data only; do not create matplotlib figures. |

Without `--reuse-existing`, the requested configurations are run again and
their artifacts overwrite the corresponding shared cache entries.

## `sweep_nccd_gemv.py`

Sweeps LPDDR4 nCCD and plots the LPDDR4/GDDR6 cycle ratio for the GEMV traces
defined in `aim_analysis/workloads.py`. CSV and plot outputs go to
`output/nccd_sweep_gemv/` by default.

```bash
python scripts/sweep_nccd_gemv.py --reuse-existing
python scripts/sweep_nccd_gemv.py --nccd-values 2,4,6,8 --metric memory_system
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `--output-dir PATH` | `output/nccd_sweep_gemv` | Directory for the sweep CSV and PNG. |
| `--nccd-values LIST` | `2,4,6,8` | Comma-separated, unique, ascending LPDDR4 nCCD values. |
| `--gddr6-nccd N` | `2` | GDDR6 baseline nCCD used to form the horizontal-axis ratio. |
| `--metric NAME` | `channel_active` | `channel_active` uses the maximum `CHx_active_cycles`; `memory_system` uses `memory_system_cycles`. |

The CSV records both cycle metrics even when only one is selected for the
ratio plot. The script checks that the selected LPDDR4 metric increases as
nCCD increases.

## `plot_gemv_cycle_breakdown.py`

Builds command-component and timing-constraint cycle breakdowns from the
critical channel's issued-command trace. Outputs are written to
`output/gemv_cycle_breakdown/` by default.

```bash
python scripts/plot_gemv_cycle_breakdown.py --reuse-existing
python scripts/plot_gemv_cycle_breakdown.py --lpddr4-nccd 6 --reuse-existing
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `--output-dir PATH` | `output/gemv_cycle_breakdown` | Directory for CSV files and plots. |
| `--lpddr4-nccd-values LIST` | `2,6` | Comma-separated LPDDR4 nCCD values included beside GDDR6. |
| `--lpddr4-nccd N` | unset | Select exactly one LPDDR4 nCCD; overrides `--lpddr4-nccd-values`. |

Generated data includes `gemv_cycle_breakdown.csv`, transition-level data,
the transition-to-constraint map, and constraint-attribution data. The two
PNG files combine absolute-cycle and percentage panels with a shared X-axis
and legend.

## `plot_gemv_energy_breakdown.py`

Calculates GEMV energy and average power using the shared cache plus
`cellar_power_calculator.py`. LPDDR4 timing is simulated while LPDDR4X DRAM
power rails are used for the LPDDR bars. Outputs are written to
`output/gemv_dram_energy_breakdown/` by default.

```bash
python scripts/plot_gemv_energy_breakdown.py --reuse-existing
python scripts/plot_gemv_energy_breakdown.py \
  --dram-energy-model trace-based --energy-scope system --reuse-existing
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `--output-dir PATH` | `output/gemv_dram_energy_breakdown` | Directory for energy CSV and plots. |
| `--nccd-values LIST` | `2,6` | Comma-separated LPDDR4 timing nCCD values. |
| `--ch-per-dv N` | `32` | Channels represented by one device in system-level estimates. |
| `--dram-energy-model NAME` | `legacy` | `legacy` estimates dynamic DRAM energy from command counts; `trace-based` replays actual command intervals from TraceRecorder for ACT/PRE, RD/WR, PIM, and standby energy. In both models, ACT4/ACT8/ACT16 are charged as 4/8/16 single-bank ACT events. |
| `--energy-scope NAME` | `dram` | `dram` includes DRAM components only; `system` additionally includes DQ, controller/PHY, and trace-relevant PNM components. |
| `--pcie-bits N` | `0` | Optional host PCIe traffic included only in system scope. GEMV traces do not encode this traffic. |
| `--include-cellar-llm-overhead` | off | In system scope, include Cellar's synthetic RMSNorm, Softmax, and RotEmbed PNM dynamic terms. |

For `trace-based`, the shared cache must contain command traces. The common
runner always records them, so a normal cached GEMV run is sufficient.

## `cellar_power_calculator.py`

This lower-level calculator consumes one or two existing Ramulator runs. It
can read the shared cache directly, or any result/timing/trace triplet created
with `DRAMTimingExporter` and `TraceRecorder`.

```bash
python scripts/cellar_power_calculator.py \
  --mlog output/ramulator/results/output_gemv_256x256_gddr6.result \
  --mtiming output/ramulator/timing/output_gemv_256x256_gddr6.timing.yaml \
  --mcmd-trace output/ramulator/command_traces/output_gemv_256x256_gddr6.cmd \
  --dram-energy-model trace-based \
  --head 1 --hidden 256 --fc 256 --token 1 --block 1 --ch_per_bl 32
```

| Parameter | Required | Meaning |
| --- | --- | --- |
| `--mlog PATH` | yes | Main Ramulator result file. |
| `--mtiming PATH` | yes | Matching `DRAMTimingExporter` timing YAML. |
| `--mcmd-trace PREFIX` | trace-based model | Matching `TraceRecorder` prefix, without `.chN`. |
| `--plog PATH`, `--ptiming PATH` | when `ch_per_bl > ch_per_dv` | Result and timing YAML for the additional PIM stage. |
| `--pcmd-trace PREFIX` | trace-based model with additional PIM stage | Trace prefix for `--plog`. |
| `--dram-energy-model NAME` | no | `legacy` or `trace-based`; defaults to `legacy`. |
| `--head N`, `--hidden N`, `--fc N`, `--token N` | yes | Workload dimensions used by Cellar's accelerator-side estimates. |
| `--block N` | yes | Number of blocks. |
| `--ch_per_bl N` | yes | Channels used by one block. |
| `--dv N` | no, `32` | Total device count. |
| `--ch_per_dv N` | no, `32` | Channels per device. |
| `--gqa N` | no, `1` | Group-query-attention factor. |

When `ch_per_bl <= ch_per_dv`, only the main run is needed. When
`ch_per_bl > ch_per_dv`, `--plog` and `--ptiming` are required, and the
trace-based model additionally requires `--pcmd-trace`.
