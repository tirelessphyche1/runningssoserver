import os
from pathlib import Path

# Configure these values as GitHub Actions repository secrets.
BOT_TOKEN = "1783818899:AAF-xdUhIg5HBKvuSltXpLOzMSVyVos70F0"
SSO_USER = "pg1797"
SSO_PASS = "Suresh@1"
SSO_LOGIN_URL = os.getenv("SSO_LOGIN_URL", "https://sso.rajasthan.gov.in/signin").strip()
BASE_URL = os.getenv("BASE_URL", "https://ssoapps.rajasthan.gov.in/JALMITRA").strip()

# GitHub-hosted runners use Linux. Runtime files live in the workspace by default.
DATA_DIR = Path(os.getenv("JALMITRA_DATA_DIR", str(Path.cwd() / "runtime_data")))
BASE_DOWNLOAD_DIR = str(DATA_DIR / "HeadlessUploads")
COOKIE_FILE_PATH = str(DATA_DIR / "cookies.json")
DATA_DIR.mkdir(parents=True, exist_ok=True)
Path(BASE_DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Add it under GitHub Settings > Secrets and variables > Actions.")

# SSO credentials are required for automated SSO/refill flows, but not necessarily for every bot feature.
DISTRICT_MAP = {
    "1": "AJMER", "2": "ALWAR", "3": "BANSWARA", "4": "BARAN", "5": "BARMER",
    "6": "BHARATPUR", "7": "BHILWARA", "8": "BIKANER", "9": "BUNDI", "10": "CHITTORGARH",
    "11": "CHURU", "12": "JAIPUR", "13": "JAISALMER", "14": "JALORE", "15": "JHALAWAR",
    "16": "JHUNJHUNU", "17": "JODHPUR", "18": "KARAULI", "19": "KOTA", "20": "NAGAUR",
    "21": "PALI", "22": "PRATAPGARH", "23": "RAJSAMAND", "24": "SAWAI MADHOPUR",
    "25": "SIKAR", "26": "SIROHI", "27": "SRI GANGANAGAR", "28": "TONK", "29": "UDAIPUR",
    "30": "DHOLPUR", "31": "DAUSA", "32": "HANUMANGARH"
}
