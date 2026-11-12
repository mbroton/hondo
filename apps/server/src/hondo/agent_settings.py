from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

DEFAULT_AGENT_NAME = "hondo"


@dataclass(frozen=True, slots=True)
class AgentModel:
    connection_id: UUID
    workspace_id: UUID
    provider: str
    model_name: str
    reads_images: bool = True

    @property
    def model_id(self) -> str:
        return f"{self.provider}:{self.model_name}"
