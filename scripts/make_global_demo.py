"""Build the showcase global demo world: 38 real-world facilities across six
continents, road/sea/air lanes (road lanes carrying the real driven polyline),
real businesses as shipment origins and customer destinations, stocked zones
that are deliberately too full for the nearest hop, a live booking book
(batch-solved and committed), A->B deliveries with multi-leg itineraries that
book staging capacity at every facility they pass through, two live disruptions
with their re-optimizations already in the log -- a capacity cut, and a hub
closure that re-routes a delivery's way out while stranding the cargo standing
in its yard -- and a queue of open shipments whose plan is DRAFTED and not
booked, so the UI opens in plan review with proposed (not yet real) routes on
the map and a COMMIT / DISCARD decision waiting (7.5).

Deterministic: no RNG, no wall clock, no network. Every timestamp is fixed
below, and every Google-derived fact (road geometry, business coordinates) is
read from the committed snapshot file `worlds/showcase_snapshots.json`. The
snapshot is refreshed by an explicit, separate mode:

    .venv\\Scripts\\python.exe scripts\\make_global_demo.py --refresh-snapshots

which is the ONLY mode that needs a maps key or a network. The ordinary build
never imports `server.maps` at all.

Run:  .venv\\Scripts\\python.exe scripts\\make_global_demo.py
Then: python -m server --db var/global-demo.sqlite3
          --profile scenarios/profiles/showcase.yaml --packs core,coldchain,chem
      (the profile matters as much as the world: the server must solve under the
      same objective — and the same rule packs — this build solved under, or the
      plan it leaves on screen re-solves differently and can never be committed)
"""

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nodal.allocate import ObjectiveConfig
from nodal.allocate.batch import commit_batch, draft_batch, solve_batch
from nodal.allocate.records import DecisionRecord
from nodal.allocate.reopt import ReoptResult, reoptimize, trapped_cargo
from nodal.domain.capacity import CapacityVector
from nodal.domain.entities import (
    Assignment,
    Certification,
    Destination,
    Disruption,
    DisruptionKind,
    Facility,
    InventoryLot,
    Lane,
    LotSpec,
    RequirementSet,
    Shipment,
    ShipmentStatus,
    StopRole,
    StorageZone,
)
from nodal.domain.units import kg
from nodal.events import EventStore, ensure_snapshots, load_state
from nodal.events import catalog as ev
from nodal.events.envelope import EventDraft
from nodal.events.state import NetworkState
from nodal.network.travel import haversine_km
from nodal.rules.messages import render_reject

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "var" / "global-demo.sqlite3"
SNAPSHOTS = REPO / "worlds" / "showcase_snapshots.json"
SNAPSHOT_VERSION = 1

T0 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
CERT_UNTIL = datetime(2027, 9, 1, tzinfo=UTC)

# The hub the demo shuts. Chosen for what it is to the rest of the world, not at
# random: it is the EXIT stop of a booked delivery that holds elsewhere (so the
# re-solve re-routes the journey and keeps the hold), and the origin facility of
# a booked shipment that is physically standing there (so that one is trapped).
CLOSED_FACILITY = "SYD"

# The objective the demo solves under. Read from the profile the LAUNCH LINE
# names, never restated here: the server has to serve the world under the same
# objective it was solved under, or committing the plan this build leaves on
# screen re-solves differently and is refused forever (§7.5).
PROFILE = REPO / "scenarios" / "profiles" / "showcase.yaml"
CONFIG = ObjectiveConfig.from_yaml(PROFILE)

# id, name, lat, lon, cold cert, hazmat cert, cold zone, bulk zone, rack slots
HUBS: list[tuple[str, str, float, float, bool, bool, bool, bool, int]] = [
    ("LAX", "Los Angeles Gateway", 33.94, -118.41, True, False, True, True, 260),
    ("DEN", "Denver Front Range", 39.74, -104.99, False, False, False, False, 220),
    ("CHI", "Chicago Inland Hub", 41.88, -87.63, True, True, True, False, 300),
    ("DFW", "Dallas Metroplex", 32.78, -96.8, False, True, False, True, 240),
    ("MEM", "Memphis Air Hub", 35.15, -90.05, False, True, False, False, 240),
    ("ATL", "Atlanta Southeast", 33.75, -84.39, True, False, True, False, 200),
    ("EWR", "Newark Bay", 40.73, -74.17, True, False, True, True, 260),
    ("HOU", "Port Houston", 29.76, -95.37, False, True, False, True, 220),
    ("YYZ", "Toronto North", 43.68, -79.63, False, False, False, False, 180),
    ("MEX", "Mexico City Altiplano", 19.43, -99.13, True, False, True, False, 200),
    ("GRU", "Sao Paulo Campinas", -23.55, -46.63, True, False, True, False, 220),
    ("BUE", "Buenos Aires Plata", -34.6, -58.38, True, False, True, False, 170),
    ("SCL", "Santiago Andes", -33.45, -70.67, True, False, True, False, 150),
    ("BOG", "Bogota Sabana", 4.71, -74.07, False, False, False, False, 140),
    ("RTM", "Rotterdam Maasvlakte", 51.92, 4.48, True, True, True, True, 320),
    ("ANR", "Antwerp Deurganck", 51.29, 4.32, False, True, False, True, 250),
    ("DUS", "Duisburg Logport", 51.43, 6.72, True, False, True, False, 240),
    ("HAM", "Hamburg Elbe", 53.55, 9.99, False, True, False, True, 260),
    ("MAD", "Madrid Meseta", 40.42, -3.7, False, False, False, False, 200),
    ("MXP", "Milan Lombardy", 45.46, 9.19, True, False, True, False, 210),
    ("LHR", "London Heath", 51.47, -0.45, True, False, True, False, 230),
    ("BHX", "Birmingham Hams Hall", 52.52, -1.68, False, False, False, False, 190),
    ("WAW", "Warsaw Junction", 52.23, 21.01, False, True, False, False, 190),
    ("DXB", "Dubai Jebel Ali", 25.25, 55.36, True, True, True, True, 340),
    ("JNB", "Johannesburg Rand", -26.2, 28.05, False, True, False, False, 170),
    ("DUR", "Durban Bayhead", -29.86, 31.02, True, False, True, False, 160),
    ("LOS", "Lagos Apapa", 6.45, 3.39, False, False, False, True, 150),
    ("SIN", "Singapore Tuas", 1.35, 103.99, True, True, True, True, 360),
    ("SHA", "Shanghai Yangshan", 31.23, 121.47, True, True, True, True, 380),
    ("SZX", "Shenzhen Yantian", 22.54, 114.05, False, True, False, True, 300),
    ("NRT", "Tokyo Narita", 35.68, 139.65, True, False, True, False, 260),
    ("KIX", "Osaka Kansai", 34.69, 135.5, False, True, False, True, 200),
    ("BOM", "Mumbai Nhava Sheva", 19.09, 72.87, False, True, False, True, 240),
    ("DEL", "Delhi Northern Ridge", 28.61, 77.21, False, False, False, False, 220),
    ("ICN", "Seoul Incheon", 37.46, 126.44, True, False, True, False, 240),
    ("SYD", "Sydney Botany", -33.87, 151.21, True, False, True, False, 200),
    ("MEL", "Melbourne Docklands", -37.81, 144.96, True, False, True, False, 180),
    ("BNE", "Brisbane Fisherman Islands", -27.47, 153.03, False, False, False, False, 160),
]

# Every ROAD pair is fetched once from the routing provider and stored with its
# real polyline; the reverse lane reuses the same geometry read backwards.
ROAD_PAIRS = [
    ("LAX", "DEN"),
    ("LAX", "HOU"),
    ("DEN", "CHI"),
    ("HOU", "DFW"),
    ("DFW", "MEM"),
    ("HOU", "MEM"),
    ("MEM", "CHI"),
    ("MEM", "ATL"),
    ("ATL", "EWR"),
    ("CHI", "YYZ"),
    ("CHI", "EWR"),
    ("MEM", "EWR"),
    ("HOU", "MEX"),
    ("RTM", "ANR"),
    ("RTM", "DUS"),
    ("RTM", "HAM"),
    ("RTM", "LHR"),
    ("RTM", "MXP"),
    ("ANR", "HAM"),
    ("DUS", "HAM"),
    ("DUS", "MXP"),
    ("LHR", "BHX"),
    ("HAM", "WAW"),
    ("MXP", "MAD"),
    ("MXP", "WAW"),
    ("SHA", "SZX"),
    ("NRT", "KIX"),
    ("BOM", "DEL"),
    ("JNB", "DUR"),
    ("GRU", "SCL"),
    ("GRU", "BUE"),
    ("SCL", "BUE"),
    ("GRU", "BOG"),
    ("SYD", "MEL"),
    ("SYD", "BNE"),
]
SEA_PAIRS = [
    ("DXB", "BOM"),  # Jebel Ali <-> Nhava Sheva: a sea corridor, not a road
    ("LAX", "SHA"),
    ("LAX", "NRT"),
    ("LAX", "SYD"),
    ("HOU", "RTM"),
    ("EWR", "RTM"),
    ("EWR", "LHR"),
    ("GRU", "LOS"),
    ("GRU", "RTM"),
    ("RTM", "DXB"),
    ("ANR", "LOS"),
    ("DXB", "SIN"),
    ("SIN", "SHA"),
    ("SIN", "SYD"),
    ("SIN", "BOM"),
    ("SHA", "ICN"),
    ("SHA", "NRT"),
    ("KIX", "SIN"),
    ("JNB", "DXB"),
    ("DUR", "SIN"),
    ("LOS", "RTM"),
    ("SCL", "LAX"),
    ("BUE", "RTM"),
    ("SZX", "SIN"),
    ("BOM", "JNB"),
    ("MEL", "SIN"),
    ("BNE", "SIN"),
]
AIR_PAIRS = [
    ("MEM", "LHR"),
    ("MEM", "NRT"),
    ("CHI", "RTM"),
    ("DEN", "MEX"),
    ("LAX", "ICN"),
    ("EWR", "MAD"),
    ("ATL", "MAD"),
    ("DXB", "LHR"),
    ("DXB", "SHA"),
    ("DXB", "DEL"),
    ("SIN", "SYD"),
    ("SIN", "NRT"),
    ("GRU", "MAD"),
    ("MEX", "MAD"),
    ("BOG", "MEX"),
    ("JNB", "LHR"),
    ("ICN", "NRT"),
    ("BOM", "SIN"),
    ("YYZ", "LHR"),
    ("MEL", "DXB"),
]

SPEED_KMH = {"sea": 38.0, "air": 850.0}  # road times come from the routing provider
HANDLING_MIN = {"road": 45, "sea": 2_880, "air": 240}  # load/unload/customs
COST_FIXED = {"road": 420, "sea": 2_600, "air": 5_400}  # dollars
COST_PER_KG = {"road": 0.028, "sea": 0.006, "air": 0.32}  # cents per kg

# Real businesses, resolved once through place search and snapshotted. Keys are
# lowercase so they never collide with a (three-letter, uppercase) facility id.
PLACES: list[tuple[str, str]] = [
    ("tesla_fremont", "Tesla Factory Fremont California"),
    ("intel_chandler", "Intel Ocotillo Campus Chandler Arizona"),
    ("boeing_everett", "Boeing Everett Delivery Center"),
    ("miller_milwaukee", "Molson Coors Milwaukee Brewery"),
    ("bimbo_mexico", "Grupo Bimbo Azcapotzalco Mexico City"),
    ("embraer_sjc", "Embraer Sao Jose dos Campos Brazil"),
    ("heineken_zoeterwoude", "Heineken Brewery Zoeterwoude Netherlands"),
    ("tata_steel_ijmuiden", "Tata Steel IJmuiden Netherlands"),
    ("asml_veldhoven", "ASML Veldhoven Netherlands"),
    ("vw_wolfsburg", "Volkswagen Werk Wolfsburg Germany"),
    ("airbus_toulouse", "Airbus Factory Toulouse France"),
    ("sasol_secunda", "Sasol Synfuels Secunda plant"),
    ("foxconn_zhengzhou", "Foxconn Zhengzhou Technology Park China"),
    ("haier_qingdao", "Haier Industrial Park Qingdao China"),
    ("dongfeng_wuhan", "Dongfeng Motor Wuhan plant China"),
    ("hyundai_ulsan", "Hyundai Motor Ulsan Plant South Korea"),
    ("tata_pune", "Tata Motors Pimpri Plant Pune India"),
    ("chuquicamata", "Chuquicamata Mine Calama Chile"),
    ("nissan_sunderland", "Nissan Motor Manufacturing UK Sunderland"),
    ("bluescope_port_kembla", "BlueScope Steelworks Port Kembla Australia"),
    ("dangote_lekki", "Dangote Refinery Lekki Nigeria"),
    ("guinness_dublin", "Guinness Brewery St James's Gate Dublin"),
    ("apm_los_angeles", "APM Terminals Pier 400 Los Angeles"),
    ("basf_ludwigshafen", "BASF Ludwigshafen Germany"),
]


@dataclass(frozen=True)
class ShipRow:
    """One shipment in the demo. `origin` is a facility id or a place key;
    `dest` is a place key for an A->B delivery (§7.9) and None otherwise, in
    which case `days` is the dwell instead of the hold."""

    id: str
    wave: int  # 1 = booked in the demo batch, 2 = left planned for the UI
    origin: str
    dest: str | None
    ready_h: int
    deadline_h: int | None
    slots: int
    group: str
    temp: tuple[int, int] | None
    compat: str | None
    days: int
    note: str = ""


# Contention is engineered, not hoped for: LAX-R1, RTM-R1, SHA-R1 and MAD-R1 are
# stocked (below) to a few dozen free slots, and several shipments whose NEAREST
# feasible facility is one of them are booked in the same batch. Each of them
# fits on its own — so the nearest hub is genuinely in the feasible set — but
# they cannot all fit, and the batch model sends the losers down real lanes.
SHIPMENTS: list[ShipRow] = [
    # -- LAX contention: three West Coast plants, ~30 free slots at LAX ----------
    ShipRow("GS-0001", 1, "tesla_fremont", None, 6, 132, 24, "general", None, None, 5),
    ShipRow("GS-0002", 1, "intel_chandler", None, 7, 132, 24, "general", None, None, 5),
    ShipRow("GS-0003", 1, "boeing_everett", None, 8, 132, 24, "general", None, None, 5),
    # -- RTM contention: three Dutch plants, ~40 free slots at RTM ---------------
    ShipRow("GS-0004", 1, "heineken_zoeterwoude", None, 6, 120, 26, "general", None, None, 6),
    ShipRow("GS-0005", 1, "tata_steel_ijmuiden", None, 7, 120, 26, "general", None, None, 6),
    ShipRow("GS-0006", 1, "asml_veldhoven", None, 8, 120, 26, "general", None, None, 6),
    # -- SHA contention: two Chinese plants, ~40 free slots at SHA ---------------
    ShipRow("GS-0007", 1, "foxconn_zhengzhou", None, 9, 144, 24, "general", None, None, 6),
    ShipRow("GS-0008", 1, "dongfeng_wuhan", None, 10, 144, 24, "general", None, None, 6),
    # -- capability-driven routing: the origin hub cannot hold the goods ---------
    ShipRow(
        "GS-0009",
        1,
        "GRU",
        None,
        14,
        None,
        30,
        "general",
        None,
        "oxidizer",
        10,
        note="no hazmat certification in South America -> sea to Europe",
    ),
    ShipRow(
        "GS-0010",
        1,
        "HAM",
        None,
        11,
        60,
        10,
        "perishable",
        (-25, -18),
        "food",
        3,
        note="HAM has no cold zone -> road to RTM or DUS",
    ),
    ShipRow(
        "GS-0011",
        1,
        "LAX",
        None,
        16,
        None,
        36,
        "general",
        None,
        "flammable",
        14,
        note="LAX has no hazmat certification -> road to DFW or HOU",
    ),
    ShipRow(
        "GS-0012",
        1,
        "YYZ",
        None,
        10,
        66,
        12,
        "perishable",
        (0, 4),
        "food",
        3,
        note="YYZ has no cold zone -> road to CHI",
    ),
    ShipRow(
        "GS-0013",
        1,
        "MEX",
        None,
        9,
        None,
        28,
        "general",
        None,
        "corrosive-acid",
        9,
        note="MEX has no hazmat certification -> road to HOU",
    ),
    ShipRow(
        "GS-0014",
        1,
        "JNB",
        None,
        12,
        60,
        14,
        "perishable",
        (0, 4),
        "food",
        4,
        note="JNB cold on a tight deadline: air, or road to Durban",
    ),
    ShipRow(
        "GS-0015",
        1,
        "LOS",
        None,
        15,
        None,
        18,
        "perishable",
        (0, 4),
        "food",
        6,
        note="no cold zone in West Africa, and Lagos has only sea lanes",
    ),
    ShipRow(
        "GS-0016",
        1,
        "BOM",
        None,
        7,
        None,
        26,
        "perishable",
        (0, 4),
        "food",
        6,
        note="BOM has no cold zone -> sea to DXB",
    ),
    ShipRow(
        "GS-0017",
        1,
        "NRT",
        None,
        9,
        120,
        12,
        "general",
        None,
        "flammable",
        5,
        note="NRT has no hazmat certification -> road to KIX",
    ),
    ShipRow(
        "GS-0031",
        1,
        "BOG",
        None,
        8,
        60,
        12,
        "perishable",
        (0, 4),
        "food",
        4,
        note="BOG has no cold zone, and its only road out of the Andes is a "
        "four-day drive: on this deadline the air lane to MEX is the only way",
    ),
    # -- ordinary inbound volume from real plants --------------------------------
    ShipRow("GS-0018", 1, "miller_milwaukee", None, 8, 96, 16, "priority", None, None, 4),
    ShipRow("GS-0019", 1, "vw_wolfsburg", None, 6, 108, 22, "general", None, None, 5),
    ShipRow("GS-0020", 1, "embraer_sjc", None, 8, 150, 22, "general", None, None, 7),
    ShipRow("GS-0021", 1, "hyundai_ulsan", None, 9, 120, 18, "priority", None, None, 4),
    ShipRow("GS-0022", 1, "sasol_secunda", None, 12, 96, 20, "general", None, None, 5),
    # -- the cargo the Sydney closure strands ------------------------------------
    ShipRow(
        "GS-0032",
        1,
        "SYD",
        None,
        10,
        None,
        22,
        "general",
        None,
        None,
        8,
        note="already in the Botany yard and booked to stay there: when SYD shuts "
        "nothing can move it, so re-optimization reports it trapped (§7.9) "
        "instead of writing a plan that cannot be executed",
    ),
    # -- A->B deliveries, booked -------------------------------------------------
    ShipRow(
        "DL-0001",
        1,
        "boeing_everett",
        "chuquicamata",
        10,
        None,
        18,
        "general",
        None,
        None,
        4,
        note="West coast plant to an Andean mine: hold in North America, exit by sea",
    ),
    ShipRow(
        "DL-0002",
        1,
        "airbus_toulouse",
        "nissan_sunderland",
        9,
        None,
        16,
        "general",
        None,
        None,
        3,
        note="Toulouse to Sunderland: enter in Iberia/Italy, hold north, exit through the UK",
    ),
    ShipRow(
        "DL-0003",
        1,
        "foxconn_zhengzhou",
        "bluescope_port_kembla",
        11,
        None,
        20,
        "general",
        None,
        None,
        5,
        note="Zhengzhou to Port Kembla: enter at SHA, hold in Asia, exit at SYD",
    ),
    ShipRow(
        "DL-0004",
        1,
        "tata_pune",
        "basf_ludwigshafen",
        8,
        None,
        14,
        "general",
        None,
        None,
        4,
        note="Pune to Ludwigshafen: enter at BOM, cross by sea, exit into the Rhine",
    ),
    ShipRow(
        "DL-0005",
        1,
        "bimbo_mexico",
        "guinness_dublin",
        12,
        None,
        12,
        "general",
        None,
        None,
        3,
        note="Mexico City to Dublin: north by road, then across the Atlantic",
    ),
    ShipRow(
        "DL-0006",
        1,
        "embraer_sjc",
        "dangote_lekki",
        10,
        None,
        20,
        "general",
        None,
        None,
        6,
        note="Sao Jose dos Campos to Lekki: enter at GRU, hold, cross the South Atlantic",
    ),
    # -- wave 2: the live planned queue the UI solves ----------------------------
    ShipRow("GS-0023", 2, "tata_pune", None, 11, 100, 20, "general", None, None, 6),
    ShipRow(
        "GS-0024",
        2,
        "SYD",
        None,
        8,
        None,
        24,
        "general",
        None,
        "oxidizer",
        12,
        note="SYD has no hazmat certification -> sea to SIN on solve",
    ),
    ShipRow(
        "GS-0025",
        2,
        "DEL",
        None,
        10,
        96,
        16,
        "perishable",
        (0, 4),
        "food",
        4,
        note="DEL has no cold zone: road to BOM then sea to DXB on solve",
    ),
    ShipRow(
        "GS-0026",
        2,
        "JNB",
        None,
        12,
        None,
        20,
        "perishable",
        (0, 4),
        "food",
        6,
        note="the solve preview weighs LHR air vs DUR road vs DXB sea",
    ),
    ShipRow("GS-0027", 2, "haier_qingdao", None, 9, 96, 12, "perishable", (-25, -18), "food", 3),
    ShipRow("GS-0028", 2, "airbus_toulouse", None, 14, 88, 14, "general", None, None, 5),
    ShipRow(
        "GS-0029",
        2,
        "WAW",
        None,
        8,
        90,
        16,
        "perishable",
        (0, 4),
        "food",
        4,
        note="WAW has no cold zone -> road to MXP or DUS on solve",
    ),
    ShipRow(
        "GS-0030",
        2,
        "SZX",
        None,
        13,
        104,
        22,
        "perishable",
        (0, 4),
        "food",
        6,
        note="SZX has no cold zone -> road to SHA on solve",
    ),
    ShipRow(
        "DL-0007",
        2,
        "asml_veldhoven",
        "nissan_sunderland",
        10,
        None,
        14,
        "general",
        None,
        None,
        4,
        note="unsolved delivery: the UI's A->B solve flow",
    ),
    ShipRow(
        "DL-0008",
        2,
        "miller_milwaukee",
        "guinness_dublin",
        12,
        None,
        12,
        "general",
        None,
        None,
        3,
        note="unsolved delivery: Milwaukee to Dublin",
    ),
    ShipRow(
        "DL-0009",
        2,
        "hyundai_ulsan",
        "apm_los_angeles",
        9,
        None,
        16,
        "general",
        None,
        None,
        5,
        note="unsolved delivery: Ulsan to the Port of Los Angeles",
    ),
]

# zone, commodity group, quantity, slots, compat class. The first block is the
# engineered pressure: LAX-R1, RTM-R1, SHA-R1 and MAD-R1 are left with only a
# few dozen free slots, so the nearest hub cannot take every arrival.
LOTS: list[tuple[str, str, int, int, str | None]] = [
    ("LAX-R1", "general", 460, 230, None),  # 260 slots -> 30 free
    ("RTM-R1", "general", 560, 280, None),  # 320 slots -> 40 free
    ("SHA-R1", "general", 680, 340, None),  # 380 slots -> 40 free
    ("MAD-R1", "general", 350, 175, None),  # 200 slots -> 25 free
    ("DEN-R1", "general", 120, 60, None),
    ("SHA-C1", "perishable", 40, 24, "food"),
    ("RTM-C1", "perishable", 36, 20, "food"),
    ("SIN-R1", "general", 140, 70, None),
    ("DXB-R1", "general", 120, 60, None),
    ("DXB-C1", "perishable", 30, 18, "food"),
    ("CHI-R1", "general", 130, 65, None),
    ("EWR-C1", "perishable", 28, 16, "food"),
    ("HOU-R1", "general", 60, 30, "corrosive-acid"),
    ("DFW-R1", "general", 90, 45, "flammable"),
    ("GRU-R1", "general", 90, 45, None),
    ("MEM-R1", "priority", 70, 35, None),
    ("BOM-R1", "general", 80, 40, "flammable"),
    ("ICN-R1", "general", 70, 35, None),
    ("NRT-C1", "perishable", 26, 14, "food"),
    ("ANR-R1", "general", 100, 50, None),
    ("DUS-R1", "general", 90, 45, None),
    ("KIX-R1", "general", 60, 30, None),
    ("DEL-R1", "general", 80, 40, None),
    ("MEL-R1", "general", 50, 25, None),
]

DEMAND_RATES = [
    ("CHI", "general", 14),
    ("EWR", "general", 10),
    ("RTM", "general", 16),
    ("ANR", "general", 9),
    ("DUS", "general", 8),
    ("DXB", "general", 12),
    ("SIN", "general", 14),
    ("SHA", "general", 12),
    ("GRU", "general", 8),
    ("DEL", "general", 7),
    ("LHR", "perishable", 6),
    ("MXP", "perishable", 5),
    ("NRT", "perishable", 6),
    ("SYD", "perishable", 4),
    ("MEL", "perishable", 3),
    ("ATL", "perishable", 5),
    ("MEM", "priority", 6),
    ("ICN", "general", 8),
    ("JNB", "general", 5),
    ("DUR", "perishable", 4),
]


# -- snapshots -----------------------------------------------------------------


def load_snapshots(path: Path = SNAPSHOTS) -> dict[str, Any]:
    """The committed Google-derived facts. No key, no network, no fallback: a
    missing or stale snapshot is a build error, never a silently degraded world."""
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"missing snapshot file {path}: run --refresh-snapshots") from exc
    if data.get("version") != SNAPSHOT_VERSION:
        raise SystemExit(f"{path}: snapshot version {data.get('version')} != {SNAPSHOT_VERSION}")
    missing = [f"{a}>{b}" for a, b in ROAD_PAIRS if f"{a}>{b}" not in data["roads"]]
    missing += [key for key, _ in PLACES if key not in data["places"]]
    if missing:
        raise SystemExit(f"{path}: stale, missing {missing}: run --refresh-snapshots")
    return data


def _clean(text: str) -> str:
    """Provider strings occasionally carry zero-width and control characters —
    invisible junk in a map label and in a committed diff."""
    return "".join(ch for ch in text if ch.isprintable()).strip()


def _ascii(text: str) -> str:
    """Printable on a console whose codepage is not UTF-8. Only the console is
    reduced: the snapshot file and the events keep the real text."""
    return text.encode("ascii", "replace").decode("ascii")


def refresh_snapshots(path: Path = SNAPSHOTS) -> dict[str, Any]:
    """Re-fetch every Google-derived fact and rewrite the snapshot file. The only
    mode that touches the network — imported here so the ordinary build cannot."""
    from server import maps

    if not maps.maps_enabled():
        raise SystemExit("--refresh-snapshots needs NODAL_GOOGLE_MAPS_KEY (or server/.env.maps)")
    coords = {hub[0]: (hub[2], hub[3]) for hub in HUBS}

    roads: dict[str, Any] = {}
    for a, b in ROAD_PAIRS:
        route = maps.road_route(*coords[a], *coords[b])
        roads[f"{a}>{b}"] = {
            "km": route["km"],
            "minutes": route["minutes"],
            # True means the provider had no driving route and this is a
            # great-circle estimate on the straight segment, not road geometry.
            "estimated": bool(route["estimated"]),
            "path": [[round(lon, 5), round(lat, 5)] for lon, lat in route["path"]],
        }
        print(f"  road {a}>{b}: {route['km']} km, estimated={route['estimated']}")

    places: dict[str, Any] = {}
    for key, query in PLACES:
        hits = maps.search_places(query, 1)
        if not hits:
            raise SystemExit(f"place search found nothing for {key!r} ({query!r})")
        hit = hits[0]
        name = _clean(hit["name"])
        places[key] = {
            "query": query,
            "name": name,
            "address": _clean(hit["address"]),
            "lat": round(float(hit["lat"]), 6),
            "lon": round(float(hit["lon"]), 6),
        }
        print(f"  place {key}: {_ascii(name)}")

    data = {"version": SNAPSHOT_VERSION, "roads": roads, "places": places}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {path}: {len(roads)} roads, {len(places)} places")
    return data


# -- world ---------------------------------------------------------------------


def build_world(store: EventStore, snap: dict[str, Any]) -> None:
    drafts: list[EventDraft] = []
    coords = {hub[0]: (hub[2], hub[3]) for hub in HUBS}
    places = snap["places"]
    # A shipment row says only "origin": a place key that shadowed a facility id
    # would silently become a hub origin instead of a business one.
    assert not set(places) & set(coords), "place keys must not collide with facility ids"

    for hub_id, name, lat, lon, cold_cert, haz_cert, cold_zone, bulk_zone, racks in HUBS:
        certifications = []
        if cold_cert:
            certifications.append(
                Certification(tag="cert:coldchain", valid_from=T0, valid_until=CERT_UNTIL)
            )
        if haz_cert:
            certifications.append(
                Certification(tag="cert:hazmat", valid_from=T0, valid_until=CERT_UNTIL)
            )
        facility = Facility(
            id=hub_id,
            name=name,
            lat=lat,
            lon=lon,
            tags=["equip:forklift", "cross-dock", "security:fenced"],
            equipment={"equip:forklift": 6},
            certifications=certifications,
            risk_factor=0.05,
        )
        drafts.append(EventDraft(ts=T0, payload=ev.FacilityRegistered(facility=facility)))
        zones = [
            StorageZone(
                id=f"{hub_id}-R1",
                facility_id=hub_id,
                kind="rack",
                capacity=CapacityVector(slots=racks, weight_g=kg(racks * 600)),
            ),
            # Every hub already advertises the `cross-dock` capability tag; this
            # is the place that tag implies. A pass-through dwell (§7.9) books
            # staging space like any stay, and a `cross-dock` zone is the only
            # thing in the model that says "stage here" — without one the goods
            # would consume the racks the storage story is about, or, at a
            # facility whose only zone rejects them, reject the whole path.
            StorageZone(
                id=f"{hub_id}-X1",
                facility_id=hub_id,
                kind="cross-dock",
                capacity=CapacityVector(slots=racks // 4),
            ),
        ]
        if cold_zone:
            zones.append(
                StorageZone(
                    id=f"{hub_id}-C1",
                    facility_id=hub_id,
                    kind="cold",
                    capacity=CapacityVector(slots=64),
                    temp_c=(-25, 5),
                )
            )
        if bulk_zone:
            zones.append(
                StorageZone(
                    id=f"{hub_id}-B1",
                    facility_id=hub_id,
                    kind="bulk",
                    capacity=CapacityVector(volume_l=900_000, weight_g=kg(600_000)),
                )
            )
        for zone in zones:
            drafts.append(EventDraft(ts=T0, payload=ev.ZoneRegistered(zone=zone)))

    def lane_draft(
        src: str,
        dst: str,
        mode: str,
        km: float,
        minutes: int,
        path: list[tuple[float, float]] | None,
    ) -> EventDraft:
        return EventDraft(
            ts=T0,
            payload=ev.LaneRegistered(
                lane=Lane(
                    id=f"L-{mode.upper()}-{src}-{dst}",
                    from_facility_id=src,
                    to_facility_id=dst,
                    mode=mode,
                    distance_km=round(km, 1),
                    minutes=minutes,
                    cost_fixed_cents=COST_FIXED[mode] * 100,
                    cost_per_kg_cents=COST_PER_KG[mode],
                    path=path,
                )
            ),
        )

    # Road lanes carry the real driven route: its distance, its duration (plus
    # the same handling allowance every lane carries) and its polyline. The
    # reverse lane reuses the geometry read backwards.
    for a, b in ROAD_PAIRS:
        entry = snap["roads"][f"{a}>{b}"]
        path = [(lon, lat) for lon, lat in entry["path"]]
        minutes = int(entry["minutes"]) + HANDLING_MIN["road"]
        drafts.append(lane_draft(a, b, "road", float(entry["km"]), minutes, path))
        drafts.append(lane_draft(b, a, "road", float(entry["km"]), minutes, list(reversed(path))))

    for pairs, mode in ((SEA_PAIRS, "sea"), (AIR_PAIRS, "air")):
        for a, b in pairs:
            km = haversine_km(*coords[a], *coords[b])
            minutes = round(km / SPEED_KMH[mode] * 60) + HANDLING_MIN[mode]
            drafts.append(lane_draft(a, b, mode, km, minutes, None))
            drafts.append(lane_draft(b, a, mode, km, minutes, None))

    for facility_id, group, per_day in DEMAND_RATES:
        drafts.append(
            EventDraft(
                ts=T0,
                payload=ev.DemandRateSet(
                    facility_id=facility_id, commodity_group=group, per_day=per_day
                ),
            )
        )

    for index, (zone_id, group, quantity, slots, compat) in enumerate(LOTS, start=1):
        drafts.append(
            EventDraft(
                ts=T0,
                payload=ev.LotReceived(
                    lot=InventoryLot(
                        id=f"LOT-{index:03d}",
                        sku=f"SKU-{group.upper()}-{index}",
                        commodity_group=group,
                        quantity=quantity,
                        size=CapacityVector(slots=slots, weight_g=kg(slots * 420)),
                        compat_class=compat,
                        zone_id=zone_id,
                        planned_departure=None,
                    )
                ),
            )
        )

    for index, row in enumerate(SHIPMENTS):
        size = CapacityVector(slots=row.slots, weight_g=kg(row.slots * 450))
        if row.origin in coords:
            origin_kwargs: dict[str, Any] = {"origin_facility_id": row.origin}
        else:
            place = places[row.origin]
            origin_kwargs = {
                "origin_lat": place["lat"],
                "origin_lon": place["lon"],
                "origin_label": place["name"],
            }
        destination = None
        if row.dest is not None:
            place = places[row.dest]
            destination = Destination(label=place["name"], lat=place["lat"], lon=place["lon"])
        shipment = Shipment(
            id=row.id,
            lines=[
                LotSpec(
                    sku=f"{row.group.upper()}-{row.id[-2:]}",
                    commodity_group=row.group,
                    quantity=row.slots,
                    size=size,
                    compat_class=row.compat,
                )
            ],
            requirements=RequirementSet(
                size=size,
                required_tags=["equip:forklift"],
                temp_c=row.temp,
                # Palletized cargo belongs in racks (or cold racks): keeps the
                # demo placements realistic and slot capacity genuinely binding.
                zone_kinds=["cold", "rack"] if row.temp else ["rack"],
                deadline=T0 + timedelta(hours=row.deadline_h) if row.deadline_h else None,
                dwell_days=row.days,
                compat_class=row.compat,
            ),
            ready_at=T0 + timedelta(hours=row.ready_h),
            destination=destination,
            hold_days=row.days if destination is not None else 0,
            **origin_kwargs,
        )
        # Registered two hours after T0, one minute apart in table order:
        # bookings exist well before readiness — the §7.6 window the disruption
        # below acts in.
        drafts.append(
            EventDraft(
                ts=T0 + timedelta(hours=2, minutes=index),
                payload=ev.ShipmentRegistered(shipment=shipment),
            )
        )

    drafts.sort(key=lambda draft: draft.ts)
    store.append(drafts, actor="demo")


# -- properties ----------------------------------------------------------------


def final_records(store: EventStore) -> dict[str, DecisionRecord]:
    """The last decision record per shipment — the state the server serves."""
    records: dict[str, DecisionRecord] = {}
    for envelope in store.read():
        payload = envelope.payload
        if isinstance(payload, ev.AllocationDecided) and payload.record is not None:
            records[payload.shipment_id] = DecisionRecord.from_event_record(payload.record)
    return records


def decision_history(store: EventStore) -> dict[str, list[Assignment]]:
    """Every allocation a shipment has been given, oldest first — the audit
    trail a supersede appends to, which is where a re-route is visible."""
    history: dict[str, list[Assignment]] = {}
    for envelope in store.read():
        payload = envelope.payload
        if isinstance(payload, ev.AllocationDecided):
            history.setdefault(payload.shipment_id, []).append(payload.assignment)
    return history


def journey(assignment: Assignment) -> tuple[Any, ...]:
    """What the map draws for a booking: the lanes in and out, the exit, and
    the ordered stops. Two assignments with the same hold and different
    journeys are the plan-staleness case (§7.6)."""
    return (
        tuple(assignment.route),
        tuple(assignment.outbound_route),
        assignment.exit_facility_id,
        tuple((stop.facility_id, stop.role.value) for stop in assignment.stops),
    )


def _origin_point(state: NetworkState, shipment: Shipment) -> tuple[float, float]:
    if shipment.origin_facility_id is not None:
        facility = state.facilities[shipment.origin_facility_id]
        return facility.lat, facility.lon
    assert shipment.origin_lat is not None and shipment.origin_lon is not None
    return shipment.origin_lat, shipment.origin_lon


def non_nearest_bookings(
    state: NetworkState, records: dict[str, DecisionRecord]
) -> list[tuple[str, str, int, str, int]]:
    """Booked shipments whose chosen facility is NOT the nearest one that passed
    every hard constraint, as (shipment, nearest feasible, its km, chosen, its km).

    `record.scored` IS the feasible set (§7.5): a facility that failed a
    constraint is in `rejected` instead, so this compares like with like — the
    nearest facility that COULD have taken the goods against the one the
    objective and the batch's shared capacity rows actually picked.
    """
    moved = []
    for shipment_id, record in sorted(records.items()):
        if record.chosen is None:
            continue
        lat, lon = _origin_point(state, state.shipments[shipment_id])

        def km_to(facility_id: str, lat: float = lat, lon: float = lon) -> float:
            facility = state.facilities[facility_id]
            return haversine_km(lat, lon, facility.lat, facility.lon)

        feasible = sorted({candidate.facility_id for candidate in record.scored})
        nearest = min(feasible, key=km_to)
        chosen = record.chosen.facility_id
        if chosen != nearest:
            moved.append(
                (shipment_id, nearest, round(km_to(nearest)), chosen, round(km_to(chosen)))
            )
    return moved


def report(store: EventStore, state: NetworkState, shut: ReoptResult) -> None:
    """Print and assert the properties the showcase exists to demonstrate. Every
    one of them is a claim the UI makes, so a regression fails the build here."""
    records = final_records(store)
    booked = {sid: r for sid, r in records.items() if r.chosen is not None}

    # The FINAL routes — post-reopt, the state the server serves — so a
    # regression in what the map draws is visible here, not in the GUI.
    for shipment_id, record in sorted(booked.items()):
        chosen = record.chosen
        assert chosen is not None
        shipment = state.shipments[shipment_id]
        origin = shipment.origin_facility_id or shipment.origin_label
        if chosen.itinerary is None:
            legs = " > ".join(
                f"{state.lanes[leg.lane_id].mode if leg.lane_id else 'drayage'}"
                f":{leg.from_label}->{leg.to_facility_id}"
                for leg in chosen.route.legs
            )
            print(_ascii(f"  {shipment_id}: {origin} -> {chosen.facility_id} [{legs}]"))
        else:
            itinerary = chosen.itinerary
            legs = " > ".join(
                f"{leg.kind}:{leg.from_label}->{leg.to_label}" for leg in itinerary.legs
            )
            print(_ascii(f"  {shipment_id}: hold {itinerary.hold.facility_id} [{legs}]"))

    moved = non_nearest_bookings(state, records)
    allocations = [m for m in moved if state.shipments[m[0]].destination is None]
    print(f"non-nearest bookings: {len(moved)}, of which {len(allocations)} are allocations")
    for shipment_id, nearest, nearest_km, chosen_id, chosen_km in moved:
        print(
            f"  {shipment_id}: nearest feasible {nearest} ({nearest_km} km) "
            f"passed over for {chosen_id} ({chosen_km} km)"
        )
    assert len(moved) >= 3, f"want >=3 non-nearest bookings, got {len(moved)}"
    # The capacity story specifically: an ordinary allocation whose nearest
    # feasible hub is full is what a nearest-facility policy gets wrong.
    assert len(allocations) >= 3, f"want >=3 non-nearest allocations, got {len(allocations)}"

    multi_leg = [
        sid
        for sid, record in sorted(booked.items())
        if record.chosen is not None
        and record.chosen.itinerary is not None
        and any(leg.lane_id is not None for leg in record.chosen.itinerary.legs)
    ]
    print(f"delivery itineraries with a lane leg: {len(multi_leg)} {multi_leg}")
    assert len(multi_leg) >= 2, f"want >=2 multi-leg deliveries, got {len(multi_leg)}"

    # Since a pass-through must find staging space to be routable at all (§7.9),
    # a delivery losing its itinerary is the way a thin world fails. Name every
    # one that should have booked, so the failure says which and not "one less".
    unbooked = []
    for row in SHIPMENTS:
        if row.wave != 1 or row.dest is None:
            continue
        booking = booked.get(row.id)
        if booking is None or booking.chosen is None or booking.chosen.itinerary is None:
            unbooked.append(row.id)
    assert not unbooked, f"deliveries that no longer route: {sorted(unbooked)}"

    # A pass-through is not a line on a map: the goods occupy the facility they
    # cross, and the booking that says so is a real reservation on a real zone
    # (§7.9). Checked in FOLDED state, which is what the timeline UI reads.
    staged: list[tuple[str, str, str]] = []
    for shipment_id in sorted(state.shipments):
        assignment = state.shipments[shipment_id].assigned
        if assignment is None:
            continue
        for stop in assignment.stops:
            if stop.role is not StopRole.TRANSIT:
                continue
            assert stop.zone_id is not None, f"{shipment_id} transits {stop.facility_id} unstaged"
            assert state.zones[stop.zone_id].facility_id == stop.facility_id
            held = [
                reservation
                for reservation in state.reservations_on_zone(stop.zone_id)
                if reservation.holder == shipment_id
                and reservation.from_ts <= stop.arrive
                and reservation.until_ts >= stop.depart
            ]
            assert held, f"{shipment_id} stages at {stop.facility_id} with no reservation"
            staged.append((shipment_id, stop.facility_id, stop.zone_id))
    print(f"transit stops holding staging capacity: {len(staged)}")
    for shipment_id, facility_id, zone_id in staged:
        print(f"  {shipment_id} stages through {facility_id} in {zone_id}")
    assert staged, "want >=1 booked transit stop with a staging reservation"

    modes: set[str] = set()
    for record in booked.values():
        chosen = record.chosen
        assert chosen is not None
        for leg in chosen.route.legs:
            if leg.lane_id is not None:
                modes.add(state.lanes[leg.lane_id].mode)
        if chosen.itinerary is not None:
            for itinerary_leg in chosen.itinerary.legs:
                if itinerary_leg.lane_id is not None:
                    modes.add(state.lanes[itinerary_leg.lane_id].mode)
    print(f"lane modes among booked routes: {sorted(modes)}")
    assert {"road", "sea", "air"} <= modes, f"want road+sea+air, got {sorted(modes)}"

    disruptions = [d for d in state.disruptions.values() if d.ended_at is None]
    reopted = sorted(sid for sid, r in records.items() if r.reopt_trigger is not None)
    print(f"active disruptions: {[d.id for d in disruptions]}, re-opted shipments: {reopted}")
    assert disruptions, "want >=1 active disruption"
    assert reopted, "want the re-optimization in the log"

    # The closure, and the two things §7.9 says it does. It is still live at
    # head, so the UI draws the re-routed journey and the trapped badge.
    closed = state.disruptions[shut.trigger]
    assert closed.kind is DisruptionKind.FACILITY_CLOSED
    assert closed.ended_at is None, f"{closed.id} must still be shut at head"

    # One: a delivery whose way out ran through the closed hub keeps its hold
    # and gets a superseding decision with a different journey around it.
    history = decision_history(store)
    superseded = {
        envelope.payload.shipment_id
        for envelope in store.read()
        if isinstance(envelope.payload, ev.AllocationSuperseded)
    }
    rerouted = []
    print(f"closure {closed.id}: {closed.target_id} shut until {closed.until_ts:%Y-%m-%d}")
    for shipment_id in shut.changed:
        decisions = history[shipment_id]
        assert len(decisions) >= 2, f"{shipment_id} was re-decided with no prior decision"
        old, new = decisions[-2], decisions[-1]
        if (old.facility_id, old.zone_ids) != (new.facility_id, new.zone_ids):
            continue  # the hold itself moved; that is not the staleness path
        assert journey(old) != journey(new), f"{shipment_id} superseded by an identical plan"
        assert shipment_id in superseded, f"{shipment_id} re-decided without an audit link"
        assert state.shipments[shipment_id].destination is not None
        assert closed.target_id in {stop.facility_id for stop in old.stops}
        assert closed.target_id not in {stop.facility_id for stop in new.stops}
        rerouted.append(shipment_id)
        print(
            f"  {shipment_id}: hold {new.facility_id}/{new.zone_ids[0]} kept, "
            f"exit {old.exit_facility_id} -> {new.exit_facility_id}, "
            f"out {list(old.outbound_route)} -> {list(new.outbound_route)}"
        )
    assert rerouted, f"want a delivery re-routed around {closed.target_id} with its hold kept"

    # Two: goods physically standing at the hub when it shut are not re-planned
    # at all. Booked ones keep the booking they have; unbooked ones cannot be
    # given one. Both come back for manual clearing, and neither is ever moved.
    trapped = sorted(entry.shipment_id for entry in shut.trapped)
    print(f"  re-routed around {closed.target_id}: {rerouted}, trapped there: {trapped}")
    assert trapped, f"want cargo trapped at {closed.target_id}"
    trapped_booked = []
    for shipment_id in trapped:
        shipment = state.shipments[shipment_id]
        assert shipment.origin_facility_id == closed.target_id
        assert shipment_id not in shut.changed, "trapped cargo is never re-planned"
        if shipment.status is ShipmentStatus.ALLOCATED:
            assert shipment.assigned is not None, "trapped cargo keeps its booking"
            trapped_booked.append(shipment_id)
        else:
            assert shipment.status is ShipmentStatus.PLANNED
            assert shipment.assigned is None
    assert trapped_booked, f"want booked cargo standing inside {closed.target_id}"

    # Three: the closure blocks DEPARTURE, not just arrival and transit (§7.9).
    # A planned shipment whose origin is the shut hub has nowhere to go — a
    # dry-run solve at head must leave it unassigned and say why, or the UI's
    # OPTIMIZE would book goods straight out of a facility nobody can enter.
    stuck_queue = sorted(
        sid
        for sid, shipment in state.shipments.items()
        if shipment.status is ShipmentStatus.PLANNED
        and shipment.origin_facility_id == closed.target_id
    )
    assert stuck_queue, f"want a planned shipment sitting at {closed.target_id}"
    assert state.last_ts is not None
    assert sorted(entry.shipment_id for entry in trapped_cargo(state, closed, state.last_ts)) == (
        sorted(set(trapped) | set(stuck_queue))
    ), "the read model's trapped set must match the closure's own"
    dry_run = solve_batch(state, stuck_queue, CONFIG, state.last_ts, batch_id="DEMO-DRYRUN")
    for shipment_id in stuck_queue:
        assert dry_run.assignments[shipment_id] is None, f"{shipment_id} booked out of a shut hub"
        record = dry_run.records[shipment_id]
        assert record.chosen is None
        reasons = {v.constraint_id for r in record.rejected for v in r.facility_verdicts}
        assert reasons == {"ORIGIN_CLOSED"}, f"{shipment_id} rejected as {sorted(reasons)}"
        why = render_reject(record.rejected[0].facility_verdicts[0])
        print(f"  {shipment_id} stays planned: {why}")

    planned = sorted(s for s, sh in state.shipments.items() if sh.status.value == "planned")
    planned_deliveries = [s for s in planned if state.shipments[s].destination is not None]
    print(f"planned queue: {len(planned)} ({len(planned_deliveries)} deliveries) {planned}")
    assert len(planned) >= 8, f"want >=8 planned, got {len(planned)}"
    assert len(planned_deliveries) >= 2, f"want >=2 planned deliveries, got {planned_deliveries}"

    road_lanes = [lane for lane in state.lanes.values() if lane.mode == "road"]
    with_path = [lane for lane in road_lanes if lane.path]
    print(f"road lanes with real geometry: {len(with_path)}/{len(road_lanes)}")
    assert len(with_path) == len(road_lanes), "every road lane must carry its drawn route"


def report_plan(state: NetworkState) -> None:
    """The plan the demo leaves ON SCREEN: drafted, never committed, so the map
    opens in review with proposed routes rather than an empty queue (§7.5).

    The proposal is an event like any other, which is the whole point — reload
    the browser and the plan is still there, because it lives in the log."""
    plan = state.pending_plan
    assert plan is not None, "the demo must end with a drafted plan at the log head"
    assert plan.based_on_seq == state.last_seq - 1, "the draft must sit ON the log it read"
    records = {sid: DecisionRecord.from_event_record(r) for sid, r in plan.records.items()}
    print(f"drafted plan {plan.batch_id}: {plan.assigned} assigned, {plan.unassigned} unassigned")
    unassigned = []
    for shipment_id in sorted(records):
        record = records[shipment_id]
        if record.chosen is None:
            reasons = sorted(
                {v.constraint_id for r in record.rejected for v in r.facility_verdicts}
            )
            unassigned.append(shipment_id)
            print(f"  {shipment_id}: UNASSIGNED {reasons}")
        else:
            print(f"  {shipment_id}: proposed {record.chosen.facility_id}/{record.chosen.zone_id}")
    assert plan.assigned + plan.unassigned == len(records)
    assert plan.assigned >= 9, f"want >=9 proposed routes on the map, got {plan.assigned}"

    # The closure story survives into the plan: the shipment sitting in the shut
    # hub's yard is the one the optimizer cannot place, and it says why.
    assert unassigned == ["GS-0024"], f"want only GS-0024 unassigned, got {unassigned}"
    stuck = records["GS-0024"]
    assert {v.constraint_id for r in stuck.rejected for v in r.facility_verdicts} == {
        "ORIGIN_CLOSED"
    }

    deliveries = sorted(sid for sid in records if state.shipments[sid].destination is not None)
    assert len(deliveries) >= 2, f"want >=2 deliveries in the plan, got {deliveries}"
    for shipment_id in deliveries:
        chosen = records[shipment_id].chosen
        assert chosen is not None and chosen.itinerary is not None, shipment_id
    print(f"  proposed deliveries: {deliveries}")


def build(db: Path, snap: dict[str, Any]) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    db.parent.mkdir(parents=True, exist_ok=True)
    config = CONFIG
    with EventStore(db) as store:
        build_world(store, snap)
        state = load_state(store)
        assert state.last_ts is not None

        # Book wave 1 as one batch. The decision time must be at the log head —
        # every registration lands within the hour after T0+2h.
        decide_at = state.last_ts
        first_wave = sorted(row.id for row in SHIPMENTS if row.wave == 1)
        result = solve_batch(state, first_wave, config, decide_at, batch_id="DEMO-WAVE-1")
        commit_batch(store, result, actor="demo")
        assigned = sum(1 for s in first_wave if result.assignments[s] is not None)
        print(f"wave 1: {assigned}/{len(first_wave)} booked")

        # A deep capacity cut lands on the busiest booked zone; re-optimize.
        state = load_state(store)
        cut_at = state.last_ts
        assert cut_at is not None
        booked_zones: dict[str, int] = {}
        for zone_id, zone in state.zones.items():
            if zone.capacity.get("slots") is None:
                continue  # the cut must bind a slot-bounded zone
            for reservation in state.reservations_on_zone(zone_id):
                booked_zones[zone_id] = booked_zones.get(zone_id, 0) + reservation.size.demand(
                    "slots"
                )
        target_zone = max(sorted(booked_zones), key=lambda z: booked_zones[z])
        disruption = Disruption(
            id="DIS-GLOBAL-1",
            kind=DisruptionKind.CAPACITY_REDUCED,
            target_id=target_zone,
            from_ts=cut_at,
            until_ts=cut_at + timedelta(days=6),
            magnitude=0.9,
        )
        print(f"cutting {target_zone} (booked {booked_zones[target_zone]} slots) by 90%")
        store.append(
            [EventDraft(ts=cut_at, payload=ev.DisruptionStarted(disruption=disruption))],
            actor="demo",
        )
        state = load_state(store)
        reopt = reoptimize(store, state, "DIS-GLOBAL-1", config, cut_at, batch_prefix="DEMO-REOPT")
        print(
            f"reopt: tier {reopt.tier}, affected {len(reopt.affected)}, "
            f"moved {len(reopt.changed)}, released {len(reopt.released)}"
        )

        # Then the closure, on a facility that is MID-ROUTE rather than a hold
        # (§7.9): SYD is the exit stop of the Zhengzhou -> Port Kembla delivery,
        # so the re-solve keeps the Singapore hold and re-routes only the way
        # out — a superseding decision with the same zone and a different
        # journey, which is the plan-staleness path made visible. The window has
        # to reach that exit stop three weeks downstream; a shorter closure
        # would end before the goods were ever due there and touch nothing.
        # GS-0032 is already standing in the Botany yard, so nothing a solver
        # decides can move it: it is reported trapped instead of re-planned.
        state = load_state(store)
        shut_at = state.last_ts
        assert shut_at is not None
        closure = Disruption(
            id="DIS-GLOBAL-2",
            kind=DisruptionKind.FACILITY_CLOSED,
            target_id=CLOSED_FACILITY,
            from_ts=shut_at,
            until_ts=shut_at + timedelta(days=25),
        )
        print(f"closing {CLOSED_FACILITY} for 25 days")
        store.append(
            [EventDraft(ts=shut_at, payload=ev.DisruptionStarted(disruption=closure))],
            actor="demo",
        )
        state = load_state(store)
        shut = reoptimize(
            store, state, "DIS-GLOBAL-2", config, shut_at, batch_prefix="DEMO-CLOSURE"
        )
        print(
            f"closure reopt: tier {shut.tier}, affected {len(shut.affected)}, "
            f"moved {len(shut.changed)}, trapped {len(shut.trapped)}"
        )

        state = load_state(store)
        report(store, state, shut)

        # Finally: solve the queue and DRAFT the plan rather than booking it, so
        # the world ships with routes that are proposed and not yet real (§7.5).
        # The draft has to be the last event in the log — a plan is pending only
        # while it is the head — so nothing may be appended after this.
        plan_at = state.last_ts
        assert plan_at is not None
        queue = sorted(
            sid for sid, s in state.shipments.items() if s.status is ShipmentStatus.PLANNED
        )
        plan = solve_batch(state, queue, config, plan_at, batch_id="DEMO-PLAN")
        draft_batch(store, plan, actor="demo")
        state = load_state(store)
        report_plan(state)

        snapshots = ensure_snapshots(store)
        state = load_state(store)
        by_status: dict[str, int] = {}
        for shipment in state.shipments.values():
            by_status[shipment.status.value] = by_status.get(shipment.status.value, 0) + 1
        print(
            f"world: {len(state.facilities)} facilities, {len(state.lanes)} lanes, "
            f"{len(state.lots)} lots, shipments {by_status}, "
            f"{state.last_seq} events, {snapshots} snapshots"
        )
        print(
            f"ready: python -m server --db {db}"
            " --profile scenarios/profiles/showcase.yaml --packs core,coldchain,chem"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB, help="output event store")
    parser.add_argument(
        "--refresh-snapshots",
        action="store_true",
        help="re-fetch road geometry and business coordinates, rewrite the snapshot, and stop",
    )
    args = parser.parse_args(argv)
    if args.refresh_snapshots:
        refresh_snapshots()
        return
    build(args.db, load_snapshots())


if __name__ == "__main__":
    main()
