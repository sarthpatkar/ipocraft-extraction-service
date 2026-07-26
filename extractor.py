import re
import fitz  # PyMuPDF
import json
import asyncio
import httpx
from typing import Dict, Any, Tuple, List, Optional
from difflib import SequenceMatcher

WORKER_VERSION = "2.0.0"
CURRENCY_REGEX = r'(?:Rs\.?|INR|₹|Rupees?)'

# ── SEBI Lot Math Helper (100% Deterministic) ────────────────────────────
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

    def get_combined_page_text(self, start_page: int, end_page: int) -> str:
        """Combines PyMuPDF plain text for a range of pages."""
        text = ""
        for i in range(start_page, min(end_page, len(self.doc))):
            text += self.doc[i].get_text() + "\n"
        return text

    def find_pages_with_keywords(self, keywords: List[str], max_pages: int = 5) -> str:
        """Locates pages containing any of the given keywords and returns combined text."""
        combined_text = ""
        count = 0
        for i, page in enumerate(self.doc):
            txt = page.get_text()
            txt_upper = txt.upper()
            if any(kw.upper() in txt_upper for kw in keywords):
                combined_text += txt + "\n"
                count += 1
                if count >= max_pages:
                    break
        return combined_text

    def stage_1_cover_page(self) -> Dict[str, Any]:
        """Extract cover page details (pages 0-15). Handles multi-BRLM formatting."""
        cover_text = self.get_combined_page_text(0, 15)
        fields = {}
        
        # 1. Company Name
        name_match = re.search(
            r'(?:PUBLIC\s+ISSUE|OFFERING|PROSPECTUS|EQUITY\s+SHARES|INITIAL\s+PUBLIC\s+OFFER)\s+(?:OF\s+)?(?:UP\s+TO\s+[\d,]+\s+EQUITY\s+SHARES\s+OF\s+)?([A-Z0-9\s\.\&\(\)\-]+?\b(?:LIMITED|LTD|PRIVATE\s+LIMITED|PVT\s+LTD))\b',
            cover_text,
            re.IGNORECASE
        )
        if not name_match:
            name_match = re.search(r'([A-Z0-9\s\.\&\(\)\-]+?\b(?:LIMITED|LTD|PRIVATE\s+LIMITED|PVT\s+LTD))\b', cover_text)
        if name_match:
            # Clean company name
            raw_name = name_match.group(1).strip()
            # Remove trailing legal disclaimers if caught
            clean_name = re.sub(r'\s+(?:HAVING|REGISTERED|OUR\s+COMPANY|WAS\s+INCORPORATED).*$', '', raw_name, flags=re.IGNORECASE)
            fields["name"] = clean_name.strip()
            self.confidence["name"] = 0.95

        # 2. Exchange
        if re.search(r'\bBSE\b', cover_text) and re.search(r'\bNSE\b', cover_text):
            fields["exchange"] = "NSE, BSE"
        elif re.search(r'\bBSE\b', cover_text):
            fields["exchange"] = "BSE"
        elif re.search(r'\bNSE\b', cover_text):
            fields["exchange"] = "NSE"
        self.confidence["exchange"] = 0.99

        # 3. IPO Type
        if re.search(r'SME\s+INITIAL\s+PUBLIC|SME\s+PLATFORM|EMERGE|CHAPTER\s+IX', cover_text, re.IGNORECASE):
            fields["ipo_type"] = "sme"
        else:
            fields["ipo_type"] = "mainboard"
        self.confidence["ipo_type"] = 0.99

        # 4. Face Value
        fv_match = re.search(fr'(?:Face\s+Value|Nominal\s+Value)[^\n]*?{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)', cover_text, re.IGNORECASE)
        if fv_match:
            fields["face_value"] = fv_match.group(1)
            self.confidence["face_value"] = 0.98

        # 5. Price Band / Offer Price
        price_band = re.search(
            fr'(?:Price\s+Band|Offer\s+Price|Issue\s+Price)[^\n]*?{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)\s*(?:to|-|–|AND)\s*{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)',
            cover_text,
            re.IGNORECASE
        )
        if not price_band:
            price_band = re.search(fr'{CURRENCY_REGEX}\s*(\d+(?:\.\d+)?)\s*(?:to|-|–)\s*{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)', cover_text, re.IGNORECASE)

        if price_band:
            fields["price_min"] = price_band.group(1)
            fields["price_max"] = price_band.group(2)
            self.confidence["price_min"] = 0.96
            self.confidence["price_max"] = 0.96
        else:
            fixed_price = re.search(fr'(?:Issue\s+Price|Offer\s+Price)\s+(?:of\s+)?{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)', cover_text, re.IGNORECASE)
            if fixed_price:
                fields["price_min"] = fixed_price.group(1)
                fields["price_max"] = fixed_price.group(1)
                self.confidence["price_min"] = 0.95
                self.confidence["price_max"] = 0.95

        # 6. Issue Dates
        open_match = re.search(r'(?:BID/ISSUE\s+OPENS?|OPENS?\s+ON)\s*[:\-]?\s*([A-Za-z0-9\s,]+?\d{4})', cover_text, re.IGNORECASE)
        if open_match:
            fields["open_date"] = open_match.group(1).strip()

        close_match = re.search(r'(?:BID/ISSUE\s+CLOSES?|CLOSES?\s+ON)\s*[:\-]?\s*([A-Za-z0-9\s,]+?\d{4})', cover_text, re.IGNORECASE)
        if close_match:
            fields["close_date"] = close_match.group(1).strip()

        # 7. Lead Managers & Registrar
        lm_match = re.search(r'BOOK\s+RUNNING\s+LEAD\s+MANAGER[S]?\s*[:\-]?\s*([^\n]+)', cover_text, re.IGNORECASE)
        if lm_match:
            fields["lead_managers"] = lm_match.group(1).strip()

        reg_match = re.search(r'REGISTRAR\s+TO\s+THE\s+ISSUE\s*[:\-]?\s*([^\n]+)', cover_text, re.IGNORECASE)
        if reg_match:
            fields["registrar"] = reg_match.group(1).strip()

        return fields

    def stage_2_basis_for_issue_price(self) -> Dict[str, Any]:
        """Parser for SEBI-mandated 'BASIS FOR ISSUE PRICE' table (EPS, P/E, ROCE)."""
        fields = {}
        basis_text = self.find_pages_with_keywords(["BASIS FOR ISSUE PRICE", "PEER GROUP COMPARISON"], max_pages=6)
        
        if basis_text:
            # EPS
            eps_match = re.search(r'Basic\s+EPS[^\n]*?(\d+(?:\.\d+)?)', basis_text, re.IGNORECASE)
            if eps_match:
                fields["eps_pre"] = eps_match.group(1)
            
            eps_post_match = re.search(r'Diluted\s+EPS[^\n]*?(\d+(?:\.\d+)?)', basis_text, re.IGNORECASE)
            if eps_post_match:
                fields["eps_post"] = eps_post_match.group(1)

            # P/E Ratio
            pe_match = re.search(r'P/E\s+Ratio[^\n]*?(\d+(?:\.\d+)?)', basis_text, re.IGNORECASE)
            if pe_match:
                fields["pe_pre"] = pe_match.group(1)
                fields["pe_post"] = pe_match.group(1)

            # Return on Net Worth / ROCE
            ronw_match = re.search(r'(?:Return\s+on\s+Net\s+Worth|RoNW|ROCE)[^\n]*?(\d+(?:\.\d+)?)\s*%', basis_text, re.IGNORECASE)
            if ronw_match:
                fields["roce"] = ronw_match.group(1)

        # Lot Size and Issue Size scanning across full document
        for page_num in range(min(120, len(self.doc))):
            text = self.doc[page_num].get_text()
            
            if "lot_size" not in fields and "lot_size" not in self.extracted_fields:
                lot_match = re.search(r'(?:Minimum\s+Bid\s+Quantity|Lot\s+Size|bid\s+for\s+at\s+least)[^\n]*?(\d+)\s*(?:Equity\s+Shares|shares)?', text, re.IGNORECASE)
                if not lot_match:
                    lot_match = re.search(r'(\d+)\s*(?:Equity\s+Shares|shares)\s*(?:and\s+in\s+multiples|thereafter)', text, re.IGNORECASE)
                if lot_match and int(lot_match.group(1)) > 0:
                    fields["lot_size"] = lot_match.group(1)
                    self.confidence["lot_size"] = 0.93

            if "issue_size" not in fields and "issue_size" not in self.extracted_fields:
                size_match = re.search(fr'aggregating\s+up\s+to\s+{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)\s*(?:Cr|Crore|Million|Lakh)', text, re.IGNORECASE)
                if size_match:
                    fields["issue_size"] = size_match.group(1)

        return fields

    def stage_3_general_information(self) -> Dict[str, Any]:
        """Context-anchored extraction within 'GENERAL INFORMATION' section."""
        fields = {}
        gen_info_text = ""
        
        for i, page in enumerate(self.doc):
            text = page.get_text()
            if "GENERAL INFORMATION" in text.upper():
                for j in range(i, min(i + 6, len(self.doc))):
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

    async def call_groq_ai(self, prompt: str) -> Dict[str, Any]:
        """Calls Groq API and returns parsed JSON object."""
        if not self.groq_key:
            return {}
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
                    timeout=30.0
                )
                if res.status_code == 200:
                    data = res.json()
                    content = data["choices"][0]["message"]["content"]
                    return json.loads(content)
                else:
                    print(f"[AI Warning]: Groq status {res.status_code}: {res.text[:150]}")
        except Exception as e:
            print(f"[AI Error]: {e}")
        return {}

    async def stage_4_investor_narratives(self, supabase_client: Any) -> Dict[str, Any]:
        """Financial Analyst Persona AI prompts for investor-grade narratives."""
        if not self.groq_key:
            print("[Stage 4 AI Note]: GROQ_API_KEY is missing in environment.")
            return {}

        fields = {}

        # 1. Prompt A: Cover & Offer Details (if any essential key is missing)
        cover_text = self.get_combined_page_text(0, 15)
        terms_text = self.find_pages_with_keywords(["PRICE BAND", "LOT SIZE", "MINIMUM BID", "TERMS OF THE ISSUE"])
        
        prompt_cover = (
            "You are a senior IPO analyst. Extract IPO parameters from this RHP text.\n"
            "Return a JSON object with these exact keys (use null if not found):\n"
            "{\n"
            '  "name": "Full legal company name",\n'
            '  "exchange": "NSE, BSE or NSE, BSE",\n'
            '  "price_min": "Lower price band number",\n'
            '  "price_max": "Upper price band number",\n'
            '  "lot_size": "Lot size bid quantity number",\n'
            '  "face_value": "Face value number",\n'
            '  "issue_size": "Total issue size in Crores",\n'
            '  "fresh_issue": "Fresh issue component in Crores",\n'
            '  "open_date": "Issue opening date YYYY-MM-DD",\n'
            '  "close_date": "Issue closing date YYYY-MM-DD",\n'
            '  "lead_managers": "Book running lead managers",\n'
            '  "registrar": "Registrar to the issue"\n'
            "}\n\n"
            f"RHP Text:\n{cover_text[:10000]}\n{terms_text[:10000]}"
        )
        res_a = await self.call_groq_ai(prompt_cover)
        for k, v in res_a.items():
            if v is not None and str(v).strip() != "" and k not in self.extracted_fields:
                fields[k] = str(v)

        # 2. Prompt B: Investor-Grade "About Company" Summary (Financial Analyst Persona)
        biz_text = self.find_pages_with_keywords(["OUR BUSINESS", "BUSINESS OVERVIEW"], max_pages=3)
        if biz_text:
            prompt_about = (
                "You are a senior equity research analyst writing for retail and HNI IPO investors.\n"
                "Summarize the company's business in 2-3 crisp, high-value sentences based on this RHP text.\n\n"
                "CRITICAL INSTRUCTIONS:\n"
                "1. Focus ON: Primary business model, key products/services, manufacturing/operational scale (capacity, plants, units), revenue drivers, and market position.\n"
                "2. DO NOT INCLUDE: Incorporation date, registered address, CIN number, legal status, or SEBI disclaimers.\n"
                "3. Format JSON: {\"about_company\": \"...\"}\n\n"
                f"RHP Business Text:\n{biz_text[:10000]}"
            )
            res_b = await self.call_groq_ai(prompt_about)
            if "about_company" in res_b and res_b["about_company"]:
                fields["about_company"] = str(res_b["about_company"])

        # 3. Prompt C: Key Business Risks (Top 5 Business/Operational Risks)
        risk_text = self.find_pages_with_keywords(["SUMMARY OF RISK FACTORS", "RISK FACTORS"], max_pages=3)
        if risk_text:
            prompt_risks = (
                "You are an IPO risk analyst. Extract the top 4-5 business & operational risks for an investor.\n"
                "CRITICAL: Exclude generic macro risks (e.g. 'changes in government policy', 'general economic slowdown').\n"
                "Focus on: customer concentration, supplier reliance, debt levels, regulatory approvals, or product risks.\n"
                "Format JSON: {\"company_risks\": \"1. Risk one\\n2. Risk two\\n3. Risk three\"}\n\n"
                f"RHP Risk Text:\n{risk_text[:10000]}"
            )
            res_c = await self.call_groq_ai(prompt_risks)
            if "company_risks" in res_c and res_c["company_risks"]:
                fields["company_risks"] = str(res_c["company_risks"])

        # 4. Prompt D: Objectives & Financial Metrics
        fin_text = self.find_pages_with_keywords(["BASIS FOR ISSUE PRICE", "SHAREHOLDING PATTERN", "OBJECTS OF THE ISSUE"])
        if fin_text:
            prompt_fin = (
                "Extract IPO financial metrics and objects from this RHP text. Return a JSON object with these keys (use null if not found):\n"
                "{\n"
                '  "objectives": "Numbered list of fund utilization plans",\n'
                '  "eps_pre": "Pre-issue EPS number",\n'
                '  "eps_post": "Post-issue EPS number",\n'
                '  "pe_pre": "Pre-issue P/E ratio number",\n'
                '  "pe_post": "Post-issue P/E ratio number",\n'
                '  "promoter_holding_pre": "Pre-issue promoter % number",\n'
                '  "promoter_holding_post": "Post-issue promoter % number",\n'
                '  "reservation_qib": "QIB reservation % number",\n'
                '  "reservation_nii": "NII reservation % number",\n'
                '  "reservation_rii": "Retail reservation % number",\n'
                '  "roce": "ROCE % number",\n'
                '  "pat_margin": "PAT Margin % number"\n'
                "}\n\n"
                f"RHP Text:\n{fin_text[:12000]}"
            )
            res_d = await self.call_groq_ai(prompt_fin)
            for k, v in res_d.items():
                if v is not None and str(v).strip() != "" and k not in self.extracted_fields:
                    fields[k] = str(v)

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
        
        # Stage 1: Cover Page (Multi-BRLM)
        s1 = self.stage_1_cover_page()
        self.extracted_fields.update(s1)

        # Stage 2 & 3: Basis for Issue Price + General Information Contacts
        s2 = self.stage_2_basis_for_issue_price()
        s3 = self.stage_3_general_information()
        self.extracted_fields.update(s2)
        self.extracted_fields.update(s3)

        if supabase_client:
            supabase_client.table("extraction_jobs").update({
                "partial_result": {**self.extracted_fields, "stage": 3}
            }).eq("id", job_id).execute()

        # Verification against Supabase database
        self.verify_registrar(supabase_client)

        # Stage 4: Investor-Grade AI Narrative Prompts
        s4 = await self.stage_4_investor_narratives(supabase_client)
        self.extracted_fields.update(s4)

        # SEBI Lot Math (100% Deterministic)
        p_max = float(self.extracted_fields.get("price_max") or 0)
        l_size = float(self.extracted_fields.get("lot_size") or 0)
        if p_max > 0 and l_size > 0:
            lot_math = calculate_sebi_lots(p_max, l_size)
            self.extracted_fields.update(lot_math)

        return {
            "worker_version": WORKER_VERSION,
            "fields": self.extracted_fields,
            "confidence": self.confidence,
            "warnings": self.warnings
        }
