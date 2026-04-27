from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class ProgramInfo(BaseModel):
    name: str
    description: str | None = None
    schedule: str | None = None


class SubmissionInfo(BaseModel):
    method: str = Field(description="form|email|portal|unknown")
    url: HttpUrl | None = None
    email: str | None = None
    requirements: str | None = None
    accepts_newcomers: bool = False


class ContactInfo(BaseModel):
    name: str | None = None
    role: str = Field(description="host|dj|producer|music_director|program_director|editor|unknown")
    show_name: str | None = None
    email: str | None = None
    contact_url: HttpUrl | None = None
    notes: str | None = None
    confidence: float = Field(ge=0, le=1, default=0.5)


class StationExtract(BaseModel):
    canonical_name: str
    aliases: list[str] = []
    country_code: str | None = None
    language: str | None = None
    city: str | None = None
    website_url: HttpUrl | None = None
    stream_url: HttpUrl | None = None
    genres: list[str] = []
    programs: list[ProgramInfo] = []
    submissions: list[SubmissionInfo] = []
    contacts: list[ContactInfo] = []
    confidence: float = Field(ge=0, le=1, default=0.5)


class FormFieldExtract(BaseModel):
    field_key: str
    name: str | None = None
    label: str | None = None
    input_type: str = "text"
    required: bool = False
    placeholder: str | None = None
    options: list[str] = []
    validation_hint: str | None = None
    max_length: int | None = None
    accept_types: str | None = None
    upload_max_mb: float | None = None


class FormRecipeSchema(BaseModel):
    station_id: int
    form_url: HttpUrl
    version: int = 1
    confidence_score: float = Field(ge=0, le=1, default=0.5)
    entry_path: list[str] = []
    required_fields: list[str] = []
    mapping_rules: dict[str, str] = {}
    field_order: list[str] = []
    upload_rules: dict[str, str] = {}
    validation_rules: list[str] = []
    success_detection: list[str] = []
    error_detection: list[str] = []
    notes_for_future_runs: str | None = None
