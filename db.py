"""Async SQLAlchemy setup + models."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import AsyncGenerator

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config

EMBED_DIM = 768


def _async_url_and_args() -> tuple[str, dict]:
    """Return (async_url, connect_args). Strip sslmode (libpq-only) and
    translate it to asyncpg's ssl kwarg when set to require/verify-*."""
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL is not set")
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://"):]
    parts = urlsplit(raw)
    qs = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = qs.pop("sslmode", None)
    # asyncpg also doesn't accept channel_binding via URL
    qs.pop("channel_binding", None)
    new_query = urlencode(qs)
    cleaned = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )
    if cleaned.startswith("postgresql://"):
        cleaned = "postgresql+asyncpg://" + cleaned[len("postgresql://"):]
    connect_args: dict = {}
    if sslmode and sslmode.lower() in ("require", "verify-ca", "verify-full"):
        connect_args["ssl"] = True
    return cleaned, connect_args


def _make_cloud_sql_engine():
    """Build an async engine backed by the Cloud SQL Python Connector.

    Authenticates with ADC. When IAM database authentication is enabled
    (the default in GCP mode) no password is used — the attached service
    account's identity is the database user. The ``Connector`` is created
    lazily on first use via ``create_async_connector()``, which binds it to
    the *running* event loop. A plain ``Connector()`` would instead spin up
    its own background-thread loop, so ``connect_async`` from uvicorn's loop
    would raise ``ConnectorLoopError`` (loop mismatch).
    """
    from google.cloud.sql.connector import IPTypes, create_async_connector

    instance = config.instance_connection_name()
    db_name = config.cloud_sql_db()
    user = config.cloud_sql_user()
    password = config.cloud_sql_password()
    if not instance or not db_name or not user:
        raise RuntimeError(
            "Cloud SQL is configured (INSTANCE_CONNECTION_NAME / "
            "CLOUD_SQL_CONNECTION_NAME set) but a database name "
            "(CLOUD_SQL_DB / DB_NAME) and user (CLOUD_SQL_USER / DB_USER) "
            "are required."
        )
    iam_auth = config.cloud_sql_iam_auth()
    ip_type = IPTypes.PRIVATE if config.cloud_sql_private_ip() else IPTypes.PUBLIC

    connector = None

    async def getconn():
        # The Connector binds to the event loop it is created on. Build it via
        # create_async_connector() on first use so it binds to the running
        # (uvicorn) loop; a plain Connector() would create its own background
        # loop and connect_async would then raise ConnectorLoopError.
        nonlocal connector
        if connector is None:
            connector = await create_async_connector(refresh_strategy="lazy")
        kwargs: dict = {
            "user": user,
            "db": db_name,
            "enable_iam_auth": iam_auth,
            "ip_type": ip_type,
        }
        if password and not iam_auth:
            kwargs["password"] = password
        return await connector.connect_async(instance, "asyncpg", **kwargs)

    return create_async_engine(
        "postgresql+asyncpg://",
        async_creator=getconn,
        pool_pre_ping=True,
        future=True,
    )


def _make_engine():
    if config.use_cloud_sql():
        return _make_cloud_sql_engine()
    url, connect_args = _async_url_and_args()
    return create_async_engine(
        url, pool_pre_ping=True, future=True, connect_args=connect_args
    )


engine = _make_engine()
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Slide(Base):
    __tablename__ = "slides"

    slide_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    slide_title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    slide_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    industry: Mapped[str] = mapped_column(String, nullable=False, default="")
    client: Mapped[str] = mapped_column(String, nullable=False, default="")
    proposal_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    graph_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    layout_type: Mapped[str] = mapped_column(String, nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reuse_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thumbnail_path: Mapped[str] = mapped_column(String, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    access_level: Mapped[str] = mapped_column(String, nullable=False, default="internal")
    # Recurring-meeting ("定例") series: the Drive folder the source file lives
    # directly under (by convention a client-name folder), plus a date parsed
    # from the file/slide name for chronological catch-up. See ``series.py``.
    folder_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", index=True
    )
    folder_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBED_DIM), nullable=True
    )
    # Content version (etag, else "size:<n>") of the source file this slide was
    # extracted from. Lets a resumed ingest tell "already-done page for the
    # current file content" (reuse) apart from "stale page from an older
    # version" (must recompute). NULL on legacy rows ingested before resume.
    source_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def to_dict(self) -> dict:
        return {
            "slideId": self.slide_id,
            "fileId": self.file_id,
            "fileName": self.file_name,
            "pageNo": self.page_no,
            "slideTitle": self.slide_title,
            "slideText": self.slide_text,
            "industry": self.industry,
            "client": self.client,
            "proposalType": self.proposal_type,
            "graphType": self.graph_type,
            "layoutType": self.layout_type,
            "tags": list(self.tags or []),
            "summary": self.summary,
            "reuseHint": self.reuse_hint,
            "thumbnailPath": self.thumbnail_path,
            "sourceUrl": self.source_url,
            "accessLevel": self.access_level,
            "folderId": self.folder_id,
            "folderName": self.folder_name,
            "docDate": self.doc_date.isoformat() if self.doc_date else None,
            "createdAt": self.created_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "updatedAt": self.updated_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    role: Mapped[str] = mapped_column(String, nullable=False, default="user")
    # role: user | admin
    # Non-admins with can_upload=True may add Drive links (admins always may).
    can_upload: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "email": self.email,
            "displayName": self.display_name,
            "role": self.role,
            "canUpload": self.can_upload,
            "createdAt": self.created_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }


class DriveFile(Base):
    __tablename__ = "drive_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_file_id: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    share_url: Mapped[str] = mapped_column(String, nullable=False)
    file_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Optional admin-chosen display name, set when the file was kept separately
    # under a disambiguated name ("名前 (1)") despite a name collision. When
    # empty, the raw Drive file_name is used. The ingest pipeline never
    # overwrites this, so the renamed name survives re-ingest.
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # pending | processing | ready | failed
    last_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_etag: Mapped[str | None] = mapped_column(String, nullable=True)
    last_ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    slide_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Recurring-meeting ("定例") series: the Drive folder this file lives directly
    # under (client folder), plus a date parsed from the file name. See series.py.
    folder_id: Mapped[str] = mapped_column(
        String, nullable=False, default="", index=True
    )
    folder_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    @property
    def effective_name(self) -> str:
        """The name to display/search by: the override if set, else Drive's."""
        return self.display_name or self.file_name

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "driveFileId": self.drive_file_id,
            "shareUrl": self.share_url,
            "fileName": self.effective_name,
            "rawName": self.file_name,
            "displayName": self.display_name,
            "status": self.status,
            "lastSize": self.last_size,
            "lastEtag": self.last_etag,
            "lastIngestedAt": self.last_ingested_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
            if self.last_ingested_at
            else None,
            "lastError": self.last_error,
            "slideCount": self.slide_count,
            "folderId": self.folder_id,
            "folderName": self.folder_name,
            "docDate": self.doc_date.isoformat() if self.doc_date else None,
            "addedAt": self.added_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }


class DriveFolder(Base):
    """A Drive folder the admin registered as a source.

    Folders are expanded into individual ``DriveFile`` rows at add time, but
    we also persist the folder itself so the incremental change poller
    (``drive_sync``) can recognise when a *new* file appears inside a watched
    folder and auto-register + ingest it.
    """

    __tablename__ = "drive_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    drive_folder_id: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )
    share_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "driveFolderId": self.drive_folder_id,
            "shareUrl": self.share_url,
            "addedAt": self.added_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }


class AddLog(Base):
    """Audit trail of Drive-link additions (manual and automatic).

    Records who added which Drive file/URL and how the collision was handled,
    so admins can review the PPTX addition history. ``actor_user_id`` is null
    for the automatic Drive-folder sync; ``actor_label`` denormalises the
    actor's name so the log survives the user being deleted/renamed.
    """

    __tablename__ = "add_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_label: Mapped[str] = mapped_column(String, nullable=False, default="")
    action: Mapped[str] = mapped_column(String, nullable=False, default="add")
    # add | overwrite | rename | auto
    drive_file_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    share_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    file_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    note: Mapped[str] = mapped_column(String, nullable=False, default="")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "createdAt": self.created_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "actorUserId": self.actor_user_id,
            "actorLabel": self.actor_label,
            "action": self.action,
            "driveFileId": self.drive_file_id,
            "shareUrl": self.share_url,
            "fileName": self.file_name,
            "note": self.note,
        }


class IngestJob(Base):
    """A single ingest run, persisted so progress survives across processes.

    The progress used to live in an in-memory singleton, which broke on
    multi-instance / ephemeral hosting (Cloud Run): the polling request could
    land on a different instance than the one running the job, so the progress
    "disappeared". Persisting each run here lets any instance read the live
    progress, and lets several jobs run/show in parallel.
    """

    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # manual (全件取り込み) | retry (単一ファイル再取り込み) | sync (自動同期)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    # running | done | failed
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    actor_label: Mapped[str] = mapped_column(String, nullable=False, default="")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_file: Mapped[str | None] = mapped_column(String, nullable=True)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    current_file_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_file_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Newline-joined display names of the files this run actually ingested,
    # accumulated as each file finishes. Persisted so the admin can see which
    # files a job processed even after it completes (when current_file is
    # cleared).
    ingested_files: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    # Bumped on every progress write (onupdate). A running job whose
    # updated_at is far in the past is "stalled" (e.g. crashed mid-run) and
    # can be reaped so it stops blocking single-flight scheduling.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def to_dict(self) -> dict:
        def iso(d: datetime | None) -> str | None:
            return (
                d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if d
                else None
            )

        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "actorLabel": self.actor_label,
            "total": self.total,
            "processed": self.processed,
            "failed": self.failed,
            "currentFile": self.current_file,
            "stage": self.stage,
            "currentFilePage": self.current_file_page,
            "currentFileTotal": self.current_file_total,
            "message": self.message,
            "ingestedFiles": [
                line for line in (self.ingested_files or "").splitlines() if line
            ],
            "startedAt": iso(self.started_at),
            "updatedAt": iso(self.updated_at),
            "finishedAt": iso(self.finished_at),
        }


class AppState(Base):
    """Tiny key/value store for cross-restart runtime state.

    Currently holds the Drive Changes API page token so incremental sync
    survives process restarts.
    """

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


FTS_EXPR = (
    "to_tsvector('simple', "
    "coalesce(slide_title,'') || ' ' || "
    "coalesce(slide_text,'') || ' ' || "
    "coalesce(summary,''))"
)

# Trigram-indexable haystack. Includes tags::text and the categorical
# facet columns (industry / proposal_type / graph_type / layout_type) so
# typing a facet value as a free-text query (e.g. "円グラフ", "金融")
# matches via the GIN(gin_trgm_ops) index. `tags::text` is immutable for
# jsonb.
SEARCH_EXPR = (
    "(coalesce(slide_title,'') || ' ' || coalesce(slide_text,'') || ' ' || "
    "coalesce(summary,'') || ' ' || coalesce(file_name,'') || ' ' || "
    "coalesce(tags::text,'') || ' ' || coalesce(industry,'') || ' ' || "
    "coalesce(client,'') || ' ' || "
    "coalesce(proposal_type,'') || ' ' || coalesce(graph_type,'') || ' ' || "
    "coalesce(layout_type,''))"
)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)

        # Idempotent column add for pre-existing databases.
        await conn.execute(
            text(
                f"ALTER TABLE slides ADD COLUMN IF NOT EXISTS embedding vector({EMBED_DIM})"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE slides ADD COLUMN IF NOT EXISTS client "
                "varchar NOT NULL DEFAULT ''"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE drive_files ADD COLUMN IF NOT EXISTS display_name "
                "varchar NOT NULL DEFAULT ''"
            )
        )
        # Recurring-meeting ("定例") series columns (folder grouping + date).
        for tbl in ("slides", "drive_files"):
            await conn.execute(
                text(
                    f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS folder_id "
                    "varchar NOT NULL DEFAULT ''"
                )
            )
            await conn.execute(
                text(
                    f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS folder_name "
                    "varchar NOT NULL DEFAULT ''"
                )
            )
            await conn.execute(
                text(
                    f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS doc_date date"
                )
            )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS slides_folder_id_idx "
                "ON slides (folder_id)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS slides_doc_date_idx "
                "ON slides (doc_date)"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS can_upload "
                "boolean NOT NULL DEFAULT false"
            )
        )
        # Resume support: per-slide content fingerprint + job heartbeat.
        await conn.execute(
            text(
                "ALTER TABLE slides ADD COLUMN IF NOT EXISTS "
                "source_fingerprint varchar"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS updated_at "
                "timestamptz NOT NULL DEFAULT now()"
            )
        )
        await conn.execute(
            text(
                "ALTER TABLE ingest_jobs ADD COLUMN IF NOT EXISTS "
                "ingested_files text"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS add_logs_created_at_idx "
                "ON add_logs (created_at DESC)"
            )
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ingest_jobs_started_at_idx "
                "ON ingest_jobs (started_at DESC)"
            )
        )

        # Ensure tags is jsonb (older DBs may have been created with json).
        await conn.execute(
            text(
                "ALTER TABLE slides ALTER COLUMN tags TYPE jsonb "
                "USING tags::jsonb"
            )
        )
        # Full-text search index over title + body + summary.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS slides_fts_idx ON slides "
                f"USING GIN ({FTS_EXPR})"
            )
        )
        # Drop the legacy trigram index (pre-tags era). Cheap no-op once
        # gone.
        await conn.execute(text("DROP INDEX IF EXISTS slides_trgm_idx"))
        # Trigram index for substring matching (handles CJK without word
        # boundaries, which to_tsvector('simple') cannot tokenize well).
        # Includes tags::text and facet columns so their substrings are
        # also indexed.
        #
        # The index is version-gated: we compare the expression stored in
        # pg_indexes against the current SEARCH_EXPR and only drop +
        # recreate when they differ. This avoids paying the rebuild cost
        # on every startup.
        existing_def = (
            await conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND indexname = 'slides_search_trgm_idx'"
                )
            )
        ).scalar_one_or_none()
        # Normalise whitespace for a tolerant comparison; Postgres
        # canonicalises the stored expression so we look for the column
        # tail rather than an exact string match.
        needs_rebuild = existing_def is None or (
            "layout_type" not in (existing_def or "")
            or "graph_type" not in (existing_def or "")
        )
        if needs_rebuild:
            await conn.execute(text("DROP INDEX IF EXISTS slides_search_trgm_idx"))
            await conn.execute(
                text(
                    "CREATE INDEX slides_search_trgm_idx ON slides "
                    f"USING GIN ({SEARCH_EXPR} gin_trgm_ops)"
                )
            )
        # jsonb_path_ops GIN index for tag containment queries.
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS slides_tags_gin_idx ON slides "
                "USING GIN (tags jsonb_path_ops)"
            )
        )
        # Facet columns get a plain btree for equality filters + sort.
        for col in ("industry", "proposal_type", "graph_type", "layout_type"):
            await conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS slides_{col}_idx "
                    f"ON slides ({col})"
                )
            )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS slides_created_at_idx "
                "ON slides (created_at DESC)"
            )
        )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
