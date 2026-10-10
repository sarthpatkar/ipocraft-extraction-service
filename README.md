# IPOCraft Extraction Service (`ipocraft-extraction-service`)

A high-performance Python FastAPI microservice that extracts structured IPO data from Indian Red Herring Prospectus (RHP) PDF documents.

## Architecture
- **Framework:** FastAPI + Uvicorn
- **Engine:** PyMuPDF (`fitz`) for PDF visual block sorting & table parsing
- **Queue:** Supabase Realtime Job Queue (`extraction_jobs` table)
- **Extraction:** Fully deterministic ("Zero-AI") — layout-aware parsing + regex; no LLM calls. SEBI lot tiers are computed (`calculate_sebi_lots`), not extracted.
- **Verification:** Dynamic registrar cross-referencing against Supabase `registrars` reference table
- **Output:** `result`, per-field `confidence`, and `warnings` written back to the job row

## How It Works
1. The IPOCraft admin panel uploads the PDF to the `rhp-uploads` Storage bucket and calls `/api/extract-rhp/submit`, which inserts a `pending` row in `extraction_jobs` (deduplicated by SHA-256 file hash).
2. `worker_loop` (`main.py`) polls for the oldest pending job every 5 s and claims it atomically (`UPDATE … WHERE status = 'pending'`).
3. `RhpExtractor.run_pipeline` (`extractor.py`) runs: triage (rejects scanned PDFs) → TOC map → cover page & contacts → financial tables (PyMuPDF TableFinder) → paragraph slicing → cover extras → registrar enrichment → lot math.
4. The job is marked `done` (or `failed` with an error); the admin panel receives the update via Supabase Realtime.

`SCHEMA_VERSION` in `main.py` must match the `schema_version` set by the submit route.

## Deployment on Render.com
1. Create a **Web Service** on Render.
2. Connect this GitHub repository (`ipocraft-extraction-service`).
3. Set Build Command: `pip install -r requirements.txt`
4. Set Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Configure Environment Variables:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `GROQ_API_KEY` (optional — currently accepted but unused)

## Run Locally
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```
Health check: `GET /` or `GET /health`
