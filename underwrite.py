"""
AIWA v3.0 - Knight Fintech Underwriting Chatbot
CORRECTED FLOW - Applicant vs Business Entity distinction
Bank statements ONLY asked if user chooses to upgrade eligibility
"""

import os
import re
import json
import logging
import requests
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AIWA")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__, static_folder=".")

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

# # ══════════════════════════════════════════════════════════════════════════════
# #  CONFIG
# # ══════════════════════════════════════════════════════════════════════════════

# ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
# MODEL             = "claude-sonnet-4-20250514"
# MAX_HISTORY_MSGS  = 6


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

import os
from dotenv import load_dotenv

# Load .env file
load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
MODEL             = "claude-sonnet-4-20250514"
MAX_HISTORY_MSGS  = 6

# Stop app if key missing
if not ANTHROPIC_API_KEY:
    raise ValueError("❌ ANTHROPIC_API_KEY not found. Add it to your .env file.")


# ══════════════════════════════════════════════════════════════════════════════
#  PERSONAS (for demo BRE simulation)
# ══════════════════════════════════════════════════════════════════════════════

PERSONAS = {
    "growth_sme": {
        "bureauScore": 742,
        "peerPercentile": 78,
        "industryRisk": "low",
        "gstCompliance": "strong",
        "avgBankBalance": 18.5,
        "bounceRate": 0.01,
        "profitMargin": 14.2,
    },
    "stressed_trader": {
        "bureauScore": 612,
        "peerPercentile": 34,
        "industryRisk": "medium",
        "gstCompliance": "average",
        "avgBankBalance": 4.2,
        "bounceRate": 0.11,
        "profitMargin": 5.1,
    },
    "new_age_startup": {
        "bureauScore": 705,
        "peerPercentile": 65,
        "industryRisk": "high",
        "gstCompliance": "limited",
        "avgBankBalance": 9.5,
        "bounceRate": 0.03,
        "profitMargin": -2,
    },
}

# ══════════════════════════════════════════════════════════════════════════════
#  VALIDATION PATTERNS
# ══════════════════════════════════════════════════════════════════════════════

PAN_REGEX    = re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$')
GSTN_REGEX   = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$')
CIN_REGEX    = re.compile(r'^[UL][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$')
UDYAM_REGEX  = re.compile(r'^UDYAM-[A-Z]{2}-[0-9]{2}-[0-9]{7}$')
MOBILE_REGEX = re.compile(r'^[6-9][0-9]{9}$')
OTP_REGEX    = re.compile(r'^[0-9]{4,6}$')

# ══════════════════════════════════════════════════════════════════════════════
#  BASE PROMPT
# ══════════════════════════════════════════════════════════════════════════════

BASE_PROMPT = """You are AIWA, credit underwriting agent for Knight Fintech (unsecured business loans, India).

CRITICAL RULES - YOU MUST FOLLOW THESE EXACTLY:
- Ask EXACTLY ONE question per response. NEVER ask multiple questions.
- ONLY ask for data that the current step requires. DO NOT skip ahead.
- Follow the SEQUENCE in your node instructions STEP BY STEP. Do not deviate.
- DO NOT extract data that you have not explicitly asked for in this response.
- If the step says "ask X", you MUST ask X and ONLY X. Nothing else.
- ONLY respond in JSON format below. No markdown, no preamble, no extra text.
- Never reveal scores, algorithms, bureau data to user.
- All offers: "preliminary", "indicative", "subject to verification".
- Address applicant by name once known.

JSON FORMAT (return ONLY this):
{"message":"text","logEntry":"tech log","logStatus":"STATUS","inputType":"text","dataExtracted":{},"guardrailFlag":null,"currentNode":"NODE_NAME"}

inputType values: text | dropdown_purpose | dropdown_industry | dropdown_lob | offer_with_options | upload_bank | upload_fin | end
guardrailFlag when needed: {"type":"block|warn","message":"reason"}

CRITICAL: You are in a SEQUENTIAL WORKFLOW. Each step must be completed before moving to the next. DO NOT skip steps."""

# ══════════════════════════════════════════════════════════════════════════════
#  NODE PROMPTS - Complete rewrite for correct flow
# ══════════════════════════════════════════════════════════════════════════════

NODE_PROMPTS = {
    
    "NODE_ONBOARD": """CURRENT NODE: NODE_ONBOARD

CRITICAL: Follow this EXACT sequence. ONE step per response.

IMPORTANT: Applicant = the PERSON applying. Business = the ENTITY being underwritten. These are DIFFERENT.

CURRENT STEP DETERMINATION:
- If NO greeting given yet: Execute Step 1 ONLY
- If greeted BUT NO mobile: Execute Step 2 ONLY
- If mobile exists BUT NO otpVerified: Execute Step 3 ONLY
- If otpVerified BUT NO applicantName: Execute Step 4 ONLY
- If applicantName BUT isGSTRegistered is None: Execute Step 5 ONLY

STEP 1 - Initial Greeting:
If this is the very first interaction (no previous messages):
Ask: "Hello! I'm AIWA from Knight Fintech. Are you looking to apply for a business loan?"
Extract ONLY: greeting=true
STOP after this.

STEP 2 - Mobile Number:
Ask: "Great! Please enter your mobile number for OTP verification."
Extract ONLY: mobile
DO NOT extract anything else. STOP after this.

STEP 3 - OTP:
Ask: "I've sent an OTP to your mobile. Please enter it."
Extract ONLY: otpVerified=true
DO NOT extract anything else. STOP after this.

STEP 4 - Applicant Name:
Ask: "Thank you! May I know your name?"
Extract ONLY: applicantName
DO NOT extract anything else. STOP after this.

STEP 5 - GST Registration Check:
Ask: "Thank you, [applicantName]! Is your business GST-registered?"
Extract ONLY: isGSTRegistered (boolean - true/false)
DO NOT extract anything else. STOP after this.

CRITICAL: NEVER ask multiple questions. NEVER skip steps. ONE question per response ONLY.
DO NOT ask for business name in this node. Business name comes AFTER GSTN in NODE_GST_ENTITY.
""",

    "NODE_GST_ENTITY": """CURRENT NODE: NODE_GST_ENTITY (GST Business - Entity Details)

CRITICAL: Follow this EXACT sequence. Complete ONE step per response. DO NOT skip ahead.
IMPORTANT: Always address the applicant by their name (applicantName) when speaking to them.

CURRENT STEP DETERMINATION:
- If NO gstn in userData: Execute Step 1 ONLY
- If gstn exists BUT NO businessName: Execute Step 2 ONLY
- If businessName exists BUT NO udyam AND NO udyamSkipped: Execute Step 3 ONLY
- If (udyam OR udyamSkipped) BUT NO industry: Execute Step 4 ONLY
- If industry exists BUT NO lineOfBusiness: Execute Step 5 ONLY
- If lineOfBusiness exists BUT NO mcaConfirmation AND NO cinSkipped: Execute Step 6 ONLY
- If mcaConfirmation=true BUT NO cin: Execute Step 7 ONLY
- If (cin OR cinSkipped OR mcaConfirmation=false) exists: Extract gstEntityComplete=true and STOP

STEP 1 - GSTN Collection:
Ask: "Please share your primary GST number (GSTN)."
Extract ONLY: gstn
DO NOT extract businessName. DO NOT extract anything else. STOP after this.

STEP 2 - Business Name (ONLY asked ONCE here):
Ask: "Thank you. While we fetch your details from CIC bureau and GSTN, please provide the complete legal business name."
Extract ONLY: businessName
DO NOT extract anything else. STOP after this.

STEP 3 - Udyam:
Ask: "Do you have a Udyam registration number? If yes, please share it. If not, type 'No'."
If user provides Udyam number: Extract ONLY: udyam
If user says no/skip/none: Extract ONLY: udyamSkipped=true
DO NOT extract anything else. STOP after this.

STEP 4 - Industry:
Ask: "Please select your business industry category."
Set inputType: "dropdown_industry"
Extract ONLY: industry
DO NOT extract anything else. STOP after this.

STEP 5 - Line of Business:
Ask: "Please select your line of business."
Set inputType: "dropdown_lob"
Extract ONLY: lineOfBusiness
DO NOT extract anything else. STOP after this.

STEP 6 - MCA/CIN Check (CONDITIONAL - READ CAREFULLY):

CRITICAL PRE-CHECK - DO THIS FIRST:
1. Look at the businessName value in userData
2. Convert it to lowercase
3. Check if it contains ANY of these EXACT strings:
   - 'private limited'
   - 'pvt ltd'
   - 'pvt. ltd'
   - ' limited' (with space before)
   - ' ltd' (with space before)
   - ' ltd.' (with space before)
   - 'llp'
   - 'l.l.p'

EXAMPLES:
- "Knight FIntech" → NO keywords found → Extract cinSkipped=true, STOP
- "ABC Traders" → NO keywords found → Extract cinSkipped=true, STOP
- "XYZ Services" → NO keywords found → Extract cinSkipped=true, STOP
- "Knight Fintech Pvt Ltd" → HAS "pvt ltd" → Ask for MCA confirmation
- "ABC Private Limited" → HAS "private limited" → Ask for MCA confirmation
- "XYZ Ltd" → HAS " ltd" → Ask for MCA confirmation

IF businessName contains NONE of the keywords above:
  DO NOT ask any question
  Extract ONLY: cinSkipped=true
  STOP immediately - move to completion

IF businessName DOES contain at least one keyword:
  Ask: "I understand that your business is MCA registered. Can you confirm the same?"
  Wait for response:
    - If user says YES/yes/confirm/correct: Extract ONLY: mcaConfirmation=true, STOP
    - If user says NO/no/not registered: Extract ONLY: mcaConfirmation=false, cinSkipped=true, STOP

STEP 7 - CIN Collection (Only if mcaConfirmation=true):
Ask: "Please share your CIN (Corporate Identification Number)."
Extract ONLY: cin
STOP after this.

COMPLETION:
After ALL steps above are done, extract: gstEntityComplete=true
This signals to move to the next node.

CRITICAL: 
- NEVER ask multiple questions in one response
- NEVER skip steps
- NEVER extract data you didn't ask for
- Business name is asked ONLY ONCE in Step 2
- CIN is asked ONLY if businessName has MCA keywords AND user confirms
- If businessName has NO MCA keywords, just set cinSkipped=true and move on
""",

    "NODE_NONGST_ENTITY": """CURRENT NODE: NODE_NONGST_ENTITY (Non-GST Business)

CRITICAL: Follow EXACT sequence. ONE step per response.

CURRENT STEP DETERMINATION:
- If NO pan: Execute Step 1 ONLY
- If pan exists BUT NO businessName: Execute Step 2 ONLY  
- If businessName exists BUT NO industry: Execute Step 3 ONLY
- If industry exists BUT NO lineOfBusiness: Execute Step 4 ONLY
- If all exist: Extract nonGstEntityComplete=true

STEP 1 - PAN:
Ask: "Please share your business PAN."
Extract ONLY: pan
STOP after this.

STEP 2 - Business Name:
Ask: "Please provide your complete business entity name."
Extract ONLY: businessName
STOP after this.

STEP 3 - Industry:
Ask: "Please select your business industry category."
Set inputType: "dropdown_industry"
Extract ONLY: industry
STOP after this.

STEP 4 - Line of Business:
Ask: "Please select your line of business."
Set inputType: "dropdown_lob"
Extract ONLY: lineOfBusiness
STOP after this.

COMPLETION:
After all completed, extract: nonGstEntityComplete=true

CRITICAL: ONE question per response. NEVER skip steps. NEVER extract data you didn't ask for.
""",

    "NODE_FINANCIALS": """CURRENT NODE: NODE_FINANCIALS (Financial Questions - Common for all)

CRITICAL: Ask ONE question at a time. Follow this EXACT sequence. DO NOT skip steps.

CURRENT STEP DETERMINATION:
- If NO vintage: Execute Step 1 ONLY
- If vintage exists BUT NO revenue: Execute Step 2 ONLY
- If revenue exists BUT NO operatingProfit: Execute Step 3 ONLY
- If operatingProfit exists BUT NO monthlyEMI: Execute Step 4 ONLY
- If monthlyEMI exists BUT NO loanAmount: Execute Step 5 ONLY
- If loanAmount exists BUT NO loanPurpose: Execute Step 6 ONLY
- If all exist: Extract financialsComplete=true

STEP 1 - Vintage:
Ask: "How many years has the business been operational?"
Extract ONLY: vintage
STOP after this.

STEP 2 - Revenue:
Ask: "What is your annual revenue from operations? (in lakhs)"
Extract ONLY: revenue
STOP after this.

STEP 3 - Operating Profit:
Ask: "What is your operating profit before interest, depreciation and tax? (in lakhs)"
Extract ONLY: operatingProfit
STOP after this.

STEP 4 - Monthly EMI:
Ask: "What is your total current monthly EMI payout from all existing loans? (in ₹)"
Extract ONLY: monthlyEMI
STOP after this.

STEP 5 - Loan Amount:
Ask: "How much loan amount are you requesting? (in lakhs)"
Extract ONLY: loanAmount
STOP after this.

STEP 6 - Loan Purpose:
Ask: "Please select the purpose of this loan."
Set inputType: "dropdown_purpose"
Extract ONLY: loanPurpose
STOP after this.

COMPLETION:
After ALL steps, extract: financialsComplete=true

CRITICAL: ONE question per response. NEVER skip ahead. NEVER extract data you didn't ask for in THIS response.
""",

    "NODE_OFFER": """CURRENT NODE: NODE_OFFER (Generate and Present Offer)

TASK: Generate offer based on collected data and present it conversationally IN THE SAME TEXT BOX.

OFFER CALCULATION:
1. Approved Amount = 60% of requested loanAmount
2. If loanPurpose contains "inventory" OR "working capital" OR "cash management":
   - Type: Revolving Credit
   - Tenure: Annual (renewal based)
   - Interest: 15-18% p.a. (use persona credit score range)
   - Show: Monthly interest on 100% utilization
3. Else:
   - Type: Term Loan
   - Tenure: 12-36 months (based on amount)
   - Interest: 15-18% p.a.
   - Show: Monthly EMI

PRESENT OFFER IN MESSAGE like this:

For Revolving Credit:
"Based on your application, here's your preliminary loan offer:

✓ Loan Amount: ₹XX.XX lakhs (60% of requested)
✓ Loan Type: Revolving Credit
✓ Interest Rate: XX% p.a.
✓ Tenure: Annual (renewal based)
✓ Monthly Interest: ₹XX,XXX (on 100% utilization)

This is subject to final verification. You can modify the requested amount if needed.

What would you like to do next?"

For Term Loan:
"Based on your application, here's your preliminary loan offer:

✓ Loan Amount: ₹XX.XX lakhs (60% of requested)
✓ Loan Type: Term Loan
✓ Interest Rate: XX% p.a.
✓ Tenure: XX months
✓ Monthly EMI: ₹XX,XXX

This is subject to final verification. You can modify the requested amount if needed.

What would you like to do next?"

Then set inputType: "offer_with_options" to show three buttons:
- For GST businesses: 3 options
- For Non-GST: 2 options only (no GSTN consent option)

Extract: offerGenerated=true, offerAmount=[calculated approved amount]
""",

    "NODE_UPGRADE_DOCS": """CURRENT NODE: NODE_UPGRADE_DOCS (User chose to upgrade by sharing documents)

SEQUENCE:

Step 1: If no bank statement uploaded:
"Please upload your bank statements for the last 12 months."
Set inputType: "upload_bank"
Wait for upload confirmation

Step 2: If bank uploaded but no financials:
"Please upload CA certified financials for the latest financial year."
Set inputType: "upload_fin"
Wait for upload confirmation

After BOTH uploaded:
- Message: "Thank you. Our system is processing your documents and model output is being sent for internal review. Please wait..."
- Extract: docsUploaded=true
""",

    "NODE_REVISED_OFFER": """CURRENT NODE: NODE_REVISED_OFFER (Present upgraded offer)

TASK: Calculate revised offer based on documents uploaded

REVISED CALCULATION:
- New Amount = Previous approved amount * 1.15 (15% increase from previous offer)
- Recalculate monthly payout based on new amount
- Maintain SAME loan type, tenure, and structure as original
- Interest rate stays the same

CRITICAL: Maintain EXACT same loan structure as original offer.

If original was Revolving Credit:
"Great news! Based on your documents, here's your upgraded offer:

✓ Loan Amount: ₹XX.XX lakhs (15% increase from previous ₹YY.YY lakhs)
✓ Loan Type: Revolving Credit
✓ Interest Rate: XX% p.a. (same as before)
✓ Tenure: Annual (renewal based)
✓ Monthly Interest: ₹XX,XXX (on 100% utilization)

Would you like to accept this offer?"

If original was Term Loan:
"Great news! Based on your documents, here's your upgraded offer:

✓ Loan Amount: ₹XX.XX lakhs (15% increase from previous ₹YY.YY lakhs)
✓ Loan Type: Term Loan
✓ Interest Rate: XX% p.a. (same as before)
✓ Tenure: XX months
✓ Monthly EMI: ₹XX,XXX

Would you like to accept this offer?"

Set inputType: "text" (they can accept or ask questions)
Extract: revisedOfferGenerated=true
""",

    "NODE_GSTN_CONSENT": """CURRENT NODE: NODE_GSTN_CONSENT (GSTN Consent Flow)

SEQUENCE - Process with confirmation:

Step 1: If gstnList not generated yet:
"Great! Let me get the list of your active GSTNs."
Then immediately say:
"I found [X] active GSTN(s):
1. [GSTN1]  (your primary GSTN)
2. [GSTN2]
(etc.)

Are these correct?"

Extract: gstnList=[list including primary], gstnListConfirmed=false, totalGstns=[count]

Step 2: If waiting for confirmation (gstnListConfirmed=false):
- If user says YES/correct/yes that's right/confirmed:
  Extract: gstnListConfirmed=true, currentGstnIndex=0
  Say: "Perfect! Now, for each GSTN I shall request you to enter the generated username followed by OTP which you will receive. This activity has to be done for each GSTN. Let's begin.
  
  Please provide the username for {first GSTN from list}."

- If user says NO/not correct/wrong/incorrect:
  Say: "Please provide your active GSTN numbers separated by commas."
  Extract: awaitingUserGstnList=true

Step 3: If awaitingUserGstnList=true and user provides comma-separated list:
Parse the comma-separated GSTNs, update gstnList
Extract: gstnList=[new list], gstnListConfirmed=true, currentGstnIndex=0, totalGstns=[count]
Say: "Thank you. Now, for each GSTN I shall request you to enter the generated username followed by OTP which you will receive. This activity has to be done for each GSTN. Let's begin.

Please provide the username for {first GSTN from list}."

Step 4: If gstnListConfirmed=true and waiting for username for current GSTN:
Extract: gstnUsername=[username]
Then say: "Thank you. Please enter the OTP sent to you."

Step 5: If waiting for OTP:
Extract: gstnOtp=[otp], currentGstnIndex=[increment by 1]

After OTP, check if more GSTNs remaining:
- If YES (currentGstnIndex < totalGstns):
  Say: "Thank you. Please provide the username for {next GSTN from list}."
  
- If NO (all GSTNs processed):
  Say: "Thank you! We are now processing your GST 3B and 2A returns for the last 24 months. Please wait..."
  Extract: allGstnsProcessed=true
  Set inputType: "text"
  
Step 6: After processing complete:
Wait 3 seconds, then present the enhanced offer.

CRITICAL: Enhanced offer MUST maintain EXACT same loan structure as original offer.

If original was Revolving Credit:
"Processing complete! Here is your updated offer based on GST data analysis:

✓ Loan Amount: ₹XX.XX lakhs (115% of your requested amount)
✓ Loan Type: Revolving Credit
✓ Interest Rate: XX.X% p.a. (reduced by 50 bps)
✓ Tenure: Annual (renewal based)
✓ Monthly Interest: ₹XX,XXX (on 100% utilization)

This enhanced offer is based on verified GST compliance data. Would you like to accept?"

If original was Term Loan:
"Processing complete! Here is your updated offer based on GST data analysis:

✓ Loan Amount: ₹XX.XX lakhs (115% of your requested amount)
✓ Loan Type: Term Loan
✓ Interest Rate: XX.X% p.a. (reduced by 50 bps)
✓ Tenure: XX months
✓ Monthly EMI: ₹XX,XXX

This enhanced offer is based on verified GST compliance data. Would you like to accept?"

Extract: gstnConsentOfferGenerated=true
""",

    "NODE_CLOSURE": """CURRENT NODE: NODE_CLOSURE (Final steps)

If user accepted offer:
"Thank you for accepting! Our relationship manager will contact you within 24 hours to complete the process. Have a great day!"
Set inputType: "end"

If user chose GSTN consent option:
"Thank you! Our relationship manager will contact you shortly to proceed with GSTN consent-based eligibility upgrade."
Set inputType: "end"

Extract: applicationComplete=true
"""
}

# ══════════════════════════════════════════════════════════════════════════════
#  SYSTEM ANALYZER - BRE Triggers for each user input
# ══════════════════════════════════════════════════════════════════════════════

class SystemAnalyzer:
    @staticmethod
    def analyze_and_trigger(node, data_extracted, user_data, response_parsed):
        """Generate BRE triggers based on what data was just extracted"""
        triggers = []
        
        # GSTN entered → Multiple triggers
        if 'gstn' in data_extracted:
            triggers.append({
                "agent": "BUREAU_AGENT",
                "action": "Triggered bureau pull via PAN extracted from GSTN",
                "status": "RUNNING"
            })
            triggers.append({
                "agent": "NEWS_SCANNER",
                "action": "Checking for adverse news and legal actions",
                "status": "RUNNING"
            })
            triggers.append({
                "agent": "GST_DELAY_MODEL",
                "action": "Evaluating GSTN delayed filing patterns",
                "status": "RUNNING"
            })
            triggers.append({
                "agent": "EPFO_MODEL",
                "action": "EPFO filing compliance check initiated",
                "status": "RUNNING"
            })
        
        # PAN entered (for non-GST)
        if 'pan' in data_extracted and not user_data.get('isGSTRegistered'):
            triggers.append({
                "agent": "BUREAU_AGENT",
                "action": "Triggered bureau pull via PAN",
                "status": "RUNNING"
            })
        
        # Industry + LOB entered → Peer model
        if 'industry' in data_extracted or 'lineOfBusiness' in data_extracted:
            if user_data.get('industry') and user_data.get('lineOfBusiness'):
                triggers.append({
                    "agent": "PEER_MODEL",
                    "action": "Preparing dataset for peer comparison model",
                    "status": "RUNNING"
                })
        
        # CIN entered → MCA data
        if 'cin' in data_extracted:
            triggers.append({
                "agent": "MCA_AGENT",
                "action": "Extracting management details and MCA master data",
                "status": "RUNNING"
            })
        
        # Financial data → DSCR computation
        if 'loanAmount' in data_extracted or 'revenue' in data_extracted:
            if user_data.get('operatingProfit') and user_data.get('monthlyEMI'):
                triggers.append({
                    "agent": "DSCR_ENGINE",
                    "action": "Computing Debt Service Coverage Ratio",
                    "status": "RUNNING"
                })
        
        # Purpose entered → Structuring
        if 'loanPurpose' in data_extracted:
            triggers.append({
                "agent": "BRE_ENGINE",
                "action": "Loan structuring based on purpose and financials",
                "status": "RUNNING"
            })
        
        # Offer generation
        if 'offerGenerated' in data_extracted:
            triggers.append({
                "agent": "ENSEMBLE_MODEL",
                "action": "Composite risk score computed using all BRE models",
                "status": "COMPLETE"
            })
        
        # Documents uploaded
        if 'docsUploaded' in data_extracted:
            triggers.append({
                "agent": "DOC_PROCESSOR",
                "action": "Documents processing and model output sent for internal review",
                "status": "PROCESSING"
            })
        
        # GSTN Consent Flow triggers
        if 'gstnList' in data_extracted:
            num_gstns = len(data_extracted.get('gstnList', []))
            triggers.append({
                "agent": "GSTN_DISCOVERY",
                "action": f"Retrieved {num_gstns} active GSTN(s) from taxpayer profile",
                "status": "COMPLETE"
            })
        
        if 'gstnListConfirmed' in data_extracted and data_extracted.get('gstnListConfirmed'):
            if user_data.get('awaitingUserGstnList'):
                # User provided their own list
                triggers.append({
                    "agent": "GSTN_VALIDATOR",
                    "action": "User-provided GSTN list validated and accepted",
                    "status": "COMPLETE"
                })
            else:
                # User confirmed system-generated list
                triggers.append({
                    "agent": "GSTN_VALIDATOR",
                    "action": "GSTN list confirmed by user, initiating authentication",
                    "status": "COMPLETE"
                })
        
        if 'gstnUsername' in data_extracted:
            current_gstn = user_data.get('gstnList', [])[user_data.get('currentGstnIndex', 0)]
            triggers.append({
                "agent": "GSTN_AUTH",
                "action": f"Username captured for {current_gstn}, initiating OTP",
                "status": "RUNNING"
            })
        
        if 'gstnOtp' in data_extracted:
            current_idx = user_data.get('currentGstnIndex', 0) - 1  # Already incremented
            if current_idx >= 0 and current_idx < len(user_data.get('gstnList', [])):
                gstn = user_data.get('gstnList', [])[current_idx]
                triggers.append({
                    "agent": "GSTN_VALIDATOR",
                    "action": f"OTP verified for {gstn}, authentication successful",
                    "status": "COMPLETE"
                })
        
        if 'allGstnsProcessed' in data_extracted:
            triggers.append({
                "agent": "GST_DATA_EXTRACTOR",
                "action": "Processing GST 3B and 2A returns for last 24 months",
                "status": "PROCESSING"
            })
            triggers.append({
                "agent": "TURNOVER_ANALYZER",
                "action": "Analyzing monthly turnover patterns and GST compliance",
                "status": "RUNNING"
            })
            triggers.append({
                "agent": "ITC_VALIDATOR",
                "action": "Validating Input Tax Credit claims and utilization",
                "status": "RUNNING"
            })
        
        if 'gstnConsentOfferGenerated' in data_extracted:
            triggers.append({
                "agent": "BRE_ENSEMBLE",
                "action": "Revised risk assessment based on GST data: -50 bps rate, 115% of requested amount approved",
                "status": "COMPLETE"
            })
        
        return triggers

# ══════════════════════════════════════════════════════════════════════════════
#  NODE ROUTER - Determines current node based on state
# ══════════════════════════════════════════════════════════════════════════════

class NodeRouter:
    @staticmethod
    def determine_node(ud, msg_count):
        """Determine which node to execute based on user_data state"""
        
        # Onboarding - includes applicant name now
        if not ud.get("mobile") or not ud.get("otpVerified") or not ud.get("applicantName") or ud.get("isGSTRegistered") is None:
            return "NODE_ONBOARD"
        
        is_gst = ud.get("isGSTRegistered")
        
        # GST Entity Collection
        if is_gst and not ud.get("gstEntityComplete"):
            return "NODE_GST_ENTITY"
        
        # Non-GST Entity Collection
        if not is_gst and not ud.get("nonGstEntityComplete"):
            return "NODE_NONGST_ENTITY"
        
        # Financial Questions (common for both)
        if not ud.get("financialsComplete"):
            return "NODE_FINANCIALS"
        
        # Initial Offer Generation
        if not ud.get("offerGenerated"):
            return "NODE_OFFER"
        
        # GSTN Consent Flow (if user chose this option)
        if ud.get("gstConsentUpgrade") and not ud.get("allGstnsProcessed"):
            return "NODE_GSTN_CONSENT"
        
        # GSTN Consent Offer (after all GSTNs processed)
        if ud.get("allGstnsProcessed") and not ud.get("gstnConsentOfferGenerated"):
            return "NODE_GSTN_CONSENT"
        
        # If user chose to upgrade with documents
        if ud.get("upgradeWithDocs") and not ud.get("docsUploaded"):
            return "NODE_UPGRADE_DOCS"
        
        # If documents uploaded, show revised offer
        if ud.get("docsUploaded") and not ud.get("revisedOfferGenerated"):
            return "NODE_REVISED_OFFER"
        
        # Closure
        return "NODE_CLOSURE"

# ══════════════════════════════════════════════════════════════════════════════
#  GSTN LIST GENERATOR (for demo)
# ══════════════════════════════════════════════════════════════════════════════

def generate_gstn_list(primary_gstn):
    """Generate GSTN list that INCLUDES user's primary GSTN + 0-2 additional ones"""
    import random
    
    # Always start with user's primary GSTN
    gstns = [primary_gstn]
    
    # State codes for variety
    state_codes = ['27', '29', '24', '06', '09', '19']
    
    # Add 0-2 additional GSTNs (so total will be 1-3)
    num_additional = random.choice([0, 1, 2])
    
    for i in range(num_additional):
        state = random.choice(state_codes)
        pan_part = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=5))
        digits = ''.join(random.choices('0123456789', k=4))
        check_char = random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
        entity_num = random.choice(['1', '2', '3'])
        final_char = random.choice('Z')
        last_char = random.choice('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ')
        
        gstn = f"{state}{pan_part}{digits}{check_char}{entity_num}{final_char}{last_char}"
        gstns.append(gstn)
    
    return gstns

# ══════════════════════════════════════════════════════════════════════════════
#  OFFER CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

def calculate_offer(user_data, persona_data, is_revised=False, is_gstn_consent=False):
    """Calculate loan offer based on user data and persona
    
    For revised/GSTN consent offers, maintains the same loan type as original offer
    """
    
    try:
        requested = float(user_data.get("loanAmount", 25))
    except:
        requested = 25
    
    # Offer calculation logic
    if is_gstn_consent:
        # GSTN consent: 1.15 times the REQUESTED amount (115% of what user asked for)
        approved_amount = round(requested * 1.15, 2)
    elif is_revised:
        # Documents: 1.15 times the original APPROVED amount
        original_approved = float(user_data.get("originalApprovedAmount", requested * 0.60))
        approved_amount = round(original_approved * 1.15, 2)
    else:
        # Base offer = 60% of requested
        approved_amount = round(requested * 0.60, 2)
        # Store for future reference
        user_data["originalApprovedAmount"] = approved_amount
    
    # Interest rate based on bureau score
    bureau_score = persona_data.get("bureauScore", 650)
    if bureau_score >= 750:
        interest_rate = 15.0
    elif bureau_score >= 700:
        interest_rate = 16.0
    elif bureau_score >= 650:
        interest_rate = 17.0
    else:
        interest_rate = 18.0
    
    # GSTN consent: Reduce by 50 basis points (0.5%)
    if is_gstn_consent:
        interest_rate = max(10.0, interest_rate - 0.5)
    
    # Determine loan type
    # For revised/GSTN consent offers, preserve the original loan type
    if (is_revised or is_gstn_consent) and user_data.get("originalLoanType"):
        # Use the original loan type to maintain consistency
        loan_type = user_data.get("originalLoanType")
        is_revolving = (loan_type == "Revolving Credit")
    else:
        # First time offer - determine based on purpose
        purpose = user_data.get("loanPurpose", "").lower()
        is_revolving = any(word in purpose for word in ["inventory", "working capital", "cash management"])
        loan_type = "Revolving Credit" if is_revolving else "Term Loan"
        # Store for future reference
        user_data["originalLoanType"] = loan_type
    
    if is_revolving:
        tenure_text = "Annual (renewal based)"
        tenure_months = 12
        # Monthly interest on full utilization (100%)
        monthly_payout = round((approved_amount * 100000 * interest_rate / 100) / 12)
    else:
        # Term Loan
        # For revised offers, also preserve the original tenure if available
        if (is_revised or is_gstn_consent) and user_data.get("originalTenureMonths"):
            tenure_months = user_data.get("originalTenureMonths")
        else:
            # First time - calculate tenure based on amount
            if approved_amount <= 10:
                tenure_months = 12
            elif approved_amount <= 25:
                tenure_months = 24
            else:
                tenure_months = 36
            user_data["originalTenureMonths"] = tenure_months
        
        tenure_text = f"{tenure_months} months"
        
        # EMI calculation
        P = approved_amount * 100000  # Convert lakhs to rupees
        r = interest_rate / 100 / 12  # Monthly interest rate
        n = tenure_months
        monthly_payout = round(P * r * pow(1 + r, n) / (pow(1 + r, n) - 1))
    
    return {
        "approvedAmount": approved_amount,
        "approvedAmountFormatted": f"₹{approved_amount:.2f} lakhs",
        "loanType": loan_type,
        "interestRate": interest_rate,
        "tenureText": tenure_text,
        "tenureMonths": tenure_months,
        "monthlyPayout": monthly_payout,
        "monthlyPayoutFormatted": f"₹{monthly_payout:,}",
        "isRevolved": is_revolving
    }

# ══════════════════════════════════════════════════════════════════════════════
#  BUILD SYSTEM PROMPT
# ══════════════════════════════════════════════════════════════════════════════

def build_system_prompt(current_node, user_data):
    """Build dynamic prompt with base + current node instructions"""
    node_inst = NODE_PROMPTS.get(current_node, "")
    
    # Clean state for context
    clean_state = {k: v for k, v in user_data.items() if not k.startswith('_')}
    state_json = json.dumps(clean_state, default=str)
    
    # Add GSTN context if in GSTN consent flow
    gstn_context = ""
    if current_node == "NODE_GSTN_CONSENT" and user_data.get("gstnList"):
        gstn_list = user_data.get("gstnList", [])
        current_idx = user_data.get("currentGstnIndex", 0)
        total = user_data.get("totalGstns", 0)
        
        gstn_context = f"""
[GSTN CONSENT CONTEXT]
Total GSTNs: {total}
GSTN List: {gstn_list}
Current GSTN Index: {current_idx}
Current GSTN: {gstn_list[current_idx] if current_idx < len(gstn_list) else 'ALL DONE'}
Remaining: {total - current_idx}

Use the GSTN list above in your messages. Ask for username/OTP for the CURRENT GSTN.
"""
    
    # Determine what data is missing to guide the LLM
    missing_fields = []
    
    if current_node == "NODE_ONBOARD":
        if not user_data.get("mobile"):
            missing_fields.append("mobile (ask for this NOW)")
        elif not user_data.get("otpVerified"):
            missing_fields.append("otpVerified (ask for OTP NOW)")
        elif not user_data.get("applicantName"):
            missing_fields.append("applicantName (ask 'May I know your name?' NOW)")
        elif user_data.get("isGSTRegistered") is None:
            missing_fields.append("isGSTRegistered (ask if business is GST-registered NOW)")
    
    if current_node == "NODE_GST_ENTITY":
        if not user_data.get("gstn"):
            missing_fields.append("GSTN (ask for this NOW)")
        elif not user_data.get("businessName"):
            missing_fields.append("businessName (ask for this NOW - ONLY TIME to ask!)")
        elif not user_data.get("udyam") and not user_data.get("udyamSkipped"):
            missing_fields.append("Udyam (ask for this NOW)")
        elif not user_data.get("industry"):
            missing_fields.append("industry (ask for dropdown NOW)")
        elif not user_data.get("lineOfBusiness"):
            missing_fields.append("lineOfBusiness (ask for dropdown NOW)")
        elif not user_data.get("mcaConfirmation") and not user_data.get("cinSkipped"):
            # Check if businessName has MCA keywords
            business_name = user_data.get("businessName", "").lower()
            mca_keywords = ['private limited', 'pvt ltd', 'pvt. ltd', 'limited', ' ltd', ' ltd.', 'llp', 'l.l.p']
            has_mca = any(keyword in business_name for keyword in mca_keywords)
            if has_mca:
                missing_fields.append("mcaConfirmation (ask if MCA registered NOW)")
            else:
                missing_fields.append("cinSkipped=true (set this NOW, no MCA keywords found)")
        elif user_data.get("mcaConfirmation") == True and not user_data.get("cin"):
            missing_fields.append("cin (ask for CIN NOW, user confirmed MCA)")
    
    if current_node == "NODE_FINANCIALS":
        if not user_data.get("vintage"):
            missing_fields.append("vintage (ask NOW)")
        elif not user_data.get("revenue"):
            missing_fields.append("revenue (ask NOW)")
        elif not user_data.get("operatingProfit"):
            missing_fields.append("operatingProfit (ask NOW)")
        elif not user_data.get("monthlyEMI"):
            missing_fields.append("monthlyEMI (ask NOW)")
        elif not user_data.get("loanAmount"):
            missing_fields.append("loanAmount (ask NOW)")
        elif not user_data.get("loanPurpose"):
            missing_fields.append("loanPurpose (dropdown NOW)")
    
    missing_context = ""
    if missing_fields:
        missing_context = f"""
[CRITICAL - NEXT STEP]
The FIRST missing field you need to collect: {missing_fields[0]}
DO NOT skip ahead. Ask ONLY for this field. STOP after extracting this field.
"""
    
    # Add reminder to use applicant name
    name_context = ""
    if user_data.get("applicantName"):
        name_context = f"""
[APPLICANT CONTEXT]
Applicant Name: {user_data.get("applicantName")}
IMPORTANT: When addressing the user, use their name naturally in your messages.
Remember: Applicant = {user_data.get("applicantName")} (the person applying)
          Business = {user_data.get("businessName", "TBD")} (the entity being underwritten)
These are DIFFERENT. Do NOT confuse them.
"""
    
    # Add explicit MCA check context if at that stage
    mca_check_context = ""
    if current_node == "NODE_GST_ENTITY" and user_data.get("lineOfBusiness") and not user_data.get("mcaConfirmation") and not user_data.get("cinSkipped"):
        business_name = user_data.get("businessName", "")
        business_name_lower = business_name.lower()
        mca_keywords = ['private limited', 'pvt ltd', 'pvt. ltd', ' limited', ' ltd', ' ltd.', 'llp', 'l.l.p']
        has_mca = any(keyword in business_name_lower for keyword in mca_keywords)
        
        mca_check_context = f"""
[CRITICAL MCA/CIN CHECK]
Business Name Provided: "{business_name}"
Business Name (lowercase): "{business_name_lower}"

MCA Keywords to check: {mca_keywords}

CHECKING: Does "{business_name_lower}" contain ANY of the keywords?
Result: {"YES - Keywords found" if has_mca else "NO - NO keywords found"}

INSTRUCTION:
{"Since keywords found, ASK: 'I understand that your business is MCA registered. Can you confirm?'" if has_mca else "NO keywords found. DO NOT ask for CIN. Just extract: cinSkipped=true"}

CRITICAL: Follow the instruction above EXACTLY. Do not make assumptions.
"""
    
    return f"""{BASE_PROMPT}

{node_inst}

{gstn_context}

{name_context}

{mca_check_context}

{missing_context}

[CURRENT STATE]
Node: {current_node}
Data collected: {state_json}

REMINDER: Ask ONLY ONE question. Extract ONLY the data you asked for. DO NOT skip ahead in the sequence.

CRITICAL CIN REMINDER:
- ONLY ask about MCA/CIN if businessName contains keywords: 'pvt ltd', 'private limited', 'limited', 'ltd', 'llp'
- If businessName = "Knight FIntech" or similar (no keywords) → Extract cinSkipped=true, DO NOT ask
- If businessName = "ABC Pvt Ltd" (has keywords) → Ask "Can you confirm MCA registered?"
"""

# ══════════════════════════════════════════════════════════════════════════════
#  PROCESS RESPONSE
# ══════════════════════════════════════════════════════════════════════════════

def process_request(current_node, parsed, user_data, persona_logs):
    """Process the LLM response and add system triggers"""
    
    triggers = []
    
    # Add persona simulation logs
    for agent, action in persona_logs:
        triggers.append({
            "agent": agent,
            "action": action,
            "status": "SIMULATED"
        })
    
    # Add BRE system triggers
    system_triggers = SystemAnalyzer.analyze_and_trigger(
        current_node,
        parsed.get("dataExtracted", {}),
        user_data,
        parsed
    )
    triggers.extend(system_triggers)
    
    parsed["systemTriggers"] = triggers
    
    logger.info(f"OK: node={current_node} type={parsed.get('inputType')} triggers={len(triggers)}")
    
    return parsed

# ══════════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

@app.route("/api/chat", methods=["POST"])
def chat():
    try:
        body = request.get_json() or {}
        messages = body.get("messages", [])
        user_data = body.get("userData", {})
        
        # Initial greeting
        if not messages:
            return jsonify({
                "message": "Hello! I'm AIWA from Knight Fintech 👋\nLet's begin your business loan assessment.",
                "logEntry": "SESSION_STARTED",
                "logStatus": "START",
                "inputType": "text",
                "dataExtracted": {},
                "guardrailFlag": None,
                "currentNode": "NODE_ONBOARD",
                "systemTriggers": []
            })
        
        # Persona selection
        persona_key = user_data.get("_persona", "growth_sme")
        if persona_key not in PERSONAS:
            persona_key = "growth_sme"
        persona_data = PERSONAS[persona_key]
        user_data["_persona"] = persona_key
        
        # Determine current node
        current_node = NodeRouter.determine_node(user_data, len(messages))
        logger.info(f"NODE: {current_node}")
        
        # Special handling for GSTN Consent flow
        if current_node == "NODE_GSTN_CONSENT":
            # Generate GSTN list if entering this node for first time
            if not user_data.get("gstnList"):
                primary_gstn = user_data.get("gstn", "27AAAAA0000A1Z0")  # User's primary GSTN
                gstn_list = generate_gstn_list(primary_gstn)
                user_data["gstnList"] = gstn_list
                user_data["totalGstns"] = len(gstn_list)
                user_data["currentGstnIndex"] = 0
                user_data["gstnListConfirmed"] = False
                logger.info(f"Generated {len(gstn_list)} GSTNs for consent flow (including primary: {primary_gstn})")
        
        # Build prompt
        system_prompt = build_system_prompt(current_node, user_data)
        
        # Trim message history
        trimmed = messages[-MAX_HISTORY_MSGS:] if len(messages) > MAX_HISTORY_MSGS else messages
        
        # Call Claude API
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        
        payload = {
            "model": MODEL,
            "max_tokens": 1000,
            "system": system_prompt,
            "messages": trimmed,
        }
        
        resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=30)
        
        if not resp.ok:
            logger.error(f"API error {resp.status_code}: {resp.text[:300]}")
            return jsonify({"error": f"API error: {resp.status_code}"}), 500
        
        data = resp.json()
        raw = data["content"][0]["text"]
        
        # Parse JSON
        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1] if len(parts) > 1 else clean
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip()
        
        try:
            parsed = json.loads(clean)
        except:
            m = re.search(r'\{.*\}', clean, re.DOTALL)
            if m:
                parsed = json.loads(m.group())
            else:
                raise ValueError(f"No JSON found")
        
        # Ensure structure
        if "message" not in parsed:
            parsed["message"] = "Please continue."
        if "dataExtracted" not in parsed:
            parsed["dataExtracted"] = {}
        if "inputType" not in parsed:
            parsed["inputType"] = "text"
        if "logEntry" not in parsed:
            parsed["logEntry"] = "Processing..."
        if "logStatus" not in parsed:
            parsed["logStatus"] = "OK"
        if "guardrailFlag" not in parsed:
            parsed["guardrailFlag"] = None
        if "currentNode" not in parsed:
            parsed["currentNode"] = current_node
        
        # Update user_data with extracted data
        user_data.update(parsed["dataExtracted"])
        
        # VALIDATION: Prevent skipping steps
        # Check if LLM tried to extract data out of sequence
        if current_node == "NODE_GST_ENTITY":
            extracted_keys = set(parsed["dataExtracted"].keys())
            
            # Check what should be allowed based on current state
            if not user_data.get("gstn") and "businessName" in extracted_keys:
                logger.warning("LLM tried to skip GSTN step, removing businessName")
                parsed["dataExtracted"] = {k:v for k,v in parsed["dataExtracted"].items() if k == "gstn"}
                user_data = {k:v for k,v in user_data.items() if k != "businessName"}
            
            if not user_data.get("businessName") and "industry" in extracted_keys:
                logger.warning("LLM tried to skip ahead to industry, removing")
                parsed["dataExtracted"] = {}
                # Remove any fields that were added
                for key in ["industry", "lineOfBusiness", "loanPurpose", "vintage", "revenue"]:
                    user_data.pop(key, None)
            
            # CRITICAL: Validate CIN logic
            business_name = user_data.get("businessName", "").lower()
            mca_keywords = ['private limited', 'pvt ltd', 'pvt. ltd', ' limited', ' ltd', ' ltd.', 'llp', 'l.l.p']
            has_mca = any(keyword in business_name for keyword in mca_keywords)
            
            # If LLM asked for MCA confirmation or CIN when NO keywords present
            if not has_mca:
                if "mcaConfirmation" in extracted_keys:
                    logger.warning(f"CRITICAL: LLM incorrectly asked for MCA confirmation for '{user_data.get('businessName')}' - NO keywords found. Forcing skip.")
                    parsed["dataExtracted"] = {"cinSkipped": True}
                    user_data["cinSkipped"] = True
                    user_data.pop("mcaConfirmation", None)
                    user_data.pop("cin", None)
                    # Regenerate message to user
                    parsed["message"] = "Thank you. Moving forward with your application."
                    parsed["logEntry"] = f"CIN skipped - '{user_data.get('businessName')}' has no MCA keywords"
                    
                if "cin" in extracted_keys:
                    logger.warning(f"CRITICAL: LLM incorrectly asked for CIN for '{user_data.get('businessName')}' - NO keywords found. Forcing skip.")
                    parsed["dataExtracted"] = {"cinSkipped": True}
                    user_data["cinSkipped"] = True
                    user_data.pop("cin", None)
                    # Regenerate message to user
                    parsed["message"] = "Thank you. Moving forward with your application."
                    parsed["logEntry"] = f"CIN skipped - '{user_data.get('businessName')}' has no MCA keywords"
            
            # If LLM asked for CIN when user denied MCA registration
            if user_data.get("mcaConfirmation") == False and "cin" in extracted_keys:
                logger.warning("LLM asked for CIN after user denied MCA registration. Removing.")
                parsed["dataExtracted"].pop("cin", None)
                user_data.pop("cin", None)
        
        if current_node == "NODE_FINANCIALS":
            extracted_keys = set(parsed["dataExtracted"].keys())
            # If multiple keys extracted in financial node, only keep the first one needed
            required_order = ["vintage", "revenue", "operatingProfit", "monthlyEMI", "loanAmount", "loanPurpose"]
            for field in required_order:
                if not user_data.get(field):
                    # This is the next field needed
                    if field in extracted_keys:
                        # Good, they extracted the right field
                        # Remove any fields that come after this in the sequence
                        idx = required_order.index(field)
                        for future_field in required_order[idx+1:]:
                            if future_field in extracted_keys:
                                logger.warning(f"LLM tried to skip ahead to {future_field}, removing")
                                parsed["dataExtracted"].pop(future_field, None)
                                user_data.pop(future_field, None)
                    break
        
        # Simulate persona agent triggers
        persona_logs = []
        
        return jsonify(process_request(current_node, parsed, user_data, persona_logs))
        
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "version": "3.0",
        "model": MODEL
    })

# if __name__ == "__main__":
#     print("\n" + "=" * 60)
#     print("  AIWA v3.0 - Knight Fintech (CORRECTED FLOW)")
#     print("=" * 60)
#     print(f"  Model: {MODEL}")
#     print(f"  Port: 5000")
#     print("=" * 60)
#     print("  GST Flow:")
#     print("    GSTN → Business Name → Udyam → Industry → LOB → CIN")
#     print("    → Vintage → Revenue → Profit → EMI → Loan → Purpose")
#     print("    → OFFER → Options → (Optional) Docs → Revised Offer")
#     print("  Non-GST Flow:")
#     print("    PAN → Business Name → Industry → LOB")
#     print("    → [Same financial questions and offer flow]")
#     print("=" * 60 + "\n")
    
#     app.run(debug=False, port=5000, use_reloader=False)

if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 5000))

    print("\n" + "=" * 60)
    print("  AIWA v3.0 - Knight Fintech (CORRECTED FLOW)")
    print("=" * 60)
    print(f"  Model: {MODEL}")
    print(f"  Port: 5000")
    print("=" * 60)
    print("  GST Flow:")
    print("    GSTN → Business Name → Udyam → Industry → LOB → CIN")
    print("    → Vintage → Revenue → Profit → EMI → Loan → Purpose")
    print("    → OFFER → Options → (Optional) Docs → Revised Offer")
    print("  Non-GST Flow:")
    print("    PAN → Business Name → Industry → LOB")
    print("    → [Same financial questions and offer flow]")
    print("=" * 60 + "\n")

    app.run(host="0.0.0.0", port=port, debug=False)
