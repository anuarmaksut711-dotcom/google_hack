"""Explicit Cloud Run topology and student working hours; no startup network IO."""
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from ..agent.conversation import ConversationTarget
from ..agent.mcp_auth import cloud_run_audience
from ..config import AppConfig, ServiceAccount
from ..domain.models import Contract, Interval, PlanningConstraints
from ..domain.time_rules import local_time, planning_horizon


class WorkingHours(Contract):
    weekdays: tuple[Annotated[int, Field(ge=0, le=6)], ...]
    start: Annotated[str, Field(pattern=r'^\d{2}:\d{2}$')]
    end: Annotated[str, Field(pattern=r'^\d{2}:\d{2}$')]

    @model_validator(mode='after')
    def valid_window(self):
        a, b = time.fromisoformat(self.start), time.fromisoformat(self.end)
        if not self.weekdays or len(set(self.weekdays)) != len(self.weekdays) or a >= b or a.minute % 15 or b.minute % 15:
            raise ValueError('explicit_non_overnight_quarter_hour_windows_required')
        return self


class MoodleSettings(Contract):
    """One explicit LMS account bound to this deployment's configured student.

    The token itself is only mounted into the private sync worker. A pinned
    secret version and an expected Moodle user ID prevent account substitution.
    """
    site_url: Literal['https://lms.astanait.edu.kz'] = 'https://lms.astanait.edu.kz'
    expected_user_id: Annotated[int, Field(strict=True, gt=0)]
    token_secret_reference: Annotated[str, Field(pattern=r'^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/secrets/[A-Za-z0-9_-]+/versions/[1-9][0-9]*$')]
    sync_origin: str
    sync_service_account: ServiceAccount
    scheduler_service_account: ServiceAccount
    interval_minutes: Literal[5, 10, 15, 20, 30, 60] = 15
    stale_after_minutes: Annotated[int, Field(strict=True, ge=10, le=180)] = 30

    @field_validator('sync_origin')
    @classmethod
    def run_origin(cls, value):
        return cloud_run_audience(value)

    @model_validator(mode='after')
    def distinct_and_fresh(self):
        if self.sync_service_account == self.scheduler_service_account:
            raise ValueError('separate_moodle_worker_and_scheduler_required')
        if self.stale_after_minutes < self.interval_minutes:
            raise ValueError('moodle_stale_window_shorter_than_sync_interval')
        return self


class CloudSettings(Contract):
    application: AppConfig
    backend_origin: str
    agent_origin: str
    mcp_origin: str
    google_signin_client_id: Annotated[str, Field(pattern=r'^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$')]
    chat: ConversationTarget
    working_hours: tuple[WorkingHours, ...]
    moodle: MoodleSettings | None = None

    @field_validator('backend_origin', 'agent_origin', 'mcp_origin')
    @classmethod
    def run_origin(cls, value):
        return cloud_run_audience(value)

    @model_validator(mode='after')
    def same_project(self):
        self.application.require_environment('cloud')
        if len({self.backend_origin, self.agent_origin, self.mcp_origin}) != 3:
            raise ValueError('three_separate_cloud_run_services_required')
        if self.chat.provider == 'vertex' and self.chat.project_id != self.application.project_id:
            raise ValueError('chat_project_mismatch')
        if self.moodle is not None:
            project = self.application.project_id
            identities = set(self.application.identities.model_dump().values())
            extra = {self.moodle.sync_service_account, self.moodle.scheduler_service_account}
            if identities.intersection(extra) or any(
                    not value.endswith('@' + project + '.iam.gserviceaccount.com') for value in extra):
                raise ValueError('moodle_identities_must_be_separate_in_same_project')
            if not self.moodle.token_secret_reference.startswith('projects/' + project + '/'):
                raise ValueError('moodle_secret_project_mismatch')
            if self.moodle.sync_origin in {self.backend_origin, self.agent_origin, self.mcp_origin}:
                raise ValueError('separate_private_moodle_service_required')
        # Check overlapping weekday windows before any server starts.
        for day in range(7):
            windows = sorted((w.start, w.end) for w in self.working_hours if day in w.weekdays)
            if any(a[1] > b[0] for a, b in zip(windows, windows[1:])):
                raise ValueError('overlapping_working_hours')
        return self

    def constraints(self, now):
        start, end = planning_horizon(now)
        zone = self.application.student.timezone
        first = start.astimezone(ZoneInfo(zone)).date()
        last = end.astimezone(ZoneInfo(zone)).date()
        windows = []
        day = first
        while day <= last:
            for rule in self.working_hours:
                if day.weekday() not in rule.weekdays:
                    continue
                left = max(start, local_time(datetime.combine(day, time.fromisoformat(rule.start)), zone))
                right = min(end, local_time(datetime.combine(day, time.fromisoformat(rule.end)), zone))
                if left < right:
                    windows.append(Interval(start=left, end=right))
            day += timedelta(days=1)
        return PlanningConstraints(config_revision=self.application.config_revision,
            timezone=zone, horizon=Interval(start=start, end=end),
            working_windows=tuple(sorted(windows, key=lambda w: w.start)))


def load_settings(path):
    try:
        return CloudSettings.model_validate_json(Path(path).read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        raise ValueError('cloud_settings_invalid_or_incomplete') from None
