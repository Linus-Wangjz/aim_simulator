#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

RAMULATOR="${REPO_ROOT}/build/ramulator2"
OUT_DIR="${REPO_ROOT}/output"

if [[ ! -x "${RAMULATOR}" ]]; then
  echo "Error: ${RAMULATOR} not found or not executable. Build first with: cmake --build build" >&2
  exit 1
fi

mkdir -p "${OUT_DIR}"

traces=(
  micro_nccd_sbk
  micro_nccd_same_bg
  micro_nccd_diff_bg
  micro_nrrd_8banks
  micro_nrc_sbk
  micro_nrc_abk
  micro_macab_256
)

configs=(
  "lpddr4:test/example_LPDDR4.yaml"
  "gddr6:test/example_GDDR6.yaml"
)

for trace in "${traces[@]}"; do
  trace_path="${REPO_ROOT}/test/${trace}.trace"
  if [[ ! -f "${trace_path}" ]]; then
    echo "Error: trace file not found: ${trace_path}" >&2
    exit 1
  fi

  output_base="${trace/#micro/output}"

  for config in "${configs[@]}"; do
    dram="${config%%:*}"
    config_path="${REPO_ROOT}/${config#*:}"
    out_path="${OUT_DIR}/${output_base}_${dram}.txt"

    if [[ ! -f "${config_path}" ]]; then
      echo "Error: config file not found: ${config_path}" >&2
      exit 1
    fi

    echo "Running ${trace}.trace with ${dram} -> ${out_path}"
    "${RAMULATOR}" -f "${config_path}" -t "${trace_path}" > "${out_path}"
  done
done

echo "Done. Results are in ${OUT_DIR}"
