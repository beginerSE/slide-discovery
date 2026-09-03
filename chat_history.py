"""Persistence helpers for user-owned conversational-search history."""
from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db import ChatConversation, ChatTurn, Slide, utcnow

_SOURCE_FIELDS = (
    "slideId",
    "fileName",
    "pageNo",
    "slideTitle",
    "thumbnailPath",
    "sourceUrl",
    "sourceType",
    "docCategory",
    "folderId",
    "folderName",
)


def new_request_id() -> str:
    return str(uuid4())


def normalize_request_id(value: str | None) -> str:
    """Use a canonical UUID, generating one when old clients omit the field."""
    cleaned = (value or "").strip()
    if not cleaned:
        return new_request_id()
    try:
        return str(UUID(cleaned))
    except ValueError:
        # Keep malformed-client retries idempotent without storing arbitrary,
        # oversized input in the unique request-id column.
        return str(uuid5(NAMESPACE_URL, cleaned))


def conversation_title(question: str, limit: int = 60) -> str:
    compact = " ".join((question or "").split())
    if len(compact) <= limit:
        return compact or "新しい会話"
    return compact[: limit - 1].rstrip() + "…"


def snapshot_sources(sources: list[dict] | None) -> list[dict]:
    """Keep only fields required to replay source cards, not full slide bodies."""
    return [
        {field: source.get(field) for field in _SOURCE_FIELDS}
        for source in (sources or [])
    ]


async def list_conversations(
    session: AsyncSession, user_id: int, *, active_id: str | None = None
) -> list[dict]:
    turn_count = (
        select(func.count(ChatTurn.id))
        .where(ChatTurn.conversation_id == ChatConversation.id)
        .correlate(ChatConversation)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(ChatConversation, turn_count.label("turn_count"))
            .where(ChatConversation.user_id == user_id)
            .order_by(ChatConversation.updated_at.desc())
        )
    ).all()
    return [
        conversation.to_dict(
            turn_count=count,
            active=conversation.id == active_id,
        )
        for conversation, count in rows
    ]


async def get_conversation(
    session: AsyncSession, user_id: int, conversation_id: str
) -> ChatConversation | None:
    return (
        await session.execute(
            select(ChatConversation)
            .where(ChatConversation.id == conversation_id)
            .where(ChatConversation.user_id == user_id)
        )
    ).scalar_one_or_none()


async def list_turns(
    session: AsyncSession, user_id: int, conversation_id: str
) -> list[dict]:
    rows = (
        await session.execute(
            select(ChatTurn)
            .where(ChatTurn.conversation_id == conversation_id)
            .where(ChatTurn.user_id == user_id)
            .order_by(ChatTurn.created_at.asc(), ChatTurn.id.asc())
        )
    ).scalars().all()
    turns = [row.to_dict() for row in rows]
    missing_slide_ids = {
        source.get("slideId")
        for turn in turns
        for source in turn.get("sources", [])
        if source.get("sourceType") != "confluence"
        and not source.get("folderId")
        and source.get("slideId")
    }
    if not missing_slide_ids:
        return turns

    folder_rows = (
        await session.execute(
            select(Slide.slide_id, Slide.folder_id, Slide.folder_name).where(
                Slide.slide_id.in_(missing_slide_ids)
            )
        )
    ).all()
    folder_by_slide = {
        slide_id: (folder_id or "", folder_name or "")
        for slide_id, folder_id, folder_name in folder_rows
        if folder_id
    }
    for turn in turns:
        enriched_sources = []
        for original in turn.get("sources", []):
            source = dict(original)
            folder = folder_by_slide.get(source.get("slideId"))
            if folder:
                source["folderId"], source["folderName"] = folder
            enriched_sources.append(source)
        turn["sources"] = enriched_sources
    return turns


async def find_turn_by_request(
    session: AsyncSession, user_id: int, request_id: str
) -> ChatTurn | None:
    return (
        await session.execute(
            select(ChatTurn)
            .where(ChatTurn.user_id == user_id)
            .where(ChatTurn.request_id == request_id)
        )
    ).scalar_one_or_none()


async def save_turn(
    session: AsyncSession,
    *,
    user_id: int,
    request_id: str,
    conversation: ChatConversation | None,
    question: str,
    answer: str,
    sources: list[dict],
    search_conditions: dict,
    series_name: str = "",
    series_count: int = 0,
) -> tuple[ChatConversation, ChatTurn]:
    """Atomically create/update a conversation and append one completed turn."""
    now = utcnow()
    if conversation is None:
        conversation = ChatConversation(
            id=str(uuid4()),
            user_id=user_id,
            title=conversation_title(question),
            created_at=now,
            updated_at=now,
        )
        session.add(conversation)
    else:
        if conversation.user_id != user_id:
            raise ValueError("conversation owner mismatch")
        conversation.updated_at = now

    turn = ChatTurn(
        conversation_id=conversation.id,
        user_id=user_id,
        request_id=request_id,
        question=question,
        answer=answer,
        sources=snapshot_sources(sources),
        search_conditions=search_conditions,
        series_name=series_name,
        series_count=series_count,
        degraded=False,
        created_at=now,
    )
    session.add(turn)
    await session.commit()
    return conversation, turn


async def delete_conversation(
    session: AsyncSession, user_id: int, conversation_id: str
) -> bool:
    deleted = (
        await session.execute(
            delete(ChatConversation)
            .where(ChatConversation.id == conversation_id)
            .where(ChatConversation.user_id == user_id)
            .returning(ChatConversation.id)
        )
    ).scalar_one_or_none()
    if deleted is None:
        await session.rollback()
        return False
    await session.commit()
    return True