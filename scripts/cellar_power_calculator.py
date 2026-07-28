import argparse
import math
from pathlib import Path

from aim_analysis.commands import COMMANDS as commands, ISR_NAMES as isrs
from aim_analysis.models import PIM_POWER_SCALE, PIM_TCCD_TIMING_KEY, dram_power_for_impl
from aim_analysis.ramulator import (
    command_count,
    command_trace_files,
    isr_count,
    last_stat_with_suffix,
    load_resolved_timing,
    parse_command_trace,
    read_result_stats,
    result_cycles,
)

KILO = 1000
MEGA = 1000000
GIGA = 1000000000

CH_PER_DV = 32.00

DRAM_ENERGY_MODELS = ("legacy", "improved")

# RED: Reduction Tree
# EXP: Exponent
# VEC: Vector Add
# SFT: Softmax
# CTR: PIMDispatcher + CXLController + Decoder + pim_ld_st + accel_controller
# DYN: Dynamic
# STT: Static
# Power in mW
# TODO: Change SFT
ACCEL_POWER = { "RED": {"SWITCH": 8.01e-03, "INT": 1.017, "LEAK": 3.34e+04},
                "EXP": {"SWITCH": 3.41e-02, "INT": 3.231, "LEAK": 7.99e+04},
                "VEC": {"SWITCH": 1.44e-02, "INT": 1.070, "LEAK": 4.31e+04},
                "CTR": {"SWITCH": 9.00e-03 + 1.65e-02 + 1.40e-02 + 0.283 + 6.26e-04, "INT": 0.216 + 1.157 + 0.220 + 32.535 + 4.28e-02, "LEAK": 2.03e+03 + 9.01e+03 + 1.82e+03 + 2.54e+05 + 540.653},
                "RV": 41}
for accel_name in ["RED", "EXP", "VEC", "CTR"]:
    ACCEL_POWER[accel_name]["DYN"] = float(ACCEL_POWER[accel_name]["SWITCH"] + ACCEL_POWER[accel_name]["INT"])
    ACCEL_POWER[accel_name]["STT"] = float(ACCEL_POWER[accel_name]["LEAK"]) / float(GIGA)

RV_COUNT = 8
# Latency of 1 SIMD operation
SB_RD_CYCLE = 1.00
SB_WR_CYCLE = 1.00
EXP_LANE_CYCLE = 11.00
RV_RMSNorm_CYCLE = 26.00
RV_ROTEmbed_CYCLE = 3.00 / RV_COUNT
RV_SFT_CYCLE_PIPELINE = 16.00 * SB_WR_CYCLE + 2.00 / RV_COUNT + 1.00 * SB_RD_CYCLE
RV_SFT_CYCLE_SINGLE = 16.00 * SB_WR_CYCLE + 2.00 + 1.00 * SB_RD_CYCLE

# latency of pipelining 32 accelerators
# each having 16 SIMD lanes
ACCEL_CYCLE = { "EXP": CH_PER_DV * SB_RD_CYCLE + EXP_LANE_CYCLE + SB_WR_CYCLE,
                "VEC": CH_PER_DV * 2.00 * SB_RD_CYCLE + 1.00 + SB_WR_CYCLE}

# GB: Global Buffer
# SB: Shared Buffer
# STT: Static
# RD: Read
# WR: Write
SRAM_POWER = {  "GB": {"STT": 0.06702101898, "RD": 0.2785010052, "WR": 0.3254884575},
                "SB": {"STT": 0.6917736525, "RD": 3.207188769, "WR": 3.754155771},
                "IB": {"STT": 18.81731768, "RD": 70.13266856, "WR": 92.43730523}}

# TRX: Transaction Engine
# PHY: Physical Interface
# TODO: make sure PHY is not DQ
CTRL_POWER = {  "TRX": 267.7082056,
                "PHY": 381.0445262}
CH_PER_CTRL = 2.00

# pJ/bit
DQ_ENERGY = 5.5
PCIE_ENERGY = 4.4

WORD_SIZE = 256


def command_processor(stat_path, timing_path):
    result = read_result_stats(Path(stat_path))
    timing_impl, timing = load_resolved_timing(Path(timing_path))
    stat = {command: command_count(result, command) for command in commands}
    stat.update({isr: isr_count(result, isr) for isr in isrs})
    stat["cycles"] = float(result_cycles(result))
    stat["idle_cycles"] = last_stat_with_suffix(result, "_idle_cycles")
    stat["active_cycles"] = last_stat_with_suffix(result, "_active_cycles")
    stat["precharged_cycles"] = last_stat_with_suffix(result, "_precharged_cycles")
    stat["dram_impl"] = timing_impl
    stat["timing"] = timing

    stat["tCK_ps"] = timing["tCK_ps"]
    stat["tRC_ns"] = timing["nRC"] * stat["tCK_ps"] / KILO
    stat["tBL_ns"] = timing["nBL"] * stat["tCK_ps"] / KILO
    stat["pim_tccd_cycles"] = timing[PIM_TCCD_TIMING_KEY[timing_impl]]
    stat["pim_tccd_ns"] = stat["pim_tccd_cycles"] * stat["tCK_ps"] / KILO

    # ms (average of all channels)
    stat["latency"] = stat["cycles"] * stat["tCK_ps"] / GIGA
    # ms (average of all channels)
    stat["active_latency"] = stat["active_cycles"] / CH_PER_DV * stat["tCK_ps"] / GIGA
    # ms (average of all channels)
    stat["precharged_latency"] = stat["precharged_cycles"] / CH_PER_DV * stat["tCK_ps"] / GIGA
    # % (average of all channels)
    stat["utilization"] = 100.00 - (stat["idle_cycles"] / CH_PER_DV / stat["cycles"]) * 100.00
    return stat


def improved_trace_stats(trace_prefix, dram_impl, memory_system_cycles):
    trace_paths = command_trace_files(trace_prefix)
    if not trace_paths:
        raise FileNotFoundError(
            f"No TraceRecorder files found for {trace_prefix}. "
            "Run Ramulator with the TraceRecorder plugin."
        )

    all_bank_count = 16 if dram_impl == "GDDR6" else 8
    bankgroup_index = 1 if dram_impl == "GDDR6" else 2
    bank_index = 2 if dram_impl == "GDDR6" else 3
    totals = {
        "ACT_STBY_cycles": 0.00,
        "PRE_STBY_cycles": 0.00,
        "ACT_cycles": 0.00,
        "PRE_cycles": 0.00,
        "RD_cycles": 0.00,
        "WR_cycles": 0.00,
        "PIM_cycles": 0.00,
    }

    def account_interval(cycles, open_banks, previous_activities):
        if cycles < 0:
            raise ValueError(f"Command trace is not monotonic: {trace_prefix}")
        if open_banks:
            totals["ACT_STBY_cycles"] += cycles
        else:
            totals["PRE_STBY_cycles"] += cycles
        for activity, scale in previous_activities:
            totals[f"{activity}_cycles"] += scale * cycles

    for path in trace_paths:
        issued = parse_command_trace(path)
        if not issued:
            continue

        open_banks = 0
        previous_clock = 0
        previous_activities = []
        for event in issued:
            clock = event.clock
            command = event.command
            addr = event.address
            account_interval(clock - previous_clock, open_banks, previous_activities)
            activities = []

            if command == "ACT16" or command == "ACT8-2":
                # Temporary model: an all-bank ACT is one ACT-energy event.
                activities.append(("ACT", 1.00))
                open_banks = (1 << all_bank_count) - 1
            elif command == "ACT4" or command == "ACT4-2":
                activities.append(("ACT", 1.00))
                bankgroup = addr[bankgroup_index]
                for bank in range(bankgroup * 4, (bankgroup + 1) * 4):
                    open_banks |= 1 << bank
            elif command == "ACT" or command == "ACT-2":
                activities.append(("ACT", 1.00))
                open_banks |= 1 << addr[bank_index]
            elif command in {"PREA", "WRA16", "WRA8"}:
                activities.append(("PRE", all_bank_count))
                open_banks = 0
            elif command == "PRE4":
                activities.append(("PRE", 4.00))
                bankgroup = addr[bankgroup_index]
                for bank in range(bankgroup * 4, (bankgroup + 1) * 4):
                    open_banks &= ~(1 << bank)
            elif command in {"PRE", "RDA", "WRA"}:
                activities.append(("PRE", 1.00))
                open_banks &= ~(1 << addr[bank_index])
            elif command == "REFab":
                open_banks = 0

            if command in {"RD", "RDA", "RDCP", "RDMAC16", "RDAF16", "RDMAC8", "RDAF8"}:
                activities.append(("RD", 1.00))
            elif command in {"AF16", "AF8"}:
                activities.append(("RD", all_bank_count))

            if command in {"WR", "WRA", "WRCP", "WRMAC16", "WRMAC8"}:
                activities.append(("WR", 1.00))
            elif command in {"WRA16", "WRA8"}:
                activities.append(("WR", all_bank_count))

            if command in {"MAC", "MAC16", "MAC8"}:
                activities.append(("PIM", 1.00))

            previous_clock = clock
            previous_activities = activities

        account_interval(memory_system_cycles - previous_clock, open_banks, previous_activities)

    return totals


def improved_dram_energy(stat, dram_power, command_trace_prefix):
    trace_stats = improved_trace_stats(command_trace_prefix, stat["dram_impl"], stat["cycles"])
    tCK_ps = stat["tCK_ps"]
    pim_power_scale = PIM_POWER_SCALE[stat["dram_impl"]]
    pim_active_power = pim_power_scale * dram_power["RD"] / (stat["pim_tccd_cycles"] / 2.00)

    dq_commands = stat["RD"] + stat["WR"] + stat["RDA"] + stat["WRA"]
    dq_commands += stat["WRGB"] + stat["RDMAC16"] + stat["RDAF16"] + stat["WRMAC16"] + stat["WRA16"]
    dq_commands += stat["RDMAC8"] + stat["RDAF8"] + stat["WRMAC8"] + stat["WRA8"]

    energy = {
        "ACT": dram_power["ACT"] * trace_stats["ACT_cycles"] * tCK_ps / 1e12,
        "PRE": dram_power["PRE"] * trace_stats["PRE_cycles"] * tCK_ps / 1e12,
        "RD": dram_power["RD"] * trace_stats["RD_cycles"] * tCK_ps / 1e12,
        "WR": dram_power["WR"] * trace_stats["WR_cycles"] * tCK_ps / 1e12,
        "PIM": pim_active_power * trace_stats["PIM_cycles"] * tCK_ps / 1e12,
        "ACT_STBY": dram_power["ACT_STBY"] * trace_stats["ACT_STBY_cycles"] * tCK_ps / 1e12,
        "PRE_STBY": dram_power["PRE_STBY"] * trace_stats["PRE_STBY_cycles"] * tCK_ps / 1e12,
        "DQ": DQ_ENERGY * WORD_SIZE * dq_commands / GIGA,
        "PCIe": 0.00,
    }
    stat["improved_trace_stats"] = trace_stats
    return energy


def power_calculator(
    stat,
    PCIE_bits,
    Head,
    HiddenDim,
    Tokens,
    GQA,
    dram_power_impl=None,
    dram_energy_model="legacy",
    command_trace_prefix=None,
):
    energy = {}
    latency = {}
    timing_impl = stat["dram_impl"]
    dram_power = dram_power_for_impl(dram_power_impl or timing_impl)
    tCK_ps = stat["tCK_ps"]
    tRC_ns = stat["tRC_ns"]
    tBL_ns = stat["tBL_ns"]

    if dram_energy_model not in DRAM_ENERGY_MODELS:
        raise ValueError(f"Unknown DRAM energy model '{dram_energy_model}'. Expected one of: {', '.join(DRAM_ENERGY_MODELS)}")
    if dram_energy_model == "improved":
        if command_trace_prefix is None:
            raise ValueError("The improved DRAM energy model requires a TraceRecorder command-trace prefix")
        energy.update(improved_dram_energy(stat, dram_power, command_trace_prefix))
        energy["PCIe"] = PCIE_bits * PCIE_ENERGY / GIGA

    # LPDDR4 splits activation into ACT*-1/ACT*-2. Only ACT*-2 is marked as
    # the actual opening command.
    elif timing_impl in {"LPDDR4", "LPDDR4X"}:
        all_bank_count = 8.00
        act_equiv = stat["ACT-2"]
        act_equiv += stat["ACT4-2"]
        act_equiv += stat["ACT8-2"]

        rd_data_commands = stat["RDCP"] + stat["RD"] + stat["RDA"]
        rd_data_commands += all_bank_count * stat["AF8"] + stat["RDMAC8"] + stat["RDAF8"]

        wr_data_commands = stat["WRCP"] + stat["WR"] + stat["WRA"]
        wr_data_commands += stat["WRMAC8"] + all_bank_count * stat["WRA8"]

        pim_commands = stat["MAC"] / all_bank_count
        pim_commands += stat["MAC8"]
        pim_commands += (stat["EWMUL16"] + stat["EWMUL8"]) / 4.00
        pim_cycle_ns = stat["pim_tccd_ns"]
        pim_active_power = PIM_POWER_SCALE[timing_impl] * dram_power["RD"] / (stat["pim_tccd_cycles"] / 2.00)

        dq_commands = stat["RD"] + stat["WR"] + stat["RDA"] + stat["WRA"]
        dq_commands += stat["WRGB"] + stat["RDMAC16"] + stat["RDAF16"] + stat["WRMAC16"] + stat["WRA16"]
        dq_commands += stat["RDMAC8"] + stat["RDAF8"] + stat["WRMAC8"] + stat["WRA8"]

        energy["ACT/PRE"] = dram_power["ACT"] * act_equiv * tRC_ns / GIGA
        energy["RD"] = dram_power["RD"] * rd_data_commands * tBL_ns / GIGA
        energy["WR"] = dram_power["WR"] * wr_data_commands * tBL_ns / GIGA
        energy["PIM"] = pim_active_power * pim_commands * pim_cycle_ns / GIGA
        energy["ACT_STBY"] = dram_power["ACT_STBY"] * CH_PER_DV * stat["active_latency"] / KILO
        energy["PRE_STBY"] = dram_power["PRE_STBY"] * CH_PER_DV * stat["precharged_latency"] / KILO
        energy["DQ"] = DQ_ENERGY * WORD_SIZE * dq_commands / GIGA
        energy["PCIe"] = PCIE_bits * PCIE_ENERGY / GIGA

    elif timing_impl == "GDDR6":
        all_bank_count = 16.00
        act_equiv = stat["ACT"]
        act_equiv += stat["ACT4"]
        act_equiv += stat["ACT16"]

        rd_data_commands = stat["RDCP"] + stat["RD"] + stat["RDA"]
        rd_data_commands += all_bank_count * stat["AF16"] + stat["RDMAC16"] + stat["RDAF16"]

        wr_data_commands = stat["WRCP"] + stat["WR"] + stat["WRA"]
        wr_data_commands += stat["WRMAC16"] + all_bank_count * stat["WRA16"]

        pim_commands = stat["MAC"] / all_bank_count
        pim_commands += stat["MAC16"]
        pim_commands += (stat["EWMUL16"] + stat["EWMUL8"]) / 4.00
        pim_cycle_ns = stat["pim_tccd_ns"]
        pim_active_power = PIM_POWER_SCALE[timing_impl] * dram_power["RD"] / (stat["pim_tccd_cycles"] / 2.00)

        dq_commands = stat["RD"] + stat["WR"] + stat["RDA"] + stat["WRA"]
        dq_commands += stat["WRGB"] + stat["RDMAC16"] + stat["RDAF16"] + stat["WRMAC16"] + stat["WRA16"]
        dq_commands += stat["RDMAC8"] + stat["RDAF8"] + stat["WRMAC8"] + stat["WRA8"]

        energy["ACT/PRE"] = dram_power["ACT"] * act_equiv * tRC_ns / GIGA
        energy["RD"] = dram_power["RD"] * rd_data_commands * tBL_ns / GIGA
        energy["WR"] = dram_power["WR"] * wr_data_commands * tBL_ns / GIGA
        energy["PIM"] = pim_active_power * pim_commands * pim_cycle_ns / GIGA
        energy["ACT_STBY"] = dram_power["ACT_STBY"] * CH_PER_DV * stat["active_latency"] / KILO
        energy["PRE_STBY"] = dram_power["PRE_STBY"] * CH_PER_DV * stat["precharged_latency"] / KILO
        energy["DQ"] = DQ_ENERGY * WORD_SIZE * dq_commands / GIGA
        energy["PCIe"] = PCIE_bits * PCIE_ENERGY / GIGA
    else:
        raise ValueError(f"Unknown DRAM impl '{timing_impl}'. Expected timing impl GDDR6 or LPDDR4")

    ISR_COUNT = stat["RD"] + stat["WR"] + stat["RDA"] + stat["WRA"]
    ISR_COUNT += stat["MAC"] + stat["MAC16"] + stat["MAC8"]
    ISR_COUNT += stat["AF16"] + stat["AF8"] + stat["EWMUL16"] + stat["EWMUL8"]
    ISR_COUNT += stat["RDCP"] + stat["WRCP"] + stat["WRGB"]
    ISR_COUNT += stat["RDMAC16"] + stat["RDAF16"] + stat["WRMAC16"] + stat["WRA16"]
    ISR_COUNT += stat["RDMAC8"] + stat["RDAF8"] + stat["WRMAC8"] + stat["WRA8"]
    CMD_COUNT = sum(stat[x] for x in commands)
    energy["MEM_CTR"] = (CTRL_POWER["TRX"] * ISR_COUNT + CTRL_POWER["PHY"] * CMD_COUNT) / CH_PER_CTRL * tCK_ps / 1e12

    GQA_factor = 1.00 + 1.00 / GQA
    latency["RMSNorm_latency"] =  HiddenDim / 16.00 / 16.00 / CH_PER_DV * ACCEL_CYCLE["VEC"]    # EMB /16.00 /16.00 ADD
    latency["RMSNorm_latency"] += SB_RD_CYCLE + SB_WR_CYCLE + 1.00                              # 1 RED
    latency["RMSNorm_latency"] += RV_RMSNorm_CYCLE                                              # 1 RISCV
    latency["RMSNorm_latency"] = 2.00 * latency["RMSNorm_latency"] * tCK_ps / GIGA
    latency["Softmax_latency"] =  Tokens * Head / 16.00 / CH_PER_DV * ACCEL_CYCLE["EXP"]        # TOK*HEAD /16.00 EXP
    latency["Softmax_latency"] += Tokens * Head / 16.00 / CH_PER_DV * ACCEL_CYCLE["VEC"]        # TOK*HEAD /16.00 ADD
    latency["Softmax_latency"] += Head * 1.00 * SB_RD_CYCLE                                     # HEAD RED
    latency["Softmax_latency"] += Head * RV_SFT_CYCLE_PIPELINE                                  # HEAD RISCV
    latency["Softmax_latency"] = latency["Softmax_latency"] * tCK_ps / GIGA
    latency["RotEmbed_latency"] = HiddenDim * RV_ROTEmbed_CYCLE                                 # EMB RISCV
    latency["RotEmbed_latency"] = GQA_factor * latency["RotEmbed_latency"] * tCK_ps / GIGA

    # Static
    energy["GB_STT"] = stat["latency"] * SRAM_POWER["GB"]["STT"] * CH_PER_DV / KILO
    energy["SB_STT"] = stat["latency"] * SRAM_POWER["SB"]["STT"] / KILO
    energy["IB_STT"] = stat["latency"] * SRAM_POWER["IB"]["STT"] / KILO
    energy["RED_STT"] = stat["latency"] * ACCEL_POWER["RED"]["STT"] * CH_PER_DV / KILO
    energy["EXP_STT"] = stat["latency"] * ACCEL_POWER["EXP"]["STT"] * CH_PER_DV / KILO
    energy["VEC_STT"] = stat["latency"] * ACCEL_POWER["VEC"]["STT"] * CH_PER_DV / KILO

    # SRAM Power
    energy["GB_RD"] = SRAM_POWER["GB"]["RD"] * stat["WRCP"] * tCK_ps / 1e12
    energy["GB_WR"] = SRAM_POWER["GB"]["WR"] * (stat["WRGB"] + stat["RDCP"]) * tCK_ps / 1e12
    energy["SB_DYN"] = 2.00 * (HiddenDim / 16.00 / 16.00 + 1.00 + 1.00) * (2.00 * SRAM_POWER["SB"]["RD"] + 1.00 * SRAM_POWER["SB"]["WR"]) * tCK_ps / 1e12    # RMSNorm: EMB /16.00 /16.00 ADD + 1 RED + 1 RV [First 16.00 is #SIMD lanes, Second is because of PU 16.00-to-1 MAC]
    energy["SB_DYN"] += (Tokens * Head / 16.00 * 3 + Head * 2.00) * SRAM_POWER["SB"]["RD"] * tCK_ps / 1e12                                                 # Softmax: TOK * HEAD / 16.00 EXP and ADD + HEAD RED
    energy["SB_DYN"] += (Tokens * Head / 16.00 * 2.00 + Head * 2.00) * SRAM_POWER["SB"]["WR"] * tCK_ps / 1e12                                              # Softmax: TOK * HEAD / 16.00 EXP and ADD + HEAD RED
    energy["SB_DYN"] += GQA_factor * HiddenDim / 16.00 * (SRAM_POWER["SB"]["RD"] + 2.00 * SRAM_POWER["SB"]["WR"]) * tCK_ps / 1e12                            # RotEmbed: EMB /16.00 RV (1 ld + 2.00 st)
    
    ISR_COUNT = 0
    for isr in isrs:
        ISR_COUNT += stat[isr]
    ISR_COUNT += 2.00 * (HiddenDim / 16.00 / 16.00 + 2.00)              # RMSNorm
    ISR_COUNT += (Tokens * Head / 16.00 * 2.00 + Head * 2.00)           # Softmax
    ISR_COUNT += GQA_factor * HiddenDim                                 # RotEmbed
    energy["IB_DYN"] = ISR_COUNT * SRAM_POWER["IB"]["RD"] * tCK_ps / 1e12

    # Accelerator Power
    energy["RV_DYN"] = 2.00 * RV_RMSNorm_CYCLE * ACCEL_POWER["RV"] * tCK_ps / 1e12                       # RMSNorm: 1 RISCV
    energy["RV_DYN"] += Head * RV_SFT_CYCLE_SINGLE * ACCEL_POWER["RV"] * tCK_ps / 1e12                   # Softmax: HEAD RISCV
    energy["RV_DYN"] += GQA_factor * HiddenDim * RV_ROTEmbed_CYCLE * ACCEL_POWER["RV"] * tCK_ps / 1e12   # RotEmbed: EMB RISCV

    energy["RED_DYN"] = 2.00 * (1.00 * ACCEL_POWER["RED"]["DYN"]) * tCK_ps / 1e12                    # 1 RED (RMSNorm)
    energy["RED_DYN"] += Head * ACCEL_POWER["RED"]["DYN"] * tCK_ps / 1e12                           # HEAD RED (Softmax)

    energy["EXP_DYN"] = Tokens * Head / 16.00 * ACCEL_POWER["EXP"]["DYN"] * tCK_ps / 1e12          # TOK*HEAD /16.00 EXP (Softmax)

    energy["VEC_DYN"] = 2.00 * HiddenDim / 16.00 / 16.00 * ACCEL_POWER["VEC"]["DYN"] * tCK_ps / 1e12     # EMB /16.00 /16.00 ADD (RMSNorm) [First 16.00 is #SIMD lanes, Second is because of PU 16.00-to-1 MAC]
    energy["VEC_DYN"] += Tokens * Head / 16.00 * ACCEL_POWER["VEC"]["DYN"] * tCK_ps / 1e12              # TOK*HEAD /16.00 ADD (Softmax)

    # We simply assume all the other components have a switching activity of 0.5
    energy["DV_CTR"] = stat["latency"] * 0.5 * (ACCEL_POWER["CTR"]["STT"] + ACCEL_POWER["CTR"]["DYN"]) / KILO

    return energy, latency

def main():
    global CH_PER_DV
    global ACCEL_CYCLE

    parser = argparse.ArgumentParser(description="Cellar Power Calculator")
    parser.add_argument("--mlog", help="path of the main ramulator log", type=str, required=True)
    parser.add_argument("--mtiming", help="DRAMTimingExporter YAML for the main ramulator log", type=str, required=True)
    parser.add_argument("--mcmd-trace", help="TraceRecorder prefix for the main ramulator log", type=str)
    parser.add_argument("--plog", help="path of the pim ramulator log (only if ch_per_bl > ch_per_dv)", type=str)
    parser.add_argument("--ptiming", help="DRAMTimingExporter YAML for the PIM ramulator log (only if ch_per_bl > ch_per_dv)", type=str)
    parser.add_argument("--pcmd-trace", help="TraceRecorder prefix for the PIM ramulator log (only if ch_per_bl > ch_per_dv)", type=str)
    parser.add_argument("--dram-energy-model", choices=DRAM_ENERGY_MODELS, default="legacy")
    parser.add_argument("--head", help="Number of heads", type=int, required=True)
    parser.add_argument("--hidden", help="Hidden dimension (embedding size)", type=int, required=True)
    parser.add_argument("--fc", help="FC layer embedding dimension", type=int, required=True)
    parser.add_argument("--token", help="Number of tokens", type=int, required=True)
    parser.add_argument("--block", help="Number of blocks", type=int, required=True)
    parser.add_argument("--ch_per_bl", help="Number of channels per block", type=int, required=True)
    parser.add_argument("--dv", help="Total number of devices (default = 32)", default=32, type=int)
    parser.add_argument("--ch_per_dv", help="Number of channels per device (default = 32)", default=32, type=int)
    parser.add_argument("--gqa", help="Factor of group query attention (default = 1)", default=1, type=int)
    args = parser.parse_args()

    mlog = args.mlog
    mtiming = args.mtiming
    mcmd_trace = args.mcmd_trace
    plog = args.plog
    ptiming = args.ptiming
    pcmd_trace = args.pcmd_trace
    fc = args.fc
    head = args.head
    hidden = args.hidden
    token = args.token
    block = args.block
    CH_PER_BL = args.ch_per_bl
    DV = args.dv
    gqa = args.gqa

    if args.ch_per_dv != CH_PER_DV:
        CH_PER_DV = args.ch_per_dv
        # latency of pipelining 32 accelerators
        # each having 16 SIMD lanes
        ACCEL_CYCLE = { "EXP": CH_PER_DV * SB_RD_CYCLE + EXP_LANE_CYCLE + SB_WR_CYCLE,
                        "VEC": CH_PER_DV * 2.00 * SB_RD_CYCLE + 1.00 + SB_WR_CYCLE}

    energy_token = {}
    power_alldv = {}
    stat_main = command_processor(mlog, mtiming)
    PCIE = hidden if CH_PER_BL <= CH_PER_DV else hidden * 10 + fc * 2.00
    if args.dram_energy_model == "improved" and not mcmd_trace:
        parser.error("--mcmd-trace is required for --dram-energy-model improved")
    energy_main, latency_main = power_calculator(
        stat_main,
        PCIE,
        head,
        hidden,
        token,
        gqa,
        dram_energy_model=args.dram_energy_model,
        command_trace_prefix=mcmd_trace,
    )

    total_ch_used = block * CH_PER_BL
    total_dv_need = 0
    if CH_PER_BL > CH_PER_DV:
        DV_PER_BL = math.ceil(float(CH_PER_BL) / float(CH_PER_DV))
        assert DV_PER_BL > 1.00
        PIPE_STAGES = DV / DV_PER_BL
        assert DV % DV_PER_BL == 0
        if not plog or not ptiming:
            parser.error("--plog and --ptiming are both required when --ch_per_bl > --ch_per_dv")
        if args.dram_energy_model == "improved" and not pcmd_trace:
            parser.error("--pcmd-trace is required for --dram-energy-model improved when --ch_per_bl > --ch_per_dv")
        stat_pim = command_processor(plog, ptiming)
        # print(stat_main)
        # print(stat_pim)
        energy_pim, latency_pim = power_calculator(
            stat_pim,
            PCIE,
            head,
            hidden,
            token,
            gqa,
            dram_energy_model=args.dram_energy_model,
            command_trace_prefix=pcmd_trace,
        )
        total_dv_need = block * DV_PER_BL
        for comp in energy_main.keys():
            energy_token[comp] = (energy_main[comp] + energy_pim[comp] * (DV_PER_BL - 1.00)) * block
            power_alldv[comp] = (energy_main[comp] + energy_pim[comp] * (DV_PER_BL - 1.00)) * PIPE_STAGES / stat_main["latency"]
    else:
        BL_PER_DV = int(CH_PER_DV / CH_PER_BL)
        total_dv_need = math.ceil(float(block) / float(BL_PER_DV))
        assert total_dv_need <= DV
        for comp in energy_main.keys():
            energy_token[comp] = energy_main[comp] * total_dv_need
            power_alldv[comp] = energy_main[comp] * total_dv_need / stat_main["latency"]
        for comp in latency_main.keys():
            latency_main[comp] = latency_main[comp] * float(BL_PER_DV)
    total_ch_need = total_dv_need * CH_PER_DV

    print("Configuration:")
    print("CH/DV,CH-used,CH-needed,DV-needed")
    print(f"{CH_PER_DV},{total_ch_used},{total_ch_need},{total_dv_need}")

    print(",\nlatency (ms)")
    print("pim,RMS,SFT,ROT,Total Acc,Total,utilization(%)")
    total_acc_latency = latency_main["RMSNorm_latency"] + latency_main["Softmax_latency"] + latency_main["RotEmbed_latency"]
    total_latency = stat_main["latency"] + latency_main["RMSNorm_latency"] + latency_main["Softmax_latency"] + latency_main["RotEmbed_latency"]
    print(f"{stat_main['latency']},{latency_main['RMSNorm_latency']},{latency_main['Softmax_latency']},{latency_main['RotEmbed_latency']},{total_acc_latency},{total_latency},{stat_main['utilization']}")

    print(",\nenergy 1 token detailed (mJ):")
    for comp in energy_token.keys():
        print(comp, end=",")
    print()
    for comp in energy_token.keys():
        print(energy_token[comp], end=",")
    print()

    print(",\nenergy 1 token summary (mJ):") 
    print("DRAM,ctrl,DQ,DV,PCIe,Total")
    print(sum(energy_token[component] for component in ["ACT/PRE", "ACT", "PRE", "RD", "WR", "PIM", "ACT_STBY", "PRE_STBY", "GB_STT", "GB_RD", "GB_WR"] if component in energy_token), end=",")
    print(energy_token["MEM_CTR"], end=",")
    print(energy_token["DQ"], end=",")
    print(energy_token["IB_STT"] + energy_token["SB_STT"] + energy_token["RED_STT"] + energy_token["EXP_STT"] + energy_token["VEC_STT"] + energy_token["IB_DYN"] + energy_token["SB_DYN"] + energy_token["RV_DYN"] + energy_token["RED_DYN"] + energy_token["EXP_DYN"] + energy_token["VEC_DYN"] + energy_token["DV_CTR"], end=",")
    print(energy_token["PCIe"], end=",")
    total_energy = 0
    for comp in energy_token.keys():
        total_energy += energy_token[comp]
    print(total_energy)

    print(",\npower all devices detailed (W):")
    for comp in power_alldv.keys():
        print(comp, end=",")
    print()
    for comp in power_alldv.keys():
        print(power_alldv[comp], end=",")
    print()

    print(",\npower all devices summary (W):") 
    print("DRAM,ctrl,DQ,DV,PCIe,Total")
    print(sum(power_alldv[component] for component in ["ACT/PRE", "ACT", "PRE", "RD", "WR", "PIM", "ACT_STBY", "PRE_STBY", "GB_STT", "GB_RD", "GB_WR"] if component in power_alldv), end=",")
    print(power_alldv["MEM_CTR"], end=",")
    print(power_alldv["DQ"], end=",")
    print(power_alldv["IB_STT"] + power_alldv["SB_STT"] + power_alldv["RED_STT"] + power_alldv["EXP_STT"] + power_alldv["VEC_STT"] + power_alldv["IB_DYN"] + power_alldv["SB_DYN"] + power_alldv["RV_DYN"] + power_alldv["RED_DYN"] + power_alldv["EXP_DYN"] + power_alldv["VEC_DYN"] + power_alldv["DV_CTR"], end=",")
    print(power_alldv["PCIe"], end=",")
    total_power = 0
    for comp in power_alldv.keys():
        total_power += power_alldv[comp]
    print(total_power)

    # Using write DRAM command
    print(",\npower cap all devices (W):")
    print("DRAM,ctrl,DQ,DV,PCIe,Total")
    dram_power = dram_power_for_impl(stat_main["dram_impl"])
    DRAM_power_cap = ((dram_power["ACT_STBY"] + dram_power["WR"] + SRAM_POWER["GB"]["STT"])) * DV * CH_PER_DV / KILO
    ctrl_power_cap = (CTRL_POWER["TRX"] + CTRL_POWER["PHY"]) * DV * CH_PER_DV / CH_PER_CTRL / KILO
    DQ_power_cap = (DQ_ENERGY * WORD_SIZE / stat_main["tBL_ns"]) * DV * CH_PER_DV / KILO
    DV_power_cap = SRAM_POWER["IB"]["STT"] + SRAM_POWER["SB"]["STT"] 
    DV_power_cap += (ACCEL_POWER["RED"]["STT"] + ACCEL_POWER["EXP"]["STT"] + ACCEL_POWER["VEC"]["STT"] + ACCEL_POWER["CTR"]["STT"]) * CH_PER_DV
    DV_power_cap += SRAM_POWER["IB"]["WR"] + SRAM_POWER["SB"]["WR"]
    DV_power_cap += (ACCEL_POWER["RED"]["DYN"] + ACCEL_POWER["EXP"]["DYN"] + ACCEL_POWER["VEC"]["DYN"] + ACCEL_POWER["CTR"]["DYN"]) * CH_PER_DV
    DV_power_cap += ACCEL_POWER["RV"] * RV_COUNT
    DV_power_cap = DV_power_cap * DV / KILO
    PCIe_power_cap = (PCIE_ENERGY * WORD_SIZE / 1.00) * DV / KILO
    print(DRAM_power_cap, end=",")
    print(ctrl_power_cap, end=",")
    print(DQ_power_cap, end=",")
    print(DV_power_cap, end=",")
    print(PCIe_power_cap, end=",")
    print(DRAM_power_cap + ctrl_power_cap + DQ_power_cap + DV_power_cap + PCIe_power_cap)


if __name__ == "__main__":
    main()
