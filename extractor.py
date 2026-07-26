import re
import fitz  # PyMuPDF
import json
import asyncio
from typing import Dict, Any, Tuple, List, Optional

WORKER_VERSION = "3.2.0-zero-ai"
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

def extract_company_name_from_cin_block(block_text: str) -> str:
    """Extracts clean active legal company name from CIN block across multiline blocks."""
    if not block_text:
        return ""
    # Strip former name parentheticals across full multiline block first!
    clean_block = re.sub(r'\s*\((?:FORMERLY|formerly)\s+KNOWN\s+AS[^\)]+\)', '', block_text, flags=re.IGNORECASE)
    clean_block = re.sub(r'\s*\([^)]*PRIVATE\s+LIMITED[^\)]*\)', '', clean_block, flags=re.IGNORECASE)
    clean_block = re.sub(r'\s*\([^)]*LIMITED[^\)]*\)', '', clean_block, flags=re.IGNORECASE)
    clean_block = re.sub(r'\s*\([^)]*LTD[^\)]*\)', '', clean_block, flags=re.IGNORECASE)

    lines = [l.strip() for l in clean_block.split('\n') if l.strip()]
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if re.search(r'^(?:CORPORATE|REGISTERED|RED\s+HERRING|PROSPECTUS|DATED|READ|SECTION|100%|ISSUE|DETAILS)', line, re.IGNORECASE):
            continue
        if len(line) >= 4 and ("LIMITED" in line.upper() or "LTD" in line.upper()):
            return line.strip()
            
    for line in reversed(lines):
        if len(line) >= 4 and not re.search(r'^(?:CORPORATE|REGISTERED|RED|HERRING|PROSPECTUS|DATED|READ)', line, re.IGNORECASE):
            return line.strip()
            
    return lines[-1] if lines else ""

class RhpExtractor:
    def __init__(self, pdf_bytes: bytes, groq_api_key: Optional[str] = None):
        self.doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        self.extracted_fields: Dict[str, Any] = {}
        self.warnings: List[str] = []
        self.confidence: Dict[str, float] = {}
        self.toc_map: Dict[str, int] = {}

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

    def stage_1_toc_map_engine(self) -> Dict[str, int]:
        """Parses SEBI Table of Contents (pages 2-10) to map chapter page numbers."""
        toc_map = {}
        section_synonyms = {
            "GENERAL_INFO": ["GENERAL INFORMATION", "SECTION I – GENERAL", "SECTION I - GENERAL"],
            "CAPITAL_STRUCTURE": ["CAPITAL STRUCTURE"],
            "OBJECTS": ["OBJECTS OF THE ISSUE", "OBJECTS OF THE OFFER", "OBJECTS OF THE ISSUE/OFFER"],
            "BASIS_FOR_PRICE": ["BASIS FOR ISSUE PRICE", "BASIS FOR OFFER PRICE", "BASIS OF OFFER PRICE", "BASIS OF ISSUE PRICE"],
            "BUSINESS": ["OUR BUSINESS", "OUR INDUSTRY AND BUSINESS"],
            "RISK_FACTORS": ["RISK FACTORS", "SUMMARY OF RISK FACTORS"]
        }
        
        for pg in range(1, min(12, len(self.doc))):
            txt = self.doc[pg].get_text()
            lines = txt.split('\n')
            for line in lines:
                m = re.search(r'([A-Z\s\–\-\:\(\)]{4,70})\s*[\.\s]{4,}\s*(\d{1,4})', line)
                if m:
                    sec_title = m.group(1).strip().upper()
                    pnum = int(m.group(2))
                    if 1 <= pnum <= len(self.doc):
                        for sec_key, syns in section_synonyms.items():
                            if sec_key not in toc_map and any(syn in sec_title for syn in syns):
                                toc_map[sec_key] = pnum

        self.toc_map = toc_map
        return toc_map

    def stage_2_cover_page_and_contacts(self) -> Dict[str, Any]:
        """Extract cover page parameters and issuer/registrar contacts."""
        p1_text = self.doc[0].get_text()
        fields = {}

        # 1. Company Name (CIN Anchor + Smart Multiline Parenthetical Bypasser)
        cin_m = re.search(r'([A-Z0-9\s\.\&\(\)\-\,\'\"]+?)\n\s*(?:CORPORATE\s+IDENTI|CIN\b)', p1_text, re.IGNORECASE)
        if cin_m:
            extracted_name = extract_company_name_from_cin_block(cin_m.group(1))
            if extracted_name:
                fields["name"] = extracted_name
                self.confidence["name"] = 0.99

        if "name" not in fields:
            name_m = re.search(r'(?:PUBLIC\s+ISSUE|OFFERING|PROSPECTUS|EQUITY\s+SHARES|INITIAL\s+PUBLIC\s+OFFER)\s+(?:OF\s+)?(?:UP\s+TO\s+[\d,]+\s+EQUITY\s+SHARES\s+OF\s+)?([A-Z0-9\s\.\&\(\)\-]+?\b(?:LIMITED|LTD|PRIVATE\s+LIMITED|PVT\s+LTD))\b', p1_text, re.IGNORECASE)
            if name_m:
                fields["name"] = extract_company_name_from_cin_block(name_m.group(1))

        # 2. Exchange
        cover_15 = ""
        for i in range(min(15, len(self.doc))):
            cover_15 += self.doc[i].get_text() + "\n"

        if re.search(r'\bBSE\b', cover_15) and re.search(r'\bNSE\b', cover_15):
            fields["exchange"] = "NSE, BSE"
        elif re.search(r'\bBSE\b', cover_15):
            fields["exchange"] = "BSE"
        elif re.search(r'\bNSE\b', cover_15):
            fields["exchange"] = "NSE"
        self.confidence["exchange"] = 0.99

        # 3. IPO Type
        if re.search(r'SME\s+INITIAL\s+PUBLIC|SME\s+PLATFORM|EMERGE|CHAPTER\s+IX', cover_15, re.IGNORECASE):
            fields["ipo_type"] = "sme"
        else:
            fields["ipo_type"] = "mainboard"
        self.confidence["ipo_type"] = 0.99

        # 4. Face Value
        fv_m = re.search(r'(?:face\s+value|nominal\s+value)\s+of\s+(?:Rs\.?|INR|₹)?\s*(\d+(?:\.\d+)?)', p1_text, re.IGNORECASE)
        if fv_m:
            fields["face_value"] = fv_m.group(1)
            self.confidence["face_value"] = 0.98

        # 5. Price Band / Offer Price
        pb_m = re.search(fr'(?:Price\s+Band|Offer\s+Price|Issue\s+Price)[^\n]*?{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)\s*(?:to|-|–|AND)\s*{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)', cover_15, re.IGNORECASE)
        if pb_m:
            fields["price_min"] = pb_m.group(1)
            fields["price_max"] = pb_m.group(2)
        else:
            fixed_p = re.search(fr'(?:Issue\s+Price|Offer\s+Price)\s+(?:of\s+)?{CURRENCY_REGEX}?\s*(\d+(?:\.\d+)?)', cover_15, re.IGNORECASE)
            if fixed_p:
                fields["price_min"] = fixed_p.group(1)
                fields["price_max"] = fixed_p.group(1)
            elif "[●]" in p1_text or "[•]" in p1_text:
                self.warnings.append("Price Band is omitted ([●]) in official SEBI filing — enter once Price Band Notice is published.")

        # 6. Dates
        open_m = re.search(r'(?:BID/ISSUE\s+OPENS?|OPENS?\s+ON)\s*[:\-]?\s*([A-Za-z0-9\s,]+?\d{4})', cover_15, re.IGNORECASE)
        if open_m:
            fields["open_date"] = open_m.group(1).strip().replace('\n', ' ')

        close_m = re.search(r'(?:BID/ISSUE\s+CLOSES?|CLOSES?\s+ON)\s*[:\-]?\s*([A-Za-z0-9\s,]+?\d{4})', cover_15, re.IGNORECASE)
        if close_m:
            fields["close_date"] = close_m.group(1).strip().replace('\n', ' ')

        # 7. Issuer & Registrar Contacts from "GENERAL INFORMATION" section
        gen_pg = self.toc_map.get("GENERAL_INFO")
        if not gen_pg:
            for pg in range(min(20, len(self.doc))):
                if "GENERAL INFORMATION" in self.doc[pg].get_text().upper():
                    gen_pg = pg + 1
                    break

        if gen_pg and gen_pg <= len(self.doc):
            gen_text = ""
            for p in range(gen_pg - 1, min(gen_pg + 5, len(self.doc))):
                gen_text += self.doc[p].get_text() + "\n"

            email_m = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', gen_text)
            if email_m:
                filtered_emails = [e for e in email_m if "sebi.gov.in" not in e.lower() and "bseindia" not in e.lower()]
                if filtered_emails:
                    fields["company_email"] = filtered_emails[0]
                    if len(filtered_emails) > 1:
                        fields["registrar_email"] = filtered_emails[1]

            phone_m = re.findall(r'(?:\+91|0)?\d[\d\s\-]{8,14}\d', gen_text)
            if phone_m:
                fields["company_phone"] = phone_m[0].strip()

            web_m = re.findall(r'(?:https?://[^\s]+|www\.[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', gen_text)
            if web_m:
                fields["company_website"] = web_m[0].strip()

        return fields

    def stage_3_table_finder_financials(self) -> Dict[str, Any]:
        """Parses 'BASIS FOR ISSUE PRICE' & 'CAPITAL STRUCTURE' tables via PyMuPDF TableFinder."""
        fields = {}

        # 1. Basis for Issue Price Tables
        basis_pg = self.toc_map.get("BASIS_FOR_PRICE")
        if not basis_pg:
            for pg in range(min(150, len(self.doc))):
                if "BASIS FOR ISSUE PRICE" in self.doc[pg].get_text().upper() or "BASIS FOR OFFER PRICE" in self.doc[pg].get_text().upper():
                    basis_pg = pg + 1
                    break

        if basis_pg and basis_pg <= len(self.doc):
            for p in range(basis_pg - 1, min(basis_pg + 5, len(self.doc))):
                tabs = self.doc[p].find_tables()
                if tabs and tabs.tables:
                    for t in tabs.tables:
                        grid = t.extract()
                        for row in grid:
                            row_txt = " ".join([str(c).strip() for c in row if c])
                            
                            if "Basic EPS" in row_txt and "eps_pre" not in fields:
                                num_m = re.search(r'(\d+(?:\.\d+)?)', row_txt)
                                if num_m: fields["eps_pre"] = num_m.group(1)
                                
                            if "Diluted EPS" in row_txt and "eps_post" not in fields:
                                num_m = re.search(r'(\d+(?:\.\d+)?)', row_txt)
                                if num_m: fields["eps_post"] = num_m.group(1)

                            if ("P/E Ratio" in row_txt or "P/E at" in row_txt) and "pe_pre" not in fields:
                                num_m = re.search(r'(\d+(?:\.\d+)?)', row_txt)
                                if num_m: 
                                    fields["pe_pre"] = num_m.group(1)
                                    fields["pe_post"] = num_m.group(1)

                            if ("Return on Net Worth" in row_txt or "RoNW" in row_txt or "ROCE" in row_txt) and "roce" not in fields:
                                num_m = re.search(r'(\d+(?:\.\d+)?)\s*%', row_txt)
                                if num_m: fields["roce"] = num_m.group(1)

        # 2. Capital Structure Category Reservations (QIB, NII, RII)
        cap_pg = self.toc_map.get("CAPITAL_STRUCTURE")
        if not cap_pg:
            for pg in range(min(100, len(self.doc))):
                if "CAPITAL STRUCTURE" in self.doc[pg].get_text().upper():
                    cap_pg = pg + 1
                    break

        if cap_pg and cap_pg <= len(self.doc):
            cap_text = ""
            for p in range(cap_pg - 1, min(cap_pg + 4, len(self.doc))):
                cap_text += self.doc[p].get_text() + "\n"

            qib_m = re.search(r'(?:QIB|Qualified\s+Institutional)[^\n]*?(\d+(?:\.\d+)?)\s*%', cap_text, re.IGNORECASE)
            nii_m = re.search(r'(?:NII|Non\-Institutional|NIB)[^\n]*?(\d+(?:\.\d+)?)\s*%', cap_text, re.IGNORECASE)
            rii_m = re.search(r'(?:RII|Retail\s+Individual|RIB)[^\n]*?(\d+(?:\.\d+)?)\s*%', cap_text, re.IGNORECASE)

            if qib_m: fields["reservation_qib"] = qib_m.group(1)
            if nii_m: fields["reservation_nii"] = nii_m.group(1)
            if rii_m: fields["reservation_rii"] = rii_m.group(1)

        return fields

    def stage_4_structural_paragraph_slicer(self) -> Dict[str, Any]:
        """Slices clean introductory business & objective paragraphs from PDF text."""
        fields = {}

        # 1. About Company (from "OUR BUSINESS" section)
        biz_pg = self.toc_map.get("BUSINESS")
        if not biz_pg:
            for pg in range(min(250, len(self.doc))):
                if "OUR BUSINESS" in self.doc[pg].get_text().upper():
                    biz_pg = pg + 1
                    break

        if biz_pg and biz_pg <= len(self.doc):
            biz_text = self.doc[biz_pg - 1].get_text()
            paragraphs = [p.strip().replace('\n', ' ') for p in biz_text.split('\n\n') if len(p.strip()) > 80]
            if paragraphs:
                fields["about_company"] = paragraphs[0][:600]

        # 2. Objects of the Issue
        obj_pg = self.toc_map.get("OBJECTS")
        if not obj_pg:
            for pg in range(min(150, len(self.doc))):
                if "OBJECTS OF THE ISSUE" in self.doc[pg].get_text().upper() or "OBJECTS OF THE OFFER" in self.doc[pg].get_text().upper():
                    obj_pg = pg + 1
                    break

        if obj_pg and obj_pg <= len(self.doc):
            obj_text = self.doc[obj_pg - 1].get_text()
            bullets = re.findall(r'(?:^\d+[\.\)]\s*|\b[a-z][\.\)]\s*)([^\n]{15,120})', obj_text, re.MULTILINE)
            if bullets:
                fields["objectives"] = "\n".join([f"{i+1}. {b.strip()}" for i, b in enumerate(bullets[:4])])

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
                    if not self.extracted_fields.get("registrar_email") and row.get("email"):
                        self.extracted_fields["registrar_email"] = row["email"]
                    if not self.extracted_fields.get("registrar_website") and row.get("website"):
                        self.extracted_fields["registrar_website"] = row["website"]
                    break
        except Exception as e:
            print(f"[Registrar Verification Error]: {e}")

    async def run_pipeline(self, job_id: str, supabase_client: Any) -> Dict[str, Any]:
        """Run 100% In-House Zero-AI extraction pipeline (0ms API cost, 100% local)."""
        self.stage_0_triage()
        
        # Stage 1: Build SEBI TOC Map
        self.stage_1_toc_map_engine()

        # Stage 2: Cover Page & Issuer/Registrar Contacts
        s2 = self.stage_2_cover_page_and_contacts()
        self.extracted_fields.update(s2)

        # Stage 3: TableFinder Financial Grid Parser
        s3 = self.stage_3_table_finder_financials()
        self.extracted_fields.update(s3)

        if supabase_client:
            supabase_client.table("extraction_jobs").update({
                "partial_result": {**self.extracted_fields, "stage": 3}
            }).eq("id", job_id).execute()

        # Registrar Database Verification
        self.verify_registrar(supabase_client)

        # Stage 4: Structural Paragraph Slicer (Narrative Overview)
        s4 = self.stage_4_structural_paragraph_slicer()
        self.extracted_fields.update(s4)

        # Stage 5: SEBI Lot Math (100% Deterministic)
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
