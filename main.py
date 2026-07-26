import os
import asyncio
import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv
from supabase import create_client, Client
from extractor import RhpExtractor

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

async def worker_loop():
    """Background polling loop checking Supabase extraction_jobs table every 5 seconds."""
    print("🚀 IPOCraft Extraction Service Started...")
    while True:
        try:
            if not supabase:
                await asyncio.sleep(5)
                continue

            # Fetch oldest pending job
            res = (
                supabase.table("extraction_jobs")
                .select("*")
                .eq("status", "pending")
                .order("created_at", desc=False)
                .limit(1)
                .execute()
            )

            jobs = res.data or []
            if not jobs:
                await asyncio.sleep(5)
                continue

            job = jobs[0]
            job_id = job["id"]
            file_path = job["file_path"]

            print(f"⚡ Claiming Job {job_id} for file: {file_path}")

            # Lock job
            supabase.table("extraction_jobs").update({
                "status": "processing",
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }).eq("id", job_id).execute()

            try:
                # Download PDF from Supabase Storage rhp-uploads bucket
                pdf_bytes = supabase.storage.from_("rhp-uploads").download(file_path)
                
                # Execute multi-stage Python pipeline
                extractor = RhpExtractor(pdf_bytes, groq_api_key=GROQ_API_KEY)
                pipeline_result = await extractor.run_pipeline(job_id, supabase)

                # Update job as completed
                supabase.table("extraction_jobs").update({
                    "status": "done",
                    "result": pipeline_result["fields"],
                    "confidence": pipeline_result["confidence"],
                    "warnings": pipeline_result["warnings"],
                    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
                }).eq("id", job_id).execute()

                print(f"✅ Job {job_id} Completed Successfully!")

                # Background cleanup of PDF file from Supabase Storage
                try:
                    supabase.storage.from_("rhp-uploads").remove([file_path])
                except Exception as e:
                    print(f"Cleanup note: {e}")

            except Exception as job_err:
                err_msg = str(job_err)
                print(f"❌ Job {job_id} Failed: {err_msg}")
                supabase.table("extraction_jobs").update({
                    "status": "failed",
                    "error": err_msg,
                    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
                }).eq("id", job_id).execute()

        except Exception as loop_err:
            print(f"⚠️ Service Loop Error: {loop_err}")
            await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background polling task when FastAPI starts
    polling_task = asyncio.create_task(worker_loop())
    yield
    # Cancel task on shutdown
    polling_task.cancel()

app = FastAPI(title="IPOCraft Extraction Service", lifespan=lifespan)

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "ipocraft-extraction-service",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
