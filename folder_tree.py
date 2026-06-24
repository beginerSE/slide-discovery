"""Build a nested Drive folder tree annotated with per-file ingest status.

Pure, DB-free logic (unit-tested in tests/test_folder_tree.py): given the flat
folders/files discovered by a live Drive walk (``drive.list_folder_tree``) and a
``drive_file_id -> DriveFile.to_dict()`` status map, produce a nested tree the
admin UI renders, plus a summary count per ingest state.

A file's ``state`` is one of:
- ``ready``        — tracked and ingested (slides extracted)
- ``processing``   — tracked, ingest in progress
- ``failed``       — tracked, last ingest failed
- ``pending``      — tracked/registered but not yet ingested
- ``unregistered`` — present in Drive but never added (the "未取り込み" gap)
"""
from __future__ import annotations

from typing import Any

_STATES = ("ready", "processing", "failed", "pending", "unregistered")


def _file_state(status: dict | None) -> str:
    if not status:
        return "unregistered"
    s = status.get("status")
    if s in ("ready", "processing", "failed"):
        return s
    return "pending"


def _empty_summary() -> dict[str, int]:
    summary = {"total": 0}
    for s in _STATES:
        summary[s] = 0
    return summary


def _sort_node(node: dict) -> None:
    node["folders"].sort(key=lambda n: (n["name"] or "").lower())
    node["files"].sort(key=lambda f: (f["name"] or "").lower())
    for child in node["folders"]:
        _sort_node(child)


def build_tree(
    root_id: str,
    root_name: str,
    folders: list[dict[str, Any]],
    files: list[dict[str, Any]],
    status_by_file_id: dict[str, dict],
) -> tuple[dict, dict]:
    """Assemble the nested tree + summary.

    ``folders`` items: ``{"id", "name", "parentId"}`` (root excluded).
    ``files`` items:   ``{"fileId", "name", "parentId"}``.
    Returns ``(root_node, summary)``.
    """

    def _make_node(fid: str, name: str) -> dict:
        return {"id": fid, "name": name, "folders": [], "files": []}

    nodes: dict[str, dict] = {root_id: _make_node(root_id, root_name or "（指定フォルダ）")}

    # 1) Materialise every folder node first so parents exist before linking.
    for f in folders:
        fid = f["id"]
        if fid == root_id:
            continue
        name = f.get("name") or "（無名フォルダ）"
        if fid not in nodes:
            nodes[fid] = _make_node(fid, name)
        elif f.get("name") and nodes[fid]["name"] == "（無名フォルダ）":
            nodes[fid]["name"] = f["name"]

    # 2) Link each folder under its parent (unknown parent -> root).
    for f in folders:
        fid = f["id"]
        if fid == root_id:
            continue
        parent = nodes.get(f.get("parentId")) or nodes[root_id]
        parent["folders"].append(nodes[fid])

    # 3) Attach files with their resolved ingest state.
    summary = _empty_summary()
    for fl in files:
        file_id = fl["fileId"]
        status = status_by_file_id.get(file_id)
        state = _file_state(status)
        st = status or {}
        entry = {
            "fileId": file_id,
            "name": fl.get("name") or st.get("fileName") or file_id,
            "state": state,
            "slideCount": st.get("slideCount") or 0,
            "lastIngestedAt": st.get("lastIngestedAt"),
            "shareUrl": st.get("shareUrl")
            or f"https://drive.google.com/file/d/{file_id}/view",
            "dbId": st.get("id"),
            "lastError": st.get("lastError"),
        }
        summary["total"] += 1
        summary[state] += 1
        parent = nodes.get(fl.get("parentId")) or nodes[root_id]
        parent["files"].append(entry)

    root = nodes[root_id]
    _sort_node(root)
    return root, summary
