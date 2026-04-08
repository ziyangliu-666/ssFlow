"""Document ingestion — turn uploaded files / URLs / images into plain text.

Supported input types:

    * ``.txt`` / ``.md`` — read the bytes directly
    * ``.pdf`` — extract text with PyMuPDF (``fitz``)
    * ``.html`` / ``.htm`` — strip with trafilatura
    * URLs — fetch with httpx + strip with trafilatura
    * ``.png`` / ``.jpg`` / ``.jpeg`` / ``.webp`` / ``.gif`` — OCR via OpenAI
      vision (reusing ``llm_client``)

All extractors return an ``IngestedDoc`` with the raw text + metadata.
The event-extractor pipeline concatenates these into the corpus it
passes to Stage 0c synthesis.

Design notes
------------
* Every extractor is async so `/extract` can fan out across many files.
* PyMuPDF and trafilatura are imported lazily inside the functions that
  need them so the rest of the codebase can import this module even
  when those optional deps are missing.
* Image OCR uses the existing `chat_json`/`chat_text` plumbing; no new
  dependencies. The model is configured in ``config.settings`` (default
  ``gpt-4o-mini`` which supports vision input).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import settings
from .llm_client import chat


log = logging.getLogger(__name__)


# ─────────────────────── Constants ───────────────────────

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".csv"}
PDF_EXTENSIONS = {".pdf"}
HTML_EXTENSIONS = {".html", ".htm"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}
MAX_TEXT_CHARS_PER_DOC = 50_000  # trim per-doc; the corpus aggregator trims again


# ─────────────────────── Result dataclass ───────────────────────


@dataclass
class IngestedDoc:
    """One ingested document ready to be added to the extractor corpus."""

    source: str                                    # original path or URL
    kind: str                                      # text | pdf | html | image | url
    text: str                                      # extracted plain text
    char_count: int = 0
    title: str = ""
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_corpus_entry(self) -> dict[str, str]:
        """Shape expected by `event_extractor.extract_event(pre_fetched_corpus=...)`."""
        return {
            "url": self.source,
            "title": self.title or Path(self.source).name,
            "excerpt": self.text[:MAX_TEXT_CHARS_PER_DOC],
        }


# ─────────────────────── Text files ───────────────────────


async def _ingest_text(path: Path) -> IngestedDoc:
    data = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    data = data.strip()
    return IngestedDoc(
        source=str(path),
        kind="text",
        text=data,
        char_count=len(data),
        title=path.name,
    )


# ─────────────────────── PDF files ───────────────────────


def _extract_pdf_sync(path: Path) -> tuple[str, str, list[str]]:
    """Synchronous PyMuPDF extraction — runs in a worker thread."""
    try:
        import fitz  # type: ignore
    except ImportError as exc:  # pragma: no cover — optional dep
        raise RuntimeError(
            "pymupdf is required to ingest PDF files. "
            "pip install 'pymupdf>=1.24.0'"
        ) from exc

    warnings: list[str] = []
    title = ""
    text_parts: list[str] = []
    try:
        with fitz.open(str(path)) as doc:
            title = (doc.metadata or {}).get("title", "") or path.name
            for page_num, page in enumerate(doc):
                try:
                    text_parts.append(page.get_text("text"))
                except Exception as exc:  # pragma: no cover
                    warnings.append(f"page {page_num} extract failed: {exc}")
    except Exception as exc:
        raise RuntimeError(f"failed to open PDF {path}: {exc}") from exc
    return "\n\n".join(text_parts).strip(), title, warnings


async def _ingest_pdf(path: Path) -> IngestedDoc:
    text, title, warnings = await asyncio.to_thread(_extract_pdf_sync, path)
    return IngestedDoc(
        source=str(path),
        kind="pdf",
        text=text,
        char_count=len(text),
        title=title or path.name,
        warnings=warnings,
    )


# ─────────────────────── HTML files ───────────────────────


def _extract_html_sync(html: str, url: str = "") -> tuple[str, str]:
    """Strip boilerplate from HTML. Returns (main_text, title)."""
    try:
        import trafilatura  # type: ignore
    except ImportError as exc:  # pragma: no cover — optional dep
        raise RuntimeError(
            "trafilatura is required to ingest HTML / URLs. "
            "pip install 'trafilatura>=1.12.0'"
        ) from exc

    extracted = trafilatura.extract(
        html,
        url=url or None,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    ) or ""
    title = ""
    try:
        metadata = trafilatura.extract_metadata(html)
        if metadata is not None:
            title = metadata.title or ""
    except Exception:  # pragma: no cover
        pass
    return extracted.strip(), title.strip()


async def _ingest_html_file(path: Path) -> IngestedDoc:
    raw = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
    text, title = await asyncio.to_thread(_extract_html_sync, raw, str(path))
    return IngestedDoc(
        source=str(path),
        kind="html",
        text=text,
        char_count=len(text),
        title=title or path.name,
    )


# ─────────────────────── URLs ───────────────────────


async def ingest_url(url: str, timeout: float = 20.0) -> IngestedDoc:
    """Fetch + strip a URL into plain text."""
    warnings: list[str] = []
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (ssFlow document ingestor; research use)"
                ),
            },
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
    except Exception as exc:
        log.warning("ingest_url: fetch failed for %s: %s", url, exc)
        return IngestedDoc(
            source=url,
            kind="url",
            text="",
            title=url,
            warnings=[f"fetch failed: {exc}"],
        )

    text, title = await asyncio.to_thread(_extract_html_sync, html, url)
    if not text:
        warnings.append("trafilatura returned empty extract")
    return IngestedDoc(
        source=url,
        kind="url",
        text=text,
        char_count=len(text),
        title=title or url,
        warnings=warnings,
    )


# ─────────────────────── Images → OCR via OpenAI vision ───────────────────────


OCR_USER_PROMPT = (
    "Extract all visible text from this image. Preserve the structure "
    "(headings, paragraphs, bullet points, tables). Output plain text only — "
    "no markdown fences. If the image contains a chart or figure, describe "
    "the axes, labels, and any visible numbers. If the image has no "
    "readable text, output the single word: EMPTY."
)


async def _ingest_image(path: Path) -> IngestedDoc:
    """OCR an image via OpenAI vision (reusing llm_client cost tracking)."""
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = await asyncio.to_thread(path.read_bytes)
    b64 = base64.b64encode(data).decode("ascii")

    messages = [
        {
            "role": "system",
            "content": "You are an OCR assistant. Extract text faithfully.",
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_USER_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
            ],
        },
    ]

    try:
        result = await chat(
            messages=messages,
            model=settings.default_model,
            max_tokens=2000,
        )
        text = (result.content or "").strip()
    except Exception as exc:
        log.warning("image OCR failed for %s: %s", path, exc)
        return IngestedDoc(
            source=str(path),
            kind="image",
            text="",
            title=path.name,
            warnings=[f"OCR failed: {exc}"],
        )

    if text.upper() == "EMPTY":
        text = ""

    return IngestedDoc(
        source=str(path),
        kind="image",
        text=text,
        char_count=len(text),
        title=path.name,
        metadata={"ocr_model": settings.default_model, "mime": mime},
    )


# ─────────────────────── Dispatcher ───────────────────────


async def ingest_file(path: str | Path) -> IngestedDoc:
    """Pick the right extractor for a local file path based on extension."""
    p = Path(path)
    if not p.exists():
        return IngestedDoc(
            source=str(p),
            kind="unknown",
            text="",
            title=p.name,
            warnings=["file not found"],
        )
    suffix = p.suffix.lower()
    try:
        if suffix in TEXT_EXTENSIONS:
            return await _ingest_text(p)
        if suffix in PDF_EXTENSIONS:
            return await _ingest_pdf(p)
        if suffix in HTML_EXTENSIONS:
            return await _ingest_html_file(p)
        if suffix in IMAGE_EXTENSIONS:
            return await _ingest_image(p)
        # Fallback: try reading as text — works for most plain files.
        try:
            return await _ingest_text(p)
        except Exception as exc:
            return IngestedDoc(
                source=str(p),
                kind="unknown",
                text="",
                title=p.name,
                warnings=[f"unsupported extension '{suffix}': {exc}"],
            )
    except Exception as exc:
        log.exception("ingest_file failed for %s", p)
        return IngestedDoc(
            source=str(p),
            kind="error",
            text="",
            title=p.name,
            warnings=[str(exc)],
        )


async def ingest_many(
    files: list[str | Path] | None = None,
    urls: list[str] | None = None,
) -> list[IngestedDoc]:
    """Run ingest_file and ingest_url across a set of inputs concurrently."""
    tasks: list[asyncio.Task[IngestedDoc]] = []
    if files:
        for f in files:
            tasks.append(asyncio.create_task(ingest_file(f)))
    if urls:
        for u in urls:
            tasks.append(asyncio.create_task(ingest_url(u)))
    if not tasks:
        return []
    return list(await asyncio.gather(*tasks))


def build_corpus(docs: list[IngestedDoc]) -> list[dict[str, str]]:
    """Turn ingested docs into the corpus entries `extract_event` expects."""
    return [d.to_corpus_entry() for d in docs if d.text]


__all__ = [
    "IngestedDoc",
    "TEXT_EXTENSIONS",
    "PDF_EXTENSIONS",
    "HTML_EXTENSIONS",
    "IMAGE_EXTENSIONS",
    "ingest_file",
    "ingest_url",
    "ingest_many",
    "build_corpus",
]
