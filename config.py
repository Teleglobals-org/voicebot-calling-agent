import boto3
import json
import re
import os
import io
import csv
import ast
import time

# ── AWS Clients ───────────────────────────────────────────────
bedrock    = boto3.client('bedrock-runtime', region_name='us-east-1')
translate  = boto3.client('translate',       region_name='us-east-1')
s3         = boto3.client('s3',              region_name='us-east-1')

# ── Config ────────────────────────────────────────────────────
AUDIO_BUCKET   = os.environ.get('AUDIO_BUCKET',   'voicebot-audio-sagar')
DATASET_BUCKET = os.environ.get('DATASET_BUCKET', 'voicebot-audio-sagar')
DATASET_PREFIX = 'archive/'

# ── Cache TTL (Fix #7: TTL-based cache invalidation) ──────────
CACHE_TTL_SECONDS = 3600  # 1 hour
_cache_timestamp  = 0

# ── Dataset file mapping ──────────────────────────────────────
CITY_FILE_MAP = {
    'mumbai':       'mumbai.csv',
    'thane':        'mumbai.csv',
    'navi mumbai':  'mumbai.csv',
    'gurgaon':      'gurgaon_10k.csv',
    'gurugram':     'gurgaon_10k.csv',
    'hyderabad':    'hyderabad.csv',
    'secunderabad': 'hyderabad.csv',
    'kolkata':      'kolkata.csv',
    'calcutta':     'kolkata.csv',
}

# ── In-memory cache ───────────────────────────────────────────
_dataset_cache = {}
_amenity_map   = {}
_prop_type_map = {}

# =============================================================
# LANGUAGE CONFIG
# =============================================================
SUPPORTED_LANGUAGES       = {"hi": "Hindi", "mr": "Marathi", "en": "English"}

# Language switching: Default is English. Switch mid-call when user
# clearly speaks in a different language. Context is preserved.
DEFAULT_LANGUAGE = "en"
LANG_SWITCH_MIN_CONFIDENCE = 0.5  # Confidence threshold to switch language

# =============================================================
# VALID VALUES
# =============================================================
VALID_PROPERTY_TYPES = [
    "apartment", "flat", "house", "villa", "bungalow",
    "penthouse", "studio", "plot", "land", "farmhouse",
    "independent", "builder floor", "residential", "commercial",
    "office", "shop", "showroom", "warehouse", "industrial",
]

# Mapping: user-said type → what we actually search for
# Types we support get mapped to searchable categories.
# Types we DON'T support (commercial, office, etc.) get a polite redirect.
PROPERTY_TYPE_MAP = {
    # Direct matches (supported)
    "apartment": "apartment",
    "flat": "flat",
    "house": "house",
    "villa": "villa",
    "bungalow": "bungalow",
    "penthouse": "penthouse",
    "studio": "studio",
    "plot": "plot",
    "land": "land",
    "farmhouse": "farmhouse",
    "independent": "independent",
    "builder floor": "builder floor",
    # Aliases → mapped to supported types
    "residential": "apartment",
    "home": "house",
    "duplex": "villa",
    "row house": "house",
    "rowhouse": "house",
    "cottage": "house",
    "mansion": "villa",
    "condo": "apartment",
    "condominium": "apartment",
    "pg": "apartment",
    "paying guest": "apartment",
    # Unsupported types (commercial/industrial) → mapped to None
    "commercial": None,
    "office": None,
    "shop": None,
    "showroom": None,
    "warehouse": None,
    "industrial": None,
    "factory": None,
    "godown": None,
    "business": None,
}

# Unsupported property types that get a polite redirect
UNSUPPORTED_PROPERTY_TYPES = [
    "commercial", "office", "shop", "showroom", "warehouse",
    "industrial", "factory", "godown", "business", "retail",
    "co-working", "coworking",
]
VALID_CONFIGURATIONS = [
    "1bhk", "2bhk", "3bhk", "4bhk", "5bhk",
    "1 bhk", "2 bhk", "3 bhk", "4 bhk", "5 bhk"
]

AMENITY_KEYWORDS = [
    "swimming pool", "power backup", "club house", "clubhouse",
    "visitor parking", "parking",
    "gymnasium", "fitness centre", "gym",
    "playground", "jogging track",
    "security personnel", "security",
    "maintenance staff", "maintenance",
    "rainwater harvesting", "rainwater",
    "intercom facility", "intercom",
    "lift", "elevator",
    "garden", "park",
    "terrace", "balcony",
    "cctv", "wifi", "internet",
    "sports",
]

# =============================================================
# NO PREFERENCE PHRASES (English + Hindi + Marathi)
# =============================================================
NO_PREFERENCE_PHRASES = [
    # English
    "no preference", "no amenities", "none", "nothing", "no specific",
    "doesn't matter", "anything", "no requirement", "not required",
    "nope", "nothing specific",
    # Transliterated
    "koi nahi", "kuch bhi", "nako", "nahi pahije",
    # Hindi
    "कोई नहीं", "कुछ भी", "नहीं", "कोई पसंद नहीं",
    "नको", "नाही पाहिजे", "कुछ नहीं", "कोई जरूरत नहीं",
    # Marathi
    "काही नाही", "काहीही चालेल", "नको", "काही नको",
    "काही विशेष नाही", "कशाचीही गरज नाही",
    # Marathi transliterated
    "kahi nahi", "kahihi chalel", "nako", "kahi nako",
]


# =============================================================
# HINDI / MARATHI AMENITY MAP
# =============================================================
HINDI_AMENITY_MAP = {
    "पार्किंग": "parking",
    "पार्क": "parking",
    "जिम": "gym",
    "व्यायामशाला": "gym",
    "जिम्नेजियम": "gymnasium",
    "स्विमिंग पूल": "swimming pool",
    "तरण तलाव": "swimming pool",
    "पूल": "swimming pool",
    "गार्डन": "garden",
    "बगीचा": "garden",
    "उद्यान": "garden",
    "लिफ्ट": "lift",
    "एलिवेटर": "elevator",
    "सुरक्षा": "security",
    "सिक्योरिटी": "security",
    "क्लबहाउस": "clubhouse",
    "क्लब हाउस": "club house",
    "पावर बैकअप": "power backup",
    "बिजली बैकअप": "power backup",
    "सीसीटीवी": "cctv",
    "वाईफाई": "wifi",
    "इंटरनेट": "internet",
    "खेल का मैदान": "playground",
    "प्लेग्राउंड": "playground",
    "जॉगिंग ट्रैक": "jogging track",
    "इंटरकॉम": "intercom",
    "छत": "terrace",
    "टेरेस": "terrace",
    "बालकनी": "balcony",
    "बाग": "garden",
    "तलाव": "swimming pool",
}

# =============================================================
# STT ERROR CORRECTION
# =============================================================
STT_CORRECTIONS = {
    # BHK corrections
    "two bhk": "2bhk",    "to bhk": "2bhk",    "two phd": "2bhk",
    "two be hk": "2bhk",  "2 be hk": "2bhk",   "to be hk": "2bhk",
    "three be hk": "3bhk","four be hk": "4bhk", "five be hk": "5bhk",
    "one be hk": "1bhk",  "three bhk": "3bhk",  "one bhk": "1bhk",
    "four bhk": "4bhk",   "five bhk": "5bhk",
    "to phd": "2bhk",     "two phd plan": "2bhk flat",
    "b h k": "bhk",       "flat meant": "flat",
    "willah": "villa",    "bunglow": "bungalow",
    # Twilio STT common mishearings
    "youtube per": "2bhk",  "youtube": "2bhk",
    "previous your": "2bhk",
    "to be ok": "2bhk",   "to pick": "2bhk",
    "three week": "3bhk", "four week": "4bhk",
    # Hindi crore/lakh corrections
    "करो":   "crore",
    "करोड़": "crore",
    "करोड":  "crore",
    "क्रोर": "crore",
    "कोर":   "crore",
    "लाख":   "lakh",
    "लखा":   "lakh",
    "लक्ष":  "lakh",
    # STT mishearing
    "crow":  "crore",
    "karod": "crore",
    "carod": "crore",
    "lak":   "lakh",
    "lac":   "lakh",
    # Common Twilio mishearings for Hindi confirmations
    "haryana": "haan",
    "han ji": "haan ji",
    # Marathi STT corrections
    "lakh rupaye": "lakh",
    "koti": "crore",
    "कोटी": "crore",
}
