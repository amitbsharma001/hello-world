from __future__ import annotations
import json
import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_GET
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse, Http404
from django.db.models import Q
from django.utils import timezone

from .models import Ticket, AIArtifact
from .forms import TicketForm, CSVUploadForm
from .conf import conf
from .services import enhance_description, generate_plan, detect_missing_info
from .services.gemini import AIConfigRequired
from .services.sla import score as sla_score, status_label
from .connectors.base import upsert_raw, ingest
from .connectors.csv_connector import CSVConnector
from .perms import filter_for_user, can_view, is_admin

log = logging.getLogger("assist.views")


def _maybe_run_ai(ticket: Ticket, user=None):
    """Run AI analysis. In production with USE_CELERY, this would enqueue."""
    if conf.USE_CELERY:
        from . import tasks
        tasks.run_full_analysis.delay(str(ticket.id))
    else:
        try:
            enhance_description(ticket, user=user)
            ticket.refresh_from_db()
            generate_plan(ticket, user=user)
            detect_missing_info(ticket, user=user)
        except Exception as e:
            log.exception("assist.ai.failed")
            messages.error(
                None,  # noop in non-request context; will surface in view
                f"AI analysis failed: {e}",
            )


# ---- Queue ----

@login_required
def queue(request):
    qs = filter_for_user(Ticket.objects.all(), request.user)

    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")
    sev = request.GET.get("sev", "")
    team = request.GET.get("team", "")

    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description_raw__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if sev:
        qs = qs.filter(severity=sev)
    if team:
        qs = qs.filter(team__icontains=team)

    tickets = list(qs[:conf.TICKET_LIST_PAGE_SIZE * 2])
    annotated = []
    for t in tickets:
        s = sla_score(t)
        annotated.append({"t": t, "s": s, "label": status_label(s)})
    annotated.sort(key=lambda x: x["s"]["operational_priority"], reverse=True)
    annotated = annotated[: conf.TICKET_LIST_PAGE_SIZE]

    context = {
        "tickets": annotated,
        "q": q,
        "status": status,
        "sev": sev,
        "team": team,
        "status_choices": Ticket.Status.choices,
        "severity_choices": Ticket.Severity.choices,
        "total": qs.count(),
        "open_count": qs.exclude(status__in=["resolved", "closed"]).count(),
        "critical_count": sum(1 for x in annotated if x["label"] == "critical"),
        "is_admin_user": is_admin(request.user),
    }
    return render(request, "aegis/queue.html", context)


# ---- Detail ----

@login_required
def detail(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not can_view(request.user, ticket):
        raise Http404("Ticket not found")
    s = sla_score(ticket)
    context = {
        "ticket": ticket,
        "score": s,
        "label": status_label(s),
        "enhanced": ticket.enhanced_payload,
        "plan": ticket.plan_payload,
        "missing": ticket.missing_info_payload,
        "history": ticket.history.all()[:20],
        "artifacts": ticket.artifacts.all()[:10],
        "is_admin_user": is_admin(request.user),
        "runs": _agent_runs_for(ticket),
    }
    return render(request, "aegis/ticket_detail.html", context)


# ---- Create ----

@login_required
def create(request):
    if request.method == "POST":
        form = TicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.source = "manual"
            ticket.owner = request.user
            ticket.opened_at = timezone.now()
            ticket.save()
            run_ai = request.POST.get("run_ai") == "on"
            if run_ai:
                _maybe_run_ai(ticket, user=request.user)
                ticket.refresh_from_db()
            messages.success(request, "Ticket created.")
            return redirect("aegis:detail", ticket_id=ticket.id)
    else:
        form = TicketForm()
    return render(request, "aegis/ticket_form.html", {"form": form})


# ---- AI actions (HTMX endpoints) ----

def _check_ticket_access(request, ticket_id):
    """Get ticket if user can access it, else 404."""
    ticket = get_object_or_404(Ticket, id=ticket_id)
    if not can_view(request.user, ticket):
        raise Http404("Ticket not found")
    return ticket


def _htmx_config_required(request):
    """Return an HTMX-friendly prompt to configure the AI key."""
    return render(request, "aegis/partials/config_required.html", status=200)


@login_required
@require_POST
def action_enhance(request, ticket_id):
    ticket = _check_ticket_access(request, ticket_id)
    try:
        enhance_description(ticket, force=True, user=request.user)
        ticket.refresh_from_db()
    except AIConfigRequired:
        return _htmx_config_required(request)
    except Exception as e:
        log.exception("enhance.failed")
        return _htmx_error(f"Enhancement failed: {e}")
    return render(request, "aegis/partials/enhanced_panel.html", {"ticket": ticket})


@login_required
@require_POST
def action_plan(request, ticket_id):
    ticket = _check_ticket_access(request, ticket_id)
    try:
        generate_plan(ticket, force=True, user=request.user)
        ticket.refresh_from_db()
    except AIConfigRequired:
        return _htmx_config_required(request)
    except Exception as e:
        log.exception("plan.failed")
        return _htmx_error(f"Plan generation failed: {e}")
    return render(request, "aegis/partials/plan_panel.html", {"ticket": ticket, "plan": ticket.plan_payload})


@login_required
@require_POST
def action_missing(request, ticket_id):
    ticket = _check_ticket_access(request, ticket_id)
    try:
        detect_missing_info(ticket, force=True, user=request.user)
        ticket.refresh_from_db()
    except AIConfigRequired:
        return _htmx_config_required(request)
    except Exception as e:
        log.exception("missing.failed")
        return _htmx_error(f"Missing-info detection failed: {e}")
    return render(request, "aegis/partials/missing_panel.html", {"ticket": ticket, "missing": ticket.missing_info_payload})


@login_required
@require_POST
def action_run_all(request, ticket_id):
    ticket = _check_ticket_access(request, ticket_id)
    try:
        enhance_description(ticket, force=True, user=request.user)
        ticket.refresh_from_db()
        generate_plan(ticket, force=True, user=request.user)
        detect_missing_info(ticket, force=True, user=request.user)
        ticket.refresh_from_db()
    except AIConfigRequired:
        return _htmx_config_required(request)
    except Exception as e:
        log.exception("run_all.failed")
        return _htmx_error(f"AI run failed: {e}")
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse("aegis:detail", args=[str(ticket.id)])
    return response


@login_required
@require_POST
def action_status(request, ticket_id):
    ticket = _check_ticket_access(request, ticket_id)
    new_status = request.POST.get("status")
    if new_status in dict(Ticket.Status.choices):
        ticket.status = new_status
        if new_status == Ticket.Status.RESOLVED and not ticket.resolved_at:
            ticket.resolved_at = timezone.now()
        ticket.save()
    return render(request, "aegis/partials/status_pill.html", {"ticket": ticket})


# ---- Agent actions (autonomous reasoning) ----

@login_required
@require_POST
def action_run_agent(request, ticket_id, agent_name):
    """Manually trigger a specific named agent on a ticket."""
    from .agents import run_named
    ticket = _check_ticket_access(request, ticket_id)
    try:
        run_named(agent_name, ticket, trigger="manual", user=request.user)
        ticket.refresh_from_db()
    except AIConfigRequired:
        return _htmx_config_required(request)
    except Exception as e:
        log.exception("agent.run_failed")
        return _htmx_error(f"Agent run failed: {e}")
    return render(request, "aegis/partials/agent_timeline.html",
                  {"ticket": ticket, "runs": _agent_runs_for(ticket)})


@login_required
@require_POST
def action_run_pipeline(request, ticket_id):
    """Run the full multi-agent pipeline (Triage -> maybe Investigator -> Resolver)."""
    from .agents import run_pipeline
    ticket = _check_ticket_access(request, ticket_id)
    try:
        run_pipeline(ticket, trigger="manual_pipeline", user=request.user)
        ticket.refresh_from_db()
    except AIConfigRequired:
        return _htmx_config_required(request)
    except Exception as e:
        log.exception("pipeline.failed")
        return _htmx_error(f"Pipeline failed: {e}")
    return render(request, "aegis/partials/agent_timeline.html",
                  {"ticket": ticket, "runs": _agent_runs_for(ticket)})


@login_required
def agents_inbox(request):
    """Org-wide feed of recent agent runs. Admins see all; users see only theirs."""
    from .models import AgentRun
    qs = AgentRun.objects.select_related("ticket").order_by("-started_at")
    if not is_admin(request.user):
        qs = qs.filter(ticket__owner=request.user)
    runs = list(qs[:50])
    # Pull recommended actions out of each run
    for r in runs:
        final = (r.output or {}).get("final") or {}
        r.recommended_actions = final.get("recommended_actions") or []
        r.recommended_severity = final.get("recommended_severity")
        r.summary = final.get("summary") or ""
        r.steps_count = r.steps.count()
    return render(request, "aegis/agents_inbox.html", {
        "runs": runs,
        "is_admin_user": is_admin(request.user),
    })


@login_required
def dashboard(request):
    """Portfolio command center: diagnostic + predictive analytics, time-range aware. Admin only."""
    from .models import AgentRun, Project, TeamMember
    from datetime import timedelta
    from collections import Counter, defaultdict

    if not is_admin(request.user):
        raise Http404()

    now = timezone.now()
    risk_window = now + timedelta(hours=8)
    RESOLVED_STATES = ("resolved", "closed")
    SLA_TARGET = {"sev1": 4, "sev2": 12, "sev3": 48, "sev4": 120}

    # ---- Time-range selector (period-over-period comparison) ----
    ALLOWED = {"7": 7, "30": 30, "90": 90}
    range_key = request.GET.get("range", "30")
    period_days = ALLOWED.get(range_key, 30)
    period_label = f"{period_days}d"
    p_start = now - timedelta(days=period_days)
    pp_start = now - timedelta(days=period_days * 2)

    # ---- Project filter (scope all analytics to one project, or all) ----
    project_options = list(Project.objects.filter(sync_enabled=True).order_by("name"))
    project_filter = request.GET.get("project", "")
    active_project = None
    base_qs = Ticket.objects.select_related("owner", "project")
    if project_filter:
        active_project = next((p for p in project_options if str(p.id) == project_filter), None)
        if active_project is None:
            active_project = Project.objects.filter(id=project_filter).first()
        if active_project:
            base_qs = base_qs.filter(project=active_project)

    tickets = list(base_qs.all())

    # ---- Team-label filter (scope all analytics to a labelled group's tickets,
    #      the same way the project filter scopes to a project) ----
    label_options = sorted({m.label for m in TeamMember.objects.exclude(label="")})
    label_filter = (request.GET.get("label") or "").strip()
    if label_filter and label_filter not in label_options:
        label_filter = ""
    if label_filter:
        labeled_accounts = {a.lower()
                            for m in TeamMember.objects.filter(label=label_filter)
                            for a in m.all_accounts}
        tickets = [t for t in tickets
                   if t.assignee and t.assignee.strip().lower() in labeled_accounts]

    def is_open(t):     return t.status not in RESOLVED_STATES
    def is_resolved(t): return t.resolved_at is not None
    def age_h(t):       return (now - t.opened_at).total_seconds() / 3600
    def res_h(t):       return (t.resolved_at - t.opened_at).total_seconds() / 3600
    def in_window(dt, a, b): return dt is not None and a <= dt < b

    open_tickets = [t for t in tickets if is_open(t)]
    resolved_tickets = [t for t in tickets if is_resolved(t)]

    def sla_state(t):
        if not t.sla_due_at: return "none"
        if t.resolved_at:    return "met" if t.resolved_at <= t.sla_due_at else "missed"
        if t.sla_due_at < now:           return "breached"
        if t.sla_due_at <= risk_window:  return "at_risk"
        return "on_track"

    def hours_to_breach(t):
        return round((t.sla_due_at - now).total_seconds() / 3600, 1) if t.sla_due_at else None

    def compliance(lst):
        ws = [t for t in lst if t.sla_due_at]
        if not ws: return None
        return round(sum(1 for t in ws if t.resolved_at <= t.sla_due_at) / len(ws) * 100)

    def mttr(lst):
        ds = [res_h(t) for t in lst]
        return round(sum(ds) / len(ds), 1) if ds else 0

    def percentile(vals, p):
        if not vals: return 0
        s = sorted(vals); k = (len(s) - 1) * p / 100
        f = int(k); c = min(f + 1, len(s) - 1)
        return round(s[f] + (s[c] - s[f]) * (k - f), 1)

    # ---- Period-scoped flow ----
    created_p  = [t for t in tickets if in_window(t.opened_at, p_start, now)]
    created_pp = [t for t in tickets if in_window(t.opened_at, pp_start, p_start)]
    resolved_p  = [t for t in tickets if in_window(t.resolved_at, p_start, now)]
    resolved_pp = [t for t in tickets if in_window(t.resolved_at, pp_start, p_start)]

    def delta(cur, prev):
        d = cur - prev
        return {"cur": cur, "prev": prev, "diff": d,
                "pct": round((d / prev * 100)) if prev else (100 if cur else 0)}

    sla_all = compliance(resolved_tickets); sla_all = sla_all if sla_all is not None else 100
    sla_p, sla_pp = compliance(resolved_p), compliance(resolved_pp)
    mttr_all = mttr(resolved_tickets)
    mttr_p, mttr_pp = mttr(resolved_p), mttr(resolved_pp)
    net_flow_p = len(created_p) - len(resolved_p)
    net_flow_pp = len(created_pp) - len(resolved_pp)

    # ---- Cycle-time percentiles (tail latency) ----
    res_hours = [res_h(t) for t in resolved_tickets]
    cycle = {"p50": percentile(res_hours, 50),
             "p90": percentile(res_hours, 90),
             "p95": percentile(res_hours, 95),
             "n": len(res_hours)}

    # ---- MTTR by severity ----
    mttr_by_sev = []
    for k, label in [("sev1", "Sev 1"), ("sev2", "Sev 2"), ("sev3", "Sev 3"), ("sev4", "Sev 4")]:
        grp = [t for t in resolved_tickets if t.severity == k]
        actual = mttr(grp); target = SLA_TARGET[k]
        mttr_by_sev.append({"key": k, "label": label, "actual": actual, "target": target,
                            "n": len(grp), "over": actual > target and len(grp) > 0,
                            "pct": min(100, round(actual / (target * 1.5) * 100)) if target else 0})

    # ---- Core counts ----
    breached = [t for t in open_tickets if sla_state(t) == "breached"]
    at_risk  = [t for t in open_tickets if sla_state(t) == "at_risk"]
    escalations = [t for t in open_tickets if t.severity in ("sev1", "sev2")]
    unassigned = [t for t in open_tickets if not t.assignee]

    # ---- Aging distribution ----
    buckets = [("< 1 day", 0, 24), ("1-3 days", 24, 72), ("3-7 days", 72, 168), ("> 1 week", 168, 1e9)]
    aging_dist = []
    for label, lo, hi in buckets:
        n = sum(1 for t in open_tickets if lo <= age_h(t) < hi)
        aging_dist.append({"label": label, "n": n})
    max_bucket = max((b["n"] for b in aging_dist), default=1) or 1
    for b in aging_dist:
        b["pct"] = round(b["n"] / max_bucket * 100)
    stale_count = sum(1 for t in open_tickets if age_h(t) >= 168)

    # ---- Status bottleneck ----
    status_stuck = []
    by_status = defaultdict(list)
    for t in open_tickets:
        by_status[t.status].append(t)
    for value, label in Ticket.Status.choices:
        if value in RESOLVED_STATES: continue
        grp = by_status.get(value, [])
        if not grp: continue
        status_stuck.append({"key": value, "label": label, "n": len(grp),
                             "avg_age": round(sum(age_h(t) for t in grp) / len(grp), 1),
                             "bottleneck": value in ("blocked", "waiting_customer", "waiting_engineering")})
    status_stuck.sort(key=lambda s: s["avg_age"], reverse=True)

    # ---- 6-week trends ----
    weeks = []
    for w in range(5, -1, -1):
        w_start = now - timedelta(days=7 * (w + 1)); w_end = now - timedelta(days=7 * w)
        weeks.append({
            "label": w_end.strftime("%b %d"),
            "created": sum(1 for t in tickets if in_window(t.opened_at, w_start, w_end)),
            "resolved": sum(1 for t in tickets if in_window(t.resolved_at, w_start, w_end)),
            "backlog": sum(1 for t in tickets if t.opened_at <= w_end and (t.resolved_at is None or t.resolved_at > w_end)),
        })
    max_wk_flow = max([max(w["created"], w["resolved"]) for w in weeks] + [1])
    max_backlog = max([w["backlog"] for w in weeks] + [1])
    for w in weeks:
        w["created_pct"] = round(w["created"] / max_wk_flow * 100)
        w["resolved_pct"] = round(w["resolved"] / max_wk_flow * 100)
        w["backlog_pct"] = round(w["backlog"] / max_backlog * 100)

    # ---- PREDICTIVE: outlook / forecast (run-rate; see services/forecasting.py) ----
    from .services.forecasting import project_backlog
    proj = project_backlog(
        [w["created"] for w in weeks],
        [w["resolved"] for w in weeks],
        len(open_tickets),
        window=4,
    )
    breaches_48h = sum(1 for t in open_tickets
                       if t.sla_due_at and now < t.sla_due_at <= now + timedelta(hours=48))
    forecast = {
        "backlog": proj["backlog"],
        "breaches_48h": breaches_48h,
        "expected_resolutions_7d": proj["expected_resolutions_7d"],
        "expected_intake_7d": proj["expected_intake_7d"],
    }

    # ---- Team scorecard ----
    team_names = sorted({t.team for t in tickets if t.team})
    team_rows = []
    for name in team_names:
        tt = [t for t in tickets if t.team == name]
        tt_open = [t for t in tt if is_open(t)]
        tt_c = [t for t in tt if in_window(t.opened_at, p_start, now)]
        tt_r = [t for t in tt if in_window(t.resolved_at, p_start, now)]
        tt_rs = [t for t in tt if t.resolved_at and t.sla_due_at]
        tt_met = [t for t in tt_rs if t.resolved_at <= t.sla_due_at]
        ages = [age_h(t) for t in tt_open]
        team_rows.append({
            "name": name, "open": len(tt_open),
            "critical": sum(1 for t in tt_open if t.severity in ("sev1", "sev2")),
            "at_risk": sum(1 for t in tt_open if sla_state(t) in ("breached", "at_risk")),
            "avg_age": round(sum(ages) / len(ages), 1) if ages else 0,
            "resolved_p": len(tt_r), "net_flow": len(tt_c) - len(tt_r),
            "sla_pct": round(len(tt_met) / len(tt_rs) * 100) if tt_rs else 100})
    team_rows.sort(key=lambda r: (r["critical"], r["at_risk"], r["open"]), reverse=True)
    max_team_open = max((r["open"] for r in team_rows), default=1) or 1
    for r in team_rows:
        r["load_pct"] = round(r["open"] / max_team_open * 100)

    # ---- Hotspots ----
    crit_open = [t for t in open_tickets if t.severity in ("sev1", "sev2")]
    hot = Counter(t.team or "—" for t in crit_open)
    total_crit = len(crit_open) or 1
    hotspots = [{"team": tm, "n": n, "pct": round(n / total_crit * 100)} for tm, n in hot.most_common(4)]

    # ---- Workload ----
    oc = Counter(t.owner.username for t in open_tickets if t.owner)
    total_owned = sum(oc.values()) or 1
    max_owner = max(oc.values(), default=1) or 1
    owners = [{"username": u, "n": n, "pct": round(n / max_owner * 100),
               "share": round(n / total_owned * 100), "overloaded": n >= max(4, max_owner)}
              for u, n in oc.most_common(6)]

    # ---- SLA watchlist ----
    watchlist = sorted([t for t in open_tickets if t.sla_due_at], key=lambda t: t.sla_due_at)[:6]
    watchlist_rows = [{"t": t, "state": sla_state(t), "htb": hours_to_breach(t)} for t in watchlist]

    # ---- Agent leverage ----
    runs_qs = AgentRun.objects.filter(started_at__gte=p_start)
    runs_count = runs_qs.count()
    touched = runs_qs.values("ticket").distinct().count()
    auto = {"runs": runs_count, "tickets_touched": touched,
            "coverage_pct": round(touched / (len(tickets) or 1) * 100),
            "minutes_saved": runs_count * 12}

    # ---- Health score + trend (see services/health.py) ----
    from .services.health import compute_health, subscores as _health_subs
    pct_stale = (stale_count / len(open_tickets) * 100) if open_tickets else 0
    _h = compute_health(sla_all, net_flow_p, len(escalations), pct_stale)
    health, grade, hstatus = _h["health"], _h["grade"], _h["status"]
    _s = _h["subscores"]
    s_sla, s_backlog, s_escal, s_aging = _s["sla"], _s["backlog"], _s["escalations"], _s["aging"]
    prior_sla = sla_pp if sla_pp is not None else s_sla
    prior_health = compute_health(prior_sla, net_flow_pp, len(escalations), pct_stale)["health"]
    health_trend = health - prior_health
    gauge_dash = round(health / 100 * 326.7, 1)
    gauge_gap = round(326.7 - gauge_dash, 1)
    health_factors = [
        {"label": "SLA compliance", "score": round(s_sla), "weight": "40%", "points": round(0.40 * s_sla)},
        {"label": "Backlog flow",   "score": round(s_backlog), "weight": "25%", "points": round(0.25 * s_backlog)},
        {"label": "Escalations",    "score": round(s_escal), "weight": "20%", "points": round(0.20 * s_escal)},
        {"label": "Aging",          "score": round(s_aging), "weight": "15%", "points": round(0.15 * s_aging)},
    ]

    # ---- Auto insights ----
    insights = []
    if net_flow_p > 0:
        insights.append({"level": "warning", "title": "Backlog is growing",
            "body": f"{len(created_p)} created vs {len(resolved_p)} resolved in the last {period_label} (net +{net_flow_p}). Resolution isn't keeping pace with intake."})
    elif net_flow_p < 0:
        insights.append({"level": "positive", "title": "Backlog is burning down",
            "body": f"{len(resolved_p)} resolved vs {len(created_p)} created in the last {period_label} (net {net_flow_p})."})
    if forecast["breaches_48h"]:
        insights.append({"level": "warning", "title": f"{forecast['breaches_48h']} breach{'es' if forecast['breaches_48h']!=1 else ''} forecast in 48h",
            "body": f"{forecast['breaches_48h']} open ticket{'s are' if forecast['breaches_48h']!=1 else ' is'} due to breach SLA within the next two days."})
    if breached:
        insights.append({"level": "critical", "title": f"{len(breached)} ticket{'s' if len(breached)!=1 else ''} past SLA",
            "body": f"{len(breached)} open ticket{'s have' if len(breached)!=1 else ' has'} already breached, with {len(at_risk)} at risk within 8 hours."})
    wt = next((r for r in team_rows if r["sla_pct"] < 60 and (r["critical"] or r["at_risk"])), None)
    if wt:
        insights.append({"level": "critical", "title": f"{wt['name']} needs attention",
            "body": f"{wt['name']} has {wt['critical']} critical and {wt['at_risk']} at-risk tickets, SLA at {wt['sla_pct']}%."})
    s1 = next((m for m in mttr_by_sev if m["key"] == "sev1" and m["over"]), None)
    if s1:
        insights.append({"level": "warning", "title": "Sev 1 resolution is slow",
            "body": f"Sev 1 MTTR is {s1['actual']}h against a {s1['target']}h target (p90 across all sev: {cycle['p90']}h)."})
    ov = next((o for o in owners if o["overloaded"]), None)
    if ov:
        insights.append({"level": "warning", "title": f"{ov['username']} is overloaded",
            "body": f"{ov['username']} holds {ov['n']} open tickets — {ov['share']}% of all active work. Consider rebalancing."})
    if backlog_outlook["state"] == "clearing":
        insights.append({"level": "positive", "title": "Backlog on track to clear",
            "body": f"At the current burn rate the open backlog clears in about {backlog_outlook['weeks']} weeks."})
    if auto["runs"]:
        insights.append({"level": "positive", "title": "Agents are carrying load",
            "body": f"Autonomous agents ran {auto['runs']} times across {auto['tickets_touched']} tickets — ~{auto['minutes_saved']} analyst-minutes saved."})
    order = {"critical": 0, "warning": 1, "info": 2, "positive": 3}
    insights.sort(key=lambda x: order.get(x["level"], 9))
    insights = insights[:5]

    exec_kpis = {
        "open": len(open_tickets), "sla_all": sla_all, "sla_p": sla_p, "sla_pp": sla_pp,
        "breached": len(breached), "at_risk": len(at_risk),
        "mttr_all": mttr_all, "mttr_p": mttr_p, "mttr_pp": mttr_pp,
        "created": delta(len(created_p), len(created_pp)),
        "resolved": delta(len(resolved_p), len(resolved_pp)),
        "net_flow_p": net_flow_p, "net_flow_pp": net_flow_pp,
        "escalations": len(escalations), "unassigned": len(unassigned),
    }

    # ---- Per-project portfolio rollup (only when viewing all projects) ----
    project_rows = []
    if not active_project:
        proj_ids = {t.project_id for t in tickets if t.project_id}
        id_to_proj = {p.id: p for p in Project.objects.filter(id__in=proj_ids)}
        for pid in proj_ids:
            proj = id_to_proj.get(pid)
            if not proj:
                continue
            pt = [t for t in tickets if t.project_id == pid]
            pt_open = [t for t in pt if is_open(t)]
            pt_rs = [t for t in pt if t.resolved_at and t.sla_due_at]
            pt_met = [t for t in pt_rs if t.resolved_at <= t.sla_due_at]
            crit = sum(1 for t in pt_open if t.severity in ("sev1", "sev2"))
            risk = sum(1 for t in pt_open if sla_state(t) in ("breached", "at_risk"))
            sla_pct = round(len(pt_met) / len(pt_rs) * 100) if pt_rs else 100
            ph = round(0.6 * sla_pct + 0.4 * max(0, 100 - crit * 12))
            phs = "Healthy" if ph >= 80 else ("Watch" if ph >= 60 else "At risk")
            project_rows.append({
                "id": pid, "key": proj.key, "name": proj.name,
                "open": len(pt_open), "critical": crit, "at_risk": risk,
                "resolved_p": sum(1 for t in pt if in_window(t.resolved_at, p_start, now)),
                "sla_pct": sla_pct, "health": ph, "hstatus": phs,
                "last_synced_at": proj.last_synced_at,
            })
        project_rows.sort(key=lambda r: (r["health"], -r["critical"]))

    # ---- Per-team-member KPIs (configured TeamMembers, matched on assignee) ----
    from .services.performance import build_performance, thresholds as perf_thresholds
    perf_cfg = perf_thresholds()
    stale_cutoff = now - timedelta(days=perf_cfg["STALE_DAYS"])
    team_view = request.GET.get("team_view", "members")
    if team_view not in ("members", "team"):
        team_view = "members"
    member_rows = []
    members = list(TeamMember.objects.filter(sync_enabled=True))
    if label_filter:
        members = [m for m in members if m.label == label_filter]
    if members:
        # Index tickets by lowercased assignee for matching
        by_assignee = defaultdict(list)
        for t in tickets:
            if t.assignee:
                by_assignee[t.assignee.strip().lower()].append(t)
        for m in members:
            mt = []
            for acct in m.all_accounts:
                mt.extend(by_assignee.get(acct.lower(), []))
            if not mt and active_project:
                continue  # hide members with no tickets in the filtered project
            mt_open = [t for t in mt if is_open(t)]
            mt_rs = [t for t in mt if t.resolved_at and t.sla_due_at]
            mt_met = [t for t in mt_rs if t.resolved_at <= t.sla_due_at]
            ages = [age_h(t) for t in mt_open]
            mt_c = [t for t in mt if in_window(t.opened_at, p_start, now)]
            mt_r = [t for t in mt if in_window(t.resolved_at, p_start, now)]
            # cross-project span: how many distinct projects/teams this person touches
            spans = {(t.project_id or t.team or "—") for t in mt}
            member_rows.append({
                "id": m.id, "name": m.name, "account": m.accounts_display,
                "source": m.systems_display,
                "open": len(mt_open),
                "critical": sum(1 for t in mt_open if t.severity in ("sev1", "sev2")),
                "at_risk": sum(1 for t in mt_open if sla_state(t) in ("breached", "at_risk")),
                "avg_age": round(sum(ages) / len(ages), 1) if ages else 0,
                "resolved_p": len(mt_r),
                "created_p": len(mt_c),
                "net_flow": len(mt_c) - len(mt_r),
                "sla_met": len(mt_met),
                "sla_total": len(mt_rs),
                "sla_pct": round(len(mt_met) / len(mt_rs) * 100) if mt_rs else 100,
                "stale_open": sum(1 for t in mt_open
                                  if (t.last_external_update_at or t.opened_at) < stale_cutoff),
                "projects": len(spans),
            })
        member_rows.sort(key=lambda r: (r["critical"], r["at_risk"], r["open"]), reverse=True)
        max_member_open = max((r["open"] for r in member_rows), default=1) or 1
        for r in member_rows:
            r["load_pct"] = round(r["open"] / max_member_open * 100)
    member_summary = {
        "tracked": len(members),
        "with_work": sum(1 for r in member_rows if r["open"] > 0),
        "overloaded": [r["name"] for r in member_rows if r["open"] >= max(4, (max(((rr["open"]) for rr in member_rows), default=1)))][:1],
    }
    perf = build_performance(member_rows, perf_cfg)

    # ---- Full KPI catalog (grouped; honest about what isn't measurable) ----
    from .services.kpis import compute_kpi_catalog
    from .models import TicketHistory, TicketAction, Escalation, EscalationItem
    from django.db.models import Count as _Count, Q as _Q

    hist_map = None
    hist_note = ""
    tids = [t.id for t in tickets]
    if len(tids) > 2000:
        hist_note = f"history skipped — {len(tids)} tickets in scope (cap 2000)"
    elif tids:
        hist_map = defaultdict(list)
        for tid, occurred, payload in (TicketHistory.objects
                                       .filter(ticket_id__in=tids)
                                       .order_by("occurred_at")
                                       .values_list("ticket_id", "occurred_at", "payload")):
            hist_map[tid].append((occurred, (payload or {}).get("status")))
        hist_map = dict(hist_map)
        if not hist_map:
            hist_map, hist_note = None, "no status-change history recorded yet"

    _ar = AgentRun.objects.filter(started_at__gte=p_start)
    agent_stats = {"total": _ar.count(),
                   "succeeded": _ar.filter(status="succeeded").count()}
    _wb = TicketAction.objects.filter(created_at__gte=p_start)
    writeback_stats = {
        "success": _wb.filter(status="success").count(),
        "mock": _wb.filter(status="mock").count(),
        "failed": _wb.filter(status="failed").count(),
    }
    _esc_active = Escalation.objects.filter(status__in=("open", "in_progress"))
    _eitems = EscalationItem.objects.filter(escalation__created_at__gte=p_start)
    escalation_stats = {
        "active": _esc_active.count(),
        "awaiting_ack": EscalationItem.objects.filter(
            escalation__in=_esc_active,
            status__in=("pending", "notified")).count(),
        "email_sent": _eitems.filter(email_status="sent").count(),
        "email_failed": _eitems.filter(email_status="failed").count(),
        "comment_ok": _eitems.filter(comment_status__in=("posted", "mock")).count(),
        "comment_failed": _eitems.filter(comment_status="failed").count(),
    }
    kpi_catalog = compute_kpi_catalog(
        tickets=tickets, now=now, p_start=p_start, pp_start=pp_start,
        sla_state=sla_state, history_by_ticket=hist_map, history_note=hist_note,
        agent_stats=agent_stats, writeback_stats=writeback_stats,
        escalation_stats=escalation_stats)

    return render(request, "aegis/dashboard.html", {
        "range_key": range_key, "period_label": period_label,
        "ranges": [("7", "7 days"), ("30", "30 days"), ("90", "90 days")],
        "project_options": project_options, "project_filter": project_filter,
        "active_project": active_project, "project_rows": project_rows,
        "member_rows": member_rows, "member_summary": member_summary,
        "perf": perf, "team_view": team_view,
        "label_options": label_options, "label_filter": label_filter,
        "kpi_catalog": kpi_catalog,
        "health": health, "grade": grade, "hstatus": hstatus,
        "health_trend": health_trend, "prior_health": prior_health,
        "gauge_dash": gauge_dash, "gauge_gap": gauge_gap,
        "health_factors": health_factors, "insights": insights,
        "exec_kpis": exec_kpis, "cycle": cycle, "forecast": forecast,
        "team_rows": team_rows, "weeks": weeks, "mttr_by_sev": mttr_by_sev,
        "aging_dist": aging_dist, "stale_count": stale_count,
        "status_stuck": status_stuck, "hotspots": hotspots,
        "watchlist_rows": watchlist_rows, "owners": owners, "auto": auto,
        "is_admin_user": True,
    })



@login_required
def projects(request):
    """Project configuration — admin only."""
    from .models import Project
    from django.db.models import Count
    if not is_admin(request.user):
        raise Http404()
    qs = (Project.objects.annotate(ticket_count=Count("tickets"))
          .order_by("-sync_enabled", "discovered", "name"))
    projects = list(qs)
    enabled = [p for p in projects if p.sync_enabled]
    discovered = [p for p in projects if p.discovered and not p.sync_enabled]
    disabled = [p for p in projects if not p.sync_enabled and not p.discovered]
    return render(request, "aegis/projects.html", {
        "projects": projects, "enabled": enabled, "discovered": discovered,
        "disabled": disabled, "source_choices": Project.Source.choices,
        "require_configured": getattr(conf, "SYNC_REQUIRE_CONFIGURED_PROJECT", True),
        "is_admin_user": True,
    })


@login_required
@require_POST
def project_toggle(request, project_id):
    from .models import Project
    if not is_admin(request.user):
        raise Http404()
    project = get_object_or_404(Project, id=project_id)
    project.sync_enabled = not project.sync_enabled
    if project.sync_enabled:
        project.discovered = False
    project.save(update_fields=["sync_enabled", "discovered", "updated_at"])
    return render(request, "aegis/partials/project_row.html",
                  {"p": project, "ticket_count": project.tickets.count()})


@login_required
def project_form(request, project_id=None):
    from .models import Project, TeamMember
    if not is_admin(request.user):
        raise Http404()
    project = get_object_or_404(Project, id=project_id) if project_id else None
    if request.method == "POST":
        key = (request.POST.get("key") or "").strip().upper()
        name = (request.POST.get("name") or "").strip()
        source = request.POST.get("source") or Project.Source.MANUAL
        external_key = (request.POST.get("external_key") or "").strip()
        default_team = (request.POST.get("default_team") or "").strip()
        description = (request.POST.get("description") or "").strip()
        sync_enabled = request.POST.get("sync_enabled") == "on"
        member_scope = request.POST.get("member_scope") or Project.MemberScope.ALL
        member_ids = request.POST.getlist("members")
        if not key or not name:
            messages.error(request, "Key and name are required.")
        else:
            dup = Project.objects.filter(key__iexact=key)
            if project:
                dup = dup.exclude(id=project.id)
            if dup.exists():
                messages.error(request, f"A project with key '{key}' already exists.")
            else:
                if project is None:
                    project = Project(key=key)
                project.key = key; project.name = name; project.source = source
                project.external_key = external_key; project.default_team = default_team
                project.description = description; project.sync_enabled = sync_enabled
                project.member_scope = member_scope
                if sync_enabled:
                    project.discovered = False
                project.save()
                # Link selected team members (used when scope = team)
                if member_ids:
                    project.members.set(TeamMember.objects.filter(id__in=member_ids))
                elif member_scope == Project.MemberScope.ALL:
                    pass  # keep existing links; they're ignored when scope=all
                messages.success(request, f"Saved project {key}.")
                return redirect("aegis:projects")
    return render(request, "aegis/project_form.html", {
        "project": project,
        "source_choices": Project.Source.choices,
        "scope_choices": Project.MemberScope.choices,
        "all_members": TeamMember.objects.all(),
        "selected_member_ids": set(str(m.id) for m in project.members.all()) if project else set(),
        "is_admin_user": True,
    })


# ---- Team member configuration ----

@login_required
def teams(request):
    """Configure the people whose work to pull, across any project. Admin only."""
    from .models import TeamMember
    from django.db.models import Count
    if not is_admin(request.user):
        raise Http404()
    members = list(TeamMember.objects.annotate(project_count=Count("projects"))
                   .order_by("-sync_enabled", "name"))
    enabled = [m for m in members if m.sync_enabled]
    return render(request, "aegis/teams.html", {
        "members": members, "enabled": enabled,
        "source_choices": TeamMember.Source.choices,
        "is_admin_user": True,
    })


@login_required
@require_POST
def team_toggle(request, member_id):
    from .models import TeamMember
    if not is_admin(request.user):
        raise Http404()
    m = get_object_or_404(TeamMember, id=member_id)
    m.sync_enabled = not m.sync_enabled
    m.save(update_fields=["sync_enabled", "updated_at"])
    return render(request, "aegis/partials/team_row.html",
                  {"m": m, "project_count": m.projects.count()})


@login_required
def team_form(request, member_id=None):
    from .models import TeamMember, Project
    if not is_admin(request.user):
        raise Http404()
    member = get_object_or_404(TeamMember, id=member_id) if member_id else None
    if request.method == "POST":
        name = (request.POST.get("name") or "").strip()
        jira_account = (request.POST.get("jira_account") or "").strip()
        servicenow_account = (request.POST.get("servicenow_account") or "").strip()
        # Legacy compat: primary account/source mirror the first configured system
        account = jira_account or servicenow_account
        source = TeamMember.Source.JIRA if jira_account else TeamMember.Source.SERVICENOW
        source_url = (request.POST.get("source_url") or "").strip()
        default_team = (request.POST.get("default_team") or "").strip()
        label = (request.POST.get("label") or "").strip()
        pull_all = request.POST.get("pull_all_projects") == "on"
        sync_enabled = request.POST.get("sync_enabled") == "on"
        project_ids = request.POST.getlist("projects")
        if not name or not (jira_account or servicenow_account):
            messages.error(request, "Name and at least one account (Jira or ServiceNow) are required.")
        else:
            if member is None:
                member = TeamMember()
            member.name = name; member.account = account; member.source = source
            member.jira_account = jira_account
            member.servicenow_account = servicenow_account
            member.source_url = source_url; member.default_team = default_team
            member.label = label
            member.pull_all_projects = pull_all; member.sync_enabled = sync_enabled
            member.save()
            member.projects.set(Project.objects.filter(id__in=project_ids))
            messages.success(request, f"Saved team member {name}.")
            return redirect("aegis:teams")
    return render(request, "aegis/team_form.html", {
        "member": member,
        "source_choices": TeamMember.Source.choices,
        "all_projects": Project.objects.all(),
        "selected_project_ids": set(str(p.id) for p in member.projects.all()) if member else set(),
        "is_admin_user": True,
    })




# ---- Per-user AI configuration ----

@login_required
def ai_settings(request):
    """Each user manages their own AI + Jira credentials here."""
    from .models import UserAIConfig
    cfg, _ = UserAIConfig.objects.get_or_create(user=request.user)

    if request.method == "POST":
        cfg.provider = request.POST.get("provider") or cfg.provider
        cfg.model_name = (request.POST.get("model_name") or "").strip() or cfg.model_name
        api_key = (request.POST.get("api_key") or "").strip()
        if api_key and api_key != "__unchanged__":
            cfg.set_api_key(api_key)
        # Optional Jira write-back creds
        cfg.jira_base_url = (request.POST.get("jira_base_url") or "").strip()
        cfg.jira_email = (request.POST.get("jira_email") or "").strip()
        jira_token = (request.POST.get("jira_token") or "").strip()
        if jira_token and jira_token != "__unchanged__":
            cfg.set_jira_token(jira_token)
        # Clearing the key
        if request.POST.get("clear_api_key") == "on":
            cfg.api_key_enc = ""
        cfg.save()
        messages.success(request, "AI settings saved." +
                         ("" if cfg.is_configured else " Add an API key to enable the agents."))
        return redirect("aegis:ai_settings")

    return render(request, "aegis/ai_settings.html", {
        "cfg": cfg,
        "provider_choices": UserAIConfig.Provider.choices,
        "require_cfg": getattr(conf, "REQUIRE_USER_AI_CONFIG", True),
        "mock_mode": conf.MOCK_GEMINI,
    })


# ---- Outbound ticket actions (Jira write-back + customer email) ----

@login_required
@require_POST
def action_push_description(request, ticket_id):
    """Push the AI-enhanced description back to the source (Jira) issue."""
    from .models import TicketAction
    from .services.jira_client import JiraClient
    ticket = _check_ticket_access(request, ticket_id)

    description = ticket.description_enhanced or ""
    if not description:
        return _htmx_error("No AI-enhanced description yet. Run 'Enhance' first.")

    issue_key = ticket.source_id or ticket.id.hex[:8].upper()
    client = JiraClient.for_user(request.user)
    result = client.update_description(issue_key, description)

    action = TicketAction.objects.create(
        ticket=ticket, user=request.user,
        kind=TicketAction.Kind.PUSH_DESCRIPTION,
        status=(TicketAction.Status.MOCK if result.mock else
                TicketAction.Status.SUCCESS if result.ok else TicketAction.Status.FAILED),
        detail=result.detail,
        payload={"issue_key": issue_key, "chars": len(description)},
    )
    return render(request, "aegis/partials/action_result.html",
                  {"action": action, "result": result, "label": "Description pushed to Jira"})


@login_required
@require_POST
def action_post_steps(request, ticket_id):
    """Post the resolution steps (resolver plan) as a Jira comment."""
    from .models import TicketAction
    from .services.jira_client import JiraClient
    ticket = _check_ticket_access(request, ticket_id)

    # Build the steps text from the action plan
    plan = ticket.plan_payload or {}
    actions = plan.get("actions") or []
    if not actions:
        return _htmx_error("No resolution plan yet. Run 'Plan' or the agent pipeline first.")

    lines = ["*Resolution steps (AI-generated, reviewed by " + request.user.get_username() + "):*", ""]
    for i, a in enumerate(actions, 1):
        title = a.get("title", "Step")
        desc = a.get("description", "")
        pr = a.get("priority", "")
        lines.append(f"{i}. [{pr}] {title} — {desc}")
    steps_text = "\n".join(lines)

    issue_key = ticket.source_id or ticket.id.hex[:8].upper()
    client = JiraClient.for_user(request.user)
    result = client.add_comment(issue_key, steps_text)

    action = TicketAction.objects.create(
        ticket=ticket, user=request.user,
        kind=TicketAction.Kind.POST_COMMENT,
        status=(TicketAction.Status.MOCK if result.mock else
                TicketAction.Status.SUCCESS if result.ok else TicketAction.Status.FAILED),
        detail=result.detail,
        payload={"issue_key": issue_key, "steps": steps_text},
    )
    return render(request, "aegis/partials/action_result.html",
                  {"action": action, "result": result, "label": "Resolution steps posted as comment",
                   "preview": steps_text})


@login_required
@require_POST
def action_customer_email(request, ticket_id):
    """Generate a customer-facing communication email (draft for review)."""
    from .models import TicketAction
    from .services.comms import generate_customer_email
    ticket = _check_ticket_access(request, ticket_id)

    try:
        email, meta = generate_customer_email(ticket, user=request.user)
    except AIConfigRequired:
        return _htmx_config_required(request)
    except Exception as e:
        log.exception("customer_email.failed")
        return _htmx_error(f"Email generation failed: {e}")

    action = TicketAction.objects.create(
        ticket=ticket, user=request.user,
        kind=TicketAction.Kind.CUSTOMER_EMAIL,
        status=TicketAction.Status.SUCCESS,
        detail="Draft generated",
        payload={"subject": email.subject, "body": email.body, "tone": email.tone},
    )
    return render(request, "aegis/partials/customer_email.html",
                  {"email": email, "action": action, "ticket": ticket})


# ---- Manual sync: pull from all connectors across all configured projects ----

@login_required
def sync_now(request):
    """Sync console — shows pull-capable connectors and a 'Pull now' button. Admin only."""
    from .models import Project, TeamMember
    from .services.sync_runner import pullable_connectors
    from .connectors import registry
    if not is_admin(request.user):
        raise Http404()

    # Build a status row per pull-capable connector
    conns = []
    for name in pullable_connectors():
        cfg = (conf.CONNECTORS or {}).get(name, {})
        configured = bool(cfg)
        if name == "jira" and not configured:
            uc = getattr(request.user, "ai_config", None)
            configured = bool(uc and uc.jira_configured)
        conns.append({"name": name, "configured": configured})

    return render(request, "aegis/sync.html", {
        "connectors": conns,
        "enabled_projects": Project.objects.filter(sync_enabled=True).count(),
        "enabled_members": TeamMember.objects.filter(sync_enabled=True).count(),
        "require_configured": getattr(conf, "SYNC_REQUIRE_CONFIGURED_PROJECT", True),
        "is_admin_user": True,
    })


@login_required
@require_POST
def sync_run(request):
    """Run the pull across all connectors. Inline by default; enqueued to Celery
    when AEGIS['USE_CELERY'] is set (production scaling path)."""
    from .services.sync_runner import run_all_pulls
    if not is_admin(request.user):
        raise Http404()
    mode = request.POST.get("mode", "all")
    if mode not in ("all", "projects", "teams"):
        mode = "all"
    window = request.POST.get("window") or None
    if window not in ("1m", "3m", "6m", "1y"):
        window = None
    _modes = {"all": "Everything", "projects": "Projects only", "teams": "Teams only"}
    _windows = {None: "since last sync", "1m": "last month", "3m": "last quarter",
                "6m": "last 6 months", "1y": "last year"}
    pull_label = f"{_modes[mode]} · {_windows[window]}"
    log.info("sync_run TRIGGERED by user=%s mode=%s window=%s use_celery=%s",
             request.user.username, mode, window, conf.USE_CELERY)
    if conf.USE_CELERY:
        from . import tasks
        tasks.pull_all_connectors.delay(user_id=request.user.id, mode=mode, window=window)
        log.info("sync_run enqueued pull_all_connectors task for user_id=%s", request.user.id)
        return render(request, "aegis/partials/sync_result.html",
                      {"summary": None, "queued": True, "pull_label": pull_label})
    summary = run_all_pulls(user=request.user, mode=mode, window=window)
    log.info("sync_run COMPLETE totals=%s", summary["totals"])
    return render(request, "aegis/partials/sync_result.html",
                  {"summary": summary, "pull_label": pull_label})


def _agent_runs_for(ticket):
    """Return the most recent agent runs for a ticket, with per-step detail prefetched."""
    runs = list(ticket.agent_runs.order_by("-started_at").prefetch_related("steps")[:5])
    for r in runs:
        final = (r.output or {}).get("final") or {}
        r.recommended_actions = final.get("recommended_actions") or []
        r.recommended_severity = final.get("recommended_severity")
        r.recommended_confidence = final.get("confidence")
        r.summary = final.get("summary") or ""
        r.ordered_steps = list(r.steps.order_by("seq"))
    return runs


# ---- CSV upload ----

@login_required
def csv_upload(request):
    if request.method == "POST":
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            content = form.cleaned_data["file"].read()
            conn = CSVConnector(content=content)
            ai = form.cleaned_data["run_ai"]

            tickets, report = ingest(conn.fetch())
            for ticket in tickets:
                if not request.user.is_staff and ticket.owner_id is None:
                    ticket.owner = request.user
                    ticket.save(update_fields=["owner"])
                if ai:
                    try:
                        _maybe_run_ai(ticket, user=request.user)
                    except Exception:
                        log.exception("csv.ai_failed")

            msg = f"Imported {report.imported} new, updated {report.updated}."
            skipped = report.skipped_disabled + report.skipped_discovered
            if skipped:
                msg += f" Skipped {skipped} from unconfigured/disabled projects"
                if report.discovered_projects:
                    msg += f" ({', '.join(report.discovered_projects)} — enable in Projects to sync)"
                msg += "."
            messages.success(request, msg)
            return redirect("aegis:queue")
    else:
        form = CSVUploadForm()
    return render(request, "aegis/csv_upload.html", {"form": form})


# ---- Helpers ----

def _htmx_error(msg: str) -> HttpResponse:
    return HttpResponse(
        f'<div class="rounded-md bg-red-50 border border-red-200 p-3 text-sm text-red-700">{msg}</div>',
        status=200,  # 200 so HTMX swaps the message in
    )


# ---- Escalations: bulk-escalate tickets with per-assignee notification ----

def _escalation_ticket_qs(request):
    """Resolve the bulk-selection filters (shared by preview and create)."""
    from .models import TeamMember
    qs = Ticket.objects.select_related("project").order_by("-opened_at")
    if request.POST.get("open_only", "on") == "on":
        qs = qs.exclude(status__in=("resolved", "closed"))
    project_id = request.POST.get("project") or ""
    if project_id:
        qs = qs.filter(project_id=project_id)
    severity = request.POST.get("severity") or ""
    if severity:
        qs = qs.filter(severity=severity)
    status = request.POST.get("status") or ""
    if status:
        qs = qs.filter(status=status)
    assignee_q = (request.POST.get("assignee") or "").strip()
    if assignee_q:
        qs = qs.filter(assignee__icontains=assignee_q)
    if request.POST.get("sla_breached") == "on":
        qs = qs.filter(sla_due_at__lt=timezone.now()).exclude(
            status__in=("resolved", "closed"))
    label = (request.POST.get("label") or "").strip()
    if label:
        accounts = [a.lower()
                    for m in TeamMember.objects.filter(label=label)
                    for a in m.all_accounts]
        # Match case-insensitively against the snapshot assignee strings.
        from django.db.models import Q
        cond = Q(pk=None)
        for a in accounts:
            cond |= Q(assignee__iexact=a)
        qs = qs.filter(cond)
    return qs


@login_required
def escalations(request):
    """Escalation console: every escalation with its cycle at a glance."""
    from .models import Escalation, EscalationItem
    from django.db.models import Count, Q as _Q
    if not is_admin(request.user):
        raise Http404()
    rows = (Escalation.objects
            .annotate(total=Count("items"),
                      sent=Count("items", filter=_Q(items__email_status="sent")),
                      failed=Count("items", filter=_Q(items__email_status="failed")),
                      resolved=Count("items", filter=_Q(items__status="resolved")))
            .order_by("-created_at")[:200])
    return render(request, "aegis/escalations.html",
                  {"rows": rows, "is_admin_user": True})


@login_required
def escalation_new(request):
    """Create a bulk escalation. POST action=preview -> htmx count partial;
    POST action=create -> escalate every matching ticket (capped)."""
    from .models import Escalation, Project, TeamMember
    from .services.escalations import (create_escalation, send_escalation_emails,
                                       post_escalation_comments, settings_for)
    if not is_admin(request.user):
        raise Http404()
    cfg = settings_for()

    if request.method == "POST" and request.POST.get("action") == "preview":
        qs = _escalation_ticket_qs(request)
        total = qs.count()
        return render(request, "aegis/partials/escalation_preview.html", {
            "total": total, "sample": qs[:15], "cap": cfg["MAX_TICKETS"],
            "over_cap": total > cfg["MAX_TICKETS"],
        })

    if request.method == "POST" and request.POST.get("action") == "create":
        title = (request.POST.get("title") or "").strip()
        message = (request.POST.get("message") or "").strip()
        level = request.POST.get("level") or Escalation.Level.L2
        notify = request.POST.get("notify", "on") == "on"
        post_to_source = request.POST.get("post_to_source", "on") == "on"
        if not title or not message:
            messages.error(request, "Title and escalation message are required.")
        else:
            qs = _escalation_ticket_qs(request)
            total = qs.count()
            if total == 0:
                messages.error(request, "No tickets match the selected filters.")
            elif total > cfg["MAX_TICKETS"]:
                messages.error(request,
                               f"{total} tickets match — over the safety cap of "
                               f"{cfg['MAX_TICKETS']}. Narrow the filters or raise "
                               f"AEGIS['ESCALATION']['MAX_TICKETS'].")
            else:
                esc = create_escalation(title=title, message=message, level=level,
                                        tickets=qs, user=request.user, notify=notify,
                                        post_to_source=post_to_source)
                if conf.USE_CELERY and (notify or post_to_source):
                    from . import tasks
                    tasks.send_escalation_emails_task.delay(str(esc.id))
                    messages.success(request,
                                     f"Escalated {total} tickets — notifications queued.")
                else:
                    parts = [f"Escalated {total} tickets."]
                    if notify:
                        r = send_escalation_emails(esc)
                        parts.append(f"Emails: {r['sent']} sent, {r['failed']} failed.")
                    if post_to_source:
                        c = post_escalation_comments(esc)
                        parts.append(f"Ticket comments: {c['posted']} posted, "
                                     f"{c['mock']} mock, {c['failed']} failed.")
                    messages.success(request, " ".join(parts))
                return redirect("aegis:escalation_detail", esc_id=esc.id)

    labels = (TeamMember.objects.exclude(label="").values_list("label", flat=True)
              .distinct().order_by("label"))
    return render(request, "aegis/escalation_form.html", {
        "projects": Project.objects.order_by("name"),
        "labels": labels,
        "severities": Ticket.Severity.choices,
        "statuses": Ticket.Status.choices,
        "levels": Escalation.Level.choices,
        "cap": cfg["MAX_TICKETS"],
        "is_admin_user": True,
    })


@login_required
def escalation_detail(request, esc_id):
    from .models import Escalation
    if not is_admin(request.user):
        raise Http404()
    esc = get_object_or_404(Escalation, id=esc_id)
    items = list(esc.items.select_related("ticket"))
    counts = {
        "total": len(items),
        "sent": sum(1 for i in items if i.email_status == "sent"),
        "failed": sum(1 for i in items if i.email_status == "failed"),
        "skipped": sum(1 for i in items if i.email_status == "skipped"),
        "posted": sum(1 for i in items if i.comment_status in ("posted", "mock")),
        "comment_failed": sum(1 for i in items if i.comment_status == "failed"),
        "acknowledged": sum(1 for i in items if i.status == "acknowledged"),
        "resolved": sum(1 for i in items if i.status == "resolved"),
    }
    return render(request, "aegis/escalation_detail.html", {
        "esc": esc, "items": items, "counts": counts, "is_admin_user": True,
    })


@login_required
@require_POST
def escalation_items_update(request, esc_id):
    """Bulk item actions: ack / resolve / cancel selected (or all), or resend
    failed+pending emails."""
    from .models import Escalation, EscalationItem
    from .services.escalations import (can_transition, send_escalation_emails,
                                       post_escalation_comments)
    if not is_admin(request.user):
        raise Http404()
    esc = get_object_or_404(Escalation, id=esc_id)
    action = request.POST.get("bulk_action") or ""
    ids = request.POST.getlist("item_ids")
    qs = esc.items.all()
    if ids and "all" not in ids:
        qs = qs.filter(id__in=ids)

    if action == "resend":
        r = send_escalation_emails(esc, only_items=[i.id for i in qs])
        messages.success(request, f"Resend: {r['sent']} sent, {r['failed']} failed "
                                  f"({r['attempted']} attempted).")
    elif action == "repost":
        c = post_escalation_comments(esc, only_items=[i.id for i in qs])
        messages.success(request, f"Repost: {c['posted']} posted, {c['mock']} mock, "
                                  f"{c['failed']} failed ({c['attempted']} attempted).")
    elif action in ("acknowledged", "resolved", "cancelled"):
        changed = skipped = 0
        for item in qs:
            if can_transition(item.status, action):
                item.status = action
                item.save(update_fields=["status", "updated_at"])
                changed += 1
            else:
                skipped += 1
        log.info("escalation.items UPDATE esc=%s action=%s changed=%d skipped=%d by=%s",
                 esc.id, action, changed, skipped, request.user.username)
        msg = f"Marked {changed} item(s) {action}."
        if skipped:
            msg += f" {skipped} skipped (already resolved/cancelled)."
        messages.success(request, msg)
    else:
        messages.error(request, "Unknown action.")
    return redirect("aegis:escalation_detail", esc_id=esc.id)


@login_required
@require_POST
def escalation_close(request, esc_id):
    from .models import Escalation
    if not is_admin(request.user):
        raise Http404()
    esc = get_object_or_404(Escalation, id=esc_id)
    new_status = request.POST.get("status") or Escalation.Status.RESOLVED
    if new_status not in (Escalation.Status.RESOLVED, Escalation.Status.CANCELLED):
        new_status = Escalation.Status.RESOLVED
    esc.status = new_status
    esc.closed_at = timezone.now()
    esc.save(update_fields=["status", "closed_at", "updated_at"])
    log.info("escalation CLOSED id=%s status=%s by=%s", esc.id, new_status,
             request.user.username)
    messages.success(request, f"Escalation marked {esc.get_status_display().lower()}.")
    return redirect("aegis:escalation_detail", esc_id=esc.id)
