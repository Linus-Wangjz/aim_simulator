import sys
import argparse
import math
import re

KILO = 1000
MEGA = 1000000
GIGA = 1000000000
FREQ = 2.00 * GIGA

CH_PER_DV = 32.00

GDDR6_COMMANDS = [
    "ACT",
    "PREA",
    "PRE",
    "RD",
    "WR",
    "RDA",
    "WRA",
    "REFab",
    "REFpb",
    "ACT4",
    "ACT16",
    "PRE4",
    "MAC",
    "MAC16",
    "AF16",
    "EWMUL16",
    "RDCP",
    "WRCP",
    "WRGB",
    "RDMAC16",
    "RDAF16",
    "WRMAC16",
    "WRA16",
    "TMOD",
    "SYNC",
    "EOC",
]

LPDDR4_COMMANDS = [
    "ACT-1",
    "ACT-2",
    "CASRD",
    "CASWR",
    "CASWRGB",
    "CASWRMAC16",
    "CASRDMAC16",
    "CASRDAF16",
    "CASWRA16",
    "RFMab",
    "RFMpb",
    "ACT4-1",
    "ACT8-1",
    "ACT16-1",
    "ACT4-2",
    "ACT8-2",
    "ACT16-2",
]

# Superset of commands emitted by the GDDR6 and LPDDR4 AiM models. LPDDR4's
# split activation uses ACT8-* because the current LPDDR4 org has eight banks.
# MAC16/RDMAC16/etc. keep the 16 suffix because that denotes the 16-BF16
# datapath chunk, not the number of activated banks.
commands = list(dict.fromkeys(GDDR6_COMMANDS + LPDDR4_COMMANDS))

isrs =  ["WR_SBK",
        "WR_GB",
        "WR_BIAS",
        "WR_AFLUT",
        "RD_MAC",
        "RD_AF",
        "RD_SBK",
        "COPY_BKGB",
        "COPY_GBBK",
        "MAC_SBK",
        "MAC_ABK",
        "AF",
        "EWMUL",
        "EWADD",
        "WR_ABK",
        "EOC",
        "SYNC"]

tRC = 44.5
tBL = 1.25
DEFAULT_PIM_TCCD_CYCLES = {
    # GDDR6_AiM_timing uses nCCDL=2. nCCDS is also 2 in the current preset.
    "GDDR6": 2.00,
    # LPDDR4_AiM_timing default; sweep outputs override this through _nCCDx.
    "LPDDR4": 6.00,
}
NCCD_PATH_RE = re.compile(r"_nCCD(\d+)")

# DRAM_POWER = {  "ACT_STBY": 415,
#                 "PRE_STBY": 317.5,
#                 "ACT": 93.9,
#                 "WR": 915,
#                 "RD": 525}

# "ACT_STBY": VDD * IDD3N
# "PRE_STBY": VDD * IDD2N
# "ACT": VDD * (IDD0 - IDD3N)  
# "WR": VDD * (IDD4W - IDD3N)
# "RD": VDD * (IDD4R - IDD3N)

GDDR6_DRAM_POWER = {
    "ACT_STBY": 527.5 / 2.00,  # 415
    "PRE_STBY": 366.3 / 2.00,  # 317.5
    "ACT": 132.6 / 2.00,       # 93.9
    "WR": 1106.3 / 2.00,       # 915
    "RD": 876.3 / 2.00,        # 525
}

LPDDR4_DRAM_POWER = {
    "ACT_STBY": 1.8 * 2 + 1.1 * 34.5 + 1.1 * 0.1,
    "PRE_STBY": 1.8 * 0.6 + 1.1 * 31 + 1.1 * 0.1,
    "ACT": 1.8 * (9 - 2) + 1.1 * (53 - 34.5) + 1.1 * (0.1 - 0.1),
    "WR": 1.8 * (2 - 2) + 1.1 * (265 - 34.5) + 1.1 * (0.3 - 0.1),
    "RD": 1.8 * (2.5 - 2) + 1.1 * (287 - 34.5) + 1.1 * (105 - 0.1),
}

DRAM_POWER_BY_IMPL = {
    "GDDR6": GDDR6_DRAM_POWER,
    "LPDDR4": LPDDR4_DRAM_POWER,
}

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

def command_processor(stat_path):
    file = open(stat_path, 'r')
    lines = file.readlines()
    file.close()
    stat = {}
    for command in commands:
        stat[command] = 0.00
    for isr in isrs:
        stat[isr] = 0.00
    stat["cycles"] = 0.00
    stat["idle_cycles"] = 0.00
    stat["active_cycles"] = 0.00
    stat["precharged_cycles"] = 0.00
    stat["dram_impl"] = ""
    stat["pim_tccd_cycles"] = 0.00
    
    for line in lines:
        words = line.split(' ')
        while len(words) > 0 and words[0] == "":
            words.pop(0)
        if words[0] == "Processing":
            if len(stat.keys()) > 0:
                print("Error: multiple files in the same log")
                exit(1)
        if "memory_system_cycles" in words[0]:
            assert stat["cycles"] == 0
            stat["cycles"] = float(words[1])
        if words[0] == "impl:" and len(words) > 1:
            impl = words[1].strip()
            if impl in {"GDDR6", "LPDDR4"}:
                stat["dram_impl"] = impl
        if "idle_cycles" in words[0]:
            stat["idle_cycles"] = float(words[1])
        if "active_cycles" in words[0]:
            stat["active_cycles"] = float(words[1])
        if "precharged_cycles" in words[0]:
            stat["precharged_cycles"] = float(words[1])
        for command in commands:
            if "num_" + command + "_commands" in words[0]:
                stat[command] += float(words[1])
        for isr in isrs:
            if "total_num_AiM_ISR_" + isr + "_requests" in words[0]:
                stat[isr] += float(words[1])

    if stat["dram_impl"] == "LPDDR4":
        match = NCCD_PATH_RE.search(str(stat_path))
        if match:
            stat["pim_tccd_cycles"] = float(match.group(1))
    if stat["pim_tccd_cycles"] == 0.00 and stat["dram_impl"] in DEFAULT_PIM_TCCD_CYCLES:
        stat["pim_tccd_cycles"] = DEFAULT_PIM_TCCD_CYCLES[stat["dram_impl"]]

    # ms (average of all channels)
    stat["latency"] = stat["cycles"] * KILO / FREQ
    # ms (average of all channels)
    stat["active_latency"] = stat["active_cycles"] / CH_PER_DV * KILO / FREQ
    # ms (average of all channels)
    stat["precharged_latency"] = stat["precharged_cycles"] / CH_PER_DV * KILO / FREQ
    # % (average of all channels)
    stat["utilization"] = 100.00 - (stat["idle_cycles"] / CH_PER_DV / stat["cycles"]) * 100.00
    return stat

def dram_power_for_impl(dram_impl):
    if dram_impl not in DRAM_POWER_BY_IMPL:
        raise ValueError(f"Unknown DRAM impl '{dram_impl}'. Expected one of: {', '.join(DRAM_POWER_BY_IMPL)}")

    dram_power = DRAM_POWER_BY_IMPL[dram_impl]
    missing = [name for name, value in dram_power.items() if value is None]
    if missing:
        raise ValueError(f"Fill {dram_impl}_DRAM_POWER values for: {', '.join(missing)}")
    return dram_power

def power_calculator(stat, PCIE_bits, Head, HiddenDim, Tokens, GQA):
    energy = {}
    latency = {}
    dram_power = dram_power_for_impl(stat["dram_impl"])
    act_bank_count = 8.00 if stat["dram_impl"] == "LPDDR4" else 16.00

    # LPDDR4 splits activation into ACT*-1/ACT*-2. Only ACT*-2 is marked as
    # the actual opening command. ACT8-* is bank fanout; MAC16/RDMAC16/etc.
    # are 16-BF16 datapath operations and are not scaled by act_bank_count.
    act_equiv = stat["ACT"] + stat["ACT-2"]
    act_equiv += 4.00 * (stat["ACT4"] + stat["ACT4-2"])
    act_equiv += 16.00 * stat["ACT16"]
    act_equiv += act_bank_count * (stat["ACT8-2"] + stat["ACT16-2"])

    rd_data_commands = stat["RDCP"] + stat["RD"] + stat["RDA"]
    rd_data_commands += 16.00 * stat["AF16"] + stat["RDMAC16"] + stat["RDAF16"]

    wr_data_commands = stat["WRCP"] + stat["WR"] + stat["WRA"]
    wr_data_commands += stat["WRMAC16"] + 16.00 * stat["WRA16"]

    pim_commands = stat["MAC"] / 16.00 + stat["MAC16"] + stat["EWMUL16"] / 4.00
    pim_tccd_ns = stat["pim_tccd_cycles"] * GIGA / FREQ

    dq_commands = stat["RD"] + stat["WR"] + stat["RDA"] + stat["WRA"]
    dq_commands += stat["WRGB"] + stat["RDMAC16"] + stat["RDAF16"] + stat["WRMAC16"] + stat["WRA16"]

    # TODO: should we use the tRC or tRCD?
    energy["ACT/PRE"] = dram_power["ACT"] * act_equiv * tRC / GIGA
    energy["RD"] = dram_power["RD"] * rd_data_commands * tBL / GIGA
    energy["WR"] = dram_power["WR"] * wr_data_commands * tBL / GIGA
    energy["PIM"] = 3 * dram_power["RD"] * pim_commands * pim_tccd_ns / GIGA
    energy["ACT_STBY"] = dram_power["ACT_STBY"] * CH_PER_DV * stat["active_latency"] / KILO
    energy["PRE_STBY"] = dram_power["PRE_STBY"] * CH_PER_DV * stat["precharged_latency"] / KILO
    energy["DQ"] = DQ_ENERGY * WORD_SIZE * dq_commands / GIGA
    energy["PCIe"] = PCIE_bits * PCIE_ENERGY / GIGA

    ISR_COUNT = stat["RD"] + stat["WR"] + stat["RDA"] + stat["WRA"] + stat["MAC"] + stat["MAC16"] + stat["AF16"] + stat["EWMUL16"] + stat["RDCP"] + stat["WRCP"] + stat["WRGB"] + stat["RDMAC16"] + stat["RDAF16"] + stat["WRMAC16"] + stat["WRA16"]
    CMD_COUNT = sum(stat[x] for x in commands)
    energy["MEM_CTR"] = (CTRL_POWER["TRX"] * ISR_COUNT + CTRL_POWER["PHY"] * CMD_COUNT) / CH_PER_CTRL / FREQ

    GQA_factor = 1.00 + 1.00 / GQA
    latency["RMSNorm_latency"] =  HiddenDim / 16.00 / 16.00 / CH_PER_DV * ACCEL_CYCLE["VEC"]    # EMB /16.00 /16.00 ADD
    latency["RMSNorm_latency"] += SB_RD_CYCLE + SB_WR_CYCLE + 1.00                              # 1 RED
    latency["RMSNorm_latency"] += RV_RMSNorm_CYCLE                                              # 1 RISCV
    latency["RMSNorm_latency"] = float(2.00 * latency["RMSNorm_latency"]) / float(FREQ / KILO)
    latency["Softmax_latency"] =  Tokens * Head / 16.00 / CH_PER_DV * ACCEL_CYCLE["EXP"]        # TOK*HEAD /16.00 EXP
    latency["Softmax_latency"] += Tokens * Head / 16.00 / CH_PER_DV * ACCEL_CYCLE["VEC"]        # TOK*HEAD /16.00 ADD
    latency["Softmax_latency"] += Head * 1.00 * SB_RD_CYCLE                                     # HEAD RED
    latency["Softmax_latency"] += Head * RV_SFT_CYCLE_PIPELINE                                  # HEAD RISCV
    latency["Softmax_latency"] = float(latency["Softmax_latency"]) / float(FREQ / KILO)
    latency["RotEmbed_latency"] = HiddenDim * RV_ROTEmbed_CYCLE                                 # EMB RISCV
    latency["RotEmbed_latency"] = float(GQA_factor * latency["RotEmbed_latency"]) / float(FREQ / KILO)

    # Static
    energy["GB_STT"] = stat["latency"] * SRAM_POWER["GB"]["STT"] * CH_PER_DV / KILO
    energy["SB_STT"] = stat["latency"] * SRAM_POWER["SB"]["STT"] / KILO
    energy["IB_STT"] = stat["latency"] * SRAM_POWER["IB"]["STT"] / KILO
    energy["RED_STT"] = stat["latency"] * ACCEL_POWER["RED"]["STT"] * CH_PER_DV / KILO
    energy["EXP_STT"] = stat["latency"] * ACCEL_POWER["EXP"]["STT"] * CH_PER_DV / KILO
    energy["VEC_STT"] = stat["latency"] * ACCEL_POWER["VEC"]["STT"] * CH_PER_DV / KILO

    # SRAM Power
    energy["GB_RD"] = SRAM_POWER["GB"]["RD"] * (stat["WRCP"]) / FREQ
    energy["GB_WR"] = SRAM_POWER["GB"]["WR"] * (stat["WRGB"] + stat["RDCP"]) / FREQ
    energy["SB_DYN"] = 2.00 * (HiddenDim / 16.00 / 16.00 + 1.00 + 1.00) * (2.00 * SRAM_POWER["SB"]["RD"] + 1.00 * SRAM_POWER["SB"]["WR"]) / FREQ    # RMSNorm: EMB /16.00 /16.00 ADD + 1 RED + 1 RV [First 16.00 is #SIMD lanes, Second is because of PU 16.00-to-1 MAC]
    energy["SB_DYN"] += ((Tokens * Head / 16.00 * 3 + Head * 2.00) * SRAM_POWER["SB"]["RD"]) / FREQ                                                 # Softmax: TOK * HEAD / 16.00 EXP and ADD + HEAD RED
    energy["SB_DYN"] += ((Tokens * Head / 16.00 * 2.00 + Head * 2.00) * SRAM_POWER["SB"]["WR"]) / FREQ                                              # Softmax: TOK * HEAD / 16.00 EXP and ADD + HEAD RED
    energy["SB_DYN"] += GQA_factor * HiddenDim / 16.00 * (SRAM_POWER["SB"]["RD"] + 2.00 * SRAM_POWER["SB"]["WR"]) / FREQ                            # RotEmbed: EMB /16.00 RV (1 ld + 2.00 st)
    
    ISR_COUNT = 0
    for isr in isrs:
        ISR_COUNT += stat[isr]
    ISR_COUNT += 2.00 * (HiddenDim / 16.00 / 16.00 + 2.00)              # RMSNorm
    ISR_COUNT += (Tokens * Head / 16.00 * 2.00 + Head * 2.00)           # Softmax
    ISR_COUNT += GQA_factor * HiddenDim                                 # RotEmbed
    energy["IB_DYN"] = ISR_COUNT * SRAM_POWER["IB"]["RD"] / FREQ

    # Accelerator Power
    energy["RV_DYN"] = 2.00 * RV_RMSNorm_CYCLE * ACCEL_POWER["RV"] / FREQ                       # RMSNorm: 1 RISCV
    energy["RV_DYN"] += Head * RV_SFT_CYCLE_SINGLE * ACCEL_POWER["RV"] / FREQ                   # Softmax: HEAD RISCV
    energy["RV_DYN"] += GQA_factor * HiddenDim * RV_ROTEmbed_CYCLE * ACCEL_POWER["RV"] / FREQ   # RotEmbed: EMB RISCV

    energy["RED_DYN"] = 2.00 * (1.00 * ACCEL_POWER["RED"]["DYN"]) / FREQ                    # 1 RED (RMSNorm)
    energy["RED_DYN"] +=(Head * ACCEL_POWER["RED"]["DYN"]) / FREQ                           # HEAD RED (Softmax)

    energy["EXP_DYN"] = (Tokens * Head / 16.00 * ACCEL_POWER["EXP"]["DYN"]) / FREQ          # TOK*HEAD /16.00 EXP (Softmax)

    energy["VEC_DYN"] = 2.00 * HiddenDim / 16.00 / 16.00 * ACCEL_POWER["VEC"]["DYN"] / FREQ     # EMB /16.00 /16.00 ADD (RMSNorm) [First 16.00 is #SIMD lanes, Second is because of PU 16.00-to-1 MAC]
    energy["VEC_DYN"] +=(Tokens * Head / 16.00 * ACCEL_POWER["VEC"]["DYN"]) / FREQ              # TOK*HEAD /16.00 ADD (Softmax)

    # We simply assume all the other components have a switching activity of 0.5
    energy["DV_CTR"] = stat["latency"] * 0.5 * (ACCEL_POWER["CTR"]["STT"] + ACCEL_POWER["CTR"]["DYN"]) / KILO

    return energy, latency

def main():
    global CH_PER_DV
    global ACCEL_CYCLE

    parser = argparse.ArgumentParser(description="Cellar Power Calculator")
    parser.add_argument("--mlog", help="path of the main ramulator log", type=str, required=True)
    parser.add_argument("--plog", help="path of the pim ramulator log (only if ch_per_bl > ch_per_dv)", type=str)
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
    plog = args.plog
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
    stat_main = command_processor(mlog)
    PCIE = hidden if CH_PER_BL <= CH_PER_DV else hidden * 10 + fc * 2.00
    energy_main, latency_main = power_calculator(stat_main, PCIE, head, hidden, token, gqa)

    total_ch_used = block * CH_PER_BL
    total_dv_need = 0
    if CH_PER_BL > CH_PER_DV:
        DV_PER_BL = math.ceil(float(CH_PER_BL) / float(CH_PER_DV))
        assert DV_PER_BL > 1.00
        PIPE_STAGES = DV / DV_PER_BL
        assert DV % DV_PER_BL == 0
        stat_pim = command_processor(plog)
        # print(stat_main)
        # print(stat_pim)
        energy_pim, latency_pim = power_calculator(stat_pim, PCIE, head, hidden, token, gqa)
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
    print(energy_token["ACT/PRE"] + energy_token["RD"] + energy_token["WR"] + energy_token["PIM"] + energy_token["ACT_STBY"] + energy_token["PRE_STBY"] + energy_token["GB_STT"] + energy_token["GB_RD"] + energy_token["GB_WR"], end=",")
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
    print(power_alldv["ACT/PRE"] + power_alldv["RD"] + power_alldv["WR"] + power_alldv["PIM"] + power_alldv["ACT_STBY"] + power_alldv["PRE_STBY"] + power_alldv["GB_STT"] + power_alldv["GB_RD"] + power_alldv["GB_WR"], end=",")
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
    DQ_power_cap = (DQ_ENERGY * WORD_SIZE / tBL) * DV * CH_PER_DV / KILO
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
