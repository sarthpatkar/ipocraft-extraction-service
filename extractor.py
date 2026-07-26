import re
import fitz  # PyMuPDF
import json
import asyncio
import httpx
from typing import Dict, Any, Tuple, List, Optional
from difflib import SequenceMatcher

CURRENCY_REGEX = r'(?:Rs\.?|INR|₹|Rupees?)'

# ── SEBI Lot Math Helper ──────────────────────────────────────────────────
def calculate_sebi_lots(price_max: float, lot_size: float) -> Dict[str, Any]:
    if not (price_max > 0 and lot_size > 0):
        return {}
    
    lot_price = price_max * lot_size
    
    # Retail: Max investment < ₹2,00,000
    retail_max_lots = max(1, int(199999 // lot_price))
    retail = {
        "retail_min_lots": "1",
        "retail_min_shares": str(int(lot_size)),
        "retail_min_amount": str(int(lot_price)),
        "retail_max_lots": str(retail_max_lots),
        "retail_max_shares": str(int(retail_max_lots * lot_size)),
        "retail_max_amount": str(int(retail_max_lots * lot_price))
    }
    
    # sHNI: Investment > ₹2,00,000 and <= ₹10,00,000
    shni_min_lots = retail_max_lots + 1
    shni_max_lots = int(1000000 // lot_price)
    
    shni = {
        "shni_min_lots": str(shni_min_lots),
        "shni_min_shares": str(int(shni_min_lots * lot_size)),
        "shni_min_amount": str(int(shni_min_lots * lot_price))
    }
    if shni_max_lots >= shni_min_lots:
        shni["shni_max_lots"] = str(shni_max_lots)
        shni["shni_max_shares"] = str(int(shni_max_lots * lot_size))
        shni["shni_max_amount"] = str(int(shni_max_lots * lot_price))

    # bHNI: Investment > ₹10,00,000
    bhni_min_lots = (shni_max_lots + 1) if shni_max_lots > 0 else (shni_min_lots + 1)
    bhni = {
        "bhni_min_lots": str(bhni_min_lots),
        "bhni_min_shares": str(int(bhni_min_lots * lot_size)),
        "bhni_min_amount": str(int(bhni_min_lots * lot_price))
    }
    
    return {**retail, **shni, **bhni}

# ── Multi-Column Text Sorting ─────────────────────────────────────────────
def extract_column_sorted_text(page: fitz.Page) -> str:
    """Extracts text by sorting bounding blocks visually to fix multi-column scrambling."""
    text_dict = page.get_text("dict")
    blocks = text_dict.get("blocks", [])
    
    text_blocks = [b for b in blocks if b.get("type") == 0]
    text_blocks.sort(key=lambda b: (round(b["bbox"][1] / 20) * 20, b["bbox"][0]))
    
    lines_out = []
    for b in text_blocks:
        for l in b.get("lines", []):
            line_text = "".join(span.get("text", "") for span in l.get("spans", []))
            if line_text.strip():
                lines_out.append(line_text.strip())
                
    return "\n".join(lines_out)

class RhpExtractor:
    def __init__(self, pdf_bytes: bytes, groq_api_key: Optional[str] = None):
        self.doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        self.groq_key = groq_api_key
        self.extracted_fields: Dict[str, Any] = {}
        self.warnings: List[str] = []
        self.confidence: Dict[str, float] = {}

    def stage_0_triage(self) -> None:
        """Check if PDF is text-based or scanned image."""
        if len(self.doc) == 0:
            raise ValueError("PDF is empty")
        
        sample_pages = min(5, len(self.doc))
        total_text = ""
        for i in range(sample_pages):
            total_text += self.doc[i].get_text()
            
        if len(total_text.strip()) < 50:
            raise ValueError("This PDF appears to be a scanned image document. Please upload a text-searchable digital PDF from SEBI EDGAR.")

    def stage_1_cover_page(self) -> Dict[str, Any]:
        """Extract cover page details (pages 0-10)."""
        cover_text = ""
        for i in range(min(10, len(self.doc))):
            cover_text += extract_column_sorted_text(self.doc[i]) + "\n"
            
        fields = {}
        
        # Company Name
        name_match = re.search(r'(?:PUBLIC\s+ISSUE|OFFERING|PROSPECTUS|EQUITY\s+SHARES)\s+OF\s+([A-Z0-9\s\.\&\(\)\-]+?(?:LIMITED|LTD))', cover_text, re.IGNORECASE)
        if not name_match:
            name_match = re.search(r'([A-Z0-9\s\.\&\(\)\-]+?(?:LIMITED|LTD))\s+(?:WAS\s+INCORPORATED|REGISTERED\s+OFFICE)', cover_text, re.IGNORECASE)
        if name_match:
            fields["name"] = name_match.group(1).strip()
            self.confidence["name"] = 0.95

        # Exchange
        if re.search(r'\bBSE\b', cover_text) and re.search(r'\bNSE\b', cover_text):
            fields["exchange"] = "NSE, BSE"
        elif re.search(r'\bBSE\b', cover_text):
            fields["exchange"] = "BSE"
        elif re.search(r'\bNSE\b', cover_text):
            fields["exchange"] = "NSE"

        # IPO Type
        if re.search(r'SME\s+INITIAL\s+PUBLIC|SME\s+PLATFORM|EMERGE|CHAPTER\s+IX', cover_text, re.IGNORECASE):
            fields["ipo_type"] = "sme"
        else:
            fields["ipo_type"] = "mainboard"
        self.confidence["ipo_type"] = 0.99

        # Face Value
        fv_match = re.search(fr'Face\s+Value\s+of\s+{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)', cover_text, re.IGNORECASE)
        if fv_match:
            fields["face_value"] = fv_match.group(1)
            self.confidence["face_value"] = 0.98

        # Price Band (Support ₹, Rs., INR, dash, to)
        price_match = re.search(fr'Price\s+Band\s+of\s+{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)\s*(?:to|-|–)\s*{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)', cover_text, re.IGNORECASE)
        if not price_match:
            price_match = re.search(fr'{CURRENCY_REGEX}\s*(\d+(?:\.\d+)?)\s*(?:to|-|–)\s*{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)', cover_text, re.IGNORECASE)
        if price_match:
            fields["price_min"] = price_match.group(1)
            fields["price_max"] = price_match.group(2)
            self.confidence["price_min"] = 0.96
            self.confidence["price_max"] = 0.96

        # Dates
        open_match = re.search(r'BID/ISSUE\s+OPENS?\s+ON\s*[:\-]?\s*([A-Za-z0-9\s,]+?\d{4})', cover_text, re.IGNORECASE)
        if open_match:
            fields["open_date"] = open_match.group(1).strip()

        close_match = re.search(r'BID/ISSUE\s+CLOSES?\s+ON\s*[:\-]?\s*([A-Za-z0-9\s,]+?\d{4})', cover_text, re.IGNORECASE)
        if close_match:
            fields["close_date"] = close_match.group(1).strip()

        # Lead Managers & Registrar Names
        lm_match = re.search(r'BOOK\s+RUNNING\s+LEAD\s+MANAGER[S]?\s*[:\-]?\s*([^\n]+)', cover_text, re.IGNORECASE)
        if lm_match:
            fields["lead_managers"] = lm_match.group(1).strip()

        reg_match = re.search(r'REGISTRAR\s+TO\s+THE\s+ISSUE\s*[:\-]?\s*([^\n]+)', cover_text, re.IGNORECASE)
        if reg_match:
            fields["registrar"] = reg_match.group(1).strip()

        return fields

    def stage_2_terms_table(self) -> Dict[str, Any]:
        """Extract Terms & Lot details from middle pages."""
        fields = {}
        for page_num in range(5, min(100, len(self.doc))):
            page = self.doc[page_num]
            text = extract_column_sorted_text(page)
            
            if "LOT SIZE" in text.upper() or "MINIMUM BID" in text.upper() or "MULTIPLES OF" in text.upper():
                lot_match = re.search(r'(\d+)\s*(?:Equity\s+Shares|Shares|lots)', text, re.IGNORECASE)
                if not lot_match:
                    lot_match = re.search(r'(?:Minimum Bid Quantity|Lot Size)\s*[:\-]?\s*(\d+)', text, re.IGNORECASE)
                if lot_match:
                    fields["lot_size"] = lot_match.group(1)
                    self.confidence["lot_size"] = 0.93
                    break

            if "ISSUE SIZE" in text.upper() or "AGGREGATING UP TO" in text.upper():
                size_match = re.search(fr'aggregating\s+up\s+to\s+{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)\s*(?:Cr|Crore|Million|Lakh)', text, re.IGNORECASE)
                if size_match:
                    fields["issue_size"] = size_match.group(1)

        p_max = float(self.extracted_fields.get("price_max") or fields.get("price_max") or 0)
        l_size = float(fields.get("lot_size") or self.extracted_fields.get("lot_size") or 0)
        
        if p_max > 0 and l_size > 0:
            lot_math = calculate_sebi_lots(p_max, l_size)
            fields.update(lot_math)

        return fields

    def stage_3_general_information(self) -> Dict[str, Any]:
        """Context-anchored extraction within 'GENERAL INFORMATION' section."""
        fields = {}
        gen_info_text = ""
        
        for i, page in enumerate(self.doc):
            text = page.get_text()
            if "GENERAL INFORMATION" in text.upper():
                for j in range(i, min(i + 5, len(self.doc))):
                    gen_info_text += self.doc[j].get_text() + "\n"
                break
                
        if gen_info_text:
            issuer_block = re.search(r'(?:REGISTERED OFFICE|ISSUER DETAILS)([\s\S]*?)(?:REGISTRAR|BOOK RUNNING)', gen_info_text, re.IGNORECASE)
            if issuer_block:
                block_txt = issuer_block.group(1)
                phone = re.search(r'(?:Tel|Phone|Telephone)\s*[:\-]?\s*(\+?\d[\d\s\-]{8,15})', block_txt, re.IGNORECASE)
                email = re.search(r'(?:E-?mail)\s*[:\-]?\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', block_txt, re.IGNORECASE)
                web = re.search(r'(?:Website|Web)\s*[:\-]?\s*(https?://[^\s]+|www\.[^\s]+)', block_txt, re.IGNORECASE)
                
                if phone: fields["company_phone"] = phone.group(1).strip()
                if email: fields["company_email"] = email.group(1).strip()
                if web: fields["company_website"] = web.group(1).strip()

            reg_block = re.search(r'REGISTRAR TO THE ISSUE([\s\S]*?)(?:COMPANY SECRETARY|COMPLIANCE|LEAD MANAGER)', gen_info_text, re.IGNORECASE)
            if reg_block:
                block_txt = reg_block.group(1)
                phone = re.search(r'(?:Tel|Phone|Telephone)\s*[:\-]?\s*(\+?\d[\d\s\-]{8,15})', block_txt, re.IGNORECASE)
                email = re.search(r'(?:E-?mail)\s*[:\-]?\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', block_txt, re.IGNORECASE)
                web = re.search(r'(?:Website|Web)\s*[:\-]?\s*(https?://[^\s]+|www\.[^\s]+)', block_txt, re.IGNORECASE)
                
                if phone: fields["registrar_phone"] = phone.group(1).strip()
                if email: fields["registrar_email"] = email.group(1).strip()
                if web: fields["registrar_website"] = web.group(1).strip()

        return fields

    async def stage_5_ai_prompts(self, supabase_client: Any) -> Dict[str, Any]:
        """AI fallback extraction for missing fields using Groq API."""
        if not self.groq_key:
            return {}

        fields = {}
        
        # Grab text of first 10 pages for comprehensive AI extraction
        sample_text = ""
        for i in range(min(10, len(self.doc))):
            sample_text += extract_column_sorted_text(self.doc[i]) + "\n"

        prompt = (
            "You are an expert IPO data extractor. Extract core IPO details from this RHP text.\n"
            "Return a JSON object with these keys (use null if not found):\n"
            "{\n"
            '  "name": "Full company name",\n'
            '  "exchange": "NSE, BSE or NSE/BSE",\n'
            '  "price_min": "Lower price band as number",\n'
            '  "price_max": "Upper price band as number",\n'
            '  "lot_size": "Lot size bid quantity as number",\n'
            '  "face_value": "Face value as number",\n'
            '  "open_date": "Issue open date",\n'
            '  "close_date": "Issue close date",\n'
            '  "about_company": "2 sentence business summary",\n'
            '  "lead_managers": "Book running lead managers",\n'
            '  "registrar": "Registrar to the issue"\n'
            "}\n\n"
            f"RHP Text:\n{sample_text[:12000]}"
        )
        
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.groq_key}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"}
                    },
                    timeout=25.0
                )
                if res.status_code == 200:
                    data = res.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                    for k, v in parsed.items():
                        if v is not None and str(v).strip() != "" and k not in self.extracted_fields:
                            fields[k] = str(v)
        except Exception as e:
            print(f"[Stage 5 AI Error]: {e}")

        return fields

    def verify_registrar(self, supabase_client: Any) -> None:
        """Cross-check extracted registrar against Supabase registrars table for verification."""
        reg_name = self.extracted_fields.get("registrar")
        if not reg_name or not supabase_client:
            return
            
        try:
            res = supabase_client.table("registrars").select("*").execute()
            rows = res.data or []
            
            for row in rows:
                aliases = [row["name"].upper()] + [a.upper() for a in row.get("name_aliases", [])]
                if any(alias in reg_name.upper() for alias in aliases):
                    if not self.extracted_fields.get("registrar_phone") and row.get("phone"):
                        self.extracted_fields["registrar_phone"] = row["phone"]
                        self.confidence["registrar_phone"] = 0.90
                    if not self.extracted_fields.get("registrar_email") and row.get("email"):
                        self.extracted_fields["registrar_email"] = row["email"]
                        self.confidence["registrar_email"] = 0.90
                    break
        except Exception as e:
            print(f"[Registrar Verification Error]: {e}")

    async def run_pipeline(self, job_id: str, supabase_client: Any) -> Dict[str, Any]:
        """Run all extraction stages with progressive partial updates to Supabase."""
        self.stage_0_triage()
        
        s1 = self.stage_1_cover_page()
        self.extracted_fields.update(s1)

        s2 = self.stage_2_terms_table()
        s3 = self.stage_3_general_information()
        self.extracted_fields.update(s2)
        self.extracted_fields.update(s3)

        if supabase_client:
            supabase_client.table("extraction_jobs").update({
                "partial_result": {**self.extracted_fields, "stage": 3}
            }).eq("id", job_id).execute()

        self.verify_registrar(supabase_client)

        # Stage 5 AI fallback runs if regex missed any fields or for narrative fields
        s5 = await self.stage_5_ai_prompts(supabase_client)
        self.extracted_fields.update(s5)

        # Re-trigger SEBI lot math if price_max and lot_size were filled by AI
        p_max = float(self.extracted_fields.get("price_max") or 0)
        l_size = float(self.extracted_fields.get("lot_size") or 0)
        if p_max > 0 and l_size > 0:
            lot_math = calculate_sebi_lots(p_max, l_size)
            self.extracted_fields.update(lot_math)

        return {
            "fields": self.extracted_fields,
            "confidence": self.confidence,
            "warnings": self.warnings
        }
