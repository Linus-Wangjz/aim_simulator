"""Shared Ramulator execution, artifact storage, and result parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

from .models import PIM_TCCD_TIMING_KEY, REQUIRED_TIMING_KEYS
from .workloads import GemvWorkload


RESULT_STAT_RE = re.compile(
    r"^\s*([^:\s]+):\s+(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(?:\s+#.*)?\s*$"
)
COMMAND_TRACE_RE = re.compile(r"^\s*(\d+)\s*,\s*([^,]+)\s*,?(.*)$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def display_path(path: Path, root: Path | None = None) -> str:
    root = root or repo_root()
    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


def parse_nccd_values(raw: str) -> list[int]:
    values = [int(value.strip()) for value in raw.split(",") if value.strip()]
    if not values:
        raise ValueError("--nccd-values must contain at least one integer")
    if values != sorted(set(values)):
        raise ValueError("--nccd-values must be unique and sorted ascending")
    return values


def run_stem(workload_name: str, dram: str, nccd: int | None = None) -> str:
    if dram == "gddr6":
        return f"output_{workload_name}_gddr6"
    if dram == "lpddr4" and nccd is not None:
        return f"output_{workload_name}_lpddr4_nCCD{nccd}"
    raise ValueError(f"Invalid GEMV run identity: dram={dram}, nCCD={nccd}")


def dram_name(
    dram: str,
    nccd: int | None = None,
    power_impl: str | None = None,
    show_nccd: bool = True,
) -> str:
    if dram == "gddr6":
        return "GDDR6"
    if dram == "lpddr4" and nccd is not None:
        base = power_impl or "LPDDR4"
        return f"{base}_nCCD{nccd}" if show_nccd else base
    raise ValueError(f"Invalid GEMV DRAM label: dram={dram}, nCCD={nccd}")


def dram_label(name: str) -> str:
    if name == "GDDR6":
        return "G6"
    if "_nCCD" not in name:
        return name
    base, nccd = name.split("_nCCD", 1)
    abbreviations = {"LPDDR4": "LP4", "LPDDR4X": "LP4X"}
    return f"{abbreviations.get(base, base)}\nn{nccd}"


def dram_sort_key(name: str) -> tuple[int, int]:
    if name == "GDDR6":
        return (0, 0)
    if "_nCCD" not in name:
        return (3, 0)
    base, nccd = name.split("_nCCD", 1)
    base_order = {"LPDDR4": 1, "LPDDR4X": 2}
    return (base_order.get(base, 3), int(nccd))


def read_result_stats(path: Path) -> dict[str, float]:
    stats: dict[str, float] = {}
    for line in path.read_text().splitlines():
        match = RESULT_STAT_RE.match(line)
        if match:
            stats[match.group(1)] = float(match.group(2))
    if not stats:
        raise ValueError(f"No Ramulator statistics found in {path}")
    return stats


def result_cycles(stats: dict[str, float]) -> int:
    try:
        return int(stats["memory_system_cycles"])
    except KeyError as error:
        raise ValueError("Ramulator result does not contain memory_system_cycles") from error


def channel_active_cycles(stats: dict[str, float]) -> list[int]:
    values = [
        int(value)
        for key, value in stats.items()
        if key.startswith("CH") and key.endswith("_active_cycles")
    ]
    if not values:
        raise ValueError("Ramulator result does not contain CH*_active_cycles")
    return values


def sum_stats_with_suffix(stats: dict[str, float], suffix: str, default: float = 0.0) -> float:
    """Sum per-channel statistics sharing a Ramulator result suffix."""
    values = [value for key, value in stats.items() if key.endswith(suffix)]
    return sum(values) if values else default


def command_count(stats: dict[str, float], command: str) -> float:
    suffix = f"num_{command}_commands"
    return sum(value for key, value in stats.items() if key.endswith(suffix))


def isr_count(stats: dict[str, float], isr: str) -> float:
    suffix = f"total_num_AiM_ISR_{isr}_requests"
    return sum(value for key, value in stats.items() if key.endswith(suffix))


@dataclass(frozen=True)
class CommandEvent:
    clock: int
    command: str
    address: tuple[int, ...]


def parse_command_trace(path: Path) -> list[CommandEvent]:
    events: list[CommandEvent] = []
    for line in path.read_text().splitlines():
        match = COMMAND_TRACE_RE.match(line)
        if not match:
            continue
        address_text = match.group(3).strip()
        try:
            address = tuple(int(value.strip()) for value in address_text.split(",") if value.strip())
        except ValueError as error:
            raise ValueError(f"Invalid TraceRecorder entry in {path}: {line}") from error
        events.append(CommandEvent(int(match.group(1)), match.group(2).strip(), address))
    if not events:
        raise ValueError(f"No issued commands found in {path}")
    return events


def command_trace_files(trace_prefix: Path | str) -> list[Path]:
    trace_prefix = Path(trace_prefix)
    def channel_index(path: Path) -> int:
        match = re.search(r"\.ch(\d+)$", path.name)
        return int(match.group(1)) if match else -1

    return sorted(
        trace_prefix.parent.glob(f"{trace_prefix.name}.ch*"),
        key=channel_index,
    )


@dataclass(frozen=True)
class RunArtifacts:
    stem: str
    result: Path
    timing: Path
    command_trace_prefix: Path

    def command_trace_files(self) -> list[Path]:
        return command_trace_files(self.command_trace_prefix)

    def is_complete(self) -> bool:
        return self.result.exists() and self.timing.exists() and bool(self.command_trace_files())


class RamulatorArtifactStore:
    def __init__(self, root: Path):
        self.root = root
        self.results_dir = root / "results"
        self.timing_dir = root / "timing"
        self.command_traces_dir = root / "command_traces"

    def artifacts(self, workload_name: str, dram: str, nccd: int | None = None) -> RunArtifacts:
        stem = run_stem(workload_name, dram, nccd)
        return RunArtifacts(
            stem=stem,
            result=self.results_dir / f"{stem}.result",
            timing=self.timing_dir / f"{stem}.timing.yaml",
            command_trace_prefix=self.command_traces_dir / f"{stem}.cmd",
        )

    def create_directories(self) -> None:
        for path in (self.results_dir, self.timing_dir, self.command_traces_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_resolved_timing(timing_path: Path) -> tuple[str, dict[str, float]]:
    try:
        document = yaml.safe_load(timing_path.read_text())
    except FileNotFoundError as error:
        raise FileNotFoundError(
            f"Resolved timing export not found: {timing_path}. "
            "Run Ramulator with the DRAMTimingExporter plugin."
        ) from error

    if not isinstance(document, dict) or not isinstance(document.get("timing"), dict):
        raise ValueError(f"Invalid DRAMTimingExporter YAML: {timing_path}")

    dram_impl = document.get("impl")
    timing = document["timing"]
    if dram_impl not in PIM_TCCD_TIMING_KEY:
        raise ValueError(f"Unsupported DRAM impl '{dram_impl}' in {timing_path}")

    required_keys = (*REQUIRED_TIMING_KEYS, PIM_TCCD_TIMING_KEY[dram_impl])
    missing = [key for key in required_keys if key not in timing]
    if missing:
        raise ValueError(f"Timing export {timing_path} is missing: {', '.join(missing)}")
    return dram_impl, {key: float(value) for key, value in timing.items()}


def write_ramulator_config(
    base_yaml: Path,
    dst: Path,
    artifacts: RunArtifacts,
    nccd: int | None = None,
) -> None:
    lines = base_yaml.read_text().splitlines(keepends=True)
    output: list[str] = []
    inserted_nccd = nccd is None
    inserted_plugins = False

    for line in lines:
        stripped = line.strip()
        if nccd is not None and stripped.startswith("nCCD:"):
            continue
        output.append(line)
        if nccd is not None and not inserted_nccd and "preset:" in stripped and "LPDDR4_AiM_timing" in stripped:
            indent = line[: len(line) - len(line.lstrip())]
            output.append(f"{indent}nCCD: {nccd}\n")
            inserted_nccd = True
        if stripped == "plugins:":
            indent = line[: len(line) - len(line.lstrip())]
            output.extend(
                [
                    f"{indent}  - ControllerPlugin:\n",
                    f"{indent}      impl: DRAMTimingExporter\n",
                    f"{indent}      path: {artifacts.timing}\n",
                    f"{indent}  - ControllerPlugin:\n",
                    f"{indent}      impl: TraceRecorder\n",
                    f"{indent}      path: {artifacts.command_trace_prefix}\n",
                ]
            )
            inserted_plugins = True

    if not inserted_nccd:
        raise RuntimeError(f"Could not find LPDDR4_AiM_timing preset in {base_yaml}")
    if not inserted_plugins:
        raise RuntimeError(f"Could not find Controller plugins section in {base_yaml}")
    dst.write_text("".join(output))


class GemvRamulatorRunner:
    """Executes GEMV configurations and stores reusable raw artifacts."""

    def __init__(
        self,
        root: Path,
        ramulator: Path,
        gddr6_yaml: Path,
        lpddr4_yaml: Path,
        artifact_store: RamulatorArtifactStore,
        reuse_existing: bool,
    ):
        self.root = root
        self.ramulator = ramulator
        self.gddr6_yaml = gddr6_yaml
        self.lpddr4_yaml = lpddr4_yaml
        self.artifact_store = artifact_store
        self.reuse_existing = reuse_existing

    def ensure(self, workload: GemvWorkload, dram: str, nccd: int | None = None) -> RunArtifacts:
        artifacts = self.artifact_store.artifacts(workload.name, dram, nccd)
        if self.reuse_existing and artifacts.is_complete():
            print(f"[reuse] {display_path(artifacts.result, self.root)}")
            return artifacts

        if not self.ramulator.exists():
            raise FileNotFoundError(f"ramulator binary not found: {self.ramulator}")
        self.artifact_store.create_directories()
        source_yaml = self.gddr6_yaml if dram == "gddr6" else self.lpddr4_yaml
        trace = self.root / workload.trace_for(dram)
        with tempfile.TemporaryDirectory(prefix="ramulator_config_", dir=self.root / "output") as tmp:
            config = Path(tmp) / f"{artifacts.stem}.yaml"
            write_ramulator_config(source_yaml, config, artifacts, nccd)
            print(f"[run] {dram_name(dram, nccd)} {workload.name}: {display_path(trace, self.root)}")
            with artifacts.result.open("w") as output:
                process = subprocess.run(
                    [str(self.ramulator), "-f", str(config), "-t", str(trace)],
                    cwd=self.root,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
        if process.returncode != 0:
            raise RuntimeError(f"Ramulator failed ({process.returncode}); see {artifacts.result}")
        if not artifacts.is_complete():
            raise RuntimeError(f"Ramulator did not create complete artifacts for {artifacts.stem}")
        return artifacts
