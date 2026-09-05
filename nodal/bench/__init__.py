"""Benchmark harness: KPIs, policy comparison, reports (§10)."""

from nodal.bench.kpi import KPIReport, RunStats, compute_kpis
from nodal.bench.render import render_comparison

__all__ = ["KPIReport", "RunStats", "compute_kpis", "render_comparison"]
