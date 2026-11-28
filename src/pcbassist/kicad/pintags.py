"""Pin conventions shared by the seed builder and the KiCad library index.

* ``PIN_TYPE_MAP`` maps KiCad electrical types to :class:`pcbassist.schemas.PinType` values.
* ``infer_tags`` derives function tags (gnd, i2c_sda, usb_dp, enable ...) from pin names.
"""

from __future__ import annotations

import re

PIN_TYPE_MAP = {
    "power_in": "power_in",
    "power_out": "power_out",
    "input": "input",
    "output": "output",
    "bidirectional": "bidir",
    "tri_state": "output",
    "passive": "passive",
    "free": "passive",
    "unspecified": "passive",
    "open_collector": "open_drain",
    "open_emitter": "open_drain",
    "no_connect": "nc",
}

NAME_TAGS: list[tuple[str, list[str]]] = [
    (r"^(GND|VSS|AGND|DGND|PGND|GNDA|GNDD|GND_\w+|VSS\w*|EP|PAD|THERMAL)$", ["gnd"]),
    (r"^(SDA|SDA/SDI|SDI/SDA)$", ["i2c_sda"]),
    (r"^(SCL|SCL/SCK|SCK/SCL)$", ["i2c_scl"]),
    (r"^SDI$", ["i2c_sda", "spi_mosi"]),
    (r"^(SCK|SPC)$", ["i2c_scl", "spi_sck"]),
    (r"^SDO$", ["spi_miso", "i2c_addr"]),
    (r"^(SA0|ADDR|ADD0|A0|A1|A2)$", ["i2c_addr"]),
    (r"^(CS|CSB|~CS|NCS)$", ["spi_cs"]),
    (r"^(MOSI|SDI\d?)$", ["spi_mosi"]),
    (r"^(MISO)$", ["spi_miso"]),
    (r"^(IO\d+|GPIO\d+|P\d\.\d+|P[0-9]_[0-9]+|IO\d+/\w+)", ["gpio"]),
    (r"^(SENSOR_V[PN])$", ["gpio"]),
    (r"^(EN|CE|SHDN|~SHDN|CHIP_EN|ON/~OFF|~SLEEP|nSLEEP|SLEEP|~EN)$", ["enable"]),
    (r"^(~RESET|~RST|RESET|RST|NRST|NRESET|nRESET|P0\.21/~RESET|~MR)$", ["reset"]),
    (r"^(TXD\d?|TX|U0TXD|TXD0)$", ["uart_tx"]),
    (r"^(RXD\d?|RX|U0RXD|RXD0)$", ["uart_rx"]),
    (r"^(D\+|DP|USB_D\+|UD\+)$", ["usb_dp"]),
    (r"^(D-|DN|USB_D-|UD-)$", ["usb_dm"]),
    (r"^CC\d?$", ["usb_cc"]),
    (r"^(VBUS)$", ["vbus"]),
    (r"^(SHIELD|SH)$", ["shield"]),
    (r"^(SBU\d)$", ["usb_sbu"]),
    (r"^(XTAL\w*|XL\d|XIN|XOUT|XI|XO|OSC\w*)$", ["crystal"]),
    (r"^(SWDIO|SWDCLK|SWCLK|SWO|SWDIO/TMS|SWCLK/TCK)$", ["swd"]),
    (r"^(INT\d?|ALERT|DRDY|~INT|~ALERT|INT/DRDY|~INT\d)$", ["interrupt"]),
    (r"^(VBAT|BAT|BAT\+|VBATT)$", ["battery"]),
    (r"^(STAT|~CHG|~PGOOD|~PG|PG|PGOOD|~FAULT|~FLT)$", ["status"]),
    (r"^(PROG|ISET|ILIM)$", ["current_set"]),
    (r"^(FB|VFB|ADJ)$", ["feedback"]),
    (r"^(SW|LX)$", ["switch_node"]),
    (r"^(BST|VBST|BOOT|CB)$", ["bootstrap"]),
    (r"^NC$", ["nc"]),
    (r"^AIN\d+$", ["adc"]),
    (r"^NFC\d$", ["nfc"]),
    (r"^TRACE\w*$", ["trace"]),
    (r"^(DEC\d|DCC|DCCH|VREG\w*)$", ["internal_regulator"]),
    (r"^K$", ["cathode"]),
    (r"^A$", ["anode"]),
    (r"^\+$", ["positive"]),
    (r"^-$", ["negative"]),
]


def expand_pin_number(num: str) -> list[str]:
    """KiCad 10 'stacked' pins: '[1,15,38,39]' or '[1-4]' map one symbol pin to many pads."""
    if not (num.startswith("[") and num.endswith("]")):
        return [num]
    out: list[str] = []
    for part in num[1:-1].split(","):
        part = part.strip()
        m = re.fullmatch(r"([A-Za-z]*)(\d+)-([A-Za-z]*)(\d+)", part)
        if m and m.group(1) == m.group(3):
            out += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(4)) + 1)]
        elif part:
            out.append(part)
    return out


def normalize_pin_name(name: str) -> str:
    """Strip KiCad rich-text markup: 'V_{SS}' -> 'VSS', '~{CHG}' -> '~CHG'."""
    name = re.sub(r"[_^]\{([^}]*)\}", r"\1", name)
    name = re.sub(r"~\{([^}]*)\}", r"~\1", name)
    return name


def infer_tags(name: str, ptype: str) -> list[str]:
    tags: list[str] = []
    candidates = [name, *name.split("/")] if "/" in name else [name]
    for pat, t in NAME_TAGS:
        if any(re.match(pat, c, flags=re.IGNORECASE) for c in candidates):
            tags += [x for x in t if x not in tags]
    if ptype == "passive" and "i2c_addr" in tags:
        # 'A1'/'A2' on passive two-terminal parts (TVS diodes) are terminals, not addresses.
        tags.remove("i2c_addr")
    if ptype in ("power_in", "power_out") and "gnd" not in tags:
        tags.append("power")
    return tags
