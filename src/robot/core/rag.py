"""Knowledge: chunk, embed, store, retrieve.

TECH_STACK.md § Ingest. Two decisions that shape everything here:

**Qwen3-Embedding-0.6B, not all-MiniLM.** MiniLM is English-centric and would silently
degrade Arabic and Indonesian. The point of this store is that a document ingested in
one of our three languages is findable by a question asked in another.

**sqlite-vec, not a vector server.** One embedded file, no daemon, no telemetry, no
network. Memory-budget.md gives it ~50 MB and it grows with the corpus.

**Ingestion never runs on the conversational hot path.** Embedding a document takes
seconds to minutes; a turn has ~2 s in total. `Ingestor` owns a queue and a worker so a
`save_memory()` call or a dropped-in file returns immediately.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import struct
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

log = logging.getLogger("robot-rag")

# Qwen3-Embedding-0.6B output width. Checked against the server at startup rather than
# trusted -- a dimension mismatch would silently poison every search.
EMBED_DIM = 1024

# ~300-500 tokens with overlap (TECH_STACK.md § Ingest). Measured in characters because
# we do not have the tokeniser here; ~4 chars/token for Latin, fewer for Arabic.
# Sized to fit the embedding server's 512-token context with room to spare. Arabic
# uses noticeably more tokens per character than Latin script, so the margin matters.
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200


# A heading: "## Something", "3. Something", "3) Something". Requires a real word after
# it, so "1.063 orang" and "13 hektare pada tahun 2025." are not mistaken for headings --
# the numbers in a document about a school are mostly not section markers.
_HEADING = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+\S|\d{1,3}[.)][ \t]+(?=[^\d\s])\S)", re.M)

# How many headings before a document counts as structured. Two could be a coincidence
# in ordinary prose ("1. " at the start of a list inside a paragraph); three is a shape.
_MIN_HEADINGS = 3


def split_sections(text: str) -> list[str]:
    """Split a structured document at its headings, or return it whole.

    A document written as numbered facts should be retrieved as numbered facts. Cutting
    it every N characters instead puts five unrelated facts in one chunk: the embedding
    is then an average of five topics and matches none of them well, and every hit drags
    four irrelevant facts into a prompt that has a hard budget.

    Measured on the Tazakka document (2922 chars, 15 numbered sections), 18 questions,
    top-4 retrieval:

        size-based   4 chunks   18/18 found   1106 chars of context   51 of 72
                                                                      retrieved chunks
                                                                      dropped by the
                                                                      prompt budget
        headings    15 chunks   18/18 found    733 chars of context    0 dropped

    Both find the facts -- this document is small enough that four coarse chunks contain
    all of it, so retrieval cannot really fail. The difference is what reaches the model:
    a third less context for the same answer, and nothing thrown away by
    `prompt.trim_context`. That last column is where accuracy goes on a corpus big enough
    for the top-4 to matter, because the budget drops chunks from the end and a coarse
    chunk carries four other facts with it when it goes.

    Anything without at least `_MIN_HEADINGS` headings is left alone -- prose is not
    improved by being cut at the one place someone happened to write "1.".
    """
    starts = [m.start() for m in _HEADING.finditer(text)]
    if len(starts) < _MIN_HEADINGS:
        return [text]
    # Everything before the first heading is the document's own preamble -- a title, a
    # byline. Keep it as its own section rather than gluing it to section 1, which would
    # make section 1 the only chunk that mentions what the document IS.
    bounds = ([0] if starts[0] > 0 else []) + starts
    sections = [text[a:b].strip() for a, b in zip(bounds, bounds[1:] + [len(text)])]
    return [s for s in sections if s]


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split into retrievable pieces: headings first, then size.

    Overlap exists so a fact that straddles a boundary is still retrievable whole.
    """
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if not text:
        return []

    sections = split_sections(text)
    if len(sections) > 1:
        # Each section on its own terms: most are already small enough to be one chunk,
        # and the few that are not fall through to the size rule below.
        out: list[str] = []
        for section in sections:
            out.extend(chunk_text(section, size, overlap) if len(section) > size
                       else [section])
        return out

    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer a paragraph break, then a sentence end, then any whitespace.
            for sep in ("\n\n", ". ", "؟ ", "! ", "\n", " "):
                cut = text.rfind(sep, start + size // 2, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


# poppler's `pdftotext`, called as a subprocess. Deliberately NOT a Python PDF library:
# the runtime path stays lean (AGENTS.md), this is a ~100 KB C binary that is already on
# the board as part of poppler-utils, and it only ever runs during ingestion -- which is
# a background job on its own queue, never the conversational hot path.
PDFTOTEXT = "pdftotext"


def read_pdf(path: Path) -> str:
    """Extract a PDF's text, or say clearly why it could not be.

    A scanned PDF is images and yields nothing; that is a real and common case, and it
    must not look like an empty document that ingested fine. There is no OCR here and
    adding one would be a second ML runtime for a job the robot does not have.
    """
    import shutil
    import subprocess

    if shutil.which(PDFTOTEXT) is None:
        raise RuntimeError(
            "reading PDFs needs `pdftotext` (sudo apt install poppler-utils)")
    result = subprocess.run(
        # `-layout` preserves columns and tables, which matters for anything with a
        # figure in it and costs nothing for plain prose.
        [PDFTOTEXT, "-layout", "-enc", "UTF-8", str(path), "-"],
        capture_output=True, timeout=120)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"pdftotext failed: {detail[-1] if detail else 'no output'}")
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError(
            f"{path.name} has no extractable text -- a scanned PDF is images, and "
            f"there is no OCR on this robot")
    return text


class Embedder:
    """Client for a llama.cpp server started with --embedding.

    A second llama-server process, which looks like it collides with AGENTS.md
    constraint 2 ("one LLM resident at a time"). It does not: memory-budget.md lists
    "Embeddings -- Qwen3-Embedding-0.6B" as its own line inside the same steady-state
    total as the LLM, so the budget already assumes both are resident. The constraint
    is about not holding two *generative* models; this is a 0.6B encoder.
    """

    def __init__(self, url: str = "http://127.0.0.1:8082", timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.dim: int | None = None

    def _post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.url}{path}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/v1/embeddings", {"input": texts})
        rows = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [row["embedding"] for row in rows]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = await asyncio.to_thread(self._embed_sync, texts)
        if vectors and self.dim is None:
            self.dim = len(vectors[0])
            if self.dim != EMBED_DIM:
                log.warning("embedding dim is %d, expected %d -- update EMBED_DIM "
                            "and rebuild the store", self.dim, EMBED_DIM)
        return vectors

    async def healthy(self, quiet: bool = False) -> bool:
        """Probe the embedding server.

        `quiet` suppresses the log line: the health poller calls this every 3 seconds,
        and an unreachable server would otherwise write ~28,000 identical warnings over
        a 24-hour soak. The caller logs state *changes* instead.
        """
        try:
            await self.embed(["ping"])
            self._unreachable = False
            return True
        except Exception as exc:
            if not quiet and not getattr(self, "_unreachable", False):
                log.warning("embedding server unreachable: %s", exc)
            self._unreachable = True
            return False


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


@dataclass
class Hit:
    text: str
    source: str
    distance: float


class Store:
    """sqlite-vec knowledge base. One file, no server."""

    def __init__(self, path: str | Path = "run/knowledge.db", dim: int = EMBED_DIM) -> None:
        self.path = Path(path)
        self.dim = dim
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.enable_load_extension(True)
        sqlite_vec.load(self._db)
        self._db.enable_load_extension(False)
        self._init_schema()

    def _init_schema(self) -> None:
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id      INTEGER PRIMARY KEY,
                text    TEXT NOT NULL,
                source  TEXT NOT NULL,
                lang    TEXT,
                added_at REAL NOT NULL
            )""")
        # Content-addressed so re-ingesting the same document is a no-op rather than a
        # duplicate that crowds out other results.
        self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS chunks_unique ON chunks(source, text)")
        self._db.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors USING vec0(
                chunk_id INTEGER PRIMARY KEY,
                embedding FLOAT[{self.dim}]
            )""")
        # A keyword index over the same rows.
        #
        # This is what retrieval falls back to when the embedding server is asleep
        # (embed_server.py). It is strictly worse -- it matches words, so an Indonesian
        # question will not find an English note, which is the whole reason the vector
        # store exists -- but "the word you said appears in this chunk" is a great deal
        # better than answering with nothing, and it costs no model and no memory.
        # A contentless-by-choice FTS5 table: it keeps its own copy of the text rather
        # than referencing `chunks` with content=/content_rowid=. That costs a duplicate
        # of at most CHUNK_CHARS per row and buys a table that can be counted. An
        # external-content index reports the CONTENT table's row count, so "is it
        # populated?" cannot be answered -- which is how the first version of this
        # shipped an index that was empty and silently matched nothing.
        self._db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)")
        self._db.commit()
        self._sync_fts()

    def _sync_fts(self) -> None:
        """Index whatever is in `chunks` but not yet in the keyword index.

        Covers a store written before this index existed, and any row an older build
        inserted without it.
        """
        try:
            if "content='chunks'" in (self._table_sql("chunks_fts") or ""):
                # An index from the first version of this. It cannot be verified, so it
                # is not worth keeping -- rebuilding from `chunks` takes milliseconds.
                self._db.execute("DROP TABLE chunks_fts")
                self._db.execute(
                    "CREATE VIRTUAL TABLE chunks_fts USING fts5(text)")
            missing = self._db.execute(
                """SELECT id, text FROM chunks
                    WHERE id NOT IN (SELECT rowid FROM chunks_fts)""").fetchall()
            if missing:
                self._db.executemany(
                    "INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", missing)
                self._db.commit()
                log.info("keyword index: added %d chunk(s)", len(missing))
        except sqlite3.OperationalError as exc:
            # FTS5 is compiled into every sqlite this project has met, but a build
            # without it must degrade to vector-only rather than refuse to open a store.
            log.warning("no FTS5 keyword index (%s); retrieval will need the embedding "
                        "server to be running", exc)

    def _table_sql(self, name: str) -> str | None:
        row = self._db.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def upsert(self, text: str, vector: list[float], source: str, lang: str | None) -> bool:
        """Returns True if this chunk was new."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO chunks (text, source, lang, added_at) VALUES (?,?,?,?)",
            (text, source, lang, time.time()),
        )
        if cur.rowcount == 0:
            return False
        chunk_id = cur.lastrowid
        self._db.execute(
            "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
            (chunk_id, _pack(vector)),
        )
        self._index_text(chunk_id, text)
        self._db.commit()
        return True

    def _index_text(self, chunk_id: int, text: str) -> None:
        try:
            self._db.execute(
                "INSERT INTO chunks_fts (rowid, text) VALUES (?, ?)", (chunk_id, text))
        except sqlite3.OperationalError:
            pass          # no FTS5 in this build; _sync_fts already said so

    def search(self, vector: list[float], k: int = 4) -> list[Hit]:
        rows = self._db.execute(
            """SELECT c.text, c.source, v.distance
                 FROM chunk_vectors v
                 JOIN chunks c ON c.id = v.chunk_id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance""",
            (_pack(vector), k),
        ).fetchall()
        return [Hit(text=t, source=s, distance=d) for t, s, d in rows]

    def search_text(self, query: str, k: int = 4) -> list[Hit]:
        """Keyword search, for when there is no embedding server to ask.

        The query is rebuilt from its word characters rather than passed through: FTS5
        has its own query syntax, and a user saying `what is the robot's name?` would
        otherwise be a syntax error rather than a search. Tokens are OR-ed and quoted,
        so this matches "any of these words" and ranks by bm25.
        """
        tokens = [t for t in re.findall(r"\w+", query, flags=re.UNICODE) if len(t) > 1]
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        try:
            rows = self._db.execute(
                """SELECT c.text, c.source, bm25(chunks_fts)
                     FROM chunks_fts f
                     JOIN chunks c ON c.id = f.rowid
                    WHERE chunks_fts MATCH ?
                    ORDER BY bm25(chunks_fts)
                    LIMIT ?""",
                (match, k),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            log.warning("keyword search unavailable: %s", exc)
            return []
        # bm25 is negative and lower is better, which is the same direction as the
        # cosine distance the vector path returns. Callers can treat them alike for
        # ordering; they are NOT comparable in magnitude and nothing tries to.
        return [Hit(text=text, source=source, distance=score) for text, source, score in rows]

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def sources(self) -> list[tuple[str, int]]:
        return self._db.execute(
            "SELECT source, COUNT(*) FROM chunks GROUP BY source ORDER BY 2 DESC"
        ).fetchall()

    def forget(self, source: str) -> int:
        ids = [r[0] for r in
               self._db.execute("SELECT id FROM chunks WHERE source = ?", (source,))]
        for chunk_id in ids:
            self._db.execute("DELETE FROM chunk_vectors WHERE chunk_id = ?", (chunk_id,))
            try:
                self._db.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
            except sqlite3.OperationalError:
                pass
        self._db.execute("DELETE FROM chunks WHERE source = ?", (source,))
        self._db.commit()
        return len(ids)

    def close(self) -> None:
        self._db.close()


class Ingestor:
    """Background chunk → embed → upsert. Never blocks a turn."""

    def __init__(self, store: Store, embedder: Embedder, on_done=None, gate=None) -> None:
        self.store = store
        self.embedder = embedder
        # `robot.core.embed_server.EmbedServer`, when the embedding server is demand
        # started. This is the ONLY thing that wakes it: "knowledge is queued" is
        # exactly this queue being non-empty. None means it is always resident.
        self.gate = gate
        # Called with (source, chunks_added, error). Ingestion is a background job, so
        # without this a failure -- an embedding server that is down, say -- is visible
        # only in the log, while the dashboard still says "ingesting".
        self.on_done = on_done
        self._queue: asyncio.Queue[tuple[str, str, str | None]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.ingested = 0
        self.failed = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def add_text(self, text: str, source: str, lang: str | None = None) -> None:
        """Queue text for ingestion and return immediately."""
        await self._queue.put((text, source, lang))

    async def add_file(self, path: str | Path) -> None:
        path = Path(path)
        text = (await asyncio.to_thread(read_pdf, path) if path.suffix.lower() == ".pdf"
                else path.read_text(errors="replace"))
        await self.add_text(text, source=path.name)

    async def _worker(self) -> None:
        held = False
        while True:
            text, source, lang = await self._queue.get()
            try:
                chunks = chunk_text(text)
                if not chunks:
                    continue
                # Acquire once and keep it for as long as the queue has work: a folder
                # of documents must not start and stop the server per file.
                if self.gate is not None and not held:
                    held = await self.gate.acquire()
                    if not held:
                        raise RuntimeError(
                            f"the embedding server could not be started "
                            f"({self.gate.detail or 'no reason given'})")
                vectors = await self.embedder.embed(chunks)
                new = sum(
                    self.store.upsert(c, v, source, lang)
                    for c, v in zip(chunks, vectors)
                )
                self.ingested += new
                log.info("ingested %s: %d chunks (%d new), %d total in store",
                         source, len(chunks), new, self.store.count())
                self._report(source, new, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failed += 1
                log.exception("ingestion failed for %s", source)
                self._report(source, 0, exc)
            finally:
                self._queue.task_done()
                # Let it go the moment there is nothing left. The idle timer in
                # EmbedServer decides how long after that it actually stops.
                if held and self._queue.empty():
                    self.gate.release()
                    held = False

    def _report(self, source: str, chunks: int, error: Exception | None) -> None:
        if self.on_done is None:
            return
        try:
            self.on_done(source, chunks, error)
        except Exception:
            log.exception("ingestion callback failed")

    @property
    def pending(self) -> int:
        return self._queue.qsize()


class Retriever:
    """Query side: embed, search, return text -- or keywords when there is no embedder.

    A vector search needs the QUERY in the same vector space as the stored chunks, and
    only the model that embedded them can put it there. The conversational LLM cannot:
    its vectors, if it produced any, would describe a different space, and sqlite-vec
    only compares numbers someone else computed. So "retrieve without the embedding
    model" is not a thing that can be arranged -- there is no substitute, only a
    different kind of search.

    That is what `policy` chooses between:

      warm   use vectors when the embedding server happens to be up, keywords otherwise.
             Never waits for a model to load, so a turn's latency does not move.
      start  wake the embedding server and wait for it. Retrieval is always at full
             quality; the first turn after an idle period pays the load time.
      off    keywords only. The embedding server is never woken by a question.

    Keyword search cannot do the one thing the vector store was built for -- an
    Indonesian question finding an English note (M7) -- so `warm` and `start` are the
    only settings under which cross-lingual retrieval works at all.
    """

    def __init__(self, store: Store, embedder: Embedder, k: int = 4,
                 max_distance: float | None = None, gate=None,
                 policy: str = "warm") -> None:
        self.store = store
        self.embedder = embedder
        self.k = k
        # Cosine distance above which a hit is noise. None keeps everything, which is
        # the safer default while the corpus is small.
        self.max_distance = max_distance
        self.gate = gate
        self.policy = policy
        # Which path the last query actually took, so the dashboard and the logs can say
        # "keywords" rather than leaving it to be inferred from result quality.
        self.last_mode: str | None = None

    async def _vectors_available(self) -> tuple[bool, bool]:
        """(can we use vectors, did we take a hold that must be released).

        The two answers have to come back together. Working the second one out
        afterwards by re-reading `gate.warm` cannot work -- acquiring is what made it
        warm -- and getting that wrong leaks the hold, so the server never goes back to
        sleep and the whole feature quietly does nothing.
        """
        if self.gate is None:
            return True, False                # always-resident embedding server
        if self.policy == "off":
            return False, False
        if self.gate.warm:
            return True, False
        if self.policy != "start":
            return False, False
        # A failed acquire is not a hold. EmbedServer counts holders, and releasing one
        # we never took would drop somebody else's -- an ingestion job, most likely.
        started = await self.gate.acquire()
        return started, started

    async def __call__(self, query: str) -> list[str]:
        if not query.strip() or self.store.count() == 0:
            return []

        hits: list[Hit] = []
        used_vectors = False
        self.last_mode = None
        available, acquired = await self._vectors_available()
        if available:
            try:
                vectors = await self.embedder.embed([query])
                if vectors:
                    hits = self.store.search(vectors[0], k=self.k)
                    if self.max_distance is not None:
                        hits = [h for h in hits if h.distance <= self.max_distance]
                    used_vectors = True
                    self.last_mode = "vector"
            except Exception as exc:
                # The server was up a moment ago and is not now, or it errored. A turn
                # is not the place to give up on the knowledge store entirely.
                log.warning("vector search failed (%s); falling back to keywords", exc)
                hits = []
            finally:
                if acquired:
                    self.gate.release()

        # An empty VECTOR result is a real answer -- nothing stored is relevant -- so it
        # is not second-guessed with keywords. An empty result because the embedding
        # server was asleep is not an answer at all, and that is what falls through.
        if not used_vectors:
            hits = self.store.search_text(query, k=self.k)
            self.last_mode = "keyword"

        if hits:
            log.info("retrieved %d chunks for %r by %s (best %.3f)",
                     len(hits), query[:48], self.last_mode, hits[0].distance)
        return [h.text for h in hits]
