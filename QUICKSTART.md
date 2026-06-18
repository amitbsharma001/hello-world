# Aegis demo quickstart (5 minutes)

```bash
cd aegis-pkg
python -m venv .venv && source .venv/bin/activate
pip install -e .
cd demo_project
python manage.py migrate
python manage.py aegis_demo --count 10 --analyze
python manage.py runserver
```

Open **http://127.0.0.1:8000/** → redirects to `/accounts/login/`.

## Demo accounts

| User | Pass | Role | Sees |
|---|---|---|---|
| `admin` | `admin` | staff | All tickets + dashboard + all agent runs |
| `alice` | `demo` | user | Only her tickets / agent runs |
| `bob` | `demo` | user | Only his tickets / agent runs |

---

## What's new in this build

### 1. PII redaction boundary (privacy)

Confidential data never reaches the AI API. Before any prompt is sent to
Gemini, `aegis/services/redaction.py` replaces sensitive substrings with
placeholders, and rehydrates the AI's response for the authorized user.

**Redacted categories (17):** emails, IPv4/IPv6, internal hostnames, URLs,
UUIDs, AWS keys, GitHub tokens, Slack tokens, JWTs, Stripe keys, generic
API keys (40+ char), credit cards, phone numbers, and filesystem usernames
(Unix / macOS / Windows paths).

```python
from aegis.services.redaction import sanitize, rehydrate
clean, rmap = sanitize("Email jdoe@acme.com from 10.42.7.13")
# clean == "Email <EMAIL_1> from <IP_1>"
rehydrate(clean, rmap)  # back to the original
```

Controlled by `AEGIS["REDACT_PII"] = True` (default on). The redaction map
is returned in each AI call's `meta` for audit. Swap `sanitize()` for
Presidio / spaCy NER in production without touching callers.

### 2. Header-on-top layout (drop-in sub-app)

Navigation is now a **top header**, not a left sidebar. Header and footer
are isolated, overridable partials so you can wrap any Django project's
existing chrome.

**Two ways to override:**

```django
{# Option A — override the block in a child template #}
{% block header %}{% include "myproject/_my_header.html" %}{% endblock %}
```

```
# Option B — shadow the partial in your project's templates dir:
#   templates/aegis/includes/_header.html   (and _footer.html)
# Django's template loader picks yours over the package's.
```

Mount the whole app under any URL prefix:

```python
urlpatterns += [path("ops/", include("aegis.urls"))]   # UI at /ops/, API at /ops/api/
```

### 3. Manager dashboard

`/dashboard/` (admin only) — an operations-grade summary:

- **6 hero KPIs**: open, critical (S1+S2), created 24h, resolved 7d, avg resolution time, agent runs 7d
- **14-day trend** bar chart of tickets created per day
- **Severity / status / team** breakdowns with colored meters
- **Workload by owner**
- **AI spend** (calls / tokens / cost, 7d)
- **Agent throughput** by agent type
- **Recent critical tickets** + **recent agent escalations** feeds

The dashboard link appears in the header for staff only.

---

## 3-minute investor demo

1. **Sign in as admin** → land on Tickets, click **Dashboard** in the top nav.
2. **Walk the dashboard** — KPIs, 14-day trend, severity meters, AI spend.
3. **Drill into a critical ticket** from the bottom feed.
4. **Scroll to the agent timeline** — Triage → Investigator → Resolver, expand a step to show thought + tool call + observation.
5. **Show the privacy boundary** — open `/admin/aegis/aiartifact/`, point to the `redaction_map` in any artifact's meta: real PII never left the building.
6. **Sign in as alice** — Personal view, no Dashboard link, scoped tickets.

---

## Project configuration & sync gating

Only **enabled** projects are ingested. Connect a source, then choose exactly
which projects flow into the workspace.

- Configure at **`/projects/`** (admin only): per-project enable/disable
  toggles, source mapping, default team.
- Inbound items (CSV, connectors, API) are gated by
  `aegis/services/sync_gate.py`:
  - project **enabled** -> imported
  - project **disabled** -> skipped (counted)
  - project **unknown** -> auto-registered as *discovered* (disabled) and held
    back until an admin enables it
- Controlled by `AEGIS["SYNC_REQUIRE_CONFIGURED_PROJECT"]` (default `True`).
  Set to `False` for legacy open-sync.
- The dashboard gains a **project filter** and a **per-project portfolio**
  health rollup.

```python
from aegis.connectors.base import ingest
tickets, report = ingest(raw_tickets)   # only enabled projects are written
report.as_dict()  # {imported, updated, skipped_disabled, skipped_discovered, ...}
```

---

## Per-user AI configuration & ticket delivery

Each user brings their own AI API key — usage and cost are attributable per person.

- Configure at **`/settings/ai/`** (gear icon in the header): provider, model,
  API key, and optional Jira write-back credentials.
- Keys are **encrypted at rest** (`services/crypto.py` — Fernet from `SECRET_KEY`,
  with a signing fallback if `cryptography` isn't installed).
- Agents are **gated on configuration**: with
  `AEGIS["REQUIRE_USER_AI_CONFIG"] = True` (default), a user must add a key
  before enhancement / planning / the agent pipeline will run. In demo (mock)
  mode any key value unlocks them; in production the real key is used.

### Ticket delivery actions (on the ticket detail page)

- **Push description to Jira** — writes the AI-enhanced description back to the
  source issue (`services/jira_client.py`, live with the user's Jira creds,
  mock otherwise).
- **Post resolution steps as comment** — posts the resolver plan as a Jira comment.
- **Generate customer email** — drafts an empathetic / resolution email via the
  user's AI key (`services/comms.py`), shown inline with copy-to-clipboard.

Every outbound action is recorded as a `TicketAction` for audit.

### Projects = Teams

Admins manage all projects/teams at `/projects/`; only sync-enabled projects
are ingested (see the sync-gating section above).

## Tests

```bash
PYTHONPATH=.:demo_project python -m pytest aegis/tests/ -q
# 126 passed  (incl. 27 redaction tests, 3 dashboard tests)
```

## Reset

```bash
rm demo_project/db.sqlite3
python demo_project/manage.py migrate
python demo_project/manage.py aegis_demo --count 10 --analyze
```
