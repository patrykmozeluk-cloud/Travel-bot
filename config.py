# config.py
import os
from typing import Any, List, Dict

# ---------- HELPERS ----------
def env(name: str, default: Any = None) -> Any:
    """Safely reads an environment variable."""
    return os.environ.get(name, default)

def to_bool(value: str) -> bool:
    """Converts a string to a boolean."""
    return value in {"1", "true", "True", "yes", "YES"}

# ---------- ENVIRONMENT-BASED CONFIG ----------
TG_TOKEN = env("TG_TOKEN")
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID")
TELEGRAM_CHANNEL_USERNAME = env("TELEGRAM_CHANNEL_USERNAME")

TELEGRAPH_TOKEN = env("TELEGRAPH_TOKEN")
GEMINI_API_KEY = env("GEMINI_API_KEY")
PERPLEXITY_API_KEY = env("PERPLEXITY_API_KEY")
BUCKET_NAME = env("BUCKET_NAME")
SENT_LINKS_FILE = env("SENT_LINKS_FILE", "sent_links.json")
TELEGRAM_SECRET = env("TELEGRAM_SECRET")
PORT = env("PORT", "8080")

# --- NordVPN Proxy Credentials ---
NORD_USER = env("NORD_USER")
NORD_PASS = env("NORD_PASS")

# ---------- RUN BEHAVIOR PARAMS ----------
HTTP_TIMEOUT = float(env("HTTP_TIMEOUT", "15.0"))
DEBUG_FEEDS = to_bool(env("DEBUG_FEEDS", "0"))
MAX_POSTS_PER_RUN = int(env("MAX_POSTS_PER_RUN", "0"))
DELETE_AFTER_HOURS = int(env("DELETE_AFTER_HOURS", "48"))
DEDUP_TTL_HOURS = int(env("DEDUP_TTL_HOURS", "336"))
MAX_PER_DOMAIN = int(env("MAX_PER_DOMAIN", "8"))
PER_HOST_CONCURRENCY = int(env("PER_HOST_CONCURRENCY", "2"))
JITTER_MIN_MS = int(env("JITTER_MIN_MS", "120"))
JITTER_MAX_MS = int(env("JITTER_MAX_MS", "400"))
AI_BATCH_SIZE = int(env("AI_BATCH_SIZE", "5"))
AI_BATCH_WAIT_SECONDS = int(env("AI_BATCH_WAIT_SECONDS", "1"))

# ---------- HARDCODED CONSTANTS & DICTIONARIES ----------

# --- HOSTS ---
THRIFTY_TRAVELER_HOST = "thriftytraveler.com"

# Define which hosts MUST use a proxy for scraping
PROXY_REQUIRED_HOSTS = set()

# --- URL CLEANING ---
DROP_PARAMS: set = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src", "src"
}

# --- DIGEST ---
DIGEST_IMAGE_URLS: List[str] = [
    "https://images.unsplash.com/photo-1516483638261-f4dbaf036963?q=80&w=2800&auto=format&fit=crop&ixlib=rb-4.0.3&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D",
    "https://images.pexels.com/photos/3408744/pexels-photo-3408744.jpeg?auto=compress&cs=tinysrgb&w=1260&h=750&dpr=2",
    "https://cdn.pixabay.com/photo/2017/01/20/00/30/maldives-1993704_1280.jpg"
]

# --- GEMINI AI ---
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# --- EMOJIS ---
EMOJI_KEYWORDS: Dict[str, List[str]] = {
    '🇬🇧': ['londyn', 'london', 'anglia', 'uk', 'brytanii'],
    '🇪🇸': ['hiszpanii', 'spain', 'barcelona', 'madryt', 'madrid', 'majorka', 'mallorca'],
    '🇮🇹': ['włochy', 'italy', 'rzym', 'rome', 'mediolan', 'milan'],
    '🇫🇷': ['francja', 'france', 'paryż', 'paris'],
    '🇩🇪': ['niemcy', 'germany', 'berlin'],
    '🇵🇹': ['portugalia', 'portugal', 'lizbona', 'lisbon'],
    '🇺🇸': ['usa', 'stany', 'york', 'chicago', 'miami'],
    '🇦🇪': ['dubaj', 'dubai', 'emiraty', 'emirates'],
    '🇯🇵': ['japonia', 'japan', 'tokio', 'tokyo'],
    '🇹🇭': ['tajlandia', 'thailand', 'bangkok'],
    '🏖️': ['plaża', 'beach', 'wakacje', 'holiday', 'morze', 'sea', 'wyspy', 'islands'],
    '✈️': ['loty', 'flights', 'lot', 'flight'],
    '🏨': ['hotel', 'nocleg'],
    '💰': ['okazja', 'deal', 'tanio', 'cheap', 'promocja'],
}

# --- HTTP & SCRAPING ---
CHROME_USER_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

BASE_HEADERS: Dict[str, str] = {
    "Accept-Encoding": "gzip, deflate", 
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}

DOMAIN_CONFIG: Dict[str, Dict[str, Any]] = {
    "travel-dealz.com": { 
        "selectors": ['article.article-item h2 a', 'article.article h2 a'],
        "headers": { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36" } 
    },
    "wakacyjnipiraci.pl": { 
        "selectors": ['article.post-list__item a.post-list__link'], 
        "rss": ["https://www.wakacyjnipiraci.pl/feed"], 
        "headers": { 
            "Accept-Encoding": "gzip, deflate", 
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7", 
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36" 
        } 
    },
    "holidaypirates.com": { 
        "selectors": ['article.post-list__item a.post-list__link'], 
        "rss": ["https://www.holidaypirates.com/feed"], 
        "headers": { 
            "Accept-Encoding": "gzip, deflate", 
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7", 
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Mobile Safari/537.36" 
        } 
    },
    "theflightdeal.com": { "selectors": ['article h2 a', '.entry-title a'], "rss": ["https://www.theflightdeal.com/feed/"] },
    "travelfree.info": { 
        "headers": { 
            "Accept-Encoding": "gzip, deflate", 
            "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.7" 
        } 
    },
    "fly4free.pl": { "rss": ["https://www.fly4free.pl/feed/"] },
    "loter.pl": { "selectors": ['article h2 a', 'article h3 a'] }
}

GENERIC_FALLBACK_SELECTORS: List[str] = ['article h2 a', 'article h3 a', 'h2 a', 'h3 a']
# MAX_DIGEST_SIZE = 15
