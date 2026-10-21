"""Bounded operational progress, never model reasoning or source contents."""
from typing import Annotated, Literal

from pydantic import Field, StrictInt

from .models import Contract


class StudyToolProgress(Contract):
    call_id: Annotated[StrictInt, Field(ge=1, le=8)]
    tool: Literal['read_assignments', 'read_schedule']
    status: Literal['running', 'completed', 'failed', 'unavailable', 'not_connected', 'unknown']
    duration_ms: Annotated[StrictInt, Field(ge=0, le=150000)] | None = None
    result_count: Annotated[StrictInt, Field(ge=0, le=1000)] | None = None


class StudyProgress(Contract):
    phase: Literal['queued', 'thinking', 'tools', 'composing', 'completed', 'failed']
    tool_calls: Annotated[tuple[StudyToolProgress, ...], Field(max_length=8)] = ()

    def settled(self, *, success):
        """A lost response cannot establish whether a pending MCP call finished."""
        return StudyProgress(phase='completed' if success else 'failed', tool_calls=tuple(
            call.model_copy(update={'status': 'unknown'}) if call.status == 'running' else call
            for call in self.tool_calls))
