"""Comparison-report rendering (§10): one table per scenario, policies as columns.

Losses render exactly like wins.
"""

from collections import defaultdict

from nodal.bench.kpi import KPIReport

# Rows shown first, in this order; any other outcome keys follow alphabetically.
LEAD_ROWS = [
    "shipments",
    "allocated",
    "unallocated",
    "total_cost_cents",
    "total_km",
    "on_time_rate",
    "lateness_mean_min",
    "lateness_p95_min",
    "transfers",
    "infeasible_preferred",
    "imbalance_mean",
    "util_band_residency",
]


def render_comparison(reports: list[KPIReport]) -> str:
    by_scenario: dict[str, list[KPIReport]] = defaultdict(list)
    for report in reports:
        by_scenario[report.scenario].append(report)

    lines: list[str] = []
    for scenario in sorted(by_scenario):
        group = sorted(by_scenario[scenario], key=lambda r: r.policy)
        seeds = sorted({r.seed for r in group})
        profiles = sorted({r.profile for r in group})
        lines.append(
            f"## {scenario}  (seed {', '.join(map(str, seeds))}; profile {', '.join(profiles)})"
        )
        lines.append("")
        policies = [r.policy for r in group]
        keys = list(LEAD_ROWS) + sorted({k for r in group for k in r.outcome} - set(LEAD_ROWS))
        lines.append("| KPI | " + " | ".join(policies) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in policies) + " |")
        for key in keys:
            if not any(key in r.outcome for r in group):
                continue
            cells = [str(r.outcome.get(key, "-")) for r in group]
            lines.append(f"| {key} | " + " | ".join(cells) + " |")
        perf_keys = sorted({k for r in group for k in r.performance})
        if perf_keys:
            lines.append("")
            lines.append("wall-clock (hardware-dependent, excluded from byte-identity):")
            for key in perf_keys:
                cells = [f"{r.policy}={r.performance.get(key, '-')}" for r in group]
                lines.append(f"  {key}: " + "  ".join(cells))
        lines.append("")
    return "\n".join(lines)
