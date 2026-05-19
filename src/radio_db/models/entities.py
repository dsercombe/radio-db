from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SQLEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from radio_db.db import Base


class SourceType(str, Enum):
    RADIO_BROWSER = "radio_browser"
    WIKIDATA = "wikidata"
    BRAVE_SEARCH = "brave_search"
    GOOGLE_CSE = "google_cse"
    TAVILY = "tavily"
    GROK_SEARCH = "grok_search"
    LINKUP = "linkup"
    DUCKDUCKGO = "duckduckgo"
    WEBSITE = "website"


class StationStatus(str, Enum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class SubmissionMethod(str, Enum):
    FORM = "form"
    EMAIL = "email"
    PORTAL = "portal"
    UNKNOWN = "unknown"


class ContactRole(str, Enum):
    HOST = "host"
    DJ = "dj"
    PRODUCER = "producer"
    MUSIC_DIRECTOR = "music_director"
    PROGRAM_DIRECTOR = "program_director"
    EDITOR = "editor"
    UNKNOWN = "unknown"


class FormStatus(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    BROKEN = "broken"
    LOGIN_REQUIRED = "login_required"
    CAPTCHA_PRESENT = "captcha_present"
    UNKNOWN = "unknown"


class FormType(str, Enum):
    GENERAL_CONTACT = "general_contact"
    MUSIC_SUBMISSION = "music_submission"
    ARTIST_UPLOAD = "artist_upload"
    SHOW_PITCH = "show_pitch"
    NEWCOMER = "newcomer"
    UNKNOWN = "unknown"


class CrawlFrontier(Base):
    __tablename__ = "crawl_frontier"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    query: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    locale: Mapped[str] = mapped_column(String(16), default="en")
    country: Mapped[str] = mapped_column(String(4), default="")
    template_name: Mapped[str] = mapped_column(String(120), default="default")
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    yield_new: Mapped[int] = mapped_column(Integer, default=0)
    yield_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    exploration_bonus: Mapped[float] = mapped_column(Float, default=2.0)
    priority_score: Mapped[float] = mapped_column(Float, default=1000.0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Station(Base):
    __tablename__ = "stations"
    __table_args__ = (
        UniqueConstraint("canonical_name", "country_code", name="uq_station_name_country"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(255), index=True)
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    country_code: Mapped[str] = mapped_column(String(8), default="")
    language: Mapped[str] = mapped_column(String(128), default="")
    website_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    stream_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[StationStatus] = mapped_column(SQLEnum(StationStatus), default=StationStatus.CANDIDATE)
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    manual_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    scan_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    scan_last_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    scan_last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    scan_claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    scan_claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    scan_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    scan_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority_tier: Mapped[int] = mapped_column(Integer, default=0)
    best_submission_route_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    best_submission_route_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    best_submission_route_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    best_submission_route_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_submission_route_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    best_submission_route_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    secondary_submission_routes_json: Mapped[str] = mapped_column(Text, default="[]")
    fingerprint: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    aliases: Mapped[list[StationAlias]] = relationship(back_populates="station", cascade="all, delete-orphan")
    genres: Mapped[list[StationGenre]] = relationship(back_populates="station", cascade="all, delete-orphan")
    programs: Mapped[list[StationProgram]] = relationship(back_populates="station", cascade="all, delete-orphan")
    submissions: Mapped[list[SubmissionChannel]] = relationship(back_populates="station", cascade="all, delete-orphan")
    contacts: Mapped[list[StationContact]] = relationship(back_populates="station", cascade="all, delete-orphan")
    people: Mapped[list[StationPerson]] = relationship(back_populates="station", cascade="all, delete-orphan")
    forms: Mapped[list[SubmissionForm]] = relationship(back_populates="station", cascade="all, delete-orphan")
    submission_assessments: Mapped[list[StationSubmissionAssessment]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    agent_runs: Mapped[list[SubmissionAgentRun]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    network_links: Mapped[list[StationNetworkLink]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    evidence_items: Mapped[list[Evidence]] = relationship(back_populates="station", cascade="all, delete-orphan")
    contact_drafts: Mapped[list[ContactDraft]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    contact_sends: Mapped[list[ContactSend]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )
    group_memberships: Mapped[list[StationGroupMembership]] = relationship(
        back_populates="station", cascade="all, delete-orphan"
    )


class StationAlias(Base):
    __tablename__ = "station_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    alias: Mapped[str] = mapped_column(String(255), index=True)

    station: Mapped[Station] = relationship(back_populates="aliases")


class StationGenre(Base):
    __tablename__ = "station_genres"
    __table_args__ = (UniqueConstraint("station_id", "genre", name="uq_station_genre"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    genre: Mapped[str] = mapped_column(String(120), index=True)

    station: Mapped[Station] = relationship(back_populates="genres")


class StationProgram(Base):
    __tablename__ = "station_programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule: Mapped[str | None] = mapped_column(String(500), nullable=True)

    station: Mapped[Station] = relationship(back_populates="programs")


class SubmissionChannel(Base):
    __tablename__ = "submission_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    method: Mapped[SubmissionMethod] = mapped_column(SQLEnum(SubmissionMethod), default=SubmissionMethod.UNKNOWN)
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepts_newcomers: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    station: Mapped[Station] = relationship(back_populates="submissions")


class StationContact(Base):
    __tablename__ = "station_contacts"
    __table_args__ = (
        UniqueConstraint("station_id", "name", "role", "show_name", "email", name="uq_station_contact"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[ContactRole] = mapped_column(SQLEnum(ContactRole), default=ContactRole.UNKNOWN)
    show_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    manual_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    station: Mapped[Station] = relationship(back_populates="contacts")


class StationPerson(Base):
    __tablename__ = "station_people"
    __table_args__ = (
        UniqueConstraint("station_id", "name", "role", "show_name", "email", name="uq_station_person"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[ContactRole] = mapped_column(SQLEnum(ContactRole), default=ContactRole.UNKNOWN)
    show_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contact_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    musical_preferences: Mapped[str | None] = mapped_column(Text, nullable=True)
    genre_affinities_json: Mapped[str] = mapped_column(Text, default="[]")
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    station: Mapped[Station] = relationship(back_populates="people")


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), nullable=True, index=True)
    source_type: Mapped[SourceType] = mapped_column(SQLEnum(SourceType), index=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    station: Mapped[Station | None] = relationship(back_populates="evidence_items")


class StationProfileSnapshot(Base):
    __tablename__ = "station_profile_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    style_tags_json: Mapped[str] = mapped_column(Text, default="[]")
    editorial_signals_json: Mapped[str] = mapped_column(Text, default="[]")
    show_personality: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_voice: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    diversity_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SubmissionForm(Base):
    __tablename__ = "forms"
    __table_args__ = (UniqueConstraint("station_id", "url", name="uq_form_station_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(1024), index=True)
    page_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    form_type: Mapped[FormType] = mapped_column(SQLEnum(FormType), default=FormType.UNKNOWN, index=True)
    status: Mapped[FormStatus] = mapped_column(SQLEnum(FormStatus), default=FormStatus.UNKNOWN, index=True)
    requires_login: Mapped[bool] = mapped_column(Boolean, default=False)
    has_captcha: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    entry_path_json: Mapped[str] = mapped_column(Text, default="[]")
    snapshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    dom_snapshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    station: Mapped[Station] = relationship(back_populates="forms")
    fields: Mapped[list[SubmissionFormField]] = relationship(back_populates="form", cascade="all, delete-orphan")
    recipes: Mapped[list[FormRecipe]] = relationship(back_populates="form", cascade="all, delete-orphan")
    agent_runs: Mapped[list[SubmissionAgentRun]] = relationship(
        back_populates="form", cascade="all, delete-orphan"
    )


class SubmissionFormField(Base):
    __tablename__ = "form_fields"
    __table_args__ = (UniqueConstraint("form_id", "field_key", name="uq_form_field_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("forms.id", ondelete="CASCADE"), index=True)
    field_key: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str | None] = mapped_column(String(500), nullable=True)
    input_type: Mapped[str] = mapped_column(String(80), default="text", index=True)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    placeholder: Mapped[str | None] = mapped_column(String(500), nullable=True)
    options_json: Mapped[str] = mapped_column(Text, default="[]")
    validation_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accept_types: Mapped[str | None] = mapped_column(String(255), nullable=True)
    upload_max_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_snapshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    form: Mapped[SubmissionForm] = relationship(back_populates="fields")


class FormRecipe(Base):
    __tablename__ = "form_recipes"
    __table_args__ = (UniqueConstraint("form_id", "version", name="uq_form_recipe_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(ForeignKey("forms.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    mode: Mapped[str] = mapped_column(String(32), default="read")
    confidence_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="active")
    instructions_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    machine_mapping_json: Mapped[str] = mapped_column(Text, default="{}")
    field_order_json: Mapped[str] = mapped_column(Text, default="[]")
    upload_strategy_json: Mapped[str] = mapped_column(Text, default="{}")
    submit_strategy_json: Mapped[str] = mapped_column(Text, default="{}")
    success_detection_rules_json: Mapped[str] = mapped_column(Text, default="[]")
    error_detection_rules_json: Mapped[str] = mapped_column(Text, default="[]")
    retry_rules_json: Mapped[str] = mapped_column(Text, default="[]")
    notes_for_future_runs: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    form: Mapped[SubmissionForm] = relationship(back_populates="recipes")


class StationSubmissionAssessment(Base):
    __tablename__ = "station_submission_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    assessment_kind: Mapped[str] = mapped_column(String(32), default="manual_scan")
    status: Mapped[str] = mapped_column(String(32), default="active")
    is_real_station: Mapped[bool] = mapped_column(Boolean, default=False)
    has_real_editorial_surface: Mapped[bool] = mapped_column(Boolean, default=False)
    accepts_music_submissions: Mapped[bool] = mapped_column(Boolean, default=False)
    accepts_new_artists: Mapped[bool] = mapped_column(Boolean, default=False)
    automation_readiness: Mapped[float] = mapped_column(Float, default=0.0)
    risk_score: Mapped[float] = mapped_column(Float, default=1.0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    station: Mapped[Station] = relationship(back_populates="submission_assessments")


class CandidateRescanQueue(Base):
    __tablename__ = "candidate_rescan_queue"
    __table_args__ = (
        UniqueConstraint("source_pool", "main_station_id", name="uq_candidate_rescan_pool_main_station"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_pool: Mapped[str] = mapped_column(String(64), index=True)
    main_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    queue_station_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    queue_fingerprint: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    old_queue_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    old_main_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    old_scan_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_decision: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    old_quality_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    old_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    last_assessment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    final_decision: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    path_quality: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    result_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EnrichmentQueue(Base):
    __tablename__ = "enrichment_queue"
    __table_args__ = (
        UniqueConstraint("source_pool", "station_id", name="uq_enrichment_queue_pool_station"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    source_pool: Mapped[str] = mapped_column(String(64), index=True)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    api_budget_class: Mapped[str] = mapped_column(String(16), default="none", index=True)
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    free_findings_json: Mapped[str] = mapped_column(Text, default="{}")
    paid_plan_json: Mapped[str] = mapped_column(Text, default="{}")
    last_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    free_last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    paid_last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EnrichmentRun(Base):
    __tablename__ = "enrichment_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    queue_id: Mapped[int | None] = mapped_column(ForeignKey("enrichment_queue.id", ondelete="SET NULL"), nullable=True, index=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id", ondelete="SET NULL"), nullable=True, index=True)
    layer: Mapped[str] = mapped_column(String(16), index=True)
    source_pool: Mapped[str] = mapped_column(String(64), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    outcome: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    urls_checked: Mapped[int] = mapped_column(Integer, default=0)
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    useful_findings_count: Mapped[int] = mapped_column(Integer, default=0)
    api_calls_json: Mapped[str] = mapped_column(Text, default="{}")
    cost_estimate_usd: Mapped[float] = mapped_column(Float, default=0.0)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EnrichmentFinding(Base):
    __tablename__ = "enrichment_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("enrichment_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    layer: Mapped[str] = mapped_column(String(16), index=True)
    finding_type: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    useful: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class SubmissionAgentRun(Base):
    __tablename__ = "submission_agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    form_id: Mapped[int | None] = mapped_column(ForeignKey("forms.id", ondelete="SET NULL"), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(32), default="scan")
    goal: Mapped[str] = mapped_column(String(255), default="manual_test")
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    current_state: Mapped[str] = mapped_column(String(64), default="station_review", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    station: Mapped[Station] = relationship(back_populates="agent_runs")
    form: Mapped[SubmissionForm | None] = relationship(back_populates="agent_runs")
    steps: Mapped[list[SubmissionAgentStep]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="SubmissionAgentStep.step_index"
    )
    issues: Mapped[list[SubmissionAgentIssue]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="SubmissionAgentIssue.id"
    )


class SubmissionAgentStep(Base):
    __tablename__ = "submission_agent_steps"
    __table_args__ = (UniqueConstraint("run_id", "step_index", name="uq_submission_agent_run_step"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("submission_agent_runs.id", ondelete="CASCADE"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    state_before: Mapped[str] = mapped_column(String(64), default="station_review")
    state_after: Mapped[str] = mapped_column(String(64), default="station_review")
    screenshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    dom_snapshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    agent_observation_json: Mapped[str] = mapped_column(Text, default="{}")
    proposed_action_json: Mapped[str] = mapped_column(Text, default="{}")
    executed_action_json: Mapped[str] = mapped_column(Text, default="{}")
    execution_result_json: Mapped[str] = mapped_column(Text, default="{}")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped[SubmissionAgentRun] = relationship(back_populates="steps")


class SubmissionAgentIssue(Base):
    __tablename__ = "submission_agent_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("submission_agent_runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[int | None] = mapped_column(
        ForeignKey("submission_agent_steps.id", ondelete="SET NULL"), nullable=True, index=True
    )
    issue_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    title: Mapped[str] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    run: Mapped[SubmissionAgentRun] = relationship(back_populates="issues")


class MarketIntelligence(Base):
    __tablename__ = "market_intelligence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_code: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    market_name: Mapped[str] = mapped_column(String(120))
    language_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    submission_norms: Mapped[str | None] = mapped_column(Text, nullable=True)
    editorial_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    outreach_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_networks_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DistributionNetwork(Base):
    __tablename__ = "distribution_networks"
    __table_args__ = (UniqueConstraint("network_key", name="uq_distribution_network_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    network_key: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(255))
    market_code: Mapped[str] = mapped_column(String(8), index=True)
    network_type: Mapped[str] = mapped_column(String(64), default="association")
    submission_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    submission_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    coverage_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rules_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    station_links: Mapped[list[StationNetworkLink]] = relationship(
        back_populates="network", cascade="all, delete-orphan"
    )


class StationNetworkLink(Base):
    __tablename__ = "station_network_links"
    __table_args__ = (UniqueConstraint("station_id", "network_id", "relationship_type", name="uq_station_network_link"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    network_id: Mapped[int] = mapped_column(ForeignKey("distribution_networks.id", ondelete="CASCADE"), index=True)
    relationship_type: Mapped[str] = mapped_column(String(64), default="member")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    station: Mapped[Station] = relationship(back_populates="network_links")
    network: Mapped[DistributionNetwork] = relationship(back_populates="station_links")


class EmailBlacklist(Base):
    __tablename__ = "email_blacklist"
    __table_args__ = (UniqueConstraint("email", name="uq_email_blacklist_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ContactTemplate(Base):
    __tablename__ = "contact_templates"
    __table_args__ = (UniqueConstraint("template_key", name="uq_contact_template_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_key: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    channel: Mapped[str] = mapped_column(String(32), default="email")
    variables_json: Mapped[str] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    locales: Mapped[list[ContactTemplateLocale]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )
    drafts: Mapped[list[ContactDraft]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class ContactTemplateLocale(Base):
    __tablename__ = "contact_template_locales"
    __table_args__ = (UniqueConstraint("template_id", "locale_key", name="uq_contact_template_locale"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("contact_templates.id", ondelete="CASCADE"), index=True)
    locale_key: Mapped[str] = mapped_column(String(32), index=True)
    language_code: Mapped[str] = mapped_column(String(16), default="en")
    subject_template: Mapped[str] = mapped_column(Text)
    body_template: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    template: Mapped[ContactTemplate] = relationship(back_populates="locales")


class StationGroup(Base):
    __tablename__ = "station_groups"
    __table_args__ = (
        UniqueConstraint("name", name="uq_station_group_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    artist_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    color_hint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    memberships: Mapped[list[StationGroupMembership]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class StationGroupMembership(Base):
    __tablename__ = "station_group_memberships"
    __table_args__ = (
        UniqueConstraint("group_id", "station_id", name="uq_station_group_membership"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("station_groups.id", ondelete="CASCADE"), index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    group: Mapped[StationGroup] = relationship(back_populates="memberships")
    station: Mapped[Station] = relationship(back_populates="group_memberships")


class OutreachCampaign(Base):
    """One persisted outreach campaign per song/release for radio pitching."""

    __tablename__ = "outreach_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    artist_name: Mapped[str] = mapped_column(String(255))
    song_title: Mapped[str] = mapped_column(String(255))
    release_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    song_language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pitch_text: Mapped[str] = mapped_column(Text, default="")
    reference_template: Mapped[str] = mapped_column(Text, default="")
    operator_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    press_release_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    tracking_code: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    drafts: Mapped[list["ContactDraft"]] = relationship(back_populates="campaign")


class OutreachLinkClick(Base):
    """Tracked click on a campaign press release redirect link."""

    __tablename__ = "outreach_link_clicks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_campaigns.id", ondelete="CASCADE"), index=True
    )
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id", ondelete="SET NULL"), nullable=True, index=True)
    target_url: Mapped[str] = mapped_column(String(2048))
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    referer: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ContactDraft(Base):
    __tablename__ = "contact_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("contact_templates.id", ondelete="SET NULL"), nullable=True, index=True
    )
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("outreach_campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    locale: Mapped[str] = mapped_column(String(32), default="-- / --")
    language: Mapped[str] = mapped_column(String(16), default="en")
    country_code: Mapped[str] = mapped_column(String(8), default="")
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    recommended_channel: Mapped[str] = mapped_column(String(32), default="manual_research")
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    available_routes_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_summary_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    station: Mapped[Station] = relationship(back_populates="contact_drafts")
    template: Mapped[ContactTemplate | None] = relationship(back_populates="drafts")
    campaign: Mapped["OutreachCampaign | None"] = relationship(back_populates="drafts")
    sends: Mapped[list[ContactSend]] = relationship(back_populates="draft", cascade="all, delete-orphan")


class ContactSend(Base):
    __tablename__ = "contact_sends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("contact_drafts.id", ondelete="CASCADE"), index=True)
    station_id: Mapped[int] = mapped_column(ForeignKey("stations.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="email")
    target_value: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), default="dry-run")
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    draft: Mapped[ContactDraft] = relationship(back_populates="sends")
    station: Mapped[Station] = relationship(back_populates="contact_sends")
    outcomes: Mapped[list[ContactOutcome]] = relationship(
        back_populates="send", cascade="all, delete-orphan"
    )


class ContactOutcome(Base):
    __tablename__ = "contact_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    send_id: Mapped[int] = mapped_column(ForeignKey("contact_sends.id", ondelete="CASCADE"), index=True)
    outcome_type: Mapped[str] = mapped_column(String(64), default="preview")
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    send: Mapped[ContactSend] = relationship(back_populates="outcomes")


class BrowserSessionRecord(Base):
    __tablename__ = "browser_sessions"
    __table_args__ = (UniqueConstraint("session_key", name="uq_browser_session_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_key: Mapped[str] = mapped_column(String(64), index=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("submission_agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    url: Mapped[str] = mapped_column(String(2048), default="about:blank")
    title: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(64), default="idle", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    screenshot_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    html_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    action_count: Mapped[int] = mapped_column(Integer, default=0)
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    events: Mapped[list[BrowserSessionEvent]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="BrowserSessionEvent.event_index"
    )


class BrowserSessionEvent(Base):
    __tablename__ = "browser_session_events"
    __table_args__ = (UniqueConstraint("session_id", "event_index", name="uq_browser_session_event_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("browser_sessions.id", ondelete="CASCADE"), index=True)
    event_index: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    url: Mapped[str] = mapped_column(String(2048), default="about:blank")
    status: Mapped[str] = mapped_column(String(64), default="idle")
    title: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped[BrowserSessionRecord] = relationship(back_populates="events")


class ScanJob(Base):
    __tablename__ = "scan_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    requested_by: Mapped[str] = mapped_column(String(255), default="system", index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    runs: Mapped[list["ScanRun"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    events: Mapped[list["ScanEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), index=True)
    worker_id: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", index=True)
    result_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    job: Mapped[ScanJob] = relationship(back_populates="runs")


class ScanEvent(Base):
    __tablename__ = "scan_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("scan_jobs.id", ondelete="CASCADE"), nullable=True, index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    level: Mapped[str] = mapped_column(String(20), default="info", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    job: Mapped[ScanJob | None] = relationship(back_populates="events")


class ScanControl(Base):
    __tablename__ = "scan_controls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    control_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_by: Mapped[str] = mapped_column(String(255), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
