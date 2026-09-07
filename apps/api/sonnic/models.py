from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Attribution(Model):
    source: str | None = Field(default=None, max_length=100)
    content_key: str | None = Field(default=None, max_length=100)
    campaign: str | None = Field(default=None, max_length=100)


class GuestInput(Model):
    locale: Literal["ru-RU", "en-US"] = "ru-RU"
    attribution: Attribution = Field(default_factory=Attribution)


class DreamInput(Model):
    dream_text: str = Field(min_length=20, max_length=10000)
    wake_emotion: str = Field(default="", max_length=120)
    dream_date: date | None = None
    recurring: bool = False
    locale: Literal["ru-RU", "en-US"] = "ru-RU"
    # Explicit user input only: never derive these from text, IP, name or locale.
    content_language: str = Field(default="und", pattern=r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$", max_length=35)
    recognition_locale: str | None = Field(default=None, pattern=r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$", max_length=35)
    region: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    cultural_perspective: str | None = Field(default=None, max_length=120)

    @field_validator("dream_date")
    @classmethod
    def not_future(cls, value: date | None) -> date | None:
        if value and value > date.today():
            raise ValueError("future_date")
        return value


class Symbol(Model):
    label: str = Field(min_length=1, max_length=120)
    evidence: str = Field(min_length=1, max_length=200)


class FreeResult(Model):
    title: str
    symbols: list[Symbol] = Field(max_length=6)
    emotional_tone: str
    summary: str
    key_connection: str
    reflection_question: str
    locked_sections: list[str] = Field(min_length=5, max_length=5)
    mode: Literal["demo", "real"] = "demo"
    locale: Literal["ru-RU", "en-US"]
    evidence_basis: Literal["template_not_ai", "model_from_user_text"] = "template_not_ai"
    research_retrieval_used: Literal[False] = False

    @model_validator(mode="after")
    def consistent_provenance(self):
        expected = "template_not_ai" if self.mode == "demo" else "model_from_user_text"
        if self.evidence_basis != expected:
            raise ValueError("inconsistent_provenance")
        return self
