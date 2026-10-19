"""Non-secret single-student configuration. No default project or resource lookup.

Use load_config at startup with the deployment's explicit environment. A local
development config never authorizes cloud execution. Credentials belong to the
platform/Secret Manager; this module never fetches or accepts token values.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Literal, Mapping, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

from .domain.models import Identifier, IanaZone, ResourceId, canonical_hash

Environment = Literal["local", "cloud"]
ProjectId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")]
Region = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z]+(?:-[a-z]+)+[0-9]$")]


ModelLocation = Region | Literal["global"]
ServiceAccount = Annotated[str, StringConstraints(strict=True, max_length=256, pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$")]
SecretReference = Annotated[str, StringConstraints(strict=True, max_length=512, pattern=r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/secrets/[a-zA-Z0-9_-]+/versions/(?:[1-9][0-9]*|latest)$")]



CLOUD_MARKERS = frozenset({"K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION", "GOOGLE_CLOUD_AGENT_ENGINE_ID"})


class ConfigurationError(ValueError):
    """Safe startup error: field paths/codes only, never configuration values."""


class ConfigValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")


class StudentResources(ConfigValue):
    trusted_user_id: Identifier

    allowed_signin_principal: Identifier
    spreadsheet_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_-]+$")]
    assignments_a1_range: Annotated[str, StringConstraints(strict=True, max_length=512)]
    calendar_id: ResourceId
    timezone: IanaZone

    @field_validator("allowed_signin_principal")
    @classmethod
    def immutable_subject(cls, value: str) -> str:
        issuer, separator, subject = value.partition("#")
        try:
            parsed = urlsplit(issuer)
            valid_issuer = (parsed.scheme == "https" and parsed.hostname
                            and parsed.username is None and parsed.password is None
                            and not parsed.query and parsed.port in (None, 443))
        except ValueError:
            valid_issuer = False
        if not separator or not valid_issuer or not subject or "#" in subject:
            raise ValueError("issuer-qualified immutable sign-in subject required")
        return value

    @field_validator("calendar_id")
    @classmethod
    def explicit_calendar(cls, value: str) -> str:
        if value.casefold() == "primary":
            raise ValueError("explicit calendar required; primary is forbidden")
        return value

    @field_validator("assignments_a1_range")
    @classmethod
    def bounded_range(cls, value: str) -> str:


        import re

        match = re.fullmatch(r"(?:'(?:[^'\x00-\x1f]|'')+'|[A-Za-z_][A-Za-z0-9_]*)!([A-Z]{1,3})([1-9][0-9]{0,6}):([A-Z]{1,3})([1-9][0-9]{0,6})", value)
        if match is None:
            raise ValueError("explicit bounded tab A1 rectangle required")

        def column(label: str) -> int:
            result = 0
            for char in label:
                result = result * 26 + ord(char) - ord("A") + 1
            return result

        left, top, right, bottom = match.groups()
        if column(left) > column(right) or int(top) > int(bottom) or column(right) > 18278:
            raise ValueError("reversed or invalid A1 rectangle")
        return value


class ServiceIdentities(ConfigValue):
    backend_service_account: ServiceAccount
    agent_service_account: ServiceAccount
    mcp_service_account: ServiceAccount

    @model_validator(mode="after")
    def distinct_roles(self) -> Self:
        if len(set(self.model_dump().values())) != 3:
            raise ValueError("three distinct service identities required")
        return self


class SecretReferences(ConfigValue):


    oauth_client_secret_reference: SecretReference | None = None
    oauth_refresh_token_secret_reference: SecretReference | None = None


class AppConfig(ConfigValue):
    mode: Literal["local_development", "cloud"]
    project_id: ProjectId
    agent_region: Region
    model_location: ModelLocation
    web_cloud_run_region: Region
    mcp_cloud_run_region: Region
    firestore_location: Annotated[str, StringConstraints(strict=True, pattern=r"^(?:[a-z]+(?:-[a-z]+)+[0-9]|[a-z]{3}[0-9])$")]
    firestore_database: Annotated[str, StringConstraints(strict=True, pattern=r"^(?:\(default\)|[a-z][a-z0-9-]{2,61}[a-z0-9])$")]
    workspace_access_mode: Literal["explicit_resource_sharing"]
    student: StudentResources
    identities: ServiceIdentities
    secrets: SecretReferences = Field(default_factory=SecretReferences)

    @model_validator(mode="after")
    def same_project(self) -> Self:
        if any(not account.endswith(f"@{self.project_id}.iam.gserviceaccount.com")
               for account in self.identities.model_dump().values()):
            raise ValueError("service identities must belong to configured project")
        if any(reference is not None and not reference.startswith(f"projects/{self.project_id}/")
               for reference in self.secrets.model_dump().values()):
            raise ValueError("secret references must belong to configured project")
        return self

    @property
    def config_revision(self) -> str:
        """Content-derived revision: a changed binding invalidates old runs/plans."""
        return "cfg_" + canonical_hash(self.model_dump(mode="json"))

    def require_environment(self, environment: Environment, *, environ: Mapping[str, str] | None = None) -> None:
        if environment not in ("local", "cloud"):
            raise ConfigurationError("explicit local/cloud environment required")
        runtime = os.environ if environ is None else environ
        if self.mode == "local_development" and (environment == "cloud" or CLOUD_MARKERS.intersection(runtime)):
            raise ConfigurationError("local_development is forbidden in cloud runtime")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("duplicate configuration key")
        result[key] = value
    return result


def load_config(path: str | Path, *, environment: Environment) -> AppConfig:
    """Load only the explicit file; no env/default-project/resource fallbacks.

    Validation errors intentionally omit input/repr and exception chaining,
    including when a mistakenly pasted secret appears in an unknown field.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
        config = AppConfig.model_validate(payload)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in item["loc"]) or "configuration" for item in error.errors()})


        if any(item["type"] == "extra_forbidden" for item in error.errors()):
            raise ConfigurationError("unknown configuration fields") from None
        raise ConfigurationError("invalid configuration fields: " + ", ".join(fields)) from None
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ConfigurationError("configuration file is unreadable or invalid JSON") from None
    config.require_environment(environment)
    return config
