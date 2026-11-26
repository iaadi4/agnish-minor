from __future__ import annotations

import json
from pathlib import Path

import pytest

from pcbassist.library import ComponentLibrary, FootprintResolver, SearchQuery
from pcbassist.paths import data_dir
from pcbassist.schemas import LibraryPart


def test_seed_has_at_least_60_parts(library: ComponentLibrary) -> None:
    assert len(library) >= 60


@pytest.mark.parametrize(
    "category",
    [
        "mcu_wireless",
        "ldo",
        "buck",
        "boost",
        "charger",
        "fuel_gauge",
        "sensor_env",
        "sensor_motion",
        "sensor_light",
        "sensor_temp",
        "connector_usb",
        "connector_battery",
        "esd",
        "crystal",
        "resistor",
        "capacitor",
        "led",
        "switch",
        "connector_rf",
    ],
)
def test_required_categories_present(library: ComponentLibrary, category: str) -> None:
    assert library.search(SearchQuery(categories=[category])), category


@pytest.mark.parametrize(
    "tag", ["wifi", "ble", "nrf52", "usb-c", "jst-ph", "i2c", "imu", "pressure"]
)
def test_required_tags_present(library: ComponentLibrary, tag: str) -> None:
    assert library.search(SearchQuery(any_tags=[tag])), tag


def test_every_pin_maps_to_a_footprint_pad(library: ComponentLibrary) -> None:
    for p in library.all():
        assert p.footprint_info is not None, p.mpn
        pads = set(p.footprint_info.pad_numbers)
        missing = [pin.number for pin in p.pins if pin.number not in pads]
        assert not missing, f"{p.mpn}: {missing}"


def test_pin_tables_have_unique_numbers(library: ComponentLibrary) -> None:
    for p in library.all():
        nums = [pin.number for pin in p.pins]
        assert len(nums) == len(set(nums)), p.mpn


def test_pin_tables_come_from_kicad(library: ComponentLibrary) -> None:
    for p in library.all():
        assert p.pin_source.startswith(("kicad-symbols@", "kicad-footprints@")), p.mpn


def test_datasheet_urls_are_http_or_null(library: ComponentLibrary) -> None:
    for p in library.all():
        assert p.datasheet_url is None or p.datasheet_url.startswith(("http://", "https://")), p.mpn


def test_ics_have_power_and_ground_pins(library: ComponentLibrary) -> None:
    for p in library.search(
        SearchQuery(
            categories=[
                "mcu_wireless",
                "ldo",
                "charger",
                "sensor_env",
                "sensor_temp",
                "sensor_motion",
            ]
        )
    ):
        tags = {t for pin in p.pins for t in pin.functions}
        assert "gnd" in tags, p.mpn
        assert "power" in tags or "battery" in tags, p.mpn
        assert p.supply_range is not None, p.mpn


def test_known_pinouts_spot_check(library: ComponentLibrary) -> None:
    # Cross-checked against manufacturer datasheets by the curator.
    mcp = library.get("MCP73831T-2ACI/OT")
    assert mcp is not None
    assert [(x.number, x.name) for x in mcp.pins] == [
        ("1", "STAT"),
        ("2", "VSS"),
        ("3", "VBAT"),
        ("4", "VDD"),
        ("5", "PROG"),
    ]
    ldo = library.get("AP2112K-3.3TRG1")
    assert ldo is not None
    assert [(x.number, x.name) for x in ldo.pins] == [
        ("1", "VIN"),
        ("2", "GND"),
        ("3", "EN"),
        ("4", "NC"),
        ("5", "VOUT"),
    ]
    usbc = library.get("USB4105-GF-A")
    assert usbc is not None
    names = {x.number: x.name for x in usbc.pins}
    assert names["A5"] == "CC1" and names["B5"] == "CC2" and names["S1"] == "SHIELD"


def test_esp32_strapping_and_usb_tags(library: ComponentLibrary) -> None:
    c3 = library.get("ESP32-C3-WROOM-02-N4")
    assert c3 is not None
    by_name = {p.name: p for p in c3.pins}
    assert "boot" in by_name["IO9"].functions
    assert "usb_dp" in by_name["IO19"].functions and "usb_dm" in by_name["IO18"].functions
    s3 = library.get("ESP32-S3-WROOM-1-N8")
    assert s3 is not None
    assert any("usb_dp" in p.functions for p in s3.pins)


def test_search_filters(library: ComponentLibrary) -> None:
    ldos_at_li_ion = library.search(SearchQuery(categories=["ldo"], supply_v=4.2))
    assert ldos_at_li_ion and all(
        p.supply_range and p.supply_range.contains(4.2) for p in ldos_at_li_ion
    )
    assert "L7805CV" not in {p.mpn for p in ldos_at_li_ion}
    hits = library.search(SearchQuery(keywords=["humidity"], all_tags=["i2c"]))
    assert hits and all("humidity" in (p.description.lower() + " ".join(p.tags)) for p in hits)
    assert len(library.search(SearchQuery(limit=3))) == 3


def test_get_normalizes_mpn(library: ComponentLibrary) -> None:
    assert library.get("mcp73831t2aci/ot") is not None
    assert library.get("NOT-A-PART") is None
    assert "AP2112K-3.3TRG1" in library and 42 not in library
    assert "ldo" in library.categories()


def test_duplicate_mpn_rejected(library: ComponentLibrary) -> None:
    p = library.all()[0]
    with pytest.raises(ValueError, match="duplicate MPN"):
        ComponentLibrary([p, p])


def test_load_from_explicit_seed_list(tmp_path: Path, library: ComponentLibrary) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps([library.all()[0].model_dump(mode="json")]))
    assert len(ComponentLibrary.load(seed)) == 1


def test_seed_records_provenance() -> None:
    raw = json.loads((data_dir() / "components_seed.json").read_text())
    assert "kicad-symbols" in raw["sources"]["kicad_symbols"]
    for item in raw["parts"]:
        LibraryPart.model_validate(item)


def test_bundled_footprint_resolver(library: ComponentLibrary) -> None:
    r = FootprintResolver.from_bundled_index()
    assert r.resolve("Capacitor_SMD:C_0402_1005Metric").resolved
    miss = r.resolve("Capacitor_SMD:C_0402")
    assert not miss.resolved and miss.suggestion and "C_0402_1005Metric" in miss.suggestion
    assert "unknown footprint library" in (r.resolve("Nope:X").suggestion or "")
    assert not r.resolve("no-colon").resolved
    assert not r.resolve(None).resolved
    assert all(r.resolve(p.footprint).resolved for p in library.all())


def test_resolver_from_local_kicad_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = tmp_path / "MyLib.pretty"
    lib.mkdir()
    (lib / "FP1.kicad_mod").write_text('(footprint "FP1")')
    monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(tmp_path))
    r = FootprintResolver.detect()
    assert "local KiCad" in r.source
    assert r.resolve("MyLib:FP1").resolved
