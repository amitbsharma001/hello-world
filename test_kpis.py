"""Tests for the dashboard KPI catalog (services/kpis.py). Fully pure."""
from datetime import datetime, timedelta, timezone

from aegis.services.kpis import (
    compute_kpi_catalog, reopen_count, median_first_action_hours,
    _delta, _fmt_hours, _percentile,
)

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
P_START = NOW - timedelta(days=30)
PP_START = NOW - timedelta(days=60)


class T:
    """Minimal ticket stand-in."""
    _n = 0

    def __init__(self, status="open", severity="sev3", assignee="a@x.com",
                 opened_days_ago=5, resolved_days_ago=None, sla_hours=None,
                 enhanced="", plan=None):
        T._n += 1
        self.id = T._n
        self.status = status
        self.severity = severity
        self.assignee = assignee
        self.opened_at = NOW - timedelta(days=opened_days_ago)
        self.resolved_at = (NOW - timedelta(days=resolved_days_ago)
                            if resolved_days_ago is not None else None)
        self.sla_due_at = (self.opened_at + timedelta(hours=sla_hours)
                           if sla_hours else None)
        self.description_enhanced = enhanced
        self.plan_payload = plan or {}


def sla_state(t):
    if not t.sla_due_at:
        return "none"
    if t.resolved_at:
        return "met" if t.resolved_at <= t.sla_due_at else "missed"
    if t.sla_due_at < NOW:
        return "breached"
    return "on_track"


def catalog(tickets, **kw):
    return compute_kpi_catalog(tickets=tickets, now=NOW, p_start=P_START,
                               pp_start=PP_START, sla_state=sla_state, **kw)


def get(cat, key):
    for c in cat["categories"]:
        for k in c["kpis"]:
            if k["key"] == key:
                return k
    raise KeyError(key)


# ---- helpers ----------------------------------------------------------------

def test_delta_direction_and_goodness():
    assert _delta(10, 5)["dir"] == "up" and _delta(10, 5)["good"] is True
    d = _delta(10, 5, lower_is_better=True)
    assert d["dir"] == "up" and d["good"] is False
    assert _delta(5, 5)["dir"] == "flat"
    assert _delta(None, 5) is None


def test_fmt_hours_switches_to_days():
    assert _fmt_hours(3.2) == "3.2h"
    assert _fmt_hours(72) == "3.0d"
    assert _fmt_hours(None) == "—"


def test_percentile_bounds():
    assert _percentile([], 50) is None
    assert _percentile([4], 90) == 4
    assert _percentile([1, 2, 3, 4], 50) == 2.5


# ---- volume / speed / sla ----------------------------------------------------

def test_volume_and_netflow():
    ts = [T(opened_days_ago=3),                                  # created in p, open
          T(opened_days_ago=10, resolved_days_ago=8, status="resolved"),  # both in p
          T(opened_days_ago=45, resolved_days_ago=40, status="resolved")]  # both in pp
    c = catalog(ts)
    assert get(c, "open_backlog")["value"] == 1
    assert get(c, "created")["value"] == 2
    assert get(c, "resolved")["value"] == 1
    nf = get(c, "net_flow")
    assert nf["raw"] == 1 and nf["value"] == "+1"
    assert get(c, "wip")["value"] == 0


def test_weeks_to_clear_guard_when_zero_throughput():
    c = catalog([T(opened_days_ago=3)])
    assert get(c, "weeks_to_clear")["value"] == "∞"


def test_mttr_lower_is_better_delta():
    ts = [T(opened_days_ago=10, resolved_days_ago=9, status="resolved"),   # 24h in p
          T(opened_days_ago=45, resolved_days_ago=44.5, status="resolved")]  # 12h in pp
    c = catalog(ts)
    m = get(c, "mttr")
    assert m["raw"] == 24.0
    assert m["delta"]["dir"] == "up" and m["delta"]["good"] is False


def test_sla_breached_and_missed():
    ts = [T(opened_days_ago=5, sla_hours=24),                              # breached open
          T(opened_days_ago=10, resolved_days_ago=8, status="resolved",
            sla_hours=24)]                                                 # missed (48h>24h)
    c = catalog(ts)
    assert get(c, "breached")["value"] == 1
    assert get(c, "missed")["value"] == 1
    assert get(c, "sla")["raw"] == 0


# ---- quality: history-derived --------------------------------------------------

def test_reopen_detection_and_rate():
    h = {1: [(NOW - timedelta(days=4), "open"),
             (NOW - timedelta(days=3), "resolved"),
             (NOW - timedelta(days=2), "open")],          # reopened
         2: [(NOW - timedelta(days=4), "open"),
             (NOW - timedelta(days=1), "resolved")]}      # clean
    assert reopen_count(h) == 1
    ts = [T(opened_days_ago=4),
          T(opened_days_ago=4, resolved_days_ago=1, status="resolved")]
    c = catalog(ts, history_by_ticket=h)
    assert get(c, "reopened")["value"] == 1
    assert get(c, "reopen_rate")["raw"] == 50


def test_first_action_median():
    opened = {1: NOW - timedelta(hours=10), 2: NOW - timedelta(hours=10)}
    h = {1: [(NOW - timedelta(hours=10), "open"),
             (NOW - timedelta(hours=8), "in_progress")],   # 2h
         2: [(NOW - timedelta(hours=10), "open"),
             (NOW - timedelta(hours=4), "in_progress")]}   # 6h
    assert median_first_action_hours(h, opened) == 4.0


def test_quality_unavailable_without_history_and_fcr_csat_always_na():
    c = catalog([T()])
    assert get(c, "reopened")["available"] is False
    assert get(c, "fcr")["available"] is False
    assert get(c, "csat")["available"] is False
    c2 = catalog([T()], history_by_ticket={1: [(NOW, "open")]})
    assert get(c2, "reopened")["available"] is True
    assert get(c2, "fcr")["available"] is False  # never fabricated


# ---- workload / ai / escalations ----------------------------------------------

def test_workload_distribution():
    ts = [T(assignee="a@x.com"), T(assignee="a@x.com"),
          T(assignee="b@x.com", severity="sev1"), T(assignee="")]
    c = catalog(ts)
    assert get(c, "unassigned")["value"] == 1
    assert get(c, "active_assignees")["value"] == 2
    assert get(c, "per_assignee")["value"] == 1.5
    assert get(c, "critical_share")["value"] == "25%"
    assert get(c, "top_load")["value"] == "50%"


def test_ai_and_escalation_stats_flow_through():
    ts = [T(enhanced="better text", plan={"actions": [1]}), T()]
    c = catalog(ts,
                agent_stats={"total": 10, "succeeded": 9},
                writeback_stats={"success": 3, "mock": 2, "failed": 1},
                escalation_stats={"active": 2, "awaiting_ack": 7,
                                  "email_sent": 8, "email_failed": 2,
                                  "comment_ok": 9, "comment_failed": 1})
    assert get(c, "agent_runs")["value"] == 10
    assert "90%" in get(c, "agent_runs")["note"]
    assert get(c, "enhance_cov")["value"] == "50%"
    assert get(c, "plan_cov")["value"] == "50%"
    assert get(c, "writebacks")["value"] == 5
    assert get(c, "esc_active")["value"] == 2
    assert get(c, "esc_email_fail")["value"] == "20%"
    assert get(c, "esc_cmt_fail")["value"] == "10%"


def test_catalog_counts_and_availability_totals():
    c = catalog([T()])
    assert c["total"] == sum(len(cc["kpis"]) for cc in c["categories"])
    # fcr + csat + reopened + reopen_rate + tta unavailable without history
    assert c["total"] - c["available"] == 5
