from typing import Literal, Protocol
from uuid import UUID
from pydantic import Field, model_validator
from apps.api.sonnic.models import DreamInput, Model, Symbol

CONTRACT_VERSION = "sonnic-project-v1"
PACK_VERSION = "knowledge-v1"


class JobInput(Model):
    contract_version: Literal["sonnic-project-v1"] = CONTRACT_VERSION
    knowledge_version: Literal["knowledge-v1"] = PACK_VERSION
    job_id: UUID
    lease_id: UUID
    attempt: int = Field(ge=1, le=3)
    input_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    data_class: Literal["synthetic", "personal"] = "synthetic"
    output_locale: Literal["ru-RU", "en-US"]
    dream: DreamInput


class PublicReflection(Model):
    title: str = Field(min_length=1, max_length=150)
    symbols: list[Symbol] = Field(max_length=6)
    summary: str = Field(min_length=20, max_length=4000)
    key_connection: str = Field(min_length=10, max_length=1000)
    reflection_question: str = Field(min_length=10, max_length=600)


class SourceUse(Model):
    source_id: str = Field(pattern="^R(0[1-9]|1[0-2])$")
    note: str = Field(min_length=10, max_length=280)


class JobOutput(Model):
    contract_version: Literal["sonnic-project-v1"] = CONTRACT_VERSION
    knowledge_version: Literal["knowledge-v1"] = PACK_VERSION
    job_id: UUID
    lease_id: UUID
    attempt: int = Field(ge=1, le=3)
    input_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    output_locale: Literal["ru-RU", "en-US"]
    outcome: Literal["reflection", "needs_context", "support", "refused"]
    reflection: PublicReflection | None
    safe_message: str | None = Field(max_length=800)
    source_use: list[SourceUse] = Field(max_length=12)

    @model_validator(mode="after")
    def exclusive_result(self):
        if self.outcome in {"reflection", "needs_context"}:
            if self.reflection is None or self.safe_message is not None:
                raise ValueError("reflection_required")
        elif self.reflection is not None or not self.safe_message or len(self.safe_message) < 10:
            raise ValueError("safe_message_required")
        return self


class ProjectTransport(Protocol):
    """Unimplemented outbound interface: enqueueing does not wake ChatGPT."""
    async def request_processing(self, job_id: UUID) -> None: ...
    async def cancel(self, job_id: UUID) -> None: ...


class UnconnectedProjectTransport:
    async def request_processing(self, job_id: UUID) -> None:
        raise RuntimeError("chatgpt_project_transport_not_connected")

    async def cancel(self, job_id: UUID) -> None:
        # Nothing external was started by this transport.
        return None
