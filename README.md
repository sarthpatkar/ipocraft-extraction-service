# IPOCraft Extraction Service (`ipocraft-extraction-service`)

A high-performance Python FastAPI microservice that extracts structured IPO data from Indian Red Herring Prospectus (RHP) PDF documents.

## Architecture
- **Framework:** FastAPI + Uvicorn
- **Engine:** PyMuPDF (`fitz`) for PDF visual block sorting & table parsing
- **Queue:** Supabase Realtime Job Queue (`extraction_jobs` table)
- **AI Model:** Groq Llama 3.3 70B for narrative field summarization
- **Verification:** Dynamic registrar cross-referencing against Supabase `registrars` reference table

## Deployment on Render.com
1. Create a **Web Service** on Render.
2. Connect this GitHub repository (`ipocraft-extraction-service`).
3. Set Build Command: `pip install -r requirements.txt`
4. Set Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Configure Environment Variables:
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `GROQ_API_KEY`
