"""Benchmark harness (§10): scenario suite x policy matrix -> reports."""

from pathlib import Path

from nodal.bench.kpi import KPIReport, compute_kpis
from nodal.bench.render import render_comparison
from nodal.sim.runner import run_scenario
from nodal.sim.scenarios import ScenarioSpec, load_scenario


def run_bench(
    scenario_paths: list[Path],
    policy_names: list[str],
    out_dir: Path,
    seed: int | None = None,
    keep_dbs: bool = False,
) -> list[KPIReport]:
    out_dir.mkdir(parents=True, exist_ok=True)
    specs: list[ScenarioSpec] = [load_scenario(path) for path in scenario_paths]
    names = [spec.name for spec in specs]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"duplicate scenario names in one bench run: {duplicates}")
    reports: list[KPIReport] = []
    for spec in specs:
        for policy_name in policy_names:
            db_path = out_dir / f"{spec.name}-{policy_name}.sqlite3"
            for suffix in ("", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)
            stats = run_scenario(spec, policy_name, db_path, seed=seed)
            report = compute_kpis(stats)
            reports.append(report)
            report_path = out_dir / f"{spec.name}-{policy_name}.json"
            report_path.write_text(
                report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            if not keep_dbs:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(db_path) + suffix).unlink(missing_ok=True)
    comparison = render_comparison(reports)
    (out_dir / "comparison.md").write_text(comparison + "\n", encoding="utf-8", newline="\n")
    return reports
