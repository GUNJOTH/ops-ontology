"""Local document and fragment ingestion for the Knowledge Registry."""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree

from build_business_semantics_layer import init_layer, stable_id


class UnsupportedDocumentError(ValueError):
    """The controlled local parser does not support the file type."""


@dataclass(frozen=True)
class DocumentFragment:
    ordinal: int
    heading: str
    content: str
    page_number: int | None = None
    section_path: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _split_text(text: str) -> list[DocumentFragment]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", text.replace("\r\n", "\n")) if block.strip()]
    fragments: list[DocumentFragment] = []
    for ordinal, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        heading = lines[0].lstrip("# ").strip() if lines else ""
        content = block.strip()
        fragments.append(DocumentFragment(ordinal, heading, content, section_path=heading))
    if not fragments and text.strip():
        fragments.append(DocumentFragment(1, "", text.strip()))
    return fragments


def _parse_docx(path: pathlib.Path) -> list[DocumentFragment]:
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t")).strip()
        if text:
            paragraphs.append(text)
    return _split_text("\n\n".join(paragraphs))


def _parse_pdf(path: pathlib.Path) -> list[DocumentFragment]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise UnsupportedDocumentError("PDF 解析依赖 pypdf 未安装") from exc
    fragments: list[DocumentFragment] = []
    for page_number, page in enumerate(PdfReader(str(path)).pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            fragments.append(DocumentFragment(page_number, "", text, page_number=page_number))
    return fragments


def parse_document(path: pathlib.Path) -> tuple[list[DocumentFragment], str]:
    """Parse one local document without copying it into the source layer."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv"}:
        fragments = _split_text(path.read_text(encoding="utf-8-sig"))
        parser = "text"
    elif suffix == ".docx":
        fragments = _parse_docx(path)
        parser = "docx-xml"
    elif suffix == ".pdf":
        fragments = _parse_pdf(path)
        parser = "pypdf"
    else:
        raise UnsupportedDocumentError(f"暂不支持文档类型：{suffix or '无扩展名'}")
    return fragments, parser


def _insert_asset(
    connection: sqlite3.Connection,
    *,
    asset_key: str,
    asset_type: str,
    knowledge_kind: str,
    title: str,
    definition: str,
    version: str,
    package_id: str,
    domain: str,
    source_id: str,
    source_uri: str,
    now: str,
) -> tuple[str, str]:
    asset_id = stable_id("KA", asset_key)
    content_hash = _hash_bytes(definition.encode("utf-8"))
    version_id = stable_id("KAV", asset_id, version)
    connection.execute(
        """
        INSERT INTO knowledge_asset(
          asset_id,asset_key,asset_type,title,canonical_definition,current_version,status,source_scope,
          knowledge_kind,package_id,knowledge_domain,source_type,source_id,source_uri,lifecycle_status,
          confidence,quality_score,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(asset_key) DO UPDATE SET
          title=excluded.title,canonical_definition=excluded.canonical_definition,
          current_version=excluded.current_version,knowledge_kind=excluded.knowledge_kind,
          package_id=excluded.package_id,knowledge_domain=excluded.knowledge_domain,
          source_type=excluded.source_type,source_id=excluded.source_id,source_uri=excluded.source_uri,
          updated_at=excluded.updated_at
        """,
        (asset_id, asset_key, asset_type, title[:500], definition, version, "draft", "local_document",
         knowledge_kind, package_id, domain, "document", source_id, source_uri, "Draft", 0.0, 0.0, now, now),
    )
    connection.execute(
        """
        INSERT INTO knowledge_asset_version(
          asset_version_id,asset_id,version,content_hash,definition_json,status,preview_count,
          replay_count,replay_pass_count,replay_fail_count,ontology_version,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(asset_id,version) DO UPDATE SET
          content_hash=excluded.content_hash,definition_json=excluded.definition_json,
          status=excluded.status
        """,
        (version_id, asset_id, version, content_hash,
         json.dumps({"content": definition, "canonical": definition}, ensure_ascii=False, sort_keys=True),
         "draft", 0, 0, 0, 0, "enterprise-operations-ontology/v2", now),
    )
    return asset_id, version_id


def import_document(
    connection: sqlite3.Connection,
    path: pathlib.Path,
    *,
    package_id: str = "platform-core",
    domain: str = "enterprise-operations",
    source_snapshot_id: str = "local-document-import",
    owner: str = "",
) -> dict[str, object]:
    """Import a document and immutable fragments as local draft assets."""
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(path)
    fragments, parser = parse_document(path)
    raw = path.read_bytes()
    document_hash = _hash_bytes(raw)
    document_key = f"DOCUMENT:{document_hash[:32]}"
    now = _now()
    init_layer(connection)
    asset_id, version_id = _insert_asset(
        connection, asset_key=document_key, asset_type="document", knowledge_kind="document",
        title=path.stem, definition="\n\n".join(fragment.content for fragment in fragments), version="v1",
        package_id=package_id, domain=domain, source_id=document_key, source_uri=str(path), now=now,
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO knowledge_asset_source(
          source_id,asset_id,source_kind,source_record_id,source_table,source_snapshot_id,
          source_status,evidence_json,source_uri,provenance_role,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (stable_id("KAS", document_key), asset_id, "document", document_key, "local_document",
         source_snapshot_id, "read_only", json.dumps({"sha256": document_hash, "parser": parser}, ensure_ascii=False),
         str(path), "source-document", now),
    )
    fragment_ids: list[str] = []
    for fragment in fragments:
        fragment_key = f"FRAGMENT:{document_key}:{fragment.ordinal}"
        fragment_id, fragment_version_id = _insert_asset(
            connection, asset_key=fragment_key, asset_type="fragment", knowledge_kind="fragment",
            title=fragment.heading or f"{path.stem}#{fragment.ordinal}", definition=fragment.content,
            version="v1", package_id=package_id, domain=domain, source_id=fragment_key,
            source_uri=str(path), now=now,
        )
        fragment_ids.append(fragment_id)
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_asset_part(
              part_id,asset_version_id,part_type,ordinal,label,expression_json,created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (stable_id("KAP", fragment_key), fragment_version_id, "evidence", fragment.ordinal,
             fragment.heading or f"fragment-{fragment.ordinal}", json.dumps({"content": fragment.content}, ensure_ascii=False), now),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_asset_source(
              source_id,asset_id,source_kind,source_record_id,source_table,source_snapshot_id,
              source_status,evidence_json,source_uri,provenance_role,page_number,section_path,fragment_hash,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (stable_id("KAS", fragment_key), fragment_id, "document_fragment", fragment_key, "local_document",
             source_snapshot_id, "read_only", json.dumps({"documentAssetId": asset_id}, ensure_ascii=False),
             str(path), "extracted-fragment", fragment.page_number, fragment.section_path,
             _hash_bytes(fragment.content.encode("utf-8")), now),
        )
    connection.execute(
        "UPDATE knowledge_asset SET owner=?,source_uri=?,updated_at=? WHERE asset_id=?",
        (owner, str(path), now, asset_id),
    )
    connection.commit()
    return {
        "assetId": asset_id,
        "assetKey": document_key,
        "versionId": version_id,
        "fragmentIds": fragment_ids,
        "fragmentCount": len(fragment_ids),
        "parser": parser,
        "contentHash": document_hash,
        "status": "draft",
        "sourceWrite": False,
        "formalPublication": False,
    }
