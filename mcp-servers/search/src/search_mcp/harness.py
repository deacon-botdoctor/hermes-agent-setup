from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlparse, urlunparse

SCHEMA_VERSION = "hermes-research-harness/v1"
LEGACY_SCHEMA_VERSION = "hermes-research-harness/v0"

IMPORTANCE_RANK = {"very_high": 0, "high": 1, "fair": 2, "low": 3}
VALID_IMPORTANCE = tuple(IMPORTANCE_RANK.keys())
VALID_FETCH_STATUS = (
    "ok",
    "metadata_only",
    "blocked",
    "truncated",
    "failed",
    "not_fetched",
    "local_only",
    "unknown",
)
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+.-]{1,}")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha1_text(value: str, n: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="ignore")).hexdigest()[:n]


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        if "://" not in url and not url.startswith("//"):
            url = "https://" + url
        parsed = urlparse(url)
        scheme = (parsed.scheme or "https").lower()
        netloc = parsed.netloc.lower().removeprefix("www.")
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        # Drop fragments; keep query because docs/search pages often need it.
        return urlunparse((scheme, netloc, path.rstrip("/") or "/", "", parsed.query, ""))
    except Exception:
        return url


def normalize_source_uri(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value) or value.startswith("//"):
        return normalize_url(value)
    return re.sub(r"\s+", " ", value)


def compact_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def token_set(value: str) -> set[str]:
    return {m.group(0).lower() for m in WORD_RE.finditer(value or "") if len(m.group(0)) >= 3}


def excerpt_for_terms(text: str, terms: Iterable[str], max_chars: int = 360) -> str:
    text = compact_ws(text)
    if not text:
        return ""
    lowered = text.lower()
    positions = [lowered.find(t.lower()) for t in terms if t and lowered.find(t.lower()) >= 0]
    if not positions:
        return text[:max_chars]
    start = max(0, min(positions) - max_chars // 3)
    end = min(len(text), start + max_chars)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def load_jsonish(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception as exc:
        raise SystemExit(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("metadata JSON must be an object")
    return parsed


@dataclass
class HarnessLimits:
    max_candidates: int = 80
    max_curated: int = 30
    max_candidate_chars: int = 40_000
    max_total_chars: int = 1_200_000
    render_default_chars: int = 12_000
    min_verify_terms: int = 2


@dataclass
class ResearchTask:
    query: str
    lane: str = "general"
    topic: str = "general"
    client_lock: str = ""
    objective: str = "Find, curate, and verify evidence before producing a report."
    required_output: str = "report.md + evidence.md + state.json + manifest.json"
    created_at: str = field(default_factory=utc_now)


@dataclass
class Candidate:
    id: str
    source_type: str
    url: str = ""
    source_uri: str = ""
    normalized_source: str = ""
    title: str = ""
    text: str = ""
    text_ref: str = ""
    fetch_status: str = "unknown"
    fetched_at: str = ""
    trust_notes: str = ""
    added_at: str = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""

    def snippet(self, n: int = 260) -> str:
        body = compact_ws(self.text)
        if not body and self.text_ref:
            body = f"[text_ref: {self.text_ref}]"
        return body[: n - 1] + "…" if len(body) > n else body


@dataclass
class CuratedItem:
    candidate_id: str
    importance: Literal["very_high", "high", "fair", "low"] = "fair"
    rationale: str = ""
    added_at: str = field(default_factory=utc_now)


@dataclass
class VerificationRecord:
    claim: str
    candidate_ids: list[str]
    status_by_candidate: dict[str, str]
    quotes_by_candidate: dict[str, str]
    required_terms: list[str] = field(default_factory=list)
    verifier_type: str = "term"
    created_at: str = field(default_factory=utc_now)

    @property
    def supported_count(self) -> int:
        return sum(1 for status in self.status_by_candidate.values() if status == "supported")


@dataclass
class ResearchState:
    task: ResearchTask
    schema_version: str = SCHEMA_VERSION
    candidates: dict[str, Candidate] = field(default_factory=dict)
    curated: dict[str, CuratedItem] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)
    verification_records: list[VerificationRecord] = field(default_factory=list)
    search_history: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    duplicate_events: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_candidate_chars: int = 0
    turn: int = 0


class ResearchHarness:
    """State-externalizing harness for Hermes search/research subagents.

    This class deliberately keeps routine bookkeeping out of the model policy:
    candidate tracking, dedupe, curation capacity, verification records, and
    budget-aware context rendering are owned by the harness.
    """

    def __init__(self, task: ResearchTask, limits: HarnessLimits | None = None):
        self.task = task
        self.limits = limits or HarnessLimits()
        self.state = ResearchState(task=task)
        self._url_index: dict[str, str] = {}
        self._source_index: dict[str, str] = {}
        self._content_index: dict[str, str] = {}

    # ── Candidate and search state ──────────────────────────────────────────
    def add_search_record(self, tool_name: str, query_or_target: str, returned: int, added: int) -> None:
        self.state.turn += 1
        self.state.search_history.append(
            f"T{self.state.turn}: {tool_name}({compact_ws(query_or_target)[:180]}) → {returned} returned, {added} added"
        )

    def add_candidate(
        self,
        *,
        source_type: str,
        url: str = "",
        source_uri: str = "",
        title: str = "",
        text: str = "",
        text_ref: str = "",
        fetch_status: str = "unknown",
        fetched_at: str = "",
        trust_notes: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Add a candidate. Returns (candidate_id, status).

        status is one of: added, duplicate_url, duplicate_source, duplicate_content, skipped_limit.
        """
        norm_url = normalize_url(url)
        norm_source = normalize_source_uri(source_uri or norm_url or url)
        clean_text = (text or "").strip()
        if len(clean_text) > self.limits.max_candidate_chars:
            clean_text = clean_text[: self.limits.max_candidate_chars] + "\n[TRUNCATED_BY_HARNESS]"
            if fetch_status in {"ok", "unknown"}:
                fetch_status = "truncated"
        status_tag = (fetch_status or "unknown").strip().lower()
        if status_tag not in VALID_FETCH_STATUS:
            status_tag = "unknown"
        content_hash = sha1_text(compact_ws(clean_text).lower(), 16) if clean_text else ""

        if norm_url and norm_url in self._url_index:
            existing = self._url_index[norm_url]
            self.state.duplicate_events.append({"kind": "url", "candidate_id": existing, "url": norm_url})
            return existing, "duplicate_url"
        if norm_source and norm_source in self._source_index:
            existing = self._source_index[norm_source]
            self.state.duplicate_events.append({"kind": "source", "candidate_id": existing, "source_uri": norm_source})
            return existing, "duplicate_source"
        if content_hash and content_hash in self._content_index:
            existing = self._content_index[content_hash]
            self.state.duplicate_events.append({"kind": "content", "candidate_id": existing, "url": norm_url, "source_uri": norm_source})
            return existing, "duplicate_content"
        if len(self.state.candidates) >= self.limits.max_candidates:
            self.state.warnings.append(f"candidate limit reached: {self.limits.max_candidates}; skipped {norm_source or norm_url or title or source_type}")
            return "", "skipped_limit"
        if self.state.total_candidate_chars + len(clean_text) > self.limits.max_total_chars:
            self.state.warnings.append("total candidate character limit reached; skipped new candidate")
            return "", "skipped_limit"

        seed = "|".join([norm_source, norm_url, title, source_type, content_hash, str(len(self.state.candidates))])
        cid = f"c_{sha1_text(seed, 10)}"
        candidate = Candidate(
            id=cid,
            source_type=source_type,
            url=norm_url,
            source_uri=source_uri or norm_url or url,
            normalized_source=norm_source,
            title=compact_ws(title),
            text=clean_text,
            text_ref=text_ref,
            fetch_status=status_tag,
            fetched_at=fetched_at or (utc_now() if status_tag in {"ok", "metadata_only", "truncated", "local_only"} else ""),
            trust_notes=compact_ws(trust_notes),
            metadata=metadata or {},
            content_hash=content_hash,
        )
        self.state.candidates[cid] = candidate
        self.state.total_candidate_chars += len(clean_text)
        if norm_url:
            self._url_index[norm_url] = cid
        if norm_source:
            self._source_index[norm_source] = cid
        if content_hash:
            self._content_index[content_hash] = cid
        return cid, "added"

    def add_web_result(
        self,
        *,
        url: str,
        title: str = "",
        text: str = "",
        source_type: str = "web_extract",
        fetch_status: str = "ok",
        metadata: dict[str, Any] | None = None,
        text_ref: str = "",
        trust_notes: str = "",
    ) -> tuple[str, str]:
        """Add a Firecrawl/web_extract/page-fetch style result."""
        metadata = {**(metadata or {}), "adapter": "web_result"}
        return self.add_candidate(
            source_type=source_type,
            url=url,
            title=title or url,
            text=text,
            text_ref=text_ref,
            fetch_status=fetch_status,
            trust_notes=trust_notes,
            metadata=metadata,
        )

    def add_x_status(self, payload: dict[str, Any]) -> list[tuple[str, str]]:
        """Add an authenticated X/Twitter status payload and linked URL stubs.

        Linked URLs are deliberately `not_fetched` unless the caller provides body text.
        """
        status_id = str(payload.get("id") or payload.get("status_id") or payload.get("tweet_id") or "").strip()
        url = str(payload.get("url") or payload.get("tweet_url") or (f"https://x.com/i/status/{status_id}" if status_id else ""))
        author = str(payload.get("author") or payload.get("username") or payload.get("screen_name") or "").strip()
        title = str(payload.get("title") or f"X status {status_id or url}" or "X status")
        text = str(payload.get("text") or payload.get("full_text") or payload.get("body") or payload.get("summary") or "")
        results: list[tuple[str, str]] = []
        cid, status = self.add_candidate(
            source_type="x_status",
            url=url,
            title=title,
            text=text,
            fetch_status="ok" if text else "metadata_only",
            metadata={**payload, "adapter": "x_status", "author": author},
        )
        results.append((cid, status))

        raw_urls = payload.get("urls") or payload.get("linked_urls") or payload.get("links") or []
        if isinstance(raw_urls, str):
            raw_urls = [raw_urls]
        for idx, item in enumerate(raw_urls[:20], 1):
            if isinstance(item, dict):
                link_url = str(item.get("expanded_url") or item.get("url") or item.get("href") or "")
                link_title = str(item.get("title") or f"Linked URL {idx}")
                link_text = str(item.get("text") or item.get("body") or item.get("content") or "")
                link_status = str(item.get("fetch_status") or ("ok" if link_text else "not_fetched"))
                link_meta = item
            else:
                link_url = str(item)
                link_title = f"Linked URL {idx} from X status"
                link_text = ""
                link_status = "not_fetched"
                link_meta = {"raw": item}
            if not link_url:
                continue
            stub_text = link_text or f"Linked URL captured from X status but body not fetched: {link_url}"
            lc_id, lc_status = self.add_candidate(
                source_type="linked_url_stub" if not link_text else "linked_url_fetch",
                url=link_url,
                title=link_title,
                text=stub_text,
                fetch_status=link_status,
                metadata={"adapter": "x_status", "parent_candidate_id": cid, **link_meta},
            )
            results.append((lc_id, lc_status))
        self.add_search_record("x_status_adapter", url or status_id, 1 + len(raw_urls), sum(1 for _, s in results if s == "added"))
        return results

    def add_local_corpus_hit(
        self,
        *,
        path: str,
        title: str = "",
        text: str = "",
        metadata: dict[str, Any] | None = None,
        text_ref: str = "",
    ) -> tuple[str, str]:
        return self.add_candidate(
            source_type="local_corpus",
            source_uri=path,
            title=title or Path(path).name,
            text=text,
            text_ref=text_ref,
            fetch_status="local_only",
            metadata={**(metadata or {}), "adapter": "local_corpus", "path": path},
        )

    def add_transcript_hit(
        self,
        *,
        chat_id: str,
        message_id: str,
        text: str,
        timestamp: str = "",
        sender: str = "",
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        source_uri = f"telegram:{chat_id}:{message_id}"
        return self.add_candidate(
            source_type="transcript",
            source_uri=source_uri,
            title=title or f"Transcript {chat_id} message {message_id}",
            text=text,
            fetch_status="local_only",
            metadata={**(metadata or {}), "adapter": "transcript", "chat_id": chat_id, "message_id": message_id, "timestamp": timestamp, "sender": sender},
        )

    def fetch_url(self, url: str, *, title: str = "", timeout: int = 20) -> tuple[str, str]:
        """Simple stdlib URL fetch. Production adapters can replace this."""
        req = urllib.request.Request(url, headers={"User-Agent": "HermesResearchHarness/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(self.limits.max_candidate_chars + 1)
                content_type = resp.headers.get("content-type", "")
            text = raw.decode("utf-8", errors="replace")
            status_tag = "truncated" if len(raw) > self.limits.max_candidate_chars else "ok"
            cid, status = self.add_candidate(
                source_type="url_fetch",
                url=url,
                title=title or url,
                text=text,
                metadata={"content_type": content_type, "adapter": "fetch_url"},
                fetch_status=status_tag,
            )
            self.add_search_record("fetch_url", url, 1, 1 if status == "added" else 0)
            return cid, status
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.state.warnings.append(f"fetch failed for {url}: {exc}")
            self.add_search_record("fetch_url", url, 0, 0)
            return "", "fetch_failed"

    # ── Curation and verification ───────────────────────────────────────────
    def curate(
        self,
        *,
        add_ids: Iterable[str] = (),
        remove_ids: Iterable[str] = (),
        importance: dict[str, str] | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        importance = importance or {}
        added: list[str] = []
        removed: list[str] = []
        evicted: list[str] = []
        dropped: list[str] = []
        invalid: list[str] = []

        for rid in remove_ids:
            rid = str(rid).strip()
            if rid in self.state.curated:
                del self.state.curated[rid]
                self.state.rejected[rid] = rationale or "removed from curated set"
                removed.append(rid)

        for cid in add_ids:
            cid = str(cid).strip()
            if not cid:
                continue
            if cid not in self.state.candidates:
                invalid.append(cid)
                continue
            tag = (importance.get(cid) or "fair").strip().lower()
            if tag not in IMPORTANCE_RANK:
                tag = "fair"
            if cid in self.state.curated:
                self.state.curated[cid].importance = tag  # retag existing
                if rationale:
                    self.state.curated[cid].rationale = rationale
                continue
            if len(self.state.curated) >= self.limits.max_curated:
                worst_id = None
                worst_rank = -1
                for existing_id, item in self.state.curated.items():
                    rank = IMPORTANCE_RANK.get(item.importance, 2)
                    if rank > worst_rank:
                        worst_id = existing_id
                        worst_rank = rank
                incoming_rank = IMPORTANCE_RANK[tag]
                if worst_id is not None and worst_rank > incoming_rank:
                    del self.state.curated[worst_id]
                    evicted.append(worst_id)
                else:
                    dropped.append(cid)
                    continue
            self.state.curated[cid] = CuratedItem(candidate_id=cid, importance=tag, rationale=rationale)
            added.append(cid)

        self.state.turn += 1
        self.state.search_history.append(
            f"T{self.state.turn}: curate(add={len(added)}, remove={len(removed)}, evict={len(evicted)}, invalid={len(invalid)})"
        )
        return {"added": added, "removed": removed, "evicted": evicted, "dropped": dropped, "invalid": invalid}

    def verify(
        self,
        *,
        claim: str,
        candidate_ids: Iterable[str],
        required_terms: Iterable[str] | None = None,
        verifier_type: str = "term",
    ) -> VerificationRecord:
        claim = compact_ws(claim)
        candidate_id_list = [str(x).strip() for x in candidate_ids]
        if required_terms is None:
            # Use the most meaningful claim tokens, capped to avoid impossible checks.
            terms = sorted(token_set(claim), key=lambda x: (-len(x), x))[:8]
        else:
            terms = [compact_ws(t).lower() for t in required_terms if compact_ws(t)]
        status_by: dict[str, str] = {}
        quotes_by: dict[str, str] = {}
        for cid in candidate_id_list:
            candidate = self.state.candidates.get(cid)
            if not candidate:
                status_by[cid] = "missing_candidate"
                quotes_by[cid] = ""
                continue
            text_lower = candidate.text.lower()
            hits = [t for t in terms if t and t.lower() in text_lower]
            if not terms:
                overlap = token_set(claim) & token_set(candidate.text)
                status = "supported" if len(overlap) >= self.limits.min_verify_terms else "unclear"
                hits = list(overlap)[:5]
            elif len(hits) == len(terms):
                status = "supported"
            elif hits:
                status = "unclear"
            else:
                status = "unsupported"
            status_by[cid] = status
            quotes_by[cid] = excerpt_for_terms(candidate.text, hits or terms, 360)
        record = VerificationRecord(
            claim=claim,
            candidate_ids=candidate_id_list,
            status_by_candidate=status_by,
            quotes_by_candidate=quotes_by,
            required_terms=list(terms),
            verifier_type=verifier_type,
        )
        self.state.verification_records.append(record)
        self.state.turn += 1
        self.state.search_history.append(
            f"T{self.state.turn}: verify({claim[:120]}) → {record.supported_count}/{len(record.status_by_candidate)} supported"
        )
        return record

    def note_open_question(self, question: str) -> None:
        q = compact_ws(question)
        if q and q not in self.state.open_questions:
            self.state.open_questions.append(q)

    # ── Context rendering and artifacts ─────────────────────────────────────
    def render_context(self, max_chars: int | None = None) -> str:
        max_chars = max_chars or self.limits.render_default_chars
        lines: list[str] = [
            "== Hermes Research Harness Context ==",
            f"Schema: {self.state.schema_version}",
            f"Query: {self.task.query}",
            f"Lane: {self.task.lane} | Topic: {self.task.topic} | Client lock: {self.task.client_lock or 'none'}",
            f"Objective: {self.task.objective}",
            "",
            f"Curated Evidence ({len(self.state.curated)}/{self.limits.max_curated}):",
        ]
        if not self.state.curated:
            lines.append("  (empty — search/fetch then curate promising candidates)")
        else:
            ordered = sorted(self.state.curated.values(), key=lambda item: (IMPORTANCE_RANK[item.importance], item.added_at))
            for item in ordered:
                cand = self.state.candidates[item.candidate_id]
                lines.append(f"  [*] {item.candidate_id} [{item.importance}] [{cand.fetch_status}] {cand.title or cand.url or cand.source_uri or cand.source_type}")
                if cand.url or cand.source_uri:
                    lines.append(f"      source: {cand.url or cand.source_uri}")
                if item.rationale:
                    lines.append(f"      rationale: {item.rationale[:240]}")
                # Curated evidence deserves enough body text for close reading;
                # keep uncurated candidates compact below.
                lines.append(f"      snippet: {cand.snippet(900)}")
        lines.append("")
        uncurated = [c for cid, c in self.state.candidates.items() if cid not in self.state.curated]
        lines.append(f"Candidate Pool: {len(self.state.candidates)} total, {len(uncurated)} uncurated")
        for cand in reversed(uncurated[-25:]):
            lines.append(f"  [ ] {cand.id} [{cand.fetch_status}] {cand.title or cand.url or cand.source_uri or cand.source_type}: {cand.snippet(220)}")
        hidden = len(uncurated) - min(len(uncurated), 25)
        if hidden > 0:
            lines.append(f"  … {hidden} older uncurated candidates hidden")
        lines.append("")
        lines.append("Verification Records:")
        if not self.state.verification_records:
            lines.append("  (none yet — verify concrete claims before final report)")
        else:
            for rec in self.state.verification_records[-8:]:
                status_bits = ", ".join(f"{cid}:{status}" for cid, status in rec.status_by_candidate.items())
                lines.append(f"  - [{rec.verifier_type}] {rec.claim[:180]} → {status_bits}")
        lines.append("")
        lines.append("Search / Action History:")
        for row in self.state.search_history[-16:] or ["  (none yet)"]:
            lines.append(f"  {row}")
        if self.state.open_questions:
            lines.append("")
            lines.append("Open Questions:")
            for q in self.state.open_questions[-10:]:
                lines.append(f"  - {q}")
        if self.state.warnings:
            lines.append("")
            lines.append("Harness Warnings:")
            for w in self.state.warnings[-10:]:
                lines.append(f"  - {w}")
        rendered = "\n".join(lines)
        if len(rendered) <= max_chars:
            return rendered
        marker = f"\n…[HARNESS_CONTEXT_TRUNCATED {len(rendered)} chars to {max_chars}]"
        available = max_chars - len(marker)
        if available <= 0:
            return marker[-max_chars:]
        head = rendered[:available]
        return head.rstrip() + marker

    def metrics(self) -> dict[str, Any]:
        verified_supported = sum(r.supported_count for r in self.state.verification_records)
        fetch_status_counts: dict[str, int] = {}
        for candidate in self.state.candidates.values():
            fetch_status_counts[candidate.fetch_status] = fetch_status_counts.get(candidate.fetch_status, 0) + 1
        return {
            "schema_version": self.state.schema_version,
            "candidate_count": len(self.state.candidates),
            "curated_count": len(self.state.curated),
            "verification_record_count": len(self.state.verification_records),
            "verified_supported_count": verified_supported,
            "duplicate_event_count": len(self.state.duplicate_events),
            "warning_count": len(self.state.warnings),
            "open_question_count": len(self.state.open_questions),
            "total_candidate_chars": self.state.total_candidate_chars,
            "fetch_status_counts": fetch_status_counts,
        }

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self.state)

    def write_state(self, state_path: str | Path) -> Path:
        path = Path(state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResearchHarness":
        task = ResearchTask(**payload["task"])
        harness = cls(task)
        harness.state = ResearchState(task=task)
        harness.state.schema_version = str(payload.get("schema_version") or LEGACY_SCHEMA_VERSION)
        for cid, row in (payload.get("candidates") or {}).items():
            row = dict(row)
            row.setdefault("source_uri", row.get("url", ""))
            row.setdefault("normalized_source", normalize_source_uri(row.get("source_uri") or row.get("url") or ""))
            row.setdefault("text_ref", "")
            row.setdefault("fetch_status", (row.get("metadata") or {}).get("fetch_status") or "unknown")
            row.setdefault("fetched_at", "")
            row.setdefault("trust_notes", "")
            cand = Candidate(**row)
            harness.state.candidates[cid] = cand
            if cand.url:
                harness._url_index[cand.url] = cid
            if cand.normalized_source:
                harness._source_index[cand.normalized_source] = cid
            if cand.content_hash:
                harness._content_index[cand.content_hash] = cid
            harness.state.total_candidate_chars += len(cand.text or "")
        for cid, row in (payload.get("curated") or {}).items():
            harness.state.curated[cid] = CuratedItem(**row)
        harness.state.rejected = dict(payload.get("rejected") or {})
        harness.state.search_history = list(payload.get("search_history") or [])
        harness.state.open_questions = list(payload.get("open_questions") or [])
        harness.state.duplicate_events = list(payload.get("duplicate_events") or [])
        harness.state.warnings = list(payload.get("warnings") or [])
        harness.state.turn = int(payload.get("turn") or 0)
        harness.state.verification_records = [VerificationRecord(**r) for r in payload.get("verification_records") or []]
        return harness

    @classmethod
    def load_state(cls, state_path: str | Path) -> "ResearchHarness":
        return cls.from_dict(json.loads(Path(state_path).expanduser().read_text(encoding="utf-8")))

    def write_artifacts(self, output_dir: str | Path) -> dict[str, str]:
        output = Path(output_dir).expanduser()
        output.mkdir(parents=True, exist_ok=True)
        state_path = output / "state.json"
        evidence_path = output / "evidence.md"
        report_path = output / "report.md"
        manifest_path = output / "manifest.json"

        self.write_state(state_path)
        evidence_path.write_text(self._render_evidence_md(), encoding="utf-8")
        report_path.write_text(self._render_report_md(), encoding="utf-8")
        manifest = {
            "schema": SCHEMA_VERSION,
            "generated_at": utc_now(),
            "query": self.task.query,
            "lane": self.task.lane,
            "topic": self.task.topic,
            "client_lock": self.task.client_lock,
            "metrics": self.metrics(),
            "artifacts": {
                "state": str(state_path),
                "evidence": str(evidence_path),
                "report": str(report_path),
                "manifest": str(manifest_path),
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return manifest["artifacts"]

    def _render_evidence_md(self) -> str:
        lines = [f"# Evidence — {self.task.query}", "", f"Generated: {utc_now()}", "", "## Curated Evidence", ""]
        if not self.state.curated:
            lines.append("No curated evidence.")
        for item in sorted(self.state.curated.values(), key=lambda i: (IMPORTANCE_RANK[i.importance], i.added_at)):
            cand = self.state.candidates[item.candidate_id]
            lines.extend([
                f"### {item.candidate_id} — {cand.title or cand.url or cand.source_uri or cand.source_type}",
                f"- Importance: `{item.importance}`",
                f"- Source: {cand.url or cand.source_uri or cand.source_type}",
                f"- Fetch status: `{cand.fetch_status}`",
                f"- Text ref: {cand.text_ref or '(inline)'}",
                f"- Rationale: {item.rationale or '(none recorded)'}",
                f"- Excerpt: {cand.snippet(900)}",
                "",
            ])
        lines.extend(["## Verification Records", ""])
        if not self.state.verification_records:
            lines.append("No verification records.")
        for rec in self.state.verification_records:
            lines.append(f"### Claim: {rec.claim}")
            lines.append(f"- Verifier: `{rec.verifier_type}`")
            if rec.required_terms:
                lines.append(f"- Required terms: {', '.join(rec.required_terms)}")
            for cid, status in rec.status_by_candidate.items():
                quote = rec.quotes_by_candidate.get(cid, "")
                lines.append(f"- `{cid}`: **{status}** — {quote}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_report_md(self) -> str:
        metrics = self.metrics()
        top = sorted(self.state.curated.values(), key=lambda i: (IMPORTANCE_RANK[i.importance], i.added_at))[:8]
        lines = [
            f"# Research Harness Report — {self.task.query}",
            "",
            f"Generated: {utc_now()}",
            "",
            "## Bottom Line",
            f"- Candidates: {metrics['candidate_count']}; curated: {metrics['curated_count']}; verification records: {metrics['verification_record_count']}; supported checks: {metrics['verified_supported_count']}.",
            f"- Fetch statuses: {json.dumps(metrics['fetch_status_counts'], sort_keys=True)}",
        ]
        if metrics["curated_count"] == 0:
            lines.append("- No evidence was curated; this run is not ready for fleet use.")
        elif metrics["verified_supported_count"] == 0:
            lines.append("- Evidence was curated, but no claim has been fully verified yet.")
        else:
            lines.append("- The harness produced source-backed curated evidence with explicit verification records.")
        lines.extend(["", "## Top Curated Evidence"])
        if not top:
            lines.append("- None.")
        for item in top:
            cand = self.state.candidates[item.candidate_id]
            lines.append(f"- `{item.candidate_id}` [{item.importance}] [{cand.fetch_status}] {cand.title or cand.url or cand.source_uri or cand.source_type}: {cand.snippet(240)}")
        if self.state.open_questions:
            lines.extend(["", "## Open Questions"])
            lines.extend(f"- {q}" for q in self.state.open_questions)
        if self.state.warnings:
            lines.extend(["", "## Warnings"])
            lines.extend(f"- {w}" for w in self.state.warnings)
        return "\n".join(lines).rstrip() + "\n"


def load_seed_json(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("items") or payload.get("candidates") or []
    if not isinstance(payload, list):
        raise SystemExit(f"seed JSON must be a list or contain items/candidates: {path}")
    return [row for row in payload if isinstance(row, dict)]


def run_scripted_agent(harness: ResearchHarness, seeds: list[dict[str, Any]], *, verify_claim: str = "") -> None:
    """Tiny deterministic test policy for smoke/integration tests.

    Real subagents can be swapped in later; this proves the harness mechanics and
    artifacts without burning model calls.
    """
    added_ids: list[str] = []
    for row in seeds:
        cid, status = harness.add_candidate(
            source_type=str(row.get("source_type") or "seed"),
            url=str(row.get("url") or ""),
            source_uri=str(row.get("source_uri") or ""),
            title=str(row.get("title") or ""),
            text=str(row.get("text") or row.get("body") or row.get("content") or ""),
            text_ref=str(row.get("text_ref") or ""),
            fetch_status=str(row.get("fetch_status") or "ok"),
            trust_notes=str(row.get("trust_notes") or ""),
            metadata={k: v for k, v in row.items() if k not in {"url", "source_uri", "title", "text", "body", "content", "text_ref", "fetch_status", "trust_notes"}},
        )
        if status == "added" and cid:
            added_ids.append(cid)
    harness.add_search_record("seed_json", f"{len(seeds)} seeds", len(seeds), len(added_ids))

    query_terms = token_set(harness.task.query)
    scored: list[tuple[int, str]] = []
    for cid, cand in harness.state.candidates.items():
        overlap = query_terms & token_set(cand.title + " " + cand.text)
        scored.append((len(overlap), cid))
    scored.sort(reverse=True)
    to_curate = [cid for score, cid in scored if score > 0][: harness.limits.max_curated]
    importance = {cid: ("high" if score >= 3 else "fair") for score, cid in scored if cid in to_curate}
    harness.curate(add_ids=to_curate, importance=importance, rationale="scripted policy: query-term overlap")
    if verify_claim and to_curate:
        harness.verify(claim=verify_claim, candidate_ids=to_curate[:5])
    elif to_curate:
        harness.verify(claim=harness.task.query, candidate_ids=to_curate[:5], required_terms=sorted(query_terms)[:5])
    if not to_curate:
        harness.note_open_question("No seeded candidates overlapped with the query; run a real search/fetch adapter.")


def parse_importance(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise SystemExit(f"importance must be candidate_id=tag: {value}")
        cid, tag = value.split("=", 1)
        out[cid.strip()] = tag.strip()
    return out


def save_loaded_state(harness: ResearchHarness, state_path: str) -> None:
    harness.write_state(state_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes state-externalizing research harness prototype")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="Create an empty harness state")
    init.add_argument("--query", required=True)
    init.add_argument("--lane", default="general")
    init.add_argument("--topic", default="general")
    init.add_argument("--client-lock", default="")
    init.add_argument("--objective", default="Find, curate, and verify evidence before producing a report.")
    init.add_argument("--state", required=True)
    init.add_argument("--max-candidates", type=int, default=80)
    init.add_argument("--max-curated", type=int, default=30)

    load = sub.add_parser("load", help="Load state and print metrics")
    load.add_argument("--state", required=True)

    resume = sub.add_parser("resume", help="Alias for load; validates state can be resumed")
    resume.add_argument("--state", required=True)

    render = sub.add_parser("render", help="Render compact harness context")
    render.add_argument("--state", required=True)
    render.add_argument("--max-chars", type=int, default=12000)

    add = sub.add_parser("add-candidate", help="Add one candidate to an existing state")
    add.add_argument("--state", required=True)
    add.add_argument("--source-type", required=True)
    add.add_argument("--url", default="")
    add.add_argument("--source-uri", default="")
    add.add_argument("--title", default="")
    add.add_argument("--text", default="")
    add.add_argument("--text-file", default="")
    add.add_argument("--text-ref", default="")
    add.add_argument("--fetch-status", default="unknown", choices=VALID_FETCH_STATUS)
    add.add_argument("--trust-notes", default="")
    add.add_argument("--metadata-json", default="")

    curate = sub.add_parser("curate", help="Curate candidates in an existing state")
    curate.add_argument("--state", required=True)
    curate.add_argument("--add", action="append", default=[])
    curate.add_argument("--remove", action="append", default=[])
    curate.add_argument("--importance", action="append", default=[], help="candidate_id=very_high|high|fair|low")
    curate.add_argument("--rationale", default="")

    verify = sub.add_parser("verify", help="Verify a claim against candidates")
    verify.add_argument("--state", required=True)
    verify.add_argument("--claim", required=True)
    verify.add_argument("--candidate", action="append", default=[])
    verify.add_argument("--required-term", action="append", default=[])
    verify.add_argument("--verifier-type", default="term")

    write = sub.add_parser("write-artifacts", help="Write state/evidence/report/manifest artifacts from a state file")
    write.add_argument("--state", required=True)
    write.add_argument("--output-dir", required=True)

    run = sub.add_parser("run", help="Run deterministic harness smoke policy over seed docs/URLs")
    run.add_argument("--query", required=True)
    run.add_argument("--lane", default="general")
    run.add_argument("--topic", default="general")
    run.add_argument("--client-lock", default="")
    run.add_argument("--seed-json", action="append", default=[])
    run.add_argument("--fetch-url", action="append", default=[])
    run.add_argument("--verify-claim", default="")
    run.add_argument("--output-dir", required=True)
    run.add_argument("--max-candidates", type=int, default=80)
    run.add_argument("--max-curated", type=int, default=30)

    args = parser.parse_args(argv)

    if args.cmd == "init":
        limits = HarnessLimits(max_candidates=args.max_candidates, max_curated=args.max_curated)
        harness = ResearchHarness(ResearchTask(args.query, lane=args.lane, topic=args.topic, client_lock=args.client_lock, objective=args.objective), limits=limits)
        harness.write_state(args.state)
        print(json.dumps({"ok": True, "state": str(Path(args.state).expanduser()), "metrics": harness.metrics()}, indent=2))
        return 0

    if args.cmd in {"load", "resume"}:
        harness = ResearchHarness.load_state(args.state)
        print(json.dumps({"ok": True, "state": str(Path(args.state).expanduser()), "metrics": harness.metrics()}, indent=2))
        return 0

    if args.cmd == "render":
        harness = ResearchHarness.load_state(args.state)
        print(harness.render_context(max_chars=args.max_chars))
        return 0

    if args.cmd == "add-candidate":
        harness = ResearchHarness.load_state(args.state)
        text = args.text
        if args.text_file:
            text = Path(args.text_file).expanduser().read_text(encoding="utf-8")
        cid, status = harness.add_candidate(
            source_type=args.source_type,
            url=args.url,
            source_uri=args.source_uri,
            title=args.title,
            text=text,
            text_ref=args.text_ref,
            fetch_status=args.fetch_status,
            trust_notes=args.trust_notes,
            metadata=load_jsonish(args.metadata_json),
        )
        save_loaded_state(harness, args.state)
        print(json.dumps({"ok": True, "candidate_id": cid, "status": status, "metrics": harness.metrics()}, indent=2))
        return 0

    if args.cmd == "curate":
        harness = ResearchHarness.load_state(args.state)
        result = harness.curate(add_ids=args.add, remove_ids=args.remove, importance=parse_importance(args.importance), rationale=args.rationale)
        save_loaded_state(harness, args.state)
        print(json.dumps({"ok": True, "result": result, "metrics": harness.metrics()}, indent=2))
        return 0

    if args.cmd == "verify":
        harness = ResearchHarness.load_state(args.state)
        record = harness.verify(claim=args.claim, candidate_ids=args.candidate, required_terms=args.required_term or None, verifier_type=args.verifier_type)
        save_loaded_state(harness, args.state)
        print(json.dumps({"ok": True, "record": dataclasses.asdict(record), "metrics": harness.metrics()}, indent=2))
        return 0

    if args.cmd == "write-artifacts":
        harness = ResearchHarness.load_state(args.state)
        artifacts = harness.write_artifacts(args.output_dir)
        print(json.dumps({"ok": True, "metrics": harness.metrics(), "artifacts": artifacts}, indent=2))
        return 0

    if args.cmd == "run":
        limits = HarnessLimits(max_candidates=args.max_candidates, max_curated=args.max_curated)
        harness = ResearchHarness(ResearchTask(args.query, lane=args.lane, topic=args.topic, client_lock=args.client_lock), limits=limits)
        seeds: list[dict[str, Any]] = []
        for seed_path in args.seed_json:
            seeds.extend(load_seed_json(Path(seed_path).expanduser()))
        for url in args.fetch_url:
            harness.fetch_url(url)
        run_scripted_agent(harness, seeds, verify_claim=args.verify_claim)
        artifacts = harness.write_artifacts(args.output_dir)
        print(json.dumps({"ok": True, "metrics": harness.metrics(), "artifacts": artifacts}, indent=2))
        return 0
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
