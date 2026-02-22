"""
AIWA v2.2 - Knight Fintech Underwriting Chatbot
Optimized for low token rate limits (10K input tokens/min)

Key optimization: Dynamic node-specific prompts. Instead of sending ALL
node instructions every call (~2800 tokens), we send a compact base prompt
(~600 tokens) + only the current node's instructions (~150 tokens).
Conversation history is trimmed to last 6 messages + state summary.

Setup:  pip install flask flask-cors requests python-dotenv
Run:    python app.py
"""

import os
import re
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AIWA")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__, static_folder=".")
CORS(app)

# ── Config ─────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "insert")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
MODEL             = "claude-sonnet-4-20250514"
MAX_HISTORY_MSGS  = 6   # Only send last N messages to save tokens

# ═══════════════════════════════════════
# DEMO PERSONAS (Synthetic Data Engine)
# ═══════════════════════════════════════


PERSONAS = {

    "growth_sme": {
        "bureauScore": 742,
        "peerPercentile": 78,
        "industryRisk": "low",
        "gstCompliance": "strong",
        "avgBankBalance": 18.5,
        "bounceRate": 0.01,
        "profitMargin": 14.2,
        "sectorBenchmark": {
            "revenueGrowth": 22,
            "profitMarginMedian": 11
        }
    },

    "stressed_trader": {
        "bureauScore": 612,
        "peerPercentile": 34,
        "industryRisk": "medium",
        "gstCompliance": "average",
        "avgBankBalance": 4.2,
        "bounceRate": 0.11,
        "profitMargin": 5.1,
        "sectorBenchmark": {
            "revenueGrowth": 8,
            "profitMarginMedian": 9
        }
    },

    "new_age_startup": {
        "bureauScore": 705,
        "peerPercentile": 65,
        "industryRisk": "high",
        "gstCompliance": "limited",
        "avgBankBalance": 9.5,
        "bounceRate": 0.03,
        "profitMargin": -2,
        "sectorBenchmark": {
            "revenueGrowth": 40,
            "profitMarginMedian": 4
        }
    }
}

# ══════════════════════════════════════════════════════════════════════════════
#  GUARDRAILS CONFIG
# ══════════════════════════════════════════════════════════════════════════════

BLACKLISTED_INDUSTRIES = [
    "gambling", "betting", "casino", "lottery", "arms", "ammunition",
    "weapons", "explosives", "adult", "pornography", "escort",
    "crypto exchange", "ponzi", "chit fund", "mlm", "multi-level marketing",
    "tobacco", "narcotics", "cannabis", "shell company", "hawala",
]

MIN_LOAN_LAKH = 1
MAX_LOAN_LAKH = 200

PAN_REGEX    = re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$')
GSTN_REGEX   = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$')
CIN_REGEX    = re.compile(r'^[UL][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$')
DIN_REGEX    = re.compile(r'^[0-9]{8}$')
UDYAM_REGEX  = re.compile(r'^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$')
MOBILE_REGEX = re.compile(r'^[6-9][0-9]{9}$')
OTP_REGEX    = re.compile(r'^[0-9]{4,6}$')


# ══════════════════════════════════════════════════════════════════════════════
#  NODE DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

NODES = {
    "NODE_ONBOARD":          {"phase": 1, "desc": "Greet > Mobile > OTP > GST check"},
    "NODE_GST_ENTITY":       {"phase": 2, "desc": "GSTN > confirm name > CIN"},
    "NODE_GST_APPLICANT":    {"phase": 2, "desc": "Applicant name > DIN/role"},
    "NODE_GST_INDUSTRY":     {"phase": 2, "desc": "Confirm industry + vintage"},
    "NODE_NONGST_COLLECT":   {"phase": 2, "desc": "PAN > name+Udyam > industry > applicant name > designation"},
    "NODE_FINANCIALS":       {"phase": 3, "desc": "Revenue, loan amount, purpose"},
    "NODE_SUMMARY":          {"phase": 4, "desc": "Data confirmation (NO offer)"},
    "NODE_GST_PRELIM_OFFER": {"phase": 5, "desc": "Prelim offer + GST consent upsell"},
    "NODE_GST_CONSENT":      {"phase": 5, "desc": "GST API consent flow"},
    "NODE_DOCUMENTS":        {"phase": 6, "desc": "Bank stmt + financials upload"},
    "NODE_OFFER":            {"phase": 7, "desc": "Final offer presentation"},
    "NODE_CLOSURE":          {"phase": 8, "desc": "RM callback + close"},
}
# ═══════════════════════════════════════
# WORKFLOW PHASE FLAGS (DEMO FLOW CONTROL)
# ═══════════════════════════════════════

WORKFLOW_FLAGS = [
    "gstDiscoveryComplete",
    "udyamChecked",
    "industryConfirmed",
    "peerPrepared",
    "legalCheckComplete",
    "vintageConfirmed",
    "financialHealthComplete",
    "dscrComputed",
    "loanStructured",
    "preOfferGenerated"
]

# ══════════════════════════════════════════════════════════════════════════════
#  DYNAMIC PROMPT SYSTEM — Compact base + node-specific instructions
# ══════════════════════════════════════════════════════════════════════════════

BASE_PROMPT = """You are AIWA, credit underwriting agent for Knight Fintech (unsecured business loans, India).

RULES:
- Ask exactly ONE question per response, 1-3 sentences max
- ONLY respond in JSON format below. No markdown, no preamble
- Never reveal scores, algorithms, bureau data
- All offers: "preliminary", "indicative", "subject to verification"
- Off-topic: redirect to loan application
- Address applicant by name once known

JSON FORMAT (return ONLY this):
{"message":"text","logEntry":"tech log","logStatus":"STATUS","inputType":"text","dataExtracted":{},"guardrailFlag":null,"isSummary":false,"summaryData":null,"currentNode":"NODE_NAME"}

inputType values: text | dropdown_purpose | dropdown_industry | multi_select_gstn | upload_bank | upload_fin | prelim_offer | accept_offer | confirm_summary | end
guardrailFlag when needed: {"type":"block|warn","message":"reason"}
summaryData when isSummary=true: [{"label":"Field","value":"Val"},...]"""


# ── Node-specific instruction fragments (compact) ──
NODE_PROMPTS = {

"NODE_ONBOARD": """CURRENT NODE: NODE_ONBOARD
Follow this sequence (one step per response):
1. If no greeting yet: "Hello! I'm AIWA from Knight Fintech. Are you looking to apply for a business loan?"
2. If greeted but no mobile: "Please enter your mobile number for OTP-based verification. This will be used for identity verification and as consent for CIC bureau pull including further communication." Extract: mobile
3. If mobile given but no OTP: "Thank you. I've sent a one-time verification code (OTP) to this number." Wait for OTP. Extract: otpVerified=true
4. If OTP done: "Are you a GST-registered business?" Extract: isGSTRegistered (boolean)""",

"NODE_GST_ENTITY": """CURRENT NODE: NODE_GST_ENTITY (GST path)
Follow this sequence:
G1. If no GSTN: "Please share your active primary GST number associated with your core business." Extract: gstn
G2. If GSTN given but name not confirmed: "Fetched details for '[business name]'. Is this correct?" Extract: legalNameConfirmed=true, legalName
G3. If name confirmed but no CIN: "Being an MCA-registered business, could you please provide your CIN?" If user says not MCA registered, extract cinSkipped=true. Else extract: cin""",

"NODE_GST_APPLICANT": """CURRENT NODE: NODE_GST_APPLICANT (GST path - Applicant Identity)
THIS IS CRITICAL. You MUST collect applicant details.
G4. If no applicant name: "Great. While I build your business's primary profile, I'll need a few details about you, as the applicant. Please enter your full name (as per official records)." Extract: applicantName
G5. If name given but no DIN/role: "Please enter your DIN, if you are an active director or partner. If not, please specify your role as the authorised signatory." Extract: din_or_role""",

"NODE_GST_INDUSTRY": """CURRENT NODE: NODE_GST_INDUSTRY (GST path)
G6. Confirm industry and vintage: "This is great progress, [Name]. I have your line of business as [industry] and broad industry as [sub-industry], operating since [year]. Does that sound right?"
If confirmed: Extract industryConfirmed=true, industry, vintage""",

"NODE_NONGST_COLLECT": """CURRENT NODE: NODE_NONGST_COLLECT (Non-GST path)
N1. If no PAN: "Understood. Please enter the PAN for this entity." Extract: pan
N2. If PAN given but no Udyam: "Great. I have your name as '[name from PAN]'. If you have Udyam registration, please enter the same or enter No." Extract: legalName, udyam (or udyamSkipped=true)
N3. If Udyam done but no industry: "Please select your broad industry category." Set inputType:"dropdown_industry". Extract: industry
N4. If industry selected but no applicant name: "I'll need a few details about the person applying for this loan. Please enter the full name of the applicant (as per official records)." Extract: applicantName
N5. If name given but no designation: "What is your designation or role in this business? (e.g., Proprietor, Partner, Authorised Signatory)" Extract: designation""",

"NODE_FINANCIALS": """CURRENT NODE: NODE_FINANCIALS
GST path order: revenue first, then loan amount.
Non-GST path order: loan amount first, then revenue.

F1. Ask for the FIRST missing item based on path:
  - GST and no revenue: "Can you help with your revenue from operations in latest financial year? Do mention in lakh or cr."
  - Non-GST and no loan amount: "What is the loan amount required? (Please mention in Lakh or Cr)"
F2. Ask for the SECOND missing item:
  - GST and no loan amount: "Can you help with the loan amount? Do mention in lakh or cr."
  - Non-GST and no revenue: "What is your annual revenue from operations? (Approximate is fine)"
F3. If both revenue+loanAmount given but no purpose: "What is the purpose of this loan?" Set inputType:"dropdown_purpose"
Extract: revenue, loanAmount, loanPurpose""",

"NODE_SUMMARY": """CURRENT NODE: NODE_SUMMARY - Data Confirmation ONLY
DO NOT show any offer, pricing, interest rate, or approved amount here. This is ONLY data confirmation.

Present all collected data. Set isSummary:true, inputType:"confirm_summary".
Fill summaryData with ALL collected fields as [{"label":"Business Name","value":"..."},{"label":"GSTN/PAN","value":"..."},{"label":"Applicant","value":"..."},{"label":"Role/Designation","value":"..."},{"label":"Industry","value":"..."},{"label":"Vintage","value":"..."},{"label":"Revenue","value":"..."},{"label":"Loan Amount","value":"..."},{"label":"Purpose","value":"..."}]

Say: "Thank you. I've completed the initial assessment. Here's a summary of your application. Please confirm if everything looks correct."
On confirm: Extract summaryConfirmed=true""",

"NODE_GST_PRELIM_OFFER": """CURRENT NODE: NODE_GST_PRELIM_OFFER (GST path only)
Show preliminary offer AND suggest enhancement via GST consent:
"We have a preliminary offer:
- Approved Limit: [reasonable amount based on revenue/request in Indian Rupee formatting]
- Facility: Revolving Working Capital
- Tenure: 12 months
- Interest Rate: [rate]% per annum

Are you ok with the current offer? If not, our Virtual RM has suggested to enhance the offer based on GST consent. Would you like to proceed with the same?"
Set inputType:"prelim_offer". Extract: prelimOfferShown=true
If user accepts current: wantsGSTConsent=false. If user wants enhancement: wantsGSTConsent=true""",

"NODE_GST_CONSENT": """CURRENT NODE: NODE_GST_CONSENT (GST consent flow)
C1. If not started: "To provide GST API consent, log into GST Portal > 'My Profile' > 'Manage API Access', enable it, choose duration, and confirm. You'll need to enter username and OTP for each GST number. Shall we begin?" Extract: gstConsentStarted=true
C2. If started but no GSTNs selected: "We have found active GSTNs for this entity. Please select the GSTNs for consent:" Set inputType:"multi_select_gstn". Extract: selectedGSTNs
C3. If GSTNs selected but no username: "Please enter the username for [first GSTN]." Extract: gstUsername
C4. If username given: "We have sent an OTP on the mobile registered with this GSTIN." Extract: gstOtpVerified=true, gstConsentComplete=true""",

"NODE_DOCUMENTS": """CURRENT NODE: NODE_DOCUMENTS
D1. If bank stmt not prompted: "Great. While I assess your business on the aforementioned data, would you like to share your last 12 months bank statements from primary bank?" Set inputType:"upload_bank"
D2. If user said yes to bank: "Please use the '+' button to upload your last 12 months Bank Statement (e-PDF)." Wait for upload. Extract: bankStatementUploaded=true
D3. If bank done, financials not prompted: "Your maximum limit can be enhanced by uploading financial performance data. Would you like to upload your latest CA-certified financials?" Set inputType:"upload_fin"
D4. If user said yes to fin: "Please use the '+' button to upload the financial statements (P&L, Balance Sheet)." Extract: financialsUploaded=true
After both prompts answered: Extract documentsComplete=true""",

"NODE_OFFER": """CURRENT NODE: NODE_OFFER - Final Offer
"Analysis complete. Based on your requested amount and our analysis of your cash flows, financials, and industry benchmarks:

Loan Offer:
- Approved Limit: [amount in Indian Rupee formatting]
- Facility: [Term Loan or Revolving Working Capital]
- Tenure: 12 months, renewal based
- Interest Rate: [rate]% per annum

This is a preliminary and indicative offer, subject to final verification. Are you ok with this offer?"
Set inputType:"accept_offer". On accept: Extract offerAccepted=true""",

"NODE_CLOSURE": """CURRENT NODE: NODE_CLOSURE
"Thank you for choosing AIWA. You can expect a call back within 15 minutes."
Set inputType:"end" """,
}


# ══════════════════════════════════════════════════════════════════════════════
#  INPUT GUARDRAILS
# ══════════════════════════════════════════════════════════════════════════════

class InputGuardrail:

    @staticmethod
    def validate_mobile(v):
        c = re.sub(r'[\s\-\+]', '', v)
        if c.startswith('91') and len(c) == 12: c = c[2:]
        return (c, None) if MOBILE_REGEX.match(c) else (None, "Please enter a valid 10-digit Indian mobile number (starting with 6-9).")

    @staticmethod
    def validate_otp(v):
        c = v.strip()
        return (c, None) if OTP_REGEX.match(c) else (None, "Please enter a valid OTP (4-6 digits).")

    @staticmethod
    def validate_pan(v):
        c = v.strip().upper()
        return (c, None) if PAN_REGEX.match(c) else (None, "Invalid PAN. Should be 10 chars: 5 letters, 4 digits, 1 letter (e.g., ABCDE1234F).")

    @staticmethod
    def validate_gstn(v):
        c = v.strip().upper()
        return (c, None) if GSTN_REGEX.match(c) else (None, "Invalid GSTN. Enter a valid 15-character GSTN (e.g., 07AADCS8891H1ZU).")

    @staticmethod
    def validate_cin(v):
        c = v.strip().upper()
        return (c, None) if CIN_REGEX.match(c) else (None, "Invalid CIN. Should be 21 chars (e.g., U74300WB1987PTC041861).")

    @staticmethod
    def validate_din(v):
        c = v.strip()
        return (c, None) if DIN_REGEX.match(c) else (None, "Invalid DIN. Should be exactly 8 digits.")

    @staticmethod
    def validate_udyam(v):
        c = v.strip().upper()
        if c.lower() in ['no', 'na', 'not applicable', 'none', 'n/a']:
            return "NOT_APPLICABLE", None
        return (c, None) if UDYAM_REGEX.match(c) else (None, "Invalid Udyam format. Expected: UDYAM-XX-00-0000000. Enter 'No' if not applicable.")

    @staticmethod
    def parse_amount_lakhs(v):
        cr = re.search(r'([\d.]+)\s*(crore|cr)', v.lower())
        if cr: return float(cr.group(1)) * 100, None
        lk = re.search(r'([\d.]+)\s*(lakh|lac|l\b)', v.lower())
        if lk: return float(lk.group(1)), None
        try:
            n = float(re.sub(r'[^\d.]', '', v))
            if n > 0: return n, None
        except ValueError: pass
        return None, "Could not parse amount. Please specify in Lakh or Crore (e.g., '25 Lakh' or '1.5 Crore')."

    @staticmethod
    def check_loan_limits(lakhs):
        if lakhs < MIN_LOAN_LAKH:
            return {"type": "block", "message": f"Minimum loan amount is Rs.{MIN_LOAN_LAKH} Lakh."}
        if lakhs > MAX_LOAN_LAKH:
            return {"type": "block", "message": f"Maximum unsecured loan is Rs.{MAX_LOAN_LAKH} Lakh (Rs.2 Crore). For higher amounts, contact our secured lending team."}
        return None

    @staticmethod
    def check_blacklisted_industry(text):
        lower = text.lower()
        for term in BLACKLISTED_INDUSTRIES:
            if term in lower:
                return {"type": "block", "message": "Unable to process applications for this industry per credit policy."}
        return None

    @staticmethod
    def sanitize_pii(text):
        text = re.sub(r'\b[6-9]\d{9}\b', '****XXXX', text)
        text = re.sub(r'\b[A-Z]{5}\d{4}[A-Z]\b', 'XXXXX****X', text)
        return text


# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM ANALYZER — Workflow API Triggers
# ══════════════════════════════════════════════════════════════════════════════

class SystemAnalyzer:

    @staticmethod
    def analyze_and_trigger(node, data_extracted, user_data, response_parsed):
        triggers = []

        if node == "NODE_ONBOARD":
            if data_extracted.get("mobile"):
                triggers.append({"agent": "FRAUD_CHECK_AGENT", "action": "Scanning blacklist and mobile vintage.", "status": "Trigger OTP"})
            if data_extracted.get("otpVerified"):
                triggers.append({"agent": "OTP_VAL", "action": "Authenticating session token.", "status": "Verified"})
                triggers.append({"agent": "BUREAU_AGENT", "action": "Triggered CIC soft-pull\n> Task: Verify NTC\n> TRIGGER BUREAU_SCORE_AGENT and TRIANGULATION_MODEL features", "status": "BUREAU_PULL"})
            if data_extracted.get("isGSTRegistered") is not None:
                path = "GST_registered" if data_extracted["isGSTRegistered"] else "GST_MCA-non_registered"
                triggers.append({"agent": "ROUTING_LOGIC", "action": f"Directing to workflow={path}.", "status": "ROUTE"})

        elif node == "NODE_GST_ENTITY":
            if data_extracted.get("gstn"):
                triggers.append({"agent": "GSTN_AGENT", "action": "INITIALIZING MULTI-AGENT WORKFLOW\n\nSTEP 1: Extract Trade Name, Legal Name, active GSTNs, PAN\n\nSTEP 2: PARALLEL EXECUTION\n> LEGAL_AGENT: e-courts, NCLT, CIBIL suit, DRT\n> BEHAVIOURAL_AGENT: Internal DB lookup\n> EPFO_AGENT: Name-based EPFO extraction\n> NEWS_AGENT: Sentiment scan\n> RATINGS_AGENT: External rating pull", "status": "Data Phase 1 Complete"})
            if data_extracted.get("legalNameConfirmed"):
                triggers.append({"agent": "VALIDATION_AGENT", "action": "Cross-referencing trade name with GST database.", "status": "Verified"})
            if data_extracted.get("cin"):
                triggers.append({"agent": "MCA_ORCHESTRATOR", "action": "Syncing with MCA Master Data for director names and status.", "status": "MCA Sync Complete"})

        elif node == "NODE_GST_APPLICANT":
            if data_extracted.get("applicantName"):
                triggers.append({"agent": "FUZZY_MATCH_AGENT", "action": "Comparing name against Director Master list from MCA.", "status": "Identity Match"})
            if data_extracted.get("din_or_role"):
                triggers.append({"agent": "DIRECTOR_VERIFICATION_AGENT", "action": "Validating DIN state and eligibility.", "status": "Role Verified"})

        elif node == "NODE_GST_INDUSTRY":
            if data_extracted.get("industryConfirmed"):
                triggers.append({"agent": "SECTORAL_RISK_AGENT", "action": "Confirming vintage and industry model, trigger INDUSTRY_PEER_RISK_AGENT", "status": "Verification"})

        elif node == "NODE_NONGST_COLLECT":
            if data_extracted.get("pan"):
                triggers.append({"agent": "PAN_VAL_AGENT", "action": "Validating PAN status and ownership.", "status": "PAN Verified"})
            if data_extracted.get("udyam"):
                st = "Validate manual industry" if data_extracted["udyam"] == "NOT_APPLICABLE" else "UDYAM Verified"
                triggers.append({"agent": "UDYAM_AGENT", "action": f"Udyam lookup: {data_extracted['udyam']}", "status": st})
            if data_extracted.get("industry"):
                triggers.append({"agent": "SECTOR_AGENT", "action": "Benchmarking industry-specific risk parameters.", "status": "Sector Mapped"})
            if data_extracted.get("applicantName"):
                triggers.append({"agent": "IDENTITY_VERIFICATION_AGENT", "action": "Validating applicant name against PAN records.\n> Cross-referencing with entity ownership data", "status": "Applicant Logged"})
            if data_extracted.get("designation"):
                triggers.append({"agent": "SIGNATORY_AUTH_AGENT", "action": f"Recording applicant role: {data_extracted['designation']}.\n> Flagging for authorised signatory verification at disbursal", "status": "Designation Verified"})

        elif node == "NODE_FINANCIALS":
            if data_extracted.get("revenue"):
                triggers.append({"agent": "OFFER_INPUT", "action": "Extracting numerical features for financial benchmarking.", "status": "Revenue Mapped"})
            if data_extracted.get("loanAmount"):
                triggers.append({"agent": "BUREAU_AGENT", "action": "Trigger obligation impact analysis.\n> BRE_Agent: Eligibility computation initiated", "status": "Eligibility Computing"})
            if data_extracted.get("loanPurpose"):
                triggers.append({"agent": "CLASSIFICATION_AGENT", "action": "Mapping loan purpose to product sub-category.", "status": "Purpose Classified"})

        elif node == "NODE_SUMMARY":
            triggers.append({"agent": "SUMMARY_VALIDATION_AGENT", "action": "Finalizing profile metadata and data integrity check.", "status": "Summary Confirmed"})

        elif node == "NODE_GST_PRELIM_OFFER":
            triggers.append({"agent": "OFFER_STRATEGY_ENGINE", "action": "Preliminary offer generated. Virtual RM suggesting enhanced limit.", "status": "Prelim Offer"})
            if data_extracted.get("wantsGSTConsent"):
                triggers.append({"agent": "OFFER_STRATEGY_ENGINE", "action": "User opted for enhanced limit. Triggering upsell flow.", "status": "Upsell Opt-in"})

        elif node == "NODE_GST_CONSENT":
            if data_extracted.get("gstConsentStarted"):
                triggers.append({"agent": "GST_CONSENT_ORCHESTRATOR", "action": "Requesting digital consent token.", "status": "Consent Pending"})
            if data_extracted.get("selectedGSTNs"):
                triggers.append({"agent": "GSTN_SELECTION_AGENT", "action": "Mapping nodes for multi-GST aggregation.", "status": "Consent Mapping"})
            if data_extracted.get("gstUsername"):
                triggers.append({"agent": "AUTH_ORCHESTRATOR", "action": "Initializing session with GST portal.", "status": "Auth Started"})
            if data_extracted.get("gstOtpVerified"):
                triggers.append({"agent": "OTP_VALIDATION_AGENT", "action": "Verifying second factor for portal access.", "status": "GSTN Verified"})

        elif node == "NODE_DOCUMENTS":
            if data_extracted.get("bankStatementUploaded"):
                triggers.append({"agent": "BSA_AGENT, TRIANGULATION_MODEL", "action": "Extracting text & metadata.\n> Task: Transaction pattern analysis\n> Task: BSA health score and EWS alerts", "status": "BSA Processing"})
            if data_extracted.get("financialsUploaded"):
                triggers.append({"agent": "FINANCIAL_AGENT, PEER COMPARISON", "action": "Parsing P&L and Balance Sheet.\n> Compute financial features\n> Flag inconsistencies via forensic model", "status": "Financials Analyzed"})

        elif node == "NODE_OFFER":
            triggers.append({"agent": "CREDIT_POLICY_ENGINE", "action": "Offer finalization.\n> Composite Risk Score\n> Model Confidence check\n> LTV Compliance: Checked", "status": "Offer Finalized"})

        elif node == "NODE_CLOSURE":
            triggers.append({"agent": "WORKFLOW_EXIT", "action": "Dispatching lead to Salesforce.\n> RM notified via Slack", "status": "Lead Exported"})

        return triggers

# ══════════════════════════════════════════════════════════════════════════════
#  PERSONA SIMULATION ENGINE (DEMO ONLY)
# ══════════════════════════════════════════════════════════════════════════════

def run_persona_agents(node, persona_data, user_data):
    """
    Simulates backend underwriting agents using demo personas.
    Executes on EVERY reply just like production APIs.
    """

    simulated = {}

    # Bureau simulation
    if node == "NODE_ONBOARD":
        simulated["bureauScore"] = persona_data.get("bureauScore")

    # Industry peer comparison
    elif node == "NODE_GST_INDUSTRY":
        simulated["peerPercentile"] = persona_data.get("peerPercentile")
        simulated["industryRisk"] = persona_data.get("industryRisk")

    # Financial benchmarking
    elif node == "NODE_FINANCIALS":
        simulated["sectorBenchmark"] = persona_data.get("sectorBenchmark")
        simulated["profitMargin"] = persona_data.get("profitMargin")

    # Bank statement analytics
    elif node == "NODE_DOCUMENTS":
        simulated["avgBankBalance"] = persona_data.get("avgBankBalance")
        simulated["bounceRate"] = persona_data.get("bounceRate")

    return simulated

# ═══════════════════════════════════════
# DEMO MODEL TRIGGER ENGINE
# ═══════════════════════════════════════

def trigger_models(node, user_data):
    updates = {}
    logs = []

    # ───────────────── GST DISCOVERY ─────────────────
    if node == "NODE_GST_ENTITY":
        updates.update({
            "pan": "ABCDE1234F",
            "gstDiscoveryComplete": True
        })

        logs += [
            ("GSTN_AGENT", "PAN extracted from GSTN"),
            ("NEWS_MODEL", "Scanning news using legal & trade name"),
            ("LEGAL_API_SCORE", "Legal score computation running"),
            ("EPFO_MODEL", "EPFO filings search triggered"),
            ("BUREAU_MODEL", "Soft bureau pull initiated"),
        ]

    # ───────────────── INDUSTRY CONFIRM ─────────────────
    elif node == "NODE_GST_INDUSTRY":
        updates["peerPrepared"] = True
        updates["industryConfirmed"] = True

        logs.append((
            "PEER_ENGINE",
            "Peer comparison dataset prepared"
        ))

    # ───────────────── FINANCIAL HEALTH ─────────────────
    elif node == "NODE_FINANCIALS":
        pbid = float(user_data.get("pbid", 20))
        emi  = float(user_data.get("monthlyObligation", 5))

        dscr = pbid / (emi * 12) if emi else 1.0

        updates["dscr"] = round(dscr, 2)
        updates["dscrComputed"] = True
        updates["financialHealthComplete"] = True

        logs.append((
            "DSCR_ENGINE",
            f"DSCR computed = {dscr:.2f}x"
        ))

    # ───────────────── LOAN STRUCTURING ─────────────────
    #elif node == "NODE_FINANCIALS":
    if node == "NODE_FINANCIALS" and not user_data.get("loanStructured"):
        updates["loanStructured"] = True

        logs.append((
            "BRE_PRELIM_MODEL",
            "Preliminary eligibility computed (70% cap)"
        ))

    # ───────────────── PRE-OFFER ─────────────────
    elif node == "NODE_GST_PRELIM_OFFER":
        updates["preOfferGenerated"] = True

        logs.append((
            "ENSEMBLE_ENGINE",
            "Risk ensemble recomputed"
        ))

    return updates, logs

# ══════════════════════════════════════════════════════════════════════════════
#  OUTPUT GUARDRAILS
# ══════════════════════════════════════════════════════════════════════════════

class OutputGuardrail:
    BLOCKED = ["guaranteed", "100% approval", "definitely approved", "your credit score",
               "your bureau score", "internal risk score", "your cibil"]

    @staticmethod
    def sanitize(parsed):
        msg = parsed.get("message", "")
        for phrase in OutputGuardrail.BLOCKED:
            if phrase in msg.lower():
                msg = re.sub(re.escape(phrase), "[redacted]", msg, flags=re.IGNORECASE)
        it = parsed.get("inputType", "")
        if it in ["accept_offer", "prelim_offer"]:
            if "preliminary" not in msg.lower() and "indicative" not in msg.lower():
                msg += "\n\n*This is a preliminary and indicative offer, subject to final verification.*"
        parsed["message"] = msg
        for k in ["bureauScore", "riskScore", "modelConfidence", "compositeScore"]:
            parsed.get("dataExtracted", {}).pop(k, None)
        return parsed

    @staticmethod
    def ensure_structure(parsed):
        defaults = {"message": "Could you please repeat that?", "logEntry": "", "logStatus": "OK",
                     "inputType": "text", "dataExtracted": {}, "guardrailFlag": None,
                     "isSummary": False, "summaryData": None, "currentNode": None}
        for k, v in defaults.items():
            if k not in parsed: parsed[k] = v
        return parsed


# ══════════════════════════════════════════════════════════════════════════════
#  NODE ROUTER
# ══════════════════════════════════════════════════════════════════════════════

class NodeRouter:

    @staticmethod
    def determine_node(ud, msg_count):
    
        # ---------- PHASE 0 : ONBOARD ----------
        if not ud.get("mobile") or not ud.get("otpVerified") \
           or ud.get("isGSTRegistered") is None:
            return "NODE_ONBOARD"
    
        is_gst = ud.get("isGSTRegistered")
    
        # ---------- GST FLOW ----------
        if is_gst:
    
            if not ud.get("gstDiscoveryComplete"):
                return "NODE_GST_ENTITY"
    
            if not ud.get("udyamChecked"):
                return "NODE_GST_ENTITY"
    
            if not ud.get("industryConfirmed"):
                return "NODE_GST_INDUSTRY"
    
            if not ud.get("legalCheckComplete"):
                return "NODE_GST_APPLICANT"
    
        # ---------- NON GST FLOW ----------
        else:
            if not ud.get("nonGSTProfileComplete"):
                return "NODE_NONGST_COLLECT"
    
        # ---------- FINANCIAL HEALTH ----------
        if not ud.get("financialHealthComplete"):
            return "NODE_FINANCIALS"
    
        # ---------- LOAN STRUCTURING ----------
        if not ud.get("loanStructured"):
            return "NODE_FINANCIALS"
    
        # ---------- PRE OFFER ----------
        if not ud.get("preOfferGenerated"):
            return "NODE_GST_PRELIM_OFFER"
    
        return "NODE_CLOSURE"
        
# ══════════════════════════════════════════════════════════════════════════════
#  INPUT GUARDRAILS — Pre-LLM checks
# ══════════════════════════════════════════════════════════════════════════════

def run_input_guardrails(msg, user_data, node):
    msg = msg.strip()
    g = InputGuardrail()

    # Injection detection
    for pat in [r'ignore\s+(all\s+)?previous', r'forget\s+your\s+rules', r'system\s*prompt', r'jailbreak']:
        if re.search(pat, msg.lower()):
            return msg, _gr("I'm AIWA, your loan assistant. How can I help with your business loan?",
                           "Injection blocked", "BLOCKED", node, guardrail={"type": "warn", "message": "Unauthorized input."})

    ex = user_data.get("_expecting")

    if ex == "mobile":
        _, err = g.validate_mobile(msg)
        if err: return msg, _gr(err, "Invalid mobile", "FAIL", node)
    elif ex == "otp":
        _, err = g.validate_otp(msg)
        if err: return msg, _gr(err, "Invalid OTP", "FAIL", node)
    elif ex == "gstn":
        _, err = g.validate_gstn(msg)
        if err: return msg, _gr(err, "Invalid GSTN", "FAIL", node)
    elif ex == "cin":
        if msg.lower() not in ['no', 'na', 'not applicable', 'none', 'skip', 'not mca registered', 'not mca']:
            _, err = g.validate_cin(msg)
            if err: return msg, _gr(err, "Invalid CIN", "FAIL", node)
    elif ex == "din":
        if msg.strip().replace(' ', '').isdigit():
            _, err = g.validate_din(msg.strip())
            if err: return msg, _gr(err, "Invalid DIN", "FAIL", node)
    elif ex == "pan":
        _, err = g.validate_pan(msg)
        if err: return msg, _gr(err, "Invalid PAN", "FAIL", node)
    elif ex == "udyam":
        _, err = g.validate_udyam(msg)
        if err: return msg, _gr(err, "Invalid Udyam", "FAIL", node)
    elif ex == "loanAmount":
        amt, err = g.parse_amount_lakhs(msg)
        if err: return msg, _gr(err, "Bad amount", "FAIL", node)
        flag = g.check_loan_limits(amt)
        if flag: return msg, _gr(flag["message"], f"Loan {amt}L out of range", "BLOCKED", node, guardrail=flag)
    elif ex == "industry":
        flag = g.check_blacklisted_industry(msg)
        if flag: return msg, _gr(flag["message"], f"Blacklisted: {msg}", "BLOCKED", "NODE_CLOSURE", guardrail=flag, it="end")

    return msg, None


def _gr(message, log, status, node, guardrail=None, it="text"):
    return {"message": message, "logEntry": f"GUARDRAIL: {log}", "logStatus": status,
            "inputType": it, "dataExtracted": {}, "guardrailFlag": guardrail,
            "isSummary": False, "summaryData": None, "currentNode": node}


# ══════════════════════════════════════════════════════════════════════════════
#  BUILD DYNAMIC PROMPT — Only current node's instructions
# ══════════════════════════════════════════════════════════════════════════════

def build_system_prompt(current_node, user_data, has_bank, has_fin, msg_count):
    """Build compact system prompt: base + current node only."""
    node_inst = NODE_PROMPTS.get(current_node, f"CURRENT NODE: {current_node}\nFollow the flow.")

    # Compact state (exclude internal _ keys)
    clean = {k: v for k, v in user_data.items() if not k.startswith('_')}

    state = (
        f"\n\n[STATE] Node:{current_node} | GST:{user_data.get('isGSTRegistered','?')} | "
        f"Bank:{has_bank} | Fin:{has_fin} | Msgs:{msg_count}\n"
        f"Data: {json.dumps(clean, default=str)}"
    )

    return BASE_PROMPT + "\n\n" + node_inst + state


def trim_messages(messages, max_msgs=MAX_HISTORY_MSGS):
    """Keep only the last N messages to save tokens."""
    if len(messages) <= max_msgs:
        return messages
    return messages[-max_msgs:]


# ══════════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        body      = request.get_json()
        messages  = body.get("messages", [])
        user_data = body.get("userData", {})
        has_bank  = body.get("hasBank", False)
        has_fin   = body.get("hasFin", False)

        # ─────────────────────────────────────────
        # DEMO PERSONA ATTACHMENT (ADD THIS BLOCK)
        # ─────────────────────────────────────────
        persona_key = user_data.get("_persona", "growth_sme")

        # fallback safety
        if persona_key not in PERSONAS:
            persona_key = "growth_sme"

        persona_data = PERSONAS[persona_key]
        user_data["_persona"] =persona_key

        logger.info(f"PERSONA ACTIVE: {persona_key}")
        # ─────────────────────────────────────────

        # ── Determine current node ──
        current_node = NodeRouter.determine_node(user_data, len(messages))
        logger.info(
            f"NODE: {current_node} | keys: {[k for k in user_data if not k.startswith('_')]}"
        )
        
        # ── Last user message ──
        last_msg = ""
        if messages and messages[-1].get("role") == "user":
            last_msg = messages[-1].get("content", "")

        # ── Input guardrails ──
        if last_msg:
            _, gr_resp = run_input_guardrails(last_msg, user_data, current_node)
            if gr_resp:
                logger.warning(f"GUARDRAIL bypass at {current_node}")
                gr_resp["systemTriggers"] = SystemAnalyzer.analyze_and_trigger(
                    current_node, gr_resp.get("dataExtracted", {}), user_data, gr_resp)
                return jsonify(gr_resp)

        # ── Build dynamic prompt (only current node) ──
        system_prompt = build_system_prompt(current_node, user_data, has_bank, has_fin, len(messages))

        # ── Trim history ──
        trimmed = trim_messages(messages)

        # ── Call Claude API ──
        headers = {
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json"
        }
        payload = {
            "model":      MODEL,
            "max_tokens": 800,
            "system":     system_prompt,
            "messages":   trimmed
        }

        resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=30)

        if resp.status_code == 401:
            return jsonify({"error": "Invalid API key. Set ANTHROPIC_API_KEY in .env or app.py"}), 401
        if resp.status_code == 429:
            return jsonify({
                "message": "I'm processing. Please wait a moment and try again.",
                "logEntry": "RATE_LIMIT", "logStatus": "RATE_LIMIT", "inputType": "text",
                "dataExtracted": {}, "guardrailFlag": None, "isSummary": False,
                "summaryData": None, "currentNode": current_node, "systemTriggers": [],
            }), 200
        if not resp.ok:
            logger.error(f"API error {resp.status_code}: {resp.text[:300]}")
            return jsonify({"error": f"Anthropic API error: {resp.text[:200]}"}), 500

        data = resp.json()
        raw = data["content"][0]["text"]

        # ── Parse JSON ──
        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"): clean = clean[4:]
        clean = clean.strip()

        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if m: parsed = json.loads(m.group())
            else: raise ValueError(f"No JSON: {clean[:200]}")

        parsed = OutputGuardrail.ensure_structure(parsed)
        parsed["currentNode"] = current_node
        parsed = OutputGuardrail.sanitize(parsed)

        # ----- DEMO MODEL EXECUTION -----
        
        updates, persona_logs = trigger_models(
            current_node,
            user_data
        )
        
        parsed["dataExtracted"].update(updates)
        simulated = run_persona_agents(
            current_node,
            persona_data,
            user_data
        )
        
        parsed["dataExtracted"].update(simulated)
        return safe_process(current_node, parsed, user_data)
        
        return safe_process(current_node, parsed, user_data, persona_logs)

# ═══════════════════════════════════════
# DEMO MODEL TRIGGER ENGINE
# ═══════════════════════════════════════


# ═══════════════════════════════════════
# REQUEST PROCESSOR (SIMPLIFIED)
# ═══════════════════════════════════════

def process_request(current_node, parsed, user_data, persona_logs):
    """
    Processes node logic and attaches system triggers
    """
    
    # Build trigger list
    triggers = []

    for agent, action in persona_logs:
        triggers.append({
            "agent": agent,
            "action": action,
            "status": "SIMULATED"
        })

    # Run system analyzer
    system_triggers = SystemAnalyzer.analyze_and_trigger(
        current_node,
        parsed.get("dataExtracted", {}),
        user_data,
        parsed
    )

    triggers.extend(system_triggers)
    parsed["systemTriggers"] = triggers

    logger.info(
        f"OK: node={current_node} "
        f"type={parsed.get('inputType')} "
        f"triggers={len(triggers)}"
    )

    return parsed


# ═══════════════════════════════════════
# ERROR HANDLING WRAPPER
# ═══════════════════════════════════════

def safe_process(current_node, parsed, user_data, persona_logs):
    try:
        return jsonify(process_request(current_node, parsed, user_data, persona_logs))

    except ValueError as e:
        logger.error(f"Parse error: {e}")
        return jsonify({
            "message": "Processing issue. Please repeat your response.",
            "logEntry": f"PARSE_ERROR: {e}",
            "logStatus": "ERROR",
            "inputType": "text",
            "dataExtracted": {},
            "guardrailFlag": None,
            "isSummary": False,
            "summaryData": None,
            "currentNode": "UNKNOWN",
            "systemTriggers": []
        }), 200

    except requests.Timeout:
        return jsonify({
            "message": "Service is slow. Please try again.",
            "logEntry": "TIMEOUT",
            "logStatus": "TIMEOUT",
            "inputType": "text",
            "dataExtracted": {},
            "guardrailFlag": None,
            "isSummary": False,
            "summaryData": None,
            "currentNode": "UNKNOWN",
            "systemTriggers": []
        }), 200

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════
# HEALTH & META ENDPOINTS
# ═══════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "version": "2.2.0",
        "model": MODEL,
        "optimization": "Dynamic node-specific prompts + history trimming",
        "nodes": list(NODES.keys()),
        "max_history_msgs": MAX_HISTORY_MSGS,
    })


@app.route("/api/nodes")
def get_nodes():
    return jsonify(NODES)


# ═══════════════════════════════════════
# APP ENTRY POINT
# ═══════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  AIWA v2.2 - Knight Fintech (Token-Optimized)")
    print("=" * 60)
    print(f"  Model:     {MODEL}")
    print(f"  Nodes:     {len(NODES)}")
    print(f"  History:   Last {MAX_HISTORY_MSGS} messages (trimmed)")
    print(f"  Prompt:    Dynamic (base ~600tok + node ~150tok)")
    print(f"  Local:     http://localhost:5000")
    print("=" * 60)
    print("  GST Flow:")
    print("    Greet>Mobile>OTP>GSTN>Name>CIN>Applicant>DIN>Industry")
    print("    >Revenue>Loan>Purpose>Summary>PrelimOffer>Consent>Docs>Offer>Close")
    print("  Non-GST Flow:")
    print("    Greet>Mobile>OTP>PAN>Udyam>Industry>ApplicantName>Designation")
    print("    >Loan>Revenue>Purpose>Summary>Docs>Offer>Close")
    print("=" * 60 + "\n")

    app.run(debug=True, port=5000)
