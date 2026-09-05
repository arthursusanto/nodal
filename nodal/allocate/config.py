"""Objective configuration profiles (§7.3, §7.8).

Weights, reference scales, and curves are configuration, not code. Every decision
record embeds a snapshot of the config it was made under.
"""

from itertools import pairwise
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nodal.network.journey import RoadLegConfig
from nodal.network.travel import TravelConfig


class ObjectiveWeights(BaseModel):
    model_config = ConfigDict(frozen=True)

    transport_cost: float = 0.35
    travel_time: float = 0.10
    lateness_risk: float = 0.20
    congestion: float = 0.10
    inv_balance: float = 0.05
    capacity_preservation: float = 0.10
    transfers: float = 0.05
    op_risk: float = 0.05

    def get(self, component: str) -> float:
        value: float = getattr(self, component)
        return value


class CurvePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: float
    y: float


def piecewise(points: list[CurvePoint], x: float) -> float:
    """Piecewise-linear evaluation. Clamped on the left; beyond the last point the
    final segment's slope extrapolates — clamping there would make 200% overload
    score like 100% and would break convexity exactly where the stage-4 epigraph
    encoding (which extends the last segment) needs it."""
    if not points:
        return 0.0
    if x <= points[0].x:
        return points[0].y
    for left, right in pairwise(points):
        if x <= right.x:
            span = right.x - left.x
            frac = 0.0 if span == 0 else (x - left.x) / span
            return left.y + frac * (right.y - left.y)
    last, prev = points[-1], points[-2]
    slope = (last.y - prev.y) / (last.x - prev.x) if last.x != prev.x else 0.0
    return last.y + slope * (x - last.x)


class SolverConfig(BaseModel):
    """Batch-solver budgets (§7.4). Reproducible modes (bench, tests) run one
    worker under a deterministic-time budget; interactive use may parallelize,
    and the record then flags the result as budget-dependent (§2)."""

    model_config = ConfigDict(frozen=True)

    det_time_budget: float = Field(default=60.0, gt=0)  # CP-SAT max_deterministic_time
    workers: int = Field(default=1, ge=1)
    random_seed: int = 0
    # Wall-clock budget (seconds) for the interactive configuration. Machine-
    # dependent, so setting it makes the result non-reproducible (§2) and the
    # record says so; reproducible modes (sim, bench) leave it None and run on
    # deterministic time alone.
    max_wall_seconds: float | None = Field(default=None, gt=0)
    # Penalty (in normalized objective units) for leaving a shipment unassigned:
    # far above any real assignment cost, so overload is diagnosable, not infeasible.
    unassigned_penalty: float = Field(default=25.0, gt=0)
    # Seed the solve with the validated flow-relax solution up front (§13). Off by
    # default: cold starts explore freely on ordinary batches, and the engine
    # falls back to a hinted re-solve on its own if no incumbent appears. Turn on
    # for very large batches where a wasted cold budget is the dominant cost.
    warm_start: bool = False


class ObjectiveConfig(BaseModel):
    """A named profile: everything a decision depends on besides state itself."""

    model_config = ConfigDict(frozen=True)

    name: str = "default"
    weights: ObjectiveWeights = ObjectiveWeights()
    # Fixed reference scales (§7.3) — normalized value 1.0 == "typical".
    cost_ref_cents: int = Field(default=50_000, gt=0)
    time_ref_minutes: int = Field(default=12 * 60, gt=0)
    buffer_ref_minutes: int = Field(default=24 * 60, gt=0)
    # Monotone convex congestion curve over facility peak utilization.
    congestion_curve: list[CurvePoint] = Field(
        default_factory=lambda: [
            CurvePoint(x=0.0, y=0.0),
            CurvePoint(x=0.60, y=0.05),
            CurvePoint(x=0.80, y=0.25),
            CurvePoint(x=0.90, y=0.60),
            CurvePoint(x=1.00, y=1.00),
        ]
    )
    cover_days: int = 14  # target_stock = cover_days * demand_rate (§7.7)
    scarcity_k: int = 2  # facility among <=k providers of a required tag => scarce
    scarcity_bonus: float = 1.0  # multiplier added to capacity_preservation when scarce
    default_dwell_days: int = 4
    disruption_adjacency_risk: float = 0.25
    util_band: tuple[float, float] = (0.55, 0.85)  # target utilization band (§10 residency)
    top_k_detail: int = 8  # scored candidates keeping full per-component detail (§7.5)
    solver: "SolverConfig" = Field(default_factory=lambda: SolverConfig())
    # Rebalancing (§7.7): a (facility, group) whose stock exceeds target by this
    # ratio donates; at most this many transfers are proposed per cycle.
    rebalance_surplus_ratio: float = 0.5
    rebalance_max_per_cycle: int = 5
    # Re-optimization (§7.6). churn_penalty: normalized objective units added to
    # every option that CHANGES an existing assignment (staying costs nothing
    # extra), so one closure doesn't reshuffle the network for marginal gains.
    # Tier 2 triggers when tier 1 leaves affected shipments unassigned, or when
    # its objective exceeds the still-feasible incumbents' separable cost by
    # more than the threshold (a lost-feasibility incumbent contributes zero to
    # that baseline, so its whole re-placement cost counts as degradation); the
    # set then expands to reservation holders on contended capacity, at most
    # reopt_hops times.
    churn_penalty: float = Field(default=0.5, ge=0)
    reopt_degradation_threshold: float = Field(default=2.0, ge=0)
    reopt_hops: int = Field(default=1, ge=0)
    packs: list[str] = Field(default_factory=lambda: ["core"])
    # Weight overrides for pack objective components (§8), keyed by namespaced
    # component name (e.g. "coldchain.excursion"); absent -> the pack's default.
    pack_weights: dict[str, float] = Field(default_factory=dict)
    travel: TravelConfig = TravelConfig()
    road: RoadLegConfig = RoadLegConfig()  # first/last-mile pricing for deliveries (§7.9)

    @model_validator(mode="after")
    def _validate_curve(self) -> "ObjectiveConfig":
        points = self.congestion_curve
        if len(points) < 2:
            raise ValueError("congestion_curve needs at least 2 points")
        xs = [p.x for p in points]
        if xs != sorted(xs) or len(set(xs)) != len(xs):
            raise ValueError("congestion_curve x values must be strictly increasing")
        slopes = [(b.y - a.y) / (b.x - a.x) for a, b in pairwise(points)]
        if any(s < 0 for s in slopes):
            raise ValueError("congestion_curve must be non-decreasing")
        if any(b < a - 1e-9 for a, b in pairwise(slopes)):
            raise ValueError("congestion_curve must be convex (CP-SAT epigraph, §7.4)")
        return self

    def congestion_penalty(self, utilization: float) -> float:
        return piecewise(self.congestion_curve, utilization)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ObjectiveConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(raw)
