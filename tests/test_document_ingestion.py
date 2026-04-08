"""Tests for `ssflow.document_ingestion`.

Avoids the network and the LLM:
  * URL ingestion is exercised against an in-process httpx mock.
  * Image OCR is patched to return canned text.
  * PDF + plain-text + HTML ingestion runs against real bytes generated
    inside the test (so no fixtures on disk).
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import ssflow.document_ingestion as di
from ssflow.document_ingestion import (
    HTML_EXTENSIONS,
    IMAGE_EXTENSIONS,
    PDF_EXTENSIONS,
    TEXT_EXTENSIONS,
    IngestedDoc,
    build_corpus,
    ingest_file,
    ingest_many,
    ingest_url,
)


# ─────────────────────── Constants / dataclass ───────────────────────


def test_extension_sets_are_disjoint_and_lowercase():
    sets = [TEXT_EXTENSIONS, PDF_EXTENSIONS, HTML_EXTENSIONS, IMAGE_EXTENSIONS]
    for s in sets:
        for ext in s:
            assert ext == ext.lower()
            assert ext.startswith(".")
    flat = [ext for s in sets for ext in s]
    assert len(flat) == len(set(flat)), "extension sets must be disjoint"


def test_ingested_doc_to_corpus_entry_uses_filename_when_no_title():
    doc = IngestedDoc(
        source="/tmp/foo.txt",
        kind="text",
        text="hello world",
    )
    entry = doc.to_corpus_entry()
    assert entry["url"] == "/tmp/foo.txt"
    assert entry["title"] == "foo.txt"
    assert entry["excerpt"] == "hello world"


def test_to_corpus_entry_truncates_to_max_chars():
    big = "A" * (di.MAX_TEXT_CHARS_PER_DOC + 100)
    doc = IngestedDoc(source="big.txt", kind="text", text=big)
    assert len(doc.to_corpus_entry()["excerpt"]) == di.MAX_TEXT_CHARS_PER_DOC


# ─────────────────────── Plain-text files ───────────────────────


@pytest.mark.asyncio
async def test_ingest_text_file(tmp_path: Path):
    f = tmp_path / "note.md"
    f.write_text("# 标题\n\nbody text 中文 mixed", encoding="utf-8")
    doc = await ingest_file(f)
    assert doc.kind == "text"
    assert doc.title == "note.md"
    assert "body text 中文 mixed" in doc.text
    assert doc.char_count == len(doc.text)
    assert doc.warnings == []


@pytest.mark.asyncio
async def test_ingest_missing_file_returns_warning():
    doc = await ingest_file("/nonexistent/foo.txt")
    assert doc.kind == "unknown"
    assert doc.text == ""
    assert any("not found" in w for w in doc.warnings)


# ─────────────────────── PDF files ───────────────────────


def _make_pdf(text: str) -> bytes:
    """Build a tiny in-memory PDF with PyMuPDF."""
    import pymupdf

    doc = pymupdf.open()  # blank
    page = doc.new_page()
    page.insert_text((72, 72), text)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


@pytest.mark.asyncio
async def test_ingest_pdf_extracts_text(tmp_path: Path):
    pytest.importorskip("pymupdf")
    f = tmp_path / "note.pdf"
    f.write_bytes(_make_pdf("Hello PDF world\nLine two"))
    doc = await ingest_file(f)
    assert doc.kind == "pdf"
    assert "Hello PDF world" in doc.text
    assert "Line two" in doc.text


# ─────────────────────── HTML files / URL ingestion ───────────────────────


HTML_SAMPLE = """\
<!DOCTYPE html>
<html>
<head><title>Sample Article</title></head>
<body>
  <nav>nav nav nav nav nav</nav>
  <article>
    <h1>The headline</h1>
    <p>The body of the article describes a market event happening today.</p>
    <p>Followed by another paragraph with substance and a few specific numbers
       like 12.34 and 56789 to make trafilatura keep it as the main content.</p>
  </article>
  <footer>footer footer footer</footer>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_ingest_html_file_strips_chrome(tmp_path: Path):
    pytest.importorskip("trafilatura")
    f = tmp_path / "article.html"
    f.write_text(HTML_SAMPLE, encoding="utf-8")
    doc = await ingest_file(f)
    assert doc.kind == "html"
    assert "headline" in doc.text.lower() or "body of the article" in doc.text.lower()
    # Boilerplate should be gone.
    assert "nav nav nav" not in doc.text
    assert "footer footer footer" not in doc.text


@pytest.mark.asyncio
async def test_ingest_url_uses_httpx_mock():
    pytest.importorskip("trafilatura")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=HTML_SAMPLE)

    transport = httpx.MockTransport(handler)

    # Patch the AsyncClient ctor to inject our mock transport.
    real_ctor = httpx.AsyncClient

    def fake_ctor(*args, **kwargs):
        kwargs["transport"] = transport
        return real_ctor(*args, **kwargs)

    with patch.object(di.httpx, "AsyncClient", fake_ctor):
        doc = await ingest_url("https://example.test/article")

    assert doc.kind == "url"
    assert "headline" in doc.text.lower() or "body of the article" in doc.text.lower()
    assert "nav nav nav" not in doc.text
    assert doc.warnings == []


@pytest.mark.asyncio
async def test_ingest_url_handles_network_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    transport = httpx.MockTransport(handler)
    real_ctor = httpx.AsyncClient

    def fake_ctor(*args, **kwargs):
        kwargs["transport"] = transport
        return real_ctor(*args, **kwargs)

    with patch.object(di.httpx, "AsyncClient", fake_ctor):
        doc = await ingest_url("https://broken.test")

    assert doc.text == ""
    assert any("fetch failed" in w for w in doc.warnings)


# ─────────────────────── Images / OCR ───────────────────────


@pytest.mark.asyncio
async def test_ingest_image_uses_chat(tmp_path: Path):
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    f = tmp_path / "shot.png"
    f.write_bytes(fake_png)

    captured: dict = {}

    class FakeResp:
        content = "Extracted text from image: hello"

    async def fake_chat(messages, model, max_tokens):
        captured["messages"] = messages
        captured["model"] = model
        return FakeResp()

    with patch.object(di, "chat", fake_chat):
        doc = await ingest_file(f)

    assert doc.kind == "image"
    assert "hello" in doc.text
    # Vision payload was passed in the user message.
    user_msg = captured["messages"][-1]
    assert isinstance(user_msg["content"], list)
    image_part = next(c for c in user_msg["content"] if c.get("type") == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_ingest_image_handles_empty_response(tmp_path: Path):
    f = tmp_path / "blank.png"
    f.write_bytes(b"\x89PNG" + b"\x00" * 16)

    class FakeResp:
        content = "EMPTY"

    async def fake_chat(messages, model, max_tokens):
        return FakeResp()

    with patch.object(di, "chat", fake_chat):
        doc = await ingest_file(f)

    assert doc.text == ""
    assert doc.kind == "image"


# ─────────────────────── ingest_many + build_corpus ───────────────────────


@pytest.mark.asyncio
async def test_ingest_many_handles_files_and_urls_concurrently(tmp_path: Path):
    f1 = tmp_path / "a.txt"
    f1.write_text("alpha", encoding="utf-8")
    f2 = tmp_path / "b.txt"
    f2.write_text("bravo", encoding="utf-8")

    def handler(req):
        return httpx.Response(200, html=HTML_SAMPLE)

    transport = httpx.MockTransport(handler)
    real_ctor = httpx.AsyncClient

    def fake_ctor(*args, **kwargs):
        kwargs["transport"] = transport
        return real_ctor(*args, **kwargs)

    with patch.object(di.httpx, "AsyncClient", fake_ctor):
        docs = await ingest_many(files=[f1, f2], urls=["https://example.test/x"])

    assert len(docs) == 3
    sources = {d.source for d in docs}
    assert str(f1) in sources
    assert str(f2) in sources
    assert "https://example.test/x" in sources


@pytest.mark.asyncio
async def test_ingest_many_returns_empty_list_when_no_inputs():
    docs = await ingest_many(files=None, urls=None)
    assert docs == []


def test_build_corpus_skips_empty_text():
    docs = [
        IngestedDoc(source="a.txt", kind="text", text="hello"),
        IngestedDoc(source="b.txt", kind="text", text=""),
        IngestedDoc(source="c.txt", kind="text", text="world"),
    ]
    corpus = build_corpus(docs)
    assert len(corpus) == 2
    sources = {entry["url"] for entry in corpus}
    assert sources == {"a.txt", "c.txt"}
