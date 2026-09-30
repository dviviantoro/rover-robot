# knowledge/

Source documents for the robot's knowledge store. Ingesting one embeds its chunks into
`run/knowledge.db` (`KNOWLEDGE_DB`); this folder keeps the originals so the store can be
rebuilt from scratch.

    ./scripts/ingest.sh knowledge/Tazakka_Informasi_Ringkas.pdf
    ./scripts/ingest.sh --list           # what is stored now
    ./scripts/ingest.sh --rebuild        # forget everything and re-ingest this folder

PDFs are read with poppler's `pdftotext` at ingest time. A scanned PDF is images and is
refused with a reason -- there is no OCR on this robot.
