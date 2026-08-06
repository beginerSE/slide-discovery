"""User guide page content: storage + Markdown rendering.

The guide is a single Markdown document an admin can edit from the UI. It is
persisted in the ``app_state`` key/value table (key ``GUIDE_KEY``) so it
survives restarts and is shared across instances, and rendered to HTML on read.

Only admins can edit the guide, so the stored Markdown is trusted authoring
input; it is rendered with the standard ``markdown`` library (tables / fenced
code / line breaks) for a friendly beginner-facing page.
"""
from __future__ import annotations

import markdown as _markdown
from sqlalchemy.ext.asyncio import AsyncSession

from db import AppState

GUIDE_KEY = "user_guide"

DEFAULT_GUIDE = """# 社内スライド検索の使い方

このツールは、社内に蓄積されたスライド資料（PowerPoint）を
AIで横断・詳細に検索できる社内向けツールです。
**キーワード**のほか、**自然文（意味）**でのセマンティック検索や
AIとの対話検索にも対応しています。

## はじめに

1. 左メニューの **スライド検索** を開きます。
2. 検索ボックスにキーワードや調べたい内容を入力します。
   - 例: `小売 DX 提案`、`コスト削減の効果を示すグラフ`
3. 結果カードのサムネイルをクリックすると、スライドの詳細が見られます。
4. 詳細ページからは、元の資料（スライド）も開けます。

## 2つの検索方法

| 方法 | こんなときに | 特徴 |
| --- | --- | --- |
| スライド検索 | キーワードで素早く探したい | 絞り込み（業界・顧客・スライド種別など）が使える |
| 対話検索 | 「〜な資料ある？」と相談したい | 質問に対してAIが要約し、根拠スライドを提示 |

## 絞り込み（フィルタ）

検索結果の左側で、業界・顧客・スライド種別・グラフ種別・タグで絞り込めます。
複数を組み合わせると、より目的に近い資料が見つかります。

## 対話検索

左メニューの **対話検索** では、知りたいことを文章で質問できます。
AIが関連スライドを探して回答し、参考にしたスライドを一覧で表示します。
回答の下の「資料を開く」から、元の資料の該当ページへ直接ジャンプできます。

## 困ったときは

- 検索しても出てこない → キーワードを短くしたり、言い換えてみてください。
- 資料が見つからない → 管理者に資料の取り込みを依頼してください。

> このページの内容は管理者が編集できます。
"""


def render_markdown(text: str) -> str:
    """Render the guide Markdown to HTML for display."""
    return _markdown.markdown(
        text or "",
        extensions=["extra", "sane_lists", "nl2br"],
        output_format="html",
    )


async def get_guide_markdown(session: AsyncSession) -> str:
    """Return the stored guide Markdown, or the default if none is saved yet."""
    row = await session.get(AppState, GUIDE_KEY)
    if row is None or not (row.value or "").strip():
        return DEFAULT_GUIDE
    return row.value


async def set_guide_markdown(session: AsyncSession, text: str) -> None:
    """Persist the guide Markdown (upsert into ``app_state``)."""
    row = await session.get(AppState, GUIDE_KEY)
    if row is None:
        session.add(AppState(key=GUIDE_KEY, value=text))
    else:
        row.value = text
    await session.commit()
