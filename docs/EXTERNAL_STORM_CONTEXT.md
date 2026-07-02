# External Storm Context Retrieval

WISER can supplement county-level outage analytics with NOAA/NHC/NCEI web context for storm-impact questions such as:

```bash
python scripts/wiser_cli.py "impact of the new storm in Connecticut" --limit 5
```

## Default Sources

- National Hurricane Center 2020 Atlantic Tropical Cyclone Reports:
  https://www.nhc.noaa.gov/data/tcr/index.php?season=2020&basin=atl
- National Hurricane Center 2020 Eastern Pacific Tropical Cyclone Reports:
  https://www.nhc.noaa.gov/data/tcr/index.php?season=2020&basin=epac
- NCEI Connecticut Billion-Dollar Weather and Climate Disasters summary:
  https://www.ncei.noaa.gov/access/billions/state-summary/CT

The NHC season pages are crawled for linked PDF reports. WISER extracts available text from those reports, chunks the paragraphs, stores them in the existing `RagDocument` and `RagChunk` tables, then retrieves the most relevant paragraphs for the user query.

Unrelated research PDFs are not part of the default impact crawl. Add them explicitly with `--web-source` only when the question needs that paper.

## Similarity Backend

The external-context search uses a hashed text-vector representation. If Meta/Facebook's FAISS library and NumPy are installed, WISER uses `faiss.IndexFlatIP` for fast inner-product nearest-neighbor search. If FAISS is not installed, WISER automatically falls back to an exact local cosine-style ranking with no external dependencies.

Optional install:

```bash
pip install faiss-cpu numpy
```

Optional PDF extraction improvement:

```bash
pip install pypdf
```

Without `pypdf`, WISER tries the system `pdftotext` command and then a lightweight PDF literal-string fallback.

## OpenAI Summaries

When `OPENAI_API_KEY` is set, impact-style questions use the OpenAI Responses API to turn retrieved NOAA/NHC/NCEI paragraphs into short cited bullets. The key is read from `.env` or the shell environment and should never be pasted into source files, docs, tests, or commits.

`.env`:

```dotenv
OPENAI_API_KEY=your_key_here
WISER_OPENAI_MODEL=gpt-5.5
```

```bash
python scripts/wiser_cli.py "impact of the new storm in Connecticut" --limit 5
```

By default WISER uses `gpt-5.5`, matching the current Responses API text-generation examples. You can override that without editing code:

```bash
export WISER_OPENAI_MODEL="gpt-5.5"
python scripts/wiser_cli.py "impact of the new storm" --openai-model gpt-5.5
```

Note that `WISER_OPENAI_MODEL` is a single knob shared by both OpenAI features: impact summaries here **and** SQL generation in the SqlAiAgent (which otherwise falls back to `gpt-4o-mini`). Setting it changes both. To pick a different model per feature for a single run, use `--openai-model` (summaries) and `--sql-model` (SQL generation).

If the key is absent, the API call fails, or you pass `--no-openai-summary`, WISER falls back to its local extractive summary.

## CLI Controls

```bash
# Crawl the default web sources before answering.
python scripts/wiser_cli.py "impact of the new storm" --crawl-web-sources

# Limit linked NHC PDFs.
python scripts/wiser_cli.py "impact of the new storm" --max-linked-pdfs 10

# Add or replace external sources.
python scripts/wiser_cli.py "impact of the new storm" \
  --web-source https://www.ncei.noaa.gov/access/billions/state-summary/CT

# Force the local exact fallback even when FAISS is installed.
python scripts/wiser_cli.py "impact of the new storm" --similarity-backend exact

# Keep summary generation fully local.
python scripts/wiser_cli.py "impact of the new storm" --no-openai-summary
```

Impact-style questions automatically trigger the default external crawl. Other RAG questions can opt in with `--crawl-web-sources`.
