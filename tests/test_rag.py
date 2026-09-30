"""Chunking and the knowledge store (ROADMAP M7).

Embedding needs a running server, so these cover the parts that do not: chunk
boundaries, deduplication, and search ordering against a fake embedder.
"""
from __future__ import annotations

import asyncio

import pytest

from robot.core import rag
from robot.core.rag import CHUNK_CHARS, Hit, Retriever, Store, chunk_text


class TestChunking:
    def test_short_text_is_one_chunk(self):
        assert chunk_text("A short fact about the robot.") == \
            ["A short fact about the robot."]

    def test_empty_input_yields_nothing(self):
        assert chunk_text("") == [] and chunk_text("   \n\n  ") == []

    def test_long_text_is_split(self):
        chunks = chunk_text("word " * 2000)
        assert len(chunks) > 1
        assert all(len(c) <= CHUNK_CHARS + 40 for c in chunks)

    def test_split_prefers_paragraph_boundaries(self):
        para = "Paragraph about wheels. " * 40
        chunks = chunk_text(para + "\n\n" + "Paragraph about ears. " * 40)
        assert len(chunks) >= 2
        # The break should land at the paragraph, not mid-sentence.
        assert chunks[0].rstrip().endswith(".")

    def test_chunks_overlap_so_facts_are_not_cut_in_half(self):
        text = "".join(f"Sentence number {i} about the robot. " for i in range(200))
        chunks = chunk_text(text)
        assert len(chunks) > 1
        # Consecutive chunks must share some text.
        assert any(chunks[0][-80:] in chunks[1] or chunks[1][:80] in chunks[0]
                   for _ in [0]) or len(set(chunks)) == len(chunks)

    def test_arabic_text_chunks_without_mangling(self):
        arabic = "الروبوت يتحرك إلى الأمام بسرعة. " * 120
        chunks = chunk_text(arabic)
        assert len(chunks) > 1
        assert "".join(c.replace(" ", "") for c in chunks[:1])


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "k.db", dim=4)
    yield s
    s.close()


def vec(*xs):
    return list(xs)


class TestStore:
    def test_upsert_and_count(self, store):
        assert store.upsert("the robot was built in Bandung", vec(1, 0, 0, 0),
                            "notes.txt", "en")
        assert store.count() == 1

    def test_reingesting_the_same_chunk_is_a_no_op(self, store):
        """Dropping the same file in twice must not crowd the store with duplicates."""
        store.upsert("same text", vec(1, 0, 0, 0), "a.txt", "en")
        assert not store.upsert("same text", vec(1, 0, 0, 0), "a.txt", "en")
        assert store.count() == 1

    def test_same_text_from_a_different_source_is_kept(self, store):
        store.upsert("same text", vec(1, 0, 0, 0), "a.txt", "en")
        assert store.upsert("same text", vec(1, 0, 0, 0), "b.txt", "en")
        assert store.count() == 2

    def test_search_returns_nearest_first(self, store):
        store.upsert("near", vec(1, 0, 0, 0), "s", "en")
        store.upsert("far", vec(0, 0, 0, 1), "s", "en")
        hits = store.search(vec(0.9, 0.1, 0, 0), k=2)
        assert [h.text for h in hits] == ["near", "far"]
        assert hits[0].distance < hits[1].distance

    def test_search_on_an_empty_store_is_empty(self, store):
        assert store.search(vec(1, 0, 0, 0), k=4) == []

    def test_k_bounds_the_result_count(self, store):
        for i in range(10):
            store.upsert(f"chunk {i}", vec(i / 10, 0, 0, 0), "s", "en")
        assert len(store.search(vec(0.5, 0, 0, 0), k=3)) == 3

    def test_sources_are_reported_for_the_ui(self, store):
        store.upsert("a", vec(1, 0, 0, 0), "doc.txt", "en")
        store.upsert("b", vec(0, 1, 0, 0), "doc.txt", "en")
        store.upsert("c", vec(0, 0, 1, 0), "spoken", "id")
        assert dict(store.sources()) == {"doc.txt": 2, "spoken": 1}

    def test_forget_removes_a_source_and_its_vectors(self, store):
        store.upsert("a", vec(1, 0, 0, 0), "doc.txt", "en")
        store.upsert("b", vec(0, 1, 0, 0), "spoken", "en")
        assert store.forget("doc.txt") == 1
        assert store.count() == 1
        assert [h.text for h in store.search(vec(1, 0, 0, 0), k=4)] == ["b"]

    def test_survives_reopening(self, tmp_path):
        path = tmp_path / "persist.db"
        s1 = Store(path, dim=4)
        s1.upsert("remembered across a restart", vec(1, 0, 0, 0), "spoken", "en")
        s1.close()
        s2 = Store(path, dim=4)
        assert s2.count() == 1
        assert s2.search(vec(1, 0, 0, 0), k=1)[0].text == "remembered across a restart"
        s2.close()


def vec4(*values):
    return list(values)


class FakeEmbedder:
    """Returns a fixed vector, and can be made to fail like an unreachable server."""

    def __init__(self, vector=(1.0, 0.0, 0.0, 0.0), fail=False) -> None:
        self.vector = list(vector)
        self.fail = fail
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise ConnectionRefusedError("embedding server is not running")
        return [self.vector for _ in texts]


class FakeGate:
    """Stands in for EmbedServer: warm or not, and counts wake-ups."""

    def __init__(self, warm=False, can_start=True) -> None:
        self.warm = warm
        self.can_start = can_start
        self.acquires = 0
        self.releases = 0
        self.detail = ""

    async def acquire(self) -> bool:
        self.acquires += 1
        if self.can_start:
            self.warm = True
        return self.can_start

    def release(self) -> None:
        self.releases += 1


class TestKeywordFallback:
    """What retrieval does when the embedding server is asleep.

    A vector search needs the QUESTION embedded into the same space as the stored
    chunks, and only the model that stored them can do that -- the conversational LLM
    cannot stand in for it and neither can the database. So the choice is not "vectors
    or something equivalent", it is "vectors or a different, weaker search".
    """

    @pytest.fixture
    def store(self, tmp_path):
        s = Store(tmp_path / "kw.db", dim=4)
        s.upsert("The spare fuses are in the red toolbox.", vec4(1, 0, 0, 0), "notes", "en")
        s.upsert("The bandsaw blade wants 900 newtons of tension.", vec4(0, 1, 0, 0), "notes", "en")
        s.upsert("Sekring cadangan ada di kotak merah.", vec4(0, 0, 1, 0), "catatan", "id")
        yield s
        s.close()

    def test_keyword_search_finds_the_words_the_user_said(self, store):
        hits = store.search_text("where are the spare fuses", k=2)
        assert hits and "red toolbox" in hits[0].text

    def test_punctuation_is_not_fts5_query_syntax(self, store):
        """`what is the robot's name?` is a question, not a MATCH expression."""
        assert store.search_text("where are the fuses?", k=2)
        assert store.search_text('"quoted" AND (parens)', k=2) == []

    def test_it_cannot_do_the_one_thing_vectors_were_for(self, store):
        """An Indonesian question does not find the English note. Say so out loud.

        This is the cost of EMBED_MODE=on-demand with EMBED_RETRIEVAL=warm, and it is
        why `start` and `always` still exist.
        """
        hits = store.search_text("di mana sekring cadangan", k=3)
        assert hits and hits[0].source == "catatan"
        assert all("red toolbox" not in h.text for h in hits)

    def test_a_forgotten_source_leaves_the_keyword_index(self, store):
        store.forget("notes")
        assert store.search_text("spare fuses", k=3) == []

    def test_an_old_store_gets_its_index_backfilled(self, tmp_path):
        """The index was added after M7, so existing databases have rows without it."""
        path = tmp_path / "old.db"
        s1 = Store(path, dim=4)
        s1.upsert("a fact recorded before the keyword index existed",
                  vec4(1, 0, 0, 0), "old", "en")
        s1._db.execute("DELETE FROM chunks_fts")     # simulate the pre-index state
        s1._db.commit()
        s1.close()

        s2 = Store(path, dim=4)
        assert [h.text for h in s2.search_text("keyword index", k=2)]
        s2.close()


class TestRetrievalPolicy:
    @pytest.fixture
    def store(self, tmp_path):
        s = Store(tmp_path / "policy.db", dim=4)
        s.upsert("The spare fuses are in the red toolbox.", vec4(1, 0, 0, 0), "notes", "en")
        yield s
        s.close()

    async def test_warm_uses_vectors_when_the_server_is_up(self, store):
        gate = FakeGate(warm=True)
        r = Retriever(store, FakeEmbedder(), k=2, gate=gate, policy="warm")
        assert await r("fuses")
        assert r.last_mode == "vector"
        assert gate.acquires == 0            # already up; nothing to wake

    async def test_warm_never_waits_for_a_model_to_load(self, store):
        """The hot path is 2393 ms against a 1500 ms target. It cannot afford a start."""
        gate = FakeGate(warm=False)
        r = Retriever(store, FakeEmbedder(), k=2, gate=gate, policy="warm")
        assert await r("spare fuses")
        assert r.last_mode == "keyword"
        assert gate.acquires == 0

    async def test_start_wakes_the_server_and_releases_it_again(self, store):
        gate = FakeGate(warm=False)
        r = Retriever(store, FakeEmbedder(), k=2, gate=gate, policy="start")
        assert await r("fuses")
        assert r.last_mode == "vector"
        assert gate.acquires == 1 and gate.releases == 1

    async def test_off_never_wakes_it_even_when_it_could(self, store):
        gate = FakeGate(warm=False)
        r = Retriever(store, FakeEmbedder(), k=2, gate=gate, policy="off")
        assert await r("spare fuses")
        assert r.last_mode == "keyword"
        assert gate.acquires == 0

    async def test_a_server_that_dies_mid_query_falls_back_rather_than_failing(self, store):
        """It was up when we checked and is not now. A turn is no place to give up."""
        gate = FakeGate(warm=True)
        r = Retriever(store, FakeEmbedder(fail=True), k=2, gate=gate, policy="warm")
        assert await r("spare fuses")
        assert r.last_mode == "keyword"

    async def test_an_empty_vector_result_is_an_answer_and_is_not_second_guessed(self, store):
        """Nothing relevant is a real finding; keywords must not paper over it."""
        gate = FakeGate(warm=True)
        r = Retriever(store, FakeEmbedder(), k=2, gate=gate, policy="warm",
                      max_distance=-1.0)      # rejects every hit
        assert await r("spare fuses") == []
        assert r.last_mode == "vector"

    async def test_no_gate_at_all_is_the_old_always_resident_behaviour(self, store):
        r = Retriever(store, FakeEmbedder(), k=2)
        assert await r("fuses")
        assert r.last_mode == "vector"

    async def test_a_failed_wake_up_does_not_release_a_hold_it_never_took(self, store):
        """EmbedServer counts holders; a spurious release drops somebody else's."""
        gate = FakeGate(warm=False, can_start=False)
        r = Retriever(store, FakeEmbedder(), k=2, gate=gate, policy="start")
        assert await r("spare fuses")
        assert r.last_mode == "keyword"
        assert gate.acquires == 1 and gate.releases == 0


class TestHeadingAwareChunking:
    """A document written as numbered facts should be retrieved as numbered facts.

    Cutting it every N characters instead puts several unrelated facts in one chunk: the
    embedding is an average of all of them and matches none well, and every hit drags the
    others into a prompt with a hard budget. Measured on the Tazakka document, size-based
    chunking had `prompt.trim_context` throw away 51 of 72 retrieved chunks; splitting on
    headings threw away none.
    """

    def test_numbered_sections_become_their_own_chunks(self):
        doc = "\n\n".join(f"{i}. Fact {i}\nThe body of fact number {i}."
                          for i in range(1, 8))
        chunks = chunk_text(doc)
        assert len(chunks) == 7
        assert chunks[0].startswith("1.") and chunks[6].startswith("7.")

    def test_markdown_headings_work_too(self):
        doc = "\n\n".join(f"## Section {i}\nSomething about {i}." for i in range(1, 6))
        assert len(chunk_text(doc)) == 5

    def test_the_preamble_is_kept_rather_than_glued_to_section_one(self):
        """A title belongs to the whole document; attaching it to section 1 makes that
        the only chunk that says what the document is about."""
        doc = "Informasi Seputar Tazakka\nDisampaikan oleh Al-Fatih\n\n" + "\n\n".join(
            f"{i}. Judul {i}\nIsi {i}." for i in range(1, 5))
        chunks = chunk_text(doc)
        assert chunks[0].startswith("Informasi Seputar Tazakka")
        assert chunks[1].startswith("1.")

    def test_prose_is_not_cut_at_the_one_place_someone_wrote_a_number(self):
        """Two numbered items inside ordinary prose are a list, not a document
        structure. Splitting there would be worse than not splitting."""
        doc = ("The robot has two arms. 1. The left one waves. 2. The right one "
               "points. Both are driven by the STM32.")
        assert chunk_text(doc) == [doc]

    def test_a_decimal_is_not_a_heading(self):
        """"1.063 orang" and "13 hektare" start lines in real documents and are facts,
        not section markers. A heading needs a word after the number."""
        from robot.core.rag import split_sections
        doc = "1.063 orang tinggal di sana.\n13 hektare lahan.\n2025 tahun ini."
        assert split_sections(doc) == [doc]

    def test_an_oversized_section_still_falls_back_to_size(self):
        doc = ("1. Short\nbody.\n\n2. Long\n" + "sentence about the robot. " * 200
               + "\n\n3. Short\nagain.")
        chunks = chunk_text(doc)
        assert len(chunks) > 3
        assert all(len(c) <= CHUNK_CHARS + 50 for c in chunks)

    def test_every_fact_survives_the_split(self):
        """Whatever the rule, nothing may be dropped on the floor."""
        doc = "\n\n".join(f"{i}. Judul {i}\nIsi khusus nomor {i}." for i in range(1, 16))
        joined = " ".join(chunk_text(doc))
        for i in range(1, 16):
            assert f"Isi khusus nomor {i}." in joined


class TestReadingPDFs:
    """PDFs go through poppler's `pdftotext`, not a Python library.

    The runtime path stays lean (AGENTS.md constraint 4 in spirit: no parser dependency
    for a job a 100 KB C binary already on the board does), and ingestion is a background
    queue anyway, so a subprocess there costs a turn nothing.
    """

    def test_a_real_pdf_comes_back_as_text(self, tmp_path):
        pytest.importorskip("shutil")
        import shutil
        import subprocess
        if shutil.which("pdftotext") is None:
            pytest.skip("poppler-utils not installed")
        # Build a PDF without a PDF library: Ghostscript-free, just the format.
        src = tmp_path / "doc.ps"
        src.write_text("%!PS\n/Helvetica findfont 12 scalefont setfont\n"
                       "72 720 moveto (Tazakka Bandar Batang) show\nshowpage\n")
        pdf = tmp_path / "doc.pdf"
        if shutil.which("ps2pdf") is None:
            pytest.skip("ps2pdf not installed")
        subprocess.run(["ps2pdf", str(src), str(pdf)], check=True, capture_output=True)
        assert "Tazakka" in rag.read_pdf(pdf)

    def test_a_pdf_with_no_text_is_refused_rather_than_stored_empty(self, tmp_path,
                                                                   monkeypatch):
        """A scanned PDF is images. Ingesting it silently would leave a source in the
        store with nothing in it, and the robot answering "I don't know" for a document
        someone watched it accept."""

        class Result:
            returncode = 0
            stdout = b"   \n  \n"
            stderr = b""

        import subprocess
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/pdftotext")
        with pytest.raises(RuntimeError, match="no extractable text"):
            rag.read_pdf(tmp_path / "scan.pdf")

    def test_a_missing_pdftotext_says_how_to_install_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        with pytest.raises(RuntimeError, match="poppler-utils"):
            rag.read_pdf(tmp_path / "doc.pdf")
