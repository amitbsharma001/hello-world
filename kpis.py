"""
Dashboard KPI catalog.

One structured, grouped catalog of every ticket-ops indicator the ingested
data can support — plus, deliberately, the well-known KPIs it CANNOT support
(first-contact resolution, CSAT), marked unavailable with the reason. Showing
"not measurable with current data" is part of being complete; inventing a
number is not.

Pure computation: everything arrives as plain values/objects so the math is
executable and testable without Django.
"""
from __future__ import annotations

from collections import Counter


# ---------- small helpers ----------------------------------------------------

def _hours(td) -> float:
    return td.total_seconds() / 3600.0


def _fmt_hours(h) -> str:
    if h is None:
        return "—"
    if h >= 48:
        return f"{h / 24:.1f}d"
    return f"{h:.1f}h"


def _pct(part, whole) -> int | None:
    return round(part / whole * 100) if whole else None


def _percentile(vals, p) -> float | None:
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 1)


def _delta(cur, prev, lower_is_better=False):
    """Direction + goodness of a period-over-period move."""
    if cur is None or prev is None:
        return None
    diff = round(cur - prev, 1)
    if diff == 0:
        return {"diff": 0, "pct": 0, "dir": "flat", "good": None}
    pct = round(diff / prev * 100) if prev else (100 if cur else 0)
    rising = diff > 0
    good = (not rising) if lower_is_better else rising
    return {"diff": diff, "pct": pct, "dir": "up" if rising else "down", "good": good}


def _kpi(key, label, value, *, raw=None, delta=None, definition="", note="",
         available=True):
    return {"key": key, "label": label, "value": value, "raw": raw,
            "delta": delta, "definition": definition, "note": note,
            "available": available}


def _na(key, label, reason):
    return _kpi(key, label, "n/a", definition=reason, note=reason, available=False)


# ---------- history-derived (reopens, time-to-first-action) ------------------

RESOLVED_STATES = ("resolved", "closed")


def reopen_count(history_by_ticket) -> int:
    """Tickets whose status history contains a resolved/closed -> open move."""
    n = 0
    for events in (history_by_ticket or {}).values():
        prev = None
        for _, status in events:
            if prev in RESOLVED_STATES and status and status not in RESOLVED_STATES:
                n += 1
                break
            if status:
                prev = status
    return n


def median_first_action_hours(history_by_ticket, opened_at_by_ticket) -> float | None:
    """Median hours from opening to the FIRST status change — an approximation
    of time-to-first-action (we ingest no comments, so true first-response is
    unknowable here)."""
    samples = []
    for tid, events in (history_by_ticket or {}).items():
        opened = opened_at_by_ticket.get(tid)
        if not opened or not events:
            continue
        first_status = None
        for occurred, status in events:
            if status is None:
                continue
            if first_status is None:
                first_status = status
                continue
            if status != first_status:
                if occurred > opened:
                    samples.append(_hours(occurred - opened))
                break
    return _percentile(samples, 50)


# ---------- the catalog -------------------------------------------------------

def compute_kpi_catalog(*, tickets, now, p_start, pp_start, sla_state,
                        history_by_ticket=None, history_note="",
                        agent_stats=None, writeback_stats=None,
                        escalation_stats=None) -> dict:
    """Build the grouped KPI catalog.

    tickets: objects with status/severity/assignee/opened_at/resolved_at/
             sla_due_at/description_enhanced/plan_payload attributes.
    sla_state: callable(ticket) -> none|met|missed|breached|at_risk|on_track.
    history_by_ticket: {ticket_id: [(occurred_at, status), ...]} time-ordered.
    """
    weeks = max((now - p_start).days / 7.0, 0.1)

    def in_window(dt, a, b):
        return dt is not None and a <= dt < b

    is_open = lambda t: t.status not in RESOLVED_STATES
    open_t = [t for t in tickets if is_open(t)]
    created_p = [t for t in tickets if in_window(t.opened_at, p_start, now)]
    created_pp = [t for t in tickets if in_window(t.opened_at, pp_start, p_start)]
    resolved_p = [t for t in tickets if in_window(t.resolved_at, p_start, now)]
    resolved_pp = [t for t in tickets if in_window(t.resolved_at, pp_start, p_start)]

    res_h_p = [_hours(t.resolved_at - t.opened_at) for t in resolved_p]
    res_h_pp = [_hours(t.resolved_at - t.opened_at) for t in resolved_pp]
    ages = [_hours(now - t.opened_at) for t in open_t]

    def compliance(lst):
        with_sla = [t for t in lst if t.sla_due_at]
        if not with_sla:
            return None
        met = sum(1 for t in with_sla if t.resolved_at and t.resolved_at <= t.sla_due_at)
        return round(met / len(with_sla) * 100)

    states = Counter(sla_state(t) for t in open_t)
    net_p = len(created_p) - len(resolved_p)
    net_pp = len(created_pp) - len(resolved_pp)
    throughput = round(len(resolved_p) / weeks, 1)
    arrival = round(len(created_p) / weeks, 1)
    aged30 = sum(1 for t in open_t if _hours(now - t.opened_at) > 30 * 24)

    # -- Volume & flow --
    vol = [
        _kpi("open_backlog", "Open backlog", len(open_t),
             definition="Tickets not resolved/closed right now."),
        _kpi("created", "Created", len(created_p),
             delta=_delta(len(created_p), len(created_pp), lower_is_better=True),
             definition="New tickets opened this window vs the prior window."),
        _kpi("resolved", "Resolved", len(resolved_p),
             delta=_delta(len(resolved_p), len(resolved_pp)),
             definition="Tickets resolved this window vs the prior window."),
        _kpi("net_flow", "Net flow", (f"+{net_p}" if net_p > 0 else str(net_p)),
             raw=net_p, delta=_delta(net_p, net_pp, lower_is_better=True),
             definition="Created minus resolved. Positive = backlog growing."),
        _kpi("throughput", "Throughput / wk", throughput,
             definition="Resolved per week over the window."),
        _kpi("arrival", "Arrivals / wk", arrival,
             definition="Created per week over the window."),
        _kpi("weeks_to_clear", "Weeks to clear",
             (round(len(open_t) / throughput, 1) if throughput else "∞"),
             definition="Backlog ÷ weekly throughput at the current run-rate. "
                        "Indicative, not a forecast.",
             note="indicative"),
        _kpi("wip", "In progress", sum(1 for t in open_t if t.status == "in_progress"),
             definition="Open tickets currently in the in-progress state."),
        _kpi("aged_backlog", "Backlog >30d", aged30,
             note=(f"{_pct(aged30, len(open_t))}% of open" if open_t else ""),
             definition="Open tickets older than 30 days."),
    ]

    # -- Speed --
    mttr_p = round(sum(res_h_p) / len(res_h_p), 1) if res_h_p else None
    mttr_pp = round(sum(res_h_pp) / len(res_h_pp), 1) if res_h_pp else None
    tta = median_first_action_hours(history_by_ticket,
                                    {t.id: t.opened_at for t in tickets}) \
        if history_by_ticket is not None else None
    speed = [
        _kpi("mttr", "MTTR", _fmt_hours(mttr_p), raw=mttr_p,
             delta=_delta(mttr_p, mttr_pp, lower_is_better=True),
             definition="Mean time to resolve (window resolved)."),
        _kpi("p50", "Median TTR", _fmt_hours(_percentile(res_h_p, 50)),
             definition="50th percentile time-to-resolve this window."),
        _kpi("p90", "P90 TTR", _fmt_hours(_percentile(res_h_p, 90)),
             definition="90th percentile time-to-resolve — the slow tail."),
        _kpi("avg_age", "Avg open age",
             _fmt_hours(round(sum(ages) / len(ages), 1) if ages else None),
             definition="Mean age of currently open tickets."),
        _kpi("oldest", "Oldest open",
             _fmt_hours(round(max(ages), 1) if ages else None),
             definition="Age of the oldest open ticket."),
        (_kpi("tta", "Time to first action", _fmt_hours(tta), raw=tta,
              definition="Median hours from open to the FIRST status change. "
                         "Approximation — comments aren't ingested, so true "
                         "first-response time is not measurable.",
              note="approx" + (f" · {history_note}" if history_note else ""))
         if history_by_ticket else
         _na("tta", "Time to first action",
             history_note or "No status-change history available yet.")),
    ]

    # -- SLA --
    crit_res = [t for t in resolved_p if t.severity in ("sev1", "sev2")]
    sla = [
        _kpi("sla", "SLA compliance",
             (f"{compliance(resolved_p)}%" if compliance(resolved_p) is not None else "—"),
             raw=compliance(resolved_p),
             delta=_delta(compliance(resolved_p), compliance(resolved_pp)),
             definition="Resolved-with-SLA met on time, this window vs prior."),
        _kpi("breached", "Breached open", states.get("breached", 0),
             definition="Open tickets already past their SLA."),
        _kpi("at_risk", "At risk ≤8h", states.get("at_risk", 0),
             definition="Open tickets whose SLA expires within 8 hours."),
        _kpi("missed", "Missed in window",
             sum(1 for t in resolved_p
                 if t.sla_due_at and t.resolved_at and t.resolved_at > t.sla_due_at),
             definition="Tickets resolved after their SLA this window."),
        _kpi("crit_sla", "Sev1/2 SLA",
             (f"{compliance(crit_res)}%" if compliance(crit_res) is not None else "—"),
             definition="SLA compliance on critical severities only.",
             note=f"n={len([t for t in crit_res if t.sla_due_at])}"),
    ]

    # -- Quality --
    if history_by_ticket:
        reopens = reopen_count(history_by_ticket)
        rrate = _pct(reopens, len(resolved_p) + reopens)
        quality = [
            _kpi("reopened", "Reopened", reopens,
                 definition="Tickets that went resolved/closed and came back open "
                            "(from status history)."),
            _kpi("reopen_rate", "Reopen rate",
                 (f"{rrate}%" if rrate is not None else "—"), raw=rrate,
                 definition="Reopened ÷ (resolved + reopened). Lower is better."),
        ]
    else:
        quality = [
            _na("reopened", "Reopened",
                history_note or "No status-change history available yet."),
            _na("reopen_rate", "Reopen rate",
                history_note or "No status-change history available yet."),
        ]
    quality += [
        _na("fcr", "First-contact resolution",
            "Needs conversation / first-touch data — the connectors ingest "
            "ticket fields, not comment threads."),
        _na("csat", "CSAT",
            "No survey source configured — this platform has no customer "
            "satisfaction signal to draw on."),
    ]

    # -- Workload & distribution --
    assignees = Counter((t.assignee or "").strip().lower() for t in open_t
                        if (t.assignee or "").strip())
    unassigned = sum(1 for t in open_t if not (t.assignee or "").strip())
    crit_open = sum(1 for t in open_t if t.severity in ("sev1", "sev2"))
    top_share = _pct(max(assignees.values()), len(open_t)) if assignees else None
    work = [
        _kpi("unassigned", "Unassigned open", unassigned,
             note=(f"{_pct(unassigned, len(open_t))}% of open" if open_t else ""),
             definition="Open tickets without an assignee."),
        _kpi("active_assignees", "Active assignees", len(assignees),
             definition="Distinct assignees holding open tickets."),
        _kpi("per_assignee", "Avg open / assignee",
             (round(sum(assignees.values()) / len(assignees), 1) if assignees else 0),
             definition="Mean open tickets per active assignee."),
        _kpi("critical_share", "Critical share",
             (f"{_pct(crit_open, len(open_t))}%" if open_t else "—"),
             definition="Share of the open backlog at Sev1/Sev2."),
        _kpi("top_load", "Top assignee load",
             (f"{top_share}%" if top_share is not None else "—"),
             definition="Share of the open backlog on the single busiest person "
                        "— concentration risk."),
    ]

    # -- AI operations --
    a = agent_stats or {}
    w = writeback_stats or {}
    enh = sum(1 for t in open_t if (t.description_enhanced or "").strip())
    plans = sum(1 for t in open_t if (t.plan_payload or {}).get("actions"))
    ai = [
        _kpi("agent_runs", "Agent runs", a.get("total", 0),
             note=(f"{_pct(a.get('succeeded', 0), a.get('total', 0))}% success"
                   if a.get("total") else ""),
             definition="Agent pipeline runs started this window, with success rate."),
        _kpi("enhance_cov", "AI-enhanced open",
             (f"{_pct(enh, len(open_t))}%" if open_t else "—"),
             definition="Open tickets with an AI-enhanced description."),
        _kpi("plan_cov", "Plan coverage",
             (f"{_pct(plans, len(open_t))}%" if open_t else "—"),
             definition="Open tickets with an AI action plan."),
        _kpi("writebacks", "Write-backs",
             (w.get("success", 0) + w.get("mock", 0)),
             note=(f"{w.get('mock', 0)} mock · {w.get('failed', 0)} failed"
                   if (w.get("mock") or w.get("failed")) else ""),
             definition="Jira/ServiceNow writes this window (comments, "
                        "descriptions, escalation posts). Mock = no live "
                        "credentials configured."),
    ]

    # -- Escalations --
    e = escalation_stats or {}
    email_attempts = e.get("email_sent", 0) + e.get("email_failed", 0)
    cmt_attempts = e.get("comment_ok", 0) + e.get("comment_failed", 0)
    esc = [
        _kpi("esc_active", "Active escalations", e.get("active", 0),
             definition="Escalations currently open or in progress."),
        _kpi("esc_await", "Awaiting ack", e.get("awaiting_ack", 0),
             definition="Escalated tickets not yet acknowledged or resolved."),
        _kpi("esc_email_fail", "Email failure rate",
             (f"{_pct(e.get('email_failed', 0), email_attempts)}%"
              if email_attempts else "—"),
             definition="Failed ÷ attempted escalation emails (window)."),
        _kpi("esc_cmt_fail", "Comment failure rate",
             (f"{_pct(e.get('comment_failed', 0), cmt_attempts)}%"
              if cmt_attempts else "—"),
             definition="Failed ÷ attempted Jira/ServiceNow escalation posts "
                        "(window)."),
    ]

    categories = [
        {"key": "volume", "label": "Volume & flow", "kpis": vol},
        {"key": "speed", "label": "Speed", "kpis": speed},
        {"key": "sla", "label": "SLA", "kpis": sla},
        {"key": "quality", "label": "Quality", "kpis": quality},
        {"key": "workload", "label": "Workload & distribution", "kpis": work},
        {"key": "ai", "label": "AI operations", "kpis": ai},
        {"key": "escalations", "label": "Escalations", "kpis": esc},
    ]
    total = sum(len(c["kpis"]) for c in categories)
    avail = sum(1 for c in categories for k in c["kpis"] if k["available"])
    return {"categories": categories, "total": total, "available": avail}
