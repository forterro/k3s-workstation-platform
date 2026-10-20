"""Workstation dashboards: 360 Observability rules respected, committed JSON up to date."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "dashboards-src/workstation.py"
OUT = ROOT / "umbrella-charts/observability/workstation-dashboards/dashboards"


def _generator():
    spec = importlib.util.spec_from_file_location("workstation", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_dashboard_follows_the_rules():
    for build in _generator().DASHBOARDS:
        d = build()
        assert d.check() == [], d.uid


def test_committed_dashboards_are_up_to_date():
    for build in _generator().DASHBOARDS:
        d = build()
        expected = json.dumps(d.render(), indent=1) + "\n"
        assert (OUT / f"{d.uid}.json").read_text() == expected, "run dashboards-src/workstation.py"


def test_dashboards_are_tabbed_v2_with_an_overview_first():
    for path in sorted(OUT.glob("*.json")):
        d = json.loads(path.read_text())
        assert d["apiVersion"] == "dashboard.grafana.app/v2beta1"
        tabs = d["spec"]["layout"]["spec"]["tabs"]
        assert tabs[0]["spec"]["title"] == "Overview" and len(tabs) > 1
