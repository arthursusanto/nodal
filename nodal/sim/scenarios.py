"""Scenario specifications (§9): everything a benchmark run depends on, in YAML."""

from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nodal.allocate.config import ObjectiveConfig


class TopologySpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    facilities: int = 10
    lat_range: tuple[float, float] = (30.0, 45.0)
    lon_range: tuple[float, float] = (-120.0, -80.0)
    zones_min: int = 1
    zones_max: int = 3
    slot_capacity_min: int = 120
    slot_capacity_max: int = 600
    cold_zone_prob: float = 0.25
    bulk_zone_prob: float = 0.2
    crossdock_prob: float = 0.5
    fenced_prob: float = 0.5
    forklift_count_max: int = 4
    coldchain_cert_prob: float = 0.0  # coldchain profiles raise this
    lanes_nearest: int = 3
    lane_speed_kmh: float = 65.0
    lane_cost_fixed_cents: int = 18_000
    lane_cost_per_kg_cents: float = 0.02
    risk_max: float = 0.3
    demand_rate_min: int = 0
    demand_rate_max: int = 20
    hazmat_cert_prob: float = 0.0  # chem profiles raise this (§8)


class DemandSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    shipments_per_day: float = 6.0
    weekday_factors: list[float] = Field(default_factory=lambda: [1.0] * 7)
    slots_min: int = 4
    slots_max: int = 30
    weight_per_slot_kg_min: int = 100
    weight_per_slot_kg_max: int = 600
    temp_controlled_prob: float = 0.0  # coldchain profiles raise this
    frozen_prob: float = 0.3  # of the temp-controlled ones
    deadline_hours_min: int = 24
    deadline_hours_max: int = 120
    deadline_prob: float = 0.85
    dwell_days_min: int = 2
    dwell_days_max: int = 8
    commodity_groups: list[str] = Field(default_factory=lambda: ["general", "priority"])
    origin_from_facility_prob: float = 0.35
    # Hours between registration (when the decision can be made) and ready_at
    # (when the shipment can actually move). Zero = decide-and-dispatch at once;
    # positive values open the §7.6 re-optimization window.
    booking_lead_hours: float = 0.0
    # Chemical compatibility classes (§8): at most one class per shipment.
    # Classes are tried in sorted-key order, first hit wins — so a later class's
    # REALIZED rate is conditional on every earlier class missing (e.g. probs of
    # 0.10/0.15 realize as ~0.10/~0.135). Zero-probability entries consume no
    # randomness (same guarding discipline as hazmat_cert_prob).
    compat_class_probs: dict[str, float] = Field(default_factory=dict)

    @field_validator("weekday_factors")
    @classmethod
    def _seven(cls, value: list[float]) -> list[float]:
        if len(value) != 7:
            raise ValueError("weekday_factors needs exactly 7 entries (Mon..Sun)")
        return value


class DisruptionSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    closures_per_30d: float = 0.0
    closure_days_min: int = 1
    closure_days_max: int = 5
    capacity_cuts_per_30d: float = 0.0
    cut_magnitude_min: float = 0.3
    cut_magnitude_max: float = 0.7
    cut_days_min: int = 2
    cut_days_max: int = 7


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    start: datetime = datetime(2026, 9, 1, tzinfo=UTC)
    horizon_days: int = 28
    seed: int = 42
    world_file: str | None = None  # explicit world YAML instead of generation
    topology: TopologySpec = TopologySpec()
    demand: DemandSpec = DemandSpec()
    disruptions: DisruptionSpec = DisruptionSpec()
    objective: ObjectiveConfig = ObjectiveConfig()
    consumption: bool = True  # demand rates drain stock daily
    batch_interval_hours: float = 6.0  # batch policies solve pending work this often
    rebalance: bool = False  # batch mode may propose rebalancing transfers (§7.7)

    @field_validator("start")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def load_scenario(path: str | Path) -> ScenarioSpec:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return ScenarioSpec.model_validate(raw)
