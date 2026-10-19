"""One server-configured principal -> student -> assignments/calendar policy.

Call require_* BEFORE any API/store dispatch. This module performs no I/O.
Resource IDs in model results, stored plans and browser requests are data to
compare, never authority. Registered event ownership and plan approval remain
additional mandatory checks in their application/storage adapters.
"""

from __future__ import annotations

from datetime import datetime

from ..config import AppConfig, Environment
from ..domain.models import PlanVersion, SourceKind, SourceReference
from ..domain.ports import TrustedContext
from .context import AccessDenied, Boundary, ServerRunBinding, TrustedRequestContext


class ResourcePolicy:
    def __init__(self, config: AppConfig, *, environment: Environment,
                 assignments_resource_id: str | None = None) -> None:
        self._config = AppConfig.model_validate(config)
        self._config.require_environment(environment)
        # Trusted startup may select a Moodle cache instead of the configured
        # Sheet. Requests/models cannot select or expand this source binding.
        from pydantic import TypeAdapter
        from ..domain.models import ResourceId
        self.assignments_resource_id = TypeAdapter(ResourceId).validate_python(
            self._config.student.spreadsheet_id if assignments_resource_id is None else assignments_resource_id)

    @property
    def config(self) -> AppConfig:
        return self._config

    def require_principal(self, boundary: Boundary, principal_id: str) -> None:
        allowed = {"backend": self.config.student.allowed_signin_principal,
                   "agent": self.config.identities.backend_service_account,
                   "mcp": self.config.identities.agent_service_account}
        if boundary not in allowed or not principal_id or principal_id != allowed[boundary]:
            raise AccessDenied("principal_not_allowed")

    def require_binding(self, binding: ServerRunBinding, *, session_id: str, run_id: str, now: datetime) -> None:
        if (binding.owner_id != self.config.student.trusted_user_id
                or binding.signin_principal_id != self.config.student.allowed_signin_principal
                or binding.session_id != session_id or binding.run_id != run_id
                or binding.config_revision != self.config.config_revision or now >= binding.expires_at):
            raise AccessDenied("run_binding_not_allowed")

    def require_context(self, context: TrustedRequestContext) -> TrustedContext:
        if not isinstance(context, TrustedRequestContext):
            raise AccessDenied("server_request_context_required")
        context.require_active()
        if context._factory.policy is not self:
            raise AccessDenied("foreign_context_authority")
        trusted = context.trusted
        self.require_principal(context.boundary, trusted.principal_id)
        student = self.config.student
        if (trusted.owner_id, trusted.project_id, trusted.region, trusted.sheet_id,
            trusted.sheet_range, trusted.calendar_id, trusted.timezone, trusted.config_revision) != (
            student.trusted_user_id, self.config.project_id, self.config.mcp_cloud_run_region,
            student.spreadsheet_id, student.assignments_a1_range, student.calendar_id,
            student.timezone, self.config.config_revision):
            raise AccessDenied("context_resource_binding_mismatch")
        return trusted

    def require_owner(self, context: TrustedRequestContext, owner_id: str) -> None:
        if owner_id != self.require_context(context).owner_id:
            raise AccessDenied("owner_not_allowed")

    def require_calendar(self, context: TrustedRequestContext, calendar_id: str) -> None:
        if calendar_id != self.require_context(context).calendar_id:
            raise AccessDenied("calendar_not_allowed")

    def require_sheet(self, context: TrustedRequestContext, sheet_id: str, sheet_range: str) -> None:
        trusted = self.require_context(context)
        if (sheet_id, sheet_range) != (trusted.sheet_id, trusted.sheet_range):
            raise AccessDenied("sheet_not_allowed")

    def require_session(self, context: TrustedRequestContext, session_id: str) -> None:
        self.require_context(context)
        if session_id != context.session_id:
            raise AccessDenied("session_not_allowed")

    def require_run(self, context: TrustedRequestContext, run_id: str) -> None:
        if run_id != self.require_context(context).run_id:
            raise AccessDenied("run_not_allowed")

    def require_source(self, context: TrustedRequestContext, source: SourceReference, *, current_run: bool = True) -> None:
        trusted = self.require_context(context)
        source = SourceReference.model_validate(source)
        self.require_owner(context, source.owner_id)
        expected_resource = self.assignments_resource_id if source.kind == SourceKind.ASSIGNMENTS else trusted.calendar_id
        if source.resource_id != expected_resource or source.config_revision != trusted.config_revision:
            raise AccessDenied("source_resource_binding_mismatch")
        if current_run:
            self.require_run(context, source.run_id)

    def require_plan(self, context: TrustedRequestContext, plan: PlanVersion) -> None:
        """Owner/calendar/config guard, NOT approval or execution permission.

        Saved approved plans may have source lineage from an earlier run. This
        check preserves it; proposal code uses require_source(current_run=True)
        on authoritative fresh snapshots. Apply additionally checks approval,
        durable lineage, availability/etags and ownership before dispatch.
        """
        trusted = self.require_context(context)
        plan = PlanVersion.model_validate(plan)
        payload = plan.payload
        self.require_owner(context, payload.owner_id)
        self.require_calendar(context, payload.calendar_id)
        if payload.constraints.config_revision != trusted.config_revision or payload.constraints.timezone != trusted.timezone:
            raise AccessDenied("plan_configuration_mismatch")
        for source in payload.source_references:
            self.require_source(context, source, current_run=False)
