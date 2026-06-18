"""Jira Cloud connector. Polls JQL since cursor."""
from __future__ import annotations
import logging
from typing import Iterable
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type
from django.utils.dateparse import parse_datetime

from .base import Connector, RawTicket

log = logging.getLogger("aegis.connectors.jira")


class JiraConnector(Connector):
    name = "jira"

    def __init__(self, config=None):
        super().__init__(config)
        self.base_url = self.config.get("base_url", "").rstrip("/")
        self.email = self.config.get("email", "")
        self.token = self.config.get("api_token", "")
        self.jql = self.config.get("jql", "updated >= -7d")
        self.page_size = int(self.config.get("page_size", 50))

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            auth=(self.email, self.token),
            timeout=30,
            headers={"Accept": "application/json"},
        )

    def health_check(self) -> bool:
        if not (self.base_url and self.email and self.token):
            return False
        try:
            with self._client() as c:
                r = c.get("/rest/api/3/myself")
                return r.status_code == 200
        except Exception:
            return False

    @retry(
        retry=retry_if_exception_type((httpx.TransportError,)),
        stop=stop_after_attempt(4),
        wait=wait_exponential_jitter(initial=1, max=10),
    )
    def _get(self, client: httpx.Client, path: str, **params):
        r = client.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _enabled_project_keys(self) -> list[str]:
        """Jira project keys for projects that are configured AND sync-enabled.

        Prefers each project's external_key (the Jira project key); falls back
        to its short key. Empty list means nothing is enabled.
        """
        from ..models import Project
        keys = []
        for p in Project.objects.filter(sync_enabled=True):
            k = (p.external_key or p.key or "").strip()
            if k:
                keys.append(k)
        return keys

    def fetch(self, cursor: str | None = None) -> Iterable[RawTicket]:
        if not self.health_check():
            log.warning("aegis.jira.unconfigured_or_unreachable")
            return

        time_clause = f"updated >= '{cursor}'" if cursor else self.jql

        # Pull ONLY configured + enabled projects — don't fetch the whole instance.
        # If gating is on and no project is enabled, pull nothing.
        from ..conf import conf
        if getattr(conf, "SYNC_REQUIRE_CONFIGURED_PROJECT", True):
            project_keys = self._enabled_project_keys()
            if not project_keys:
                log.info("aegis.jira.no_enabled_projects — nothing to pull")
                return
            project_in = ", ".join(f'"{k}"' for k in project_keys)
            jql = f"project in ({project_in}) AND ({time_clause})"
            log.info("aegis.jira.scoped_pull projects=%s", project_keys)
        else:
            jql = time_clause

        start_at = 0
        with self._client() as client:
            while True:
                data = self._get(
                    client, "/rest/api/3/search",
                    jql=jql, startAt=start_at, maxResults=self.page_size,
                    fields="summary,description,status,priority,assignee,labels,components,project,created,updated",
                )
                issues = data.get("issues", [])
                if not issues:
                    return
                for issue in issues:
                    yield self._to_raw(issue)
                total = data.get("total", 0)
                start_at += len(issues)
                if start_at >= total:
                    return

    def _to_raw(self, issue: dict) -> RawTicket:
        f = issue.get("fields", {}) or {}
        components = f.get("components") or []
        team = components[0]["name"] if components else ""
        project_key = (f.get("project") or {}).get("key", "")
        return RawTicket(
            source="jira",
            source_id=issue.get("key", ""),
            source_url=f"{self.base_url}/browse/{issue.get('key', '')}",
            title=f.get("summary") or "",
            description=self._adf_to_text(f.get("description")),
            status=(f.get("status") or {}).get("name", "open"),
            severity=(f.get("priority") or {}).get("name", "Medium"),
            assignee=(f.get("assignee") or {}).get("emailAddress", "") or "",
            team=team,
            project=project_key,
            tags=list(f.get("labels", []) or []),
            opened_at=parse_datetime(f.get("created", "")) if f.get("created") else None,
            last_external_update_at=parse_datetime(f.get("updated", "")) if f.get("updated") else None,
            extra={"jira_id": issue.get("id")},
        )

    def _adf_to_text(self, adf) -> str:
        """Atlassian Document Format → flat text. Best-effort."""
        if not adf:
            return ""
        if isinstance(adf, str):
            return adf
        out = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "text":
                    out.append(node.get("text", ""))
                for child in node.get("content", []) or []:
                    walk(child)
                if node.get("type") in ("paragraph", "heading"):
                    out.append("\n")
            elif isinstance(node, list):
                for n in node:
                    walk(n)

        walk(adf)
        return "".join(out).strip()
