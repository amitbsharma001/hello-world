# Integrate in 30 minutes

A copy‑paste path to drop the approvals engine into an **existing** Django + Celery
project and have a working, production‑safe approve‑then‑run‑task loop.

Everything is env‑driven — you change settings once and configure per environment
with variables (see `.env.example`).

---

## 1. Copy the apps in  (2 min)

Copy `apps/approvals/` (and `apps/dynamic_forms/` if you want the form builder)
into your project, and add the deps to your requirements:

```
djangorestframework  django-filter  drf-spectacular  celery  redis  psycopg[binary]
```

---

## 2. Paste this into `settings.py`  (5 min)

```python
import os

INSTALLED_APPS += [
    "rest_framework", "django_filters", "drf_spectacular",
    "apps.approvals",
    "apps.dynamic_forms",            # optional (form builder)
]

MIDDLEWARE += ["apps.approvals.audit.AuditContextMiddleware"]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
}

# Celery
CELERY_BROKER_URL = os.environ["CELERY_BROKER_URL"]
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_EAGER", "0") == "1"

# Notifications + email (SMTP turns on automatically when EMAIL_HOST is set)
NOTIFICATION_CHANNELS = os.environ.get("NOTIFICATION_CHANNELS", "inapp,email").split(",")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@example.com")
if os.environ.get("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ["EMAIL_HOST"]
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"

# Production hardening (auto-on when DEBUG is False)
if not DEBUG:
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]
```

Add the routes to your root `urls.py` by copying the URL block from this
project's `config/urls.py` (the DRF router for `workflows / forms / submissions /
approval-requests / approval-tasks / automation-actions / action-runs`, plus the
`/approvals/console/`, `/forms-builder/`, and public `/forms/<token>/` paths). The
combined router lives at the project level because `dynamic_forms` depends on
`approvals` (one direction), so:

```python
# config/urls.py  — paste the block from this repo, mounted under your API prefix
urlpatterns += [ path("api/v1/", include(router.urls)), ... ]
```

Wire Celery (once) in your `celery.py`:

```python
app.autodiscover_tasks()   # finds apps.approvals.tasks automatically
```

---

## 3. Set environment variables  (3 min)

Copy `.env.example` and fill it in. Minimum for production:

```
DJANGO_SECRET_KEY=…  DJANGO_DEBUG=0  DJANGO_ALLOWED_HOSTS=app.you.com
POSTGRES_DB/USER/PASSWORD/HOST  CELERY_BROKER_URL=redis://…  CELERY_EAGER=0
EMAIL_HOST=…  DEFAULT_FROM_EMAIL=…
```

---

## 4. Migrate + define one workflow  (5 min)

```bash
python manage.py migrate
```

```python
# run once (e.g. in a release command). Idempotent.
from apps.approvals.flow import Flow
Flow.define(
    "invoice_approval",
    steps=[Flow.step("Finance", group="Finance", policy="all")],
    on_approved=Flow.run_task("apps.billing.tasks.charge_customer", args=["{subject_id}"]),
    notify_approvers=True, notify_initiator=True,
)
```

Make sure your reviewers exist as **users** and are in the `Finance` group.

---

## 5. Submit from your code  (5 min)

```python
from apps.approvals.flow import Flow
Flow.submit(invoice, by=request.user, workflow="invoice_approval")
Flow.status(invoice)        # "under_review" → "approved"
```

Or add `ApprovableMixin` to the model and call `invoice.submit_for_approval(by=user)`.

---

## 6. Run it  (5 min)

```bash
python manage.py runserver           # web
celery -A config worker -l info      # runs on_approved tasks + emails
celery -A config beat   -l info      # SLA escalation + form expiry
```

- Approvers get an email and act at **`/approvals/console/`** (it loads live data
  from `/api/v1/approval-requests/console/` and the Approve/Reject/Delegate buttons
  POST to the task API — already wired, just log in).
- Or drive everything from your own UI against `/api/v1/...`.

---

## Done — what you have

A working approve‑then‑run‑task loop: submit an object, approvers act in the
console, and your Celery task fires on approval — with audit logging, retries, and
status you can read via `obj.approval_status`, the API, or the console.

### If you only have 30 minutes, skip these for later
- Swap `SessionAuthentication` for JWT if your client is a SPA/mobile app.
- Tighten per‑viewset permissions and the public‑form throttle.
- Move webhook/deploy secrets into a secret manager.

See `PRODUCTION.md` for the full hardening runbook and `INTEGRATION.md` for the
Celery/automation patterns.
