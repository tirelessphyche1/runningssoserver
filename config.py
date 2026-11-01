import os
from telegram import ReplyKeyboardMarkup

# Credentials (can be overridden with system environment variables on Linux)
BOT_TOKEN = os.getenv("BOT_TOKEN", "2126511071:AAEwPMIEilE-Nz7YKxyJI3exFspdEfc_8Qc")
SSO_USER = os.getenv("SSO_USER", "pg1797")
SSO_PASS = os.getenv("SSO_PASS", "Suresh@1")

SSO_LOGIN_URL = "https://sso.rajasthan.gov.in/signin"
BASE_URL = "https://ssoapps.rajasthan.gov.in/JALMITRA"

# Cross-platform relative storage paths (works on both Linux and Windows)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DOWNLOAD_DIR = os.path.join(BASE_DIR, "HeadlessUploads")
COOKIE_FILE_PATH = os.path.join(BASE_DIR, "cookies.json")

os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

# Main Navigation Keyboard
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🔑 Login SSO", "📝 New Form"],
        ["💳 Refill Wallet", "💳 Pay Application"],
        ["📊 Track Summary", "💰 Pending Payment"],
        ["🔧 Pending Plumber", "🔍 Pending Verification"],
        ["⚠️ Objections", "📁 List Saved Files"],
        ["🗑️ Clear Storage & Session"],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    [["❌ Cancel Operation"]],
    resize_keyboard=True,
)

MENU_BUTTON_REGEX = (
    r"^(🔑 Login SSO|📝 New Form|💳 Refill Wallet|💳 Pay Application|📊 Track Summary|"
    r"💰 Pending Payment|🔧 Pending Plumber|🔍 Pending Verification|⚠️ Objections|"
    r"📁 List Saved Files|🗑️ Clear Storage & Session|❌ Cancel Operation)$"
)

# District mappings
DISTRICT_MAP = {
    "1": "AJMER", "2": "ALWAR", "3": "BANSWARA", "4": "BARAN", "5": "BARMER",
    "6": "BHARATPUR", "7": "BHILWARA", "8": "BIKANER", "9": "BUNDI", "10": "CHITTORGARH",
    "11": "CHURU", "12": "JAIPUR", "13": "JAISALMER", "14": "JALORE", "15": "JHALAWAR",
    "16": "JHUNJHUNU", "17": "JODHPUR", "18": "KARAULI", "19": "KOTA", "20": "NAGAUR",
    "21": "PALI", "22": "PRATAPGARH", "23": "RAJSAMAND", "24": "SAWAI MADHOPUR",
    "25": "SIKAR", "26": "SIROHI", "27": "SRI GANGANAGAR", "28": "TONK", "29": "UDAIPUR",
    "30": "DHOLPUR", "31": "DAUSA", "32": "HANUMANGARH"
}

# Conversation States
(
    CHECK_SAVED_COOKIES, COOKIES, CITIZEN_TYPE, HAVE_JANAADHAR, APPLICANT_NAME,
    FATHER_NAME, GENDER, DOB, MOBILE_NO, EMAIL_ID, CONSUMER_CATEGORY,
    IDENTITY_TYPE, IDENTITY_NUMBER, PERM_DISTRICT, PERM_AREA_TYPE,
    PERM_BLOCK_SELECT, PERM_WARD_SELECT, PERM_ADDRESS, PERM_PINCODE,
    PERM_PROOF_TYPE, PERM_PROOF_NUM, CORR_SAMEAS, CONN_LOCATION_CHOICE,
    CONN_APP_TYPE, CONN_DISTRICT, CONN_DIVISION_SELECT, CONN_SUBDIVISION_SELECT,
    CONN_TYPE, CONN_SUB_CAT, CONN_PROP_CAT, CONN_PROP_OWNERSHIP, CONN_CATEGORY,
    CONN_BUILDING_HEIGHT, CONN_IS_MULTISTORY, CONN_TOTAL_FLOORS, CONN_PIPE_SIZE,
    CONN_AREAS, RAIN_HARVESTING, DOC_PHOTO, DOC_PHOTO_COLLECT, DOC_ID_PROOF,
    DOC_ID_PROOF_COLLECT, DOC_ADDR_PROOF, DOC_ADDR_PROOF_COLLECT, DOC_OWNERSHIP,
    DOC_OWNERSHIP_COLLECT, DOC_SITE_PLAN, DOC_SITE_PLAN_COLLECT, DOC_PROP_PHOTO,
    DOC_PROP_PHOTO_COLLECT, DOC_SUPPORTING_CHOICE, DOC_SUPPORTING_UPLOAD,
    DOC_SUPPORTING_COLLECT, PAY_APP_INPUT, PAY_REMARKS, PAY_DOC
) = range(56)