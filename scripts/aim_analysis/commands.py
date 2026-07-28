"""AiM command definitions and command-transition timing attribution."""

from __future__ import annotations


GDDR6_COMMANDS = (
    "ACT", "PREA", "PRE", "RD", "WR", "RDA", "WRA", "REFab", "REFpb", "ACT4", "ACT16", "PRE4",
    "MAC", "MAC16", "AF16", "EWMUL16", "RDCP", "WRCP", "WRGB", "RDMAC16", "RDAF16", "WRMAC16",
    "WRA16", "TMOD", "SYNC", "EOC",
)

LPDDR4_COMMANDS = (
    "ACT-1", "ACT-2", "CASRD", "CASWR", "CASWRGB", "CASWRMAC8", "CASRDMAC8", "CASWRA8", "RFMab",
    "RFMpb", "ACT4-1", "ACT8-1", "ACT4-2", "ACT8-2", "MAC8", "AF8", "EWMUL8", "RDMAC8", "RDAF8",
    "WRMAC8", "WRA8",
)

COMMANDS = tuple(dict.fromkeys(GDDR6_COMMANDS + LPDDR4_COMMANDS))

ISR_NAMES = (
    "WR_SBK", "WR_GB", "WR_BIAS", "WR_AFLUT", "RD_MAC", "RD_AF", "RD_SBK", "COPY_BKGB", "COPY_GBBK",
    "MAC_SBK", "MAC_ABK", "AF", "EWMUL", "EWADD", "WR_ABK", "EOC", "SYNC",
)

STACK_LABELS = ("WR_GB", "WR_BIAS", "ACT_PRE", "MAC_ABK", "RD_MAC", "TMOD", "Other")
CONSTRAINT_LABELS = (
    "nMODCH", "nCCD/nCCDS", "nCCD/nBL", "nRCDRDMAC", "nRP/nRPab", "nRFCab", "nRTW", "CAS sync",
    "ACT split", "Issue gap", "Other",
)

TRANSITION_CONSTRAINT_MAP = {
    ("TMOD", "WRGB"): "nMODCH",
    ("TMOD", "CASWRGB"): "nMODCH",
    ("TMOD", "CASWRMAC8"): "nMODCH",
    ("TMOD", "CASRDMAC8"): "nMODCH",
    ("TMOD", "RDMAC16"): "nMODCH",
    ("TMOD", "RDMAC8"): "nMODCH",
    ("TMOD", "ACT16"): "nMODCH",
    ("TMOD", "ACT8-1"): "nMODCH",
    ("TMOD", "PREA"): "nMODCH",
    ("MAC16", "MAC16"): "nCCD/nCCDS",
    ("MAC8", "MAC8"): "nCCD/nCCDS",
    ("WRGB", "WRGB"): "nCCD/nBL",
    ("WRGB", "WRMAC16"): "nCCD/nBL",
    ("WRGB", "WRMAC8"): "nCCD/nBL",
    ("WRGB", "CASWRGB"): "nCCD/nBL",
    ("WRGB", "CASWRMAC8"): "nCCD/nBL",
    ("ACT16", "MAC16"): "nRCDRDMAC",
    ("ACT8-2", "MAC8"): "nRCDRDMAC",
    ("PREA", "ACT16"): "nRP/nRPab",
    ("PREA", "ACT8-1"): "nRP/nRPab",
    ("PREA", "REFab"): "nRP/nRPab",
    ("REFab", "ACT8-1"): "nRFCab",
    ("RDMAC16", "WRGB"): "nRTW",
    ("RDMAC16", "CASWRGB"): "nRTW",
    ("RDMAC16", "WRMAC16"): "nRTW",
    ("RDMAC8", "WRGB"): "nRTW",
    ("RDMAC8", "CASWRGB"): "nRTW",
    ("RDMAC8", "WRMAC8"): "nRTW",
    ("RDMAC8", "CASWRMAC8"): "nRTW",
    ("CASWRGB", "WRGB"): "CAS sync",
    ("CASWRMAC8", "WRMAC8"): "CAS sync",
    ("CASRDMAC8", "RDMAC8"): "CAS sync",
    ("ACT8-1", "ACT8-2"): "ACT split",
    ("WRMAC16", "TMOD"): "Issue gap",
    ("WRMAC8", "TMOD"): "Issue gap",
    ("MAC16", "TMOD"): "Issue gap",
    ("MAC8", "TMOD"): "Issue gap",
    ("WRGB", "TMOD"): "Issue gap",
    ("REFab", "TMOD"): "Issue gap",
}


def command_component(command: str) -> str:
    if command in {"CASWRGB", "WRGB"}:
        return "WR_GB"
    if command in {"WRMAC16", "CASWRMAC8", "WRMAC8"}:
        return "WR_BIAS"
    if command in {"RDMAC16", "CASRDMAC8", "RDMAC8"}:
        return "RD_MAC"
    if command in {"MAC", "MAC16", "MAC8"}:
        return "MAC_ABK"
    if command == "TMOD":
        return "TMOD"
    if command.startswith("ACT") or command.startswith("PRE"):
        return "ACT_PRE"
    return "Other"


def transition_constraint(preceding: str, following: str) -> str:
    return TRANSITION_CONSTRAINT_MAP.get((preceding, following), "Other")
