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
comprehend = boto3.client('comprehend',      region_name='us-east-1')
transcribe = boto3.client('transcribe',      region_name='us-east-1')
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


def _is_cache_expired():
    """Fix #7: Check if cache has expired."""
    global _cache_timestamp
    return (time.time() - _cache_timestamp) > CACHE_TTL_SECONDS


def _reset_cache_if_expired():
    """Fix #7: Reset caches if TTL has expired."""
    global _dataset_cache, _amenity_map, _prop_type_map, _cache_timestamp
    if _is_cache_expired():
        print("[CACHE] TTL expired, clearing caches")
        _dataset_cache = {}
        _amenity_map   = {}
        _prop_type_map = {}
        _cache_timestamp = time.time()


def s3_get(filename):
    key = DATASET_PREFIX + filename
    print(f"[S3] Getting: s3://{DATASET_BUCKET}/{key}")
    return s3.get_object(Bucket=DATASET_BUCKET, Key=key)


# =============================================================
# LANGUAGE CONFIG
# =============================================================
SUPPORTED_LANGUAGES       = {"hi": "Hindi", "mr": "Marathi", "en": "English"}
TRANSCRIBE_LANG_CODES     = {"hi": "hi-IN", "mr": "mr-IN", "en": "en-IN"}
TRANSCRIBE_AUTO_DETECT    = ["hi-IN", "mr-IN", "en-IN", "en-US"]
COMPREHEND_MIN_CONFIDENCE = 0.4

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
    "independent", "builder floor"
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
    "काही नाही", "काहीही चालेल",
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
}


def correct_stt_errors(text):
    if not text:
        return text
    corrected = text.lower().strip()
    for wrong, right in STT_CORRECTIONS.items():
        if wrong in corrected:
            corrected = corrected.replace(wrong, right)
            print(f"[STT FIX] '{wrong}' → '{right}'")
    # Also normalize common patterns
    # "i prefer 2 bhk" → "i prefer 2bhk"
    corrected = re.sub(r'(\d)\s+bhk', r'\1bhk', corrected, flags=re.IGNORECASE)
    return corrected


# =============================================================
# DATASET LOADING (Fix #7: TTL-based cache)
# =============================================================

def load_lookup_maps():
    global _amenity_map, _prop_type_map
    _reset_cache_if_expired()
    if _amenity_map:
        return
    for fname, target_map, id_col, label_col in [
        ('AMENITIES.csv',     _amenity_map,   'id', 'label'),
        ('PROPERTY_TYPE.csv', _prop_type_map, 'id', 'label'),
    ]:
        try:
            obj    = s3_get(fname)
            text   = obj['Body'].read().decode('utf-8', errors='ignore')
            reader = csv.DictReader(io.StringIO(text))
            count  = 0
            for row in reader:
                target_map[str(row[id_col]).strip()] = row[label_col].strip()
                count += 1
            print(f"[DATASET] Loaded {count} rows from {fname}")
        except Exception as e:
            print(f"[DATASET] Error loading {fname}: {e}")
    if not _amenity_map:
        print("[FALLBACK] Using default amenity map")
        _amenity_map.update({"1": "Gym", "2": "Parking", "3": "Garden"})


def load_city_dataset(city_name):
    _reset_cache_if_expired()
    city_lower = city_name.strip().lower()
    filename   = None
    for key, fname in CITY_FILE_MAP.items():
        if key in city_lower:
            filename = fname
            break
    if not filename:
        print(f"[DATASET] No file for city: {city_name}")
        return []
    if filename in _dataset_cache:
        print(f"[DATASET] Cache hit: {filename} ({len(_dataset_cache[filename])} rows)")
        return _dataset_cache[filename]
    try:
        obj    = s3_get(filename)
        text   = obj['Body'].read().decode('utf-8', errors='ignore')
        rows   = list(csv.DictReader(io.StringIO(text)))
        _dataset_cache[filename] = rows
        print(f"[DATASET] Loaded {len(rows)} rows from {filename}")
        return rows
    except Exception as e:
        print(f"[DATASET] Error loading {filename}: {e}")
        return []


def get_amenity_labels(amenity_ids_str):
    if not amenity_ids_str or not str(amenity_ids_str).strip():
        return []
    load_lookup_maps()
    ids    = [x.strip() for x in str(amenity_ids_str).split(',') if x.strip()]
    labels = [_amenity_map.get(i, '') for i in ids]
    return [l for l in labels if l]


def parse_price_to_inr(price_str):
    try:
        return int(float(str(price_str).strip().replace(',', '')))
    except:
        return 0


def budget_to_inr_range(budget_str):
    """
    Handles budget parsing with unit detection.
    Fix #10: Returns None if unit cannot be determined (triggers clarification).

    Returns:
        tuple (min, max) in INR, or None if ambiguous.
    """
    if not budget_str:
        return (0, 999999999)

    text = str(budget_str).lower().strip()

    # Normalize Hindi budget words
    hindi_crore_words = ["करो", "करोड़", "करोड", "क्रोर", "कोर", "karod", "carod", "crow"]
    hindi_lakh_words  = ["लाख", "लखा", "लक्ष", "lak", "lac"]
    for word in hindi_crore_words:
        if word in text:
            text = text.replace(word, "crore")
            print(f"[BUDGET FIX] Hindi crore '{word}' → 'crore'")
    for word in hindi_lakh_words:
        if word in text:
            text = text.replace(word, "lakh")
            print(f"[BUDGET FIX] Hindi lakh '{word}' → 'lakh'")

    print(f"[BUDGET PARSE] normalized: '{text}'")

    # Detect unit
    is_crore = any(w in text for w in ['crore', 'cr', 'करोड़', 'करोड'])
    is_lakh  = any(w in text for w in ['lakh', 'lakhs', 'lac', 'lacs', 'लाख'])

    # Fix #10: If no unit detected, return None to trigger clarification
    if not is_crore and not is_lakh:
        nums = re.findall(r'[\d.]+', text)
        if nums:
            print(f"[BUDGET PARSE] No unit detected for value '{text}', needs clarification")
            return None  # Ambiguous - caller should ask user to clarify
        else:
            return (0, 999999999)

    mult = 10000000 if is_crore else 100000
    print(f"[BUDGET PARSE] unit={'crore' if is_crore else 'lakh'} mult={mult}")

    numbers = re.findall(r'[\d.]+', text)
    if not numbers:
        return (0, 999999999)

    nums = [float(n) for n in numbers]
    print(f"[BUDGET PARSE] numbers={nums}")

    is_range = (
        '-' in text or
        ' to ' in text or
        ' se ' in text or
        'से' in text or
        len(nums) >= 2
    )

    if is_range and len(nums) >= 2:
        low  = int(min(nums) * mult)
        high = int(max(nums) * mult * 1.3)
        print(f"[BUDGET PARSE] range: {low} - {high}")
        return (low, high)
    else:
        val  = int(nums[0] * mult)
        high = int(val * 1.3)
        print(f"[BUDGET PARSE] single: 0 - {high}")
        return (0, high)


def bedroom_to_float(config_str):
    if not config_str:
        return None
    m = re.search(r'(\d+)', str(config_str))
    if m:
        return float(m.group(1))
    if 'studio' in str(config_str).lower():
        return 0.0
    return None


def normalize_property_type(user_type):
    mapping = {
        'apartment':     ['Residential Apartment', 'Serviced Apartments', 'Studio Apartment'],
        'flat':          ['Residential Apartment', 'Studio Apartment'],
        'house':         ['Independent House/Villa'],
        'villa':         ['Independent House/Villa'],
        'bungalow':      ['Independent House/Villa', 'Farm House'],
        'farmhouse':     ['Farm House'],
        'studio':        ['Studio Apartment'],
        'plot':          ['Residential Land'],
        'land':          ['Residential Land'],
        'penthouse':     ['Residential Apartment'],
        'independent':   ['Independent House/Villa', 'Independent/Builder Floor'],
        'builder floor': ['Independent/Builder Floor'],
    }
    lower = str(user_type).lower().strip()
    for key, vals in mapping.items():
        if key in lower:
            return vals
    return ['Residential Apartment']


# =============================================================
# PROPERTY SEARCH ENGINE
# =============================================================

def search_properties(city, property_type, configuration, budget, amenities, offset=0):
    print(f"[SEARCH] city={city} type={property_type} config={configuration} "
          f"budget={budget} amenities={amenities}")

    load_lookup_maps()
    rows = load_city_dataset(city)
    if not rows:
        return []

    target_types    = normalize_property_type(property_type)
    target_bedrooms = bedroom_to_float(configuration)

    # Fix #10: Handle None budget range (ambiguous)
    budget_range = budget_to_inr_range(budget)
    if budget_range is None:
        budget_min, budget_max = 0, 999999999
    else:
        budget_min, budget_max = budget_range

    amenity_ids_wanted = set()
    if amenities and amenities.lower() not in ('no specific preference', 'no preference', ''):
        for label in amenities.split(','):
            label = label.strip().lower()
            for aid, alabel in _amenity_map.items():
                if label in alabel.lower():
                    amenity_ids_wanted.add(aid)
    print(f"[SEARCH] amenity_ids_wanted={amenity_ids_wanted}")

    matches    = []
    city_lower = city.lower()

    for row in rows:
        row_price = parse_price_to_inr(row.get('MIN_PRICE', 0))

        if target_types and row.get('PROPERTY_TYPE') not in target_types:
            continue

        row_city = str(row.get('CITY', '')).lower()
        loc_str  = str(row.get('location', '')).lower()
        if city_lower not in row_city and city_lower not in loc_str:
            continue

        if budget_max < 999999999 and row_price > (budget_max * 1.5):
            continue

        if target_bedrooms is not None:
            try:
                row_bed = float(str(row.get('BEDROOM_NUM', '')).strip())
                if abs(row_bed - target_bedrooms) > 1:
                    continue
            except:
                pass

        amenity_score = 0
        if amenity_ids_wanted:
            row_ids       = set(x.strip() for x in str(row.get('AMENITIES', '')).split(','))
            amenity_score = len(amenity_ids_wanted & row_ids)

        score = 0
        try:
            if target_bedrooms is not None:
                diff   = abs(float(row.get('BEDROOM_NUM', 0)) - target_bedrooms)
                score += 5 if diff == 0 else (3 if diff <= 1 else 0)
        except:
            pass

        budget_mid = (budget_min + budget_max) / 2
        if row_price > 0:
            score += max(0, 5 - abs(row_price - budget_mid) / 10000000)

        score += amenity_score * 3
        matches.append({'row': row, 'amenity_score': amenity_score,
                        'price_inr': row_price, 'score': score})

    print(f"[SEARCH] Matched {len(matches)} rows before ranking")

    if not matches:
        print("[SMART RELAX] Expanding budget range...")
        for row in rows:
            row_price = parse_price_to_inr(row.get('MIN_PRICE', 0))
            if row_price <= (budget_max * 1.5):
                matches.append({'row': row, 'amenity_score': 0,
                                'price_inr': row_price, 'score': 1})

    matches.sort(key=lambda x: -x['score'])

    results = []
    for m in matches[offset:offset + 3]:
        row            = m['row']
        amenity_labels = get_amenity_labels(row.get('AMENITIES', ''))
        top_amenities  = ', '.join(amenity_labels[:5]) if amenity_labels else 'Not specified'

        price_display = str(row.get('PRICE', '')).strip()
        if not price_display or price_display in ('nan', ''):
            p = m['price_inr']
            if p >= 10000000:
                price_display = f"Rs. {p/10000000:.2f} Cr"
            elif p >= 100000:
                price_display = f"Rs. {p/100000:.2f} L"
            elif p > 0:
                price_display = f"Rs. {p:,}"
            else:
                price_display = "Price on Request"

        locality = ''
        try:
            loc_data = ast.literal_eval(str(row.get('location', '{}')))
            locality = (loc_data.get('LOCALITY_NAME', '')
                        or loc_data.get('LOCALITY_WO_CITY', ''))
        except:
            pass

        society = str(row.get('SOCIETY_NAME', row.get('PROP_NAME', ''))).strip()
        if not society or society == 'nan':
            society = 'Property'

        results.append({
            'name':      society,
            'type':      str(row.get('PROPERTY_TYPE', '')),
            'bedrooms':  str(row.get('BEDROOM_NUM', '')).replace('.0', ''),
            'price':     price_display,
            'amenities': top_amenities,
            'locality':  locality,
            'city':      str(row.get('CITY', city)),
        })

    return results


def format_property_results(results):
    if not results:
        return None
    count = len(results)
    lines = [f"I found {count} matching {'property' if count == 1 else 'properties'} for you."]
    for i, p in enumerate(results, 1):
        beds = f"{p['bedrooms']} BHK " if p['bedrooms'] and p['bedrooms'] != 'nan' else ""
        loc  = f" in {p['locality']}," if p['locality'] else ","
        lines.append(
            f"Option {i}: {p['name']}. "
            f"{beds}{p['type']}{loc} {p['city']}. "
            f"Price: {p['price']}. "
            f"Key amenities: {p['amenities']}."
        )
        lines.append(f"Why this property: Matches your budget and includes {p['amenities']}.")
    lines.append("Would you like more details on any of these, or shall I show more options?")
    response = " ".join(lines)
    if len(response) > 40000:
        response = response[:40000]
    print(f"[RESULT COUNT RETURNED] {len(results)}")
    return response


# =============================================================
# AMAZON TRANSCRIBE (Fix #3: Async with shorter timeout)
# =============================================================

def transcribe_audio(audio_s3_key, hint_lang=None):
    """
    Transcribe audio from S3. Uses a shorter timeout (15s) to avoid
    blocking Lambda for too long. For Twilio flow, prefer using
    Twilio's built-in <Gather> speech recognition instead.
    """
    job_name  = f"voicebot_{int(time.time() * 1000)}"
    audio_uri = f"s3://{AUDIO_BUCKET}/{audio_s3_key}"
    print(f"[TRANSCRIBE] {job_name} | {audio_uri}")

    job_params = {
        "TranscriptionJobName": job_name,
        "Media":            {"MediaFileUri": audio_uri},
        "MediaFormat":      "wav",
        "OutputBucketName": AUDIO_BUCKET,
        "OutputKey":        f"output/{job_name}.json"
    }
    if hint_lang and hint_lang in TRANSCRIBE_LANG_CODES:
        job_params["LanguageCode"] = TRANSCRIBE_LANG_CODES[hint_lang]
    else:
        job_params["IdentifyLanguage"] = True
        job_params["LanguageOptions"]  = TRANSCRIBE_AUTO_DETECT

    transcribe.start_transcription_job(**job_params)

    # Fix #3: Reduced timeout from 60s to 15s to avoid Lambda timeout
    max_wait = 15
    for _ in range(max_wait):
        time.sleep(1)
        result = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        status = result['TranscriptionJob']['TranscriptionJobStatus']
        if status == 'COMPLETED':
            try:
                obj  = s3.get_object(Bucket=AUDIO_BUCKET, Key=f"output/{job_name}.json")
                data = json.loads(obj['Body'].read())
                text = data['results']['transcripts'][0]['transcript']
            except Exception as e:
                print(f"[TRANSCRIBE] Read error: {e}")
                return "", hint_lang or "en"
            detected = result['TranscriptionJob'].get(
                'LanguageCode', TRANSCRIBE_LANG_CODES.get(hint_lang, 'en-IN')
            )
            lang = detected.split('-')[0]
            if lang not in SUPPORTED_LANGUAGES:
                lang = "en"
            return text, lang
        elif status == 'FAILED':
            print(f"[TRANSCRIBE] Failed: {result['TranscriptionJob'].get('FailureReason')}")
            return "", hint_lang or "en"

    print(f"[TRANSCRIBE] Timed out after {max_wait}s")
    return "", hint_lang or "en"


# =============================================================
# LANGUAGE DETECTION (Fix #8: Confidence-based switching)
# =============================================================

def detect_language(text):
    """
    Detect language with confidence scoring.
    Returns (language_code, confidence_score) tuple.

    IMPORTANT: Defaults to English unless there is STRONG evidence of Hindi/Marathi.
    This prevents false positives when English text contains words that look like
    Hindi transliterations (e.g., "main" in English vs "main" meaning "I" in Hindi).

    Also handles the case where Twilio transcribes English speech in Devanagari
    (e.g., "आई एम लुकिंग फॉर ए फ्लैट" = "I am looking for a flat" in Devanagari).
    """
    if not text or not text.strip():
        return "en", 0.0
    tl = text.lower()
    word_count = len(text.split())

    # Layer 1: Devanagari script check
    devanagari_count = sum(1 for c in text if '\u0900' <= c <= '\u097F')
    if devanagari_count > 2:
        # Check if this is ACTUAL Hindi or just English transliterated in Devanagari
        # Common English words written in Devanagari by Twilio's hi-IN transcription
        # ONLY include words that are PURELY English with no Hindi usage
        # Do NOT include: नो, यस, थैंक यू — these are used in Hindi conversations
        english_in_devanagari = [
            "आई एम", "लुकिंग", "फॉर", "अपार्टमेंट",
            "हाउस", "विला", "बीएचके", "प्रॉपर्टी",
            "ओके", "प्लीज",
            "वन", "टू", "थ्री", "फोर", "फाइव",
            "हेलो", "गुड", "बाय", "स्टार्ट",
            "आई वांट", "आई नीड", "माई नेम"
        ]
        # True Hindi words (not English transliterations)
        # These are words that ONLY exist in Hindi, never in English
        # Avoid very short words (2 chars) that match as substrings
        true_hindi_words = [
            "मुझे", "चाहिए", "कहाँ", "कितना", "क्या",
            "मेरा", "मेरी", "हमें", "ढूंढ", "बताओ",
            "शुक्रिया", "अच्छा", "ठीक", "नहीं",
            "कोई", "कुछ", "वाला", "वाली",
            "दिखाओ", "खोजो", "पसंद", "रहा", "रही",
            "बजट", "लाख", "करोड़", "करोड", "फ्लैट",
            "माय", "मैं", "आप", "हम",
            "नो थैंक", "धन्यवाद", "शुक्रिया",
        ]
        # Marathi-specific words
        marathi_words = ["मला", "पाहिजे", "आहे", "नाही", "आम्हाला"]

        english_hits = sum(1 for w in english_in_devanagari if w in text)
        hindi_hits = sum(1 for w in true_hindi_words if w in text)
        marathi_hits = sum(1 for w in marathi_words if w in text)

        print(f"[LANG] Devanagari detected: english_hits={english_hits}, hindi_hits={hindi_hits}, marathi_hits={marathi_hits}")

        # Only classify as English transliteration if:
        # - Multiple English-exclusive words found (>=2)
        # - AND zero Hindi/Marathi words found
        # This prevents false positives on mixed text like "माय बजट इस 1 crore"
        if english_hits >= 2 and hindi_hits == 0 and marathi_hits == 0:
            print(f"[LANG] Devanagari text is English transliteration, treating as English")
            return "en", 0.7

        if marathi_hits > 0 and marathi_hits >= hindi_hits:
            return "mr", 0.95
        if hindi_hits > 0:
            return "hi", 0.95

        # If no clear signal from word matching, default to Hindi for Devanagari text
        # (Devanagari script = Hindi unless proven otherwise)
        return "hi", 0.85

    # Layer 2: Keyword scoring — STRICT matching for romanized text
    # Only count words that are EXCLUSIVELY Hindi (not English words)
    # NOTE: "mai" is Hindi for "I" but "main" is also English — handle carefully
    hi_exclusive_words = ["mujhe", "mera", "chahiye", "chahie", "kya", "nahi", "haan",
                          "dhund", "dhundh", "chahta", "chahti", "batao", "shukriya",
                          "achha", "theek", "hoon", "hun", "kahan", "kitna",
                          "ghar", "kamra", "paisa", "hai", "hum",
                          "namaste", "ji", "meri", "humko", "humein",
                          "dekho", "dikhao", "bolo", "sunao",
                          "raha", "rahi", "rahe", "wala", "wali",
                          "karo", "kijiye", "dijiye", "chahiye",
                          "mai", "hoon", "hu",
                          "dhoond", "khoj", "pasand",
                          "zaroorat", "zarurat", "lena", "dena",
                          "acha", "accha", "thik", "bilkul"]

    # "main" is ambiguous (English "main road" vs Hindi "main = I")
    # Only count it as Hindi if another Hindi word is also present
    hi_ambiguous_words = ["main", "mein"]
    mr_exclusive_words = ["namaskar", "mala", "pahije", "ahe", "aahe",
                          "shodhat", "havay", "kaay", "kuthe", "madhe",
                          "nakko", "marathi", "flat pahije",
                          "mumbai madhe", "punyat", "aahet", "havet"]

    hi_score = sum(1 for w in hi_exclusive_words if re.search(r'\b'+w+r'\b', tl))
    mr_score = sum(1 for w in mr_exclusive_words if re.search(r'\b'+w+r'\b', tl))

    # Count ambiguous words only if at least one exclusive Hindi word is present
    if hi_score >= 1:
        hi_score += sum(1 for w in hi_ambiguous_words if re.search(r'\b'+w+r'\b', tl))

    # A single Hindi keyword is enough evidence if it's an exclusive word
    if hi_score >= 1 and hi_score > mr_score:
        confidence = min(hi_score / max(word_count, 1) + 0.4, 1.0)
        return "hi", confidence
    if mr_score >= 1 and mr_score > hi_score:
        confidence = min(mr_score / max(word_count, 1) + 0.4, 1.0)
        return "mr", confidence

    # Layer 3: AWS Comprehend — ONLY if keyword scoring gave no result
    # This saves ~100ms on most turns where keywords are sufficient
    try:
        langs = sorted(
            comprehend.detect_dominant_language(Text=text[:300]).get('Languages', []),
            key=lambda x: x['Score'], reverse=True
        )
        for l in langs:
            code = l['LanguageCode']
            score = l['Score']
            # For English, accept at normal threshold
            if code == 'en' and score >= COMPREHEND_MIN_CONFIDENCE:
                return 'en', score
            # For Hindi/Marathi, require HIGHER confidence to avoid false positives
            if code in ('hi', 'mr') and score >= 0.7:
                return code, score
    except Exception as e:
        print(f"[LANG L3] {e}")

    # Layer 4: Skip Translate auto-detect (adds 100ms, rarely useful)
    # Default to English if nothing else matched

    # DEFAULT: English (unless proven otherwise)
    return "en", 0.6


def should_switch_language(detected_lang, detected_confidence, current_lang, word_count):
    """
    Determine if the bot should switch to the detected language.

    Rules:
    - Default language is English. But switch readily when Hindi/Marathi is detected.
    - Switch TO Hindi/Marathi if confidence >= 0.5 (a single Hindi keyword is enough).
    - Switch BACK to English if confidence >= 0.5.
    - Single-word utterances never trigger a switch (too ambiguous).
    - Context (step, collected fields) is NEVER reset on language switch.
    """
    if not current_lang:
        return True  # No language set yet, accept whatever is detected

    if current_lang == detected_lang:
        return True  # Same language, no switch needed

    # Single-word utterances should NEVER trigger a language switch
    if word_count <= 1:
        print(f"[LANG SWITCH] Skipping: single word, keeping '{current_lang}'")
        return False

    # Any direction: switch if confidence >= 0.5
    if detected_confidence >= 0.5:
        print(f"[LANG SWITCH] '{current_lang}' → '{detected_lang}' (confidence={detected_confidence:.2f}, words={word_count})")
        return True

    print(f"[LANG SWITCH] Keeping '{current_lang}' (confidence={detected_confidence:.2f} < 0.5)")
    return False


# =============================================================
# TRANSLATION (Fix #9: Ensure LLM responds in English)
# =============================================================

def translate_to_english(text, source_lang):
    """
    Translate to English for field extraction.
    OPTIMIZATION: Skip translation for romanized Hindi (Latin script) —
    regex extraction works directly on "mujhe mumbai mein flat chahiye".
    Only translate actual Devanagari text.
    """
    if source_lang == "en" or not text.strip():
        return text

    # Check if text is in Latin script (romanized Hindi)
    # If so, skip translation — regex can extract from it directly
    has_devanagari = any('\u0900' <= c <= '\u097F' for c in text)
    if not has_devanagari:
        # Romanized Hindi like "mujhe mumbai mein flat chahiye"
        # No need to translate — extraction works on this directly
        print(f"[→EN SKIP] Romanized text, using as-is: '{text[:40]}'")
        return text

    # Only translate actual Devanagari text
    try:
        result = translate.translate_text(
            Text=text, SourceLanguageCode=source_lang, TargetLanguageCode="en"
        ).get('TranslatedText', text)
        print(f"[→EN] '{text[:40]}' → '{result[:40]}'")
        return result
    except Exception as e:
        print(f"[→EN ERROR] {e}")
        return text


def translate_reply(english_reply, target_lang):
    """
    Translate English reply to target language.
    If translation fails or returns garbage, return the English original
    (better to speak English than gibberish).
    """
    if target_lang == "en" or not english_reply.strip():
        return english_reply

    # If the reply already contains Devanagari script, skip translation
    has_devanagari = any('\u0900' <= c <= '\u097F' for c in english_reply)
    if has_devanagari:
        print(f"[TRANSLATE SKIP] Already contains Devanagari, skipping translation.")
        return english_reply

    try:
        result = translate.translate_text(
            Text=english_reply, SourceLanguageCode="en", TargetLanguageCode=target_lang
        ).get('TranslatedText', english_reply)

        # Sanity check: if translation is empty or same as input, return original
        if not result or not result.strip():
            print(f"[→{target_lang}] Translation returned empty, using English")
            return english_reply

        print(f"[→{target_lang}] '{english_reply[:40]}' → '{result[:40]}'")
        return result
    except Exception as e:
        print(f"[→{target_lang} ERROR] {e}")
        return english_reply


# =============================================================
# AMENITY EXTRACTION
# =============================================================

def extract_amenities_locally(text_english):
    cleaned  = text_english.strip().lower()
    original = text_english.strip()

    # Check no-preference phrases first (English + Hindi + Marathi)
    for phrase in NO_PREFERENCE_PHRASES:
        phrase_lower = phrase.lower()
        if phrase_lower == cleaned or cleaned.startswith(phrase_lower):
            return "No specific preference"
        if phrase in original:
            return "No specific preference"

    found = []

    # Step 1: Check Hindi/Marathi words in original text
    for hindi_word, english_equiv in HINDI_AMENITY_MAP.items():
        if hindi_word in original:
            label = english_equiv.title()
            if english_equiv == 'gym':                           label = 'Gym'
            elif english_equiv == 'swimming pool':               label = 'Swimming Pool'
            elif english_equiv == 'parking':                     label = 'Parking'
            elif english_equiv == 'lift':                        label = 'Lift'
            elif english_equiv == 'garden':                      label = 'Garden'
            elif english_equiv == 'security':                    label = 'Security'
            elif english_equiv in ('clubhouse', 'club house'):   label = 'Clubhouse'
            elif english_equiv == 'cctv':                        label = 'CCTV'
            elif english_equiv == 'wifi':                        label = 'WiFi'
            elif english_equiv == 'power backup':                label = 'Power Backup'
            elif english_equiv == 'playground':                  label = 'Playground'
            elif english_equiv == 'jogging track':               label = 'Jogging Track'
            elif english_equiv == 'intercom':                    label = 'Intercom'
            elif english_equiv == 'terrace':                     label = 'Terrace'
            elif english_equiv == 'balcony':                     label = 'Balcony'
            elif english_equiv == 'elevator':                    label = 'Lift'
            if label not in found:
                found.append(label)
                print(f"[HINDI AMENITY] '{hindi_word}' → '{label}'")

    # Step 2: Check English keywords in cleaned text
    for kw in AMENITY_KEYWORDS:
        pattern = r'\b' + re.escape(kw) + r'\b'
        if re.search(pattern, cleaned):
            label = kw.title()
            if kw == 'gym':                          label = 'Gym'
            elif kw == 'swimming pool':              label = 'Swimming Pool'
            elif kw == 'parking':                    label = 'Parking'
            elif kw == 'pool':                       label = 'Swimming Pool'
            elif kw == 'lift':                       label = 'Lift'
            elif kw == 'park':                       label = 'Park'
            elif kw == 'garden':                     label = 'Garden'
            elif kw == 'security':                   label = 'Security'
            elif kw in ('clubhouse', 'club house'):  label = 'Clubhouse'
            elif kw == 'cctv':                       label = 'CCTV'
            elif kw == 'wifi':                       label = 'WiFi'
            if label not in found:
                found.append(label)

    # Step 3: If still nothing found and input has Devanagari, try translate
    if not found and any('\u0900' <= c <= '\u097F' for c in original):
        print(f"[AMENITY] Hindi not matched locally, trying translate...")
        try:
            translated       = translate.translate_text(
                Text=original, SourceLanguageCode="hi", TargetLanguageCode="en"
            ).get('TranslatedText', '')
            print(f"[AMENITY TRANSLATE] '{original}' → '{translated}'")
            translated_lower = translated.strip().lower()

            for phrase in NO_PREFERENCE_PHRASES:
                if phrase.lower() in translated_lower:
                    return "No specific preference"

            for kw in AMENITY_KEYWORDS:
                pattern = r'\b' + re.escape(kw) + r'\b'
                if re.search(pattern, translated_lower):
                    label = kw.title()
                    if kw == 'gym':                          label = 'Gym'
                    elif kw == 'swimming pool':              label = 'Swimming Pool'
                    elif kw == 'parking':                    label = 'Parking'
                    elif kw == 'pool':                       label = 'Swimming Pool'
                    elif kw == 'lift':                       label = 'Lift'
                    elif kw == 'garden':                     label = 'Garden'
                    elif kw == 'security':                   label = 'Security'
                    elif kw in ('clubhouse', 'club house'):  label = 'Clubhouse'
                    elif kw == 'cctv':                       label = 'CCTV'
                    if label not in found:
                        found.append(label)
        except Exception as e:
            print(f"[AMENITY TRANSLATE ERROR] {e}")

    return ", ".join(found) if found else ""


# =============================================================
# FIELD EXTRACTION via Bedrock (Fix #4: Single combined call)
# =============================================================

def extract_all_fields(text_english):
    """
    Extract ALL fields in a single Bedrock call.
    OPTIMIZATION: Try regex extraction first. Only call Bedrock if regex
    can't extract the property type (the most important field).
    """
    result = {}

    # ── FAST PATH: Regex extraction (0ms) ─────────────────────
    text_lower = text_english.lower().strip()

    # Property type
    for pt in VALID_PROPERTY_TYPES:
        if pt in text_lower:
            result['property_type'] = pt
            break

    # Configuration
    m = re.search(r'(\d)\s*bhk', text_lower, re.IGNORECASE)
    if m:
        result['configuration'] = f"{m.group(1)}BHK"
    elif 'studio' in text_lower:
        result['configuration'] = "Studio"

    # Location
    for city in CITY_FILE_MAP.keys():
        if city in text_lower:
            result['location'] = city.title()
            break

    # Budget
    has_number = bool(re.search(r'\d', text_lower))
    has_unit = any(w in text_lower for w in ['lakh', 'lakhs', 'lac', 'crore', 'cr'])
    if has_number and has_unit:
        budget_match = re.search(r'([\d.]+\s*(?:to|-|se|से)\s*[\d.]+\s*(?:lakh|lakhs|lac|crore|cr)s?|[\d.]+\s*(?:lakh|lakhs|lac|crore|cr)s?)', text_lower)
        if budget_match:
            result['budget'] = budget_match.group(1).strip()

    # If we got property_type from regex, skip Bedrock entirely
    if result.get('property_type'):
        # Fill missing fields with empty strings
        for field in ['property_type', 'configuration', 'location', 'budget']:
            if field not in result:
                result[field] = ''
        print(f"[EXTRACT ALL] Regex fast path: {result}")
        return result

    # ── SLOW PATH: Bedrock LLM (only if regex failed) ─────────
    prompt = f"""Extract property search details from the user message.
Only extract values that are EXPLICITLY mentioned. Use empty string "" for anything not mentioned.

Property types: apartment, flat, house, villa, bungalow, penthouse, studio, plot, farmhouse
Configurations: 1BHK, 2BHK, 3BHK, 4BHK, 5BHK, Studio
Cities: Mumbai, Gurgaon, Hyderabad, Kolkata, Pune, Delhi, Thane, Navi Mumbai
Budget examples: "50 lakhs", "1 crore", "1.5-2.5 crore", "50-75 lakh"

Hindi/Marathi budget words: करो/करोड़ = crore, लाख = lakh

Return ONLY valid JSON on ONE line. No explanation.
Format: {{"property_type": "", "configuration": "", "location": "", "budget": ""}}

User: {text_english}
Output:"""

    try:
        response = bedrock.invoke_model(
            modelId="meta.llama3-8b-instruct-v1:0",
            body=json.dumps({
                "prompt":      prompt,
                "max_gen_len": 80,
                "temperature": 0
            })
        )
        raw = json.loads(response['body'].read()).get('generation', '').strip()
        print(f"[EXTRACT ALL] {raw[:120]}")

        match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if not match:
            return result  # Return whatever regex found

        json_str = match.group(0)
        json_str = re.sub(r':\s*(None|null|undefined)', ': ""', json_str)

        parsed = json.loads(json_str)

        # Merge Bedrock results with regex results (regex takes priority)
        for field in ['property_type', 'configuration', 'location', 'budget']:
            if field not in result or not result[field]:
                value = parsed.get(field, "")
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                if value is None or str(value).lower() in ('none', 'null', 'undefined', 'n/a'):
                    value = ""
                result[field] = str(value).strip()

        print(f"[EXTRACT ALL RESULT] {result}")
        return result

    except Exception as e:
        print(f"[EXTRACT ALL] Error: {e}")
        # Return whatever regex found
        for field in ['property_type', 'configuration', 'location', 'budget']:
            if field not in result:
                result[field] = ''
        return result


def extract_single_field(field_name, text_english):
    """
    Fallback: Extract a single field. Used when the combined extraction
    didn't capture a specific field and we need to re-ask.
    Uses regex first (Fix #4), falls back to Bedrock only if needed.
    """
    # Try regex extraction first for simple fields
    text_lower = text_english.lower().strip()

    if field_name == "configuration":
        m = re.search(r'(\d)\s*bhk', text_lower, re.IGNORECASE)
        if m:
            return f"{m.group(1)}BHK"
        if 'studio' in text_lower:
            return "Studio"

    if field_name == "property_type":
        for pt in VALID_PROPERTY_TYPES:
            if pt in text_lower:
                return pt

    if field_name == "location":
        for city in CITY_FILE_MAP.keys():
            if city in text_lower:
                return city.title()

    if field_name == "budget":
        # Check if it has numbers + unit keywords
        has_number = bool(re.search(r'\d', text_lower))
        has_unit = any(w in text_lower for w in ['lakh', 'lakhs', 'lac', 'crore', 'cr'])
        if has_number and has_unit:
            # Extract just the budget part (number + unit), not the full sentence
            budget_match = re.search(r'([\d.]+\s*(?:to|-|se|से)\s*[\d.]+\s*(?:lakh|lakhs|lac|crore|cr)s?|[\d.]+\s*(?:lakh|lakhs|lac|crore|cr)s?)', text_lower)
            if budget_match:
                return budget_match.group(1).strip()
            return text_english.strip()

    # Fallback to Bedrock for complex cases
    prompts = {
        "property_type": f"""Extract the property type from the user message ONLY if explicitly mentioned.
Types: apartment, flat, house, villa, bungalow, penthouse, studio, plot, farmhouse.
If not mentioned, return empty string "".
Return ONLY valid JSON on ONE line. No explanation.
Format: {{"property_type": "value"}}
User: {text_english}
Output:""",

        "configuration": f"""Extract the BHK configuration ONLY if explicitly mentioned.
Examples: 1BHK, 2BHK, 3BHK, 4BHK, 5BHK, Studio.
If not mentioned, return empty string "".
Return ONLY valid JSON on ONE line. No explanation.
Format: {{"configuration": "value"}}
User: {text_english}
Output:""",

        "location": f"""Extract the city name ONLY if explicitly mentioned.
Indian cities only: Mumbai, Gurgaon, Hyderabad, Kolkata, Pune, Delhi, Thane.
If no city mentioned, return empty string "".
Return ONLY valid JSON on ONE line. No explanation.
Format: {{"location": "value"}}
User: {text_english}
Output:""",

        "budget": f"""Extract the budget amount ONLY if explicitly mentioned.
Hindi budget words: करो/करोड़ = crore, लाख = lakh
Examples: "50 lakhs", "1 crore", "1.5-2.5 crore"
If not mentioned, return empty string "".
Return ONLY valid JSON on ONE line. No explanation.
Format: {{"budget": "value"}}
User: {text_english}
Output:"""
    }

    if field_name not in prompts:
        return ""

    try:
        response = bedrock.invoke_model(
            modelId="meta.llama3-8b-instruct-v1:0",
            body=json.dumps({
                "prompt":      prompts[field_name],
                "max_gen_len": 40,
                "temperature": 0
            })
        )
        raw   = json.loads(response['body'].read()).get('generation', '').strip()
        print(f"[EXTRACT {field_name}] {raw[:80]}")

        match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if not match:
            return ""

        json_str = match.group(0)
        json_str = re.sub(r':\s*(None|null|undefined)', ': ""', json_str)

        parsed = json.loads(json_str)
        value  = parsed.get(field_name, "")

        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        if value is None or str(value).lower() in ('none', 'null', 'undefined', 'n/a'):
            return ""

        return str(value).strip()

    except Exception as e:
        print(f"[EXTRACT {field_name}] Error: {e}")
        return ""


# =============================================================
# VALIDATION
# =============================================================

def is_meaningful(value, field_name):
    if not value or not str(value).strip():
        return False
    noise   = {"hello", "hi", "hey", "yes", "no", "ok", "okay", "sure",
               "invalid", "null", "na", "n/a", "idk", "any", "whatever",
               "namaste", "namaskar", "none", "undefined"}
    cleaned = str(value).strip().lower()
    if cleaned in noise:
        return False
    if field_name == "property_type":
        return any(pt in cleaned for pt in VALID_PROPERTY_TYPES)
    if field_name == "configuration":
        return (any(c in cleaned for c in VALID_CONFIGURATIONS) or
                bool(re.search(r'\d\s*bhk', cleaned, re.IGNORECASE)))
    if field_name == "amenities":
        return ("no specific preference" in cleaned or
                any(re.search(r'\b' + re.escape(kw) + r'\b', cleaned) for kw in AMENITY_KEYWORDS))
    if field_name == "location":
        if cleaned in ('none', 'null', 'na', 'n/a'):
            return False
        return len(cleaned) >= 3 and not cleaned.isdigit()
    if field_name == "budget":
        return (bool(re.search(r'\d', cleaned)) or
                any(kw in cleaned for kw in ["lakh", "crore", "lakhs", "crores", "lac"]))
    return True


# =============================================================
# INTENT HELPERS
# =============================================================

def user_is_done(text_en):
    """
    Detect if user wants to end the conversation.
    Only triggers on clear exit phrases, not just "thank you" alone
    (which could be mid-conversation politeness).
    """
    text = text_en.strip().lower()
    # Strong exit signals (always end)
    strong_exit = ["bye", "goodbye", "see you", "that's all", "i'm done",
                   "im done", "all set", "no need", "nothing else", "no more"]
    if any(p in text for p in strong_exit):
        return True
    # "thank you" only ends if it's the main content (short utterance)
    # or combined with exit words
    if "thank" in text and len(text.split()) <= 4:
        return True
    # "done" only if it's the primary word
    if text in ("done", "i am done", "im done", "all done"):
        return True
    return False


def user_wants_restart(text_en):
    phrases = ["start over", "restart", "reset", "begin again", "new search",
               "change requirements", "different property", "start again"]
    return any(p in text_en.strip().lower() for p in phrases)


def user_wants_more(text_en):
    phrases = ["more", "other options", "another", "show more",
               "more options", "refine", "something else", "next"]
    return any(p in text_en.strip().lower() for p in phrases)


def user_wants_stop(text_en):
    """Detect if user wants to stop viewing results."""
    phrases = ["stop", "enough", "that's enough", "no more", "sufficient",
               "bas", "ruk", "band karo"]
    return any(p in text_en.strip().lower() for p in phrases)


# =============================================================
# MAIN HANDLER (Fix #4, #5, #6, #8, #9 applied)
# =============================================================

def lambda_handler(event, context):

    print(f"[EVENT KEYS] {list(event.keys())}")
    session_attributes = event.get('sessionState', {}).get('sessionAttributes', {})
    intent_name        = event.get('sessionState', {}).get('intent', {}).get('name', 'FallbackIntent')
    input_mode         = event.get('inputMode', 'Text')

    print(f"[INPUT MODE]     {input_mode}")
    print(f"[SESSION BEFORE] {session_attributes}")

    user_lang    = session_attributes.get('user_lang', DEFAULT_LANGUAGE)
    user_text_en = ""
    user_text_raw = ""

    # ── MODE A: VOICE via Transcribe ──────────────────────────
    if event.get('audio_s3_key'):
        audio_key          = event['audio_s3_key']
        user_text_raw, _   = transcribe_audio(
            audio_s3_key=audio_key,
            hint_lang=user_lang if user_lang else None
        )
        if not user_text_raw:
            error_msg = translate_reply(
                "I'm sorry, I couldn't hear you clearly. Could you please repeat?",
                user_lang or DEFAULT_LANGUAGE
            )
            return build_response(intent_name, error_msg, session_attributes)

        # Detect language and switch if user changed language mid-call
        detected_lang, confidence = detect_language(user_text_raw)
        word_count = len(user_text_raw.split())

        if should_switch_language(detected_lang, confidence, user_lang, word_count):
            old_lang = user_lang
            user_lang = detected_lang
            if old_lang != user_lang:
                print(f"[MID-CALL LANG SWITCH] '{old_lang}' → '{user_lang}' (context preserved)")

        if not user_lang:
            user_lang = DEFAULT_LANGUAGE
        session_attributes['user_lang'] = user_lang

        print(f"[LANG DETECTED - AUDIO] {detected_lang} (conf={confidence:.2f}), using: {user_lang}")
        user_text_en = translate_to_english(user_text_raw, user_lang)

    # ── MODE B: TEXT via Lex / Bridge Lambda ──────────────────
    else:
        user_text_raw = event.get('inputTranscript', '').strip()
        if input_mode == 'Speech':
            user_text_raw = correct_stt_errors(user_text_raw)

        if user_text_raw:
            # Detect language and switch if user changed language mid-call
            detected_lang, confidence = detect_language(user_text_raw)
            word_count = len(user_text_raw.split())

            if should_switch_language(detected_lang, confidence, user_lang, word_count):
                old_lang = user_lang
                user_lang = detected_lang
                if old_lang != user_lang:
                    print(f"[MID-CALL LANG SWITCH] '{old_lang}' → '{user_lang}' (context preserved)")

            if not user_lang:
                user_lang = detected_lang if detected_lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

            session_attributes['user_lang'] = user_lang
            print(f"[LANG DETECTED - TEXT] {detected_lang} (conf={confidence:.2f}), using: {user_lang}")
            user_text_en = translate_to_english(user_text_raw, user_lang)
        else:
            user_text_en = ""
            if not user_lang:
                user_lang = DEFAULT_LANGUAGE

    if not user_lang:
        user_lang = DEFAULT_LANGUAGE
        session_attributes['user_lang'] = user_lang

    print(f"[LANG]    {user_lang}")
    print(f"[USER EN] {user_text_en}")

    # ── Fix #5: Removed separate intent classification LLM call ──
    # Intent is now determined by the step-based state machine and
    # keyword matching, which is faster and more reliable.

    # ── reply() helper ──────────────────────────────────────────
    # Translates the English response to the user's current language.
    # Language switch is seamless: only user_lang changes, all other
    # session attributes (step, property_type, etc.) remain intact.
    def reply(message, close=False):
        if user_lang != 'en':
            message = translate_reply(message, user_lang)
        print(f"[LANG]       {user_lang}")
        print(f"[BOT REPLY]  {message[:120]}")
        return build_response(intent_name, message, session_attributes, close=close)

    # ── Global checks ──────────────────────────────────────────
    if user_is_done(user_text_en):
        session_attributes = {'user_lang': user_lang}
        return reply(
            "Thank you! Have a great day. Goodbye!",
            close=True
        )

    if user_wants_restart(user_text_en):
        session_attributes = {'user_lang': user_lang, 'step': 'property_type'}
        return reply("Sure, let's start fresh. What type of property are you looking for?")

    current_step = session_attributes.get('step', 'greet')

    # ── GREET ──────────────────────────────────────────────────
    if current_step == 'greet' or not user_text_en:
        session_attributes['step'] = 'property_type'
        words = user_text_en.split()
        if len(words) >= 3:
            # Try to extract property type from the first utterance
            fields = extract_all_fields(user_text_en)
            prop = fields.get('property_type', '')
            if not is_meaningful(prop, 'property_type'):
                # Try single field extraction as fallback
                prop = extract_single_field('property_type', user_text_en)

            if is_meaningful(prop, 'property_type'):
                session_attributes['property_type'] = prop.strip().lower()
                # Also grab other fields if mentioned
                config = fields.get('configuration', '') if len(words) >= 5 else ''
                if is_meaningful(config, 'configuration'):
                    session_attributes['configuration'] = config.strip().upper()
                loc = fields.get('location', '') if len(words) >= 5 else ''
                if is_meaningful(loc, 'location'):
                    session_attributes['location'] = loc.strip().title()
                bud = fields.get('budget', '') if len(words) >= 5 else ''
                if is_meaningful(bud, 'budget'):
                    session_attributes['budget'] = bud.strip()

                # Determine next step based on what's missing
                if not is_meaningful(session_attributes.get('configuration', ''), 'configuration'):
                    session_attributes['step'] = 'configuration'
                    return reply(
                        f"Got it, a {prop.title()}. "
                        f"What configuration do you need? 1BHK, 2BHK, 3BHK, or 4BHK?"
                    )
                elif not session_attributes.get('amenities'):
                    session_attributes['step'] = 'amenities'
                    return reply(
                        f"Got it, {session_attributes.get('configuration', '')} {prop.title()}. "
                        f"Any preferred amenities? Parking, Gym, Pool, Garden, or Lift? "
                        f"Or say No preference."
                    )
                elif not is_meaningful(session_attributes.get('location', ''), 'location'):
                    session_attributes['step'] = 'location'
                    return reply(
                        f"Which city are you looking in? "
                        f"Mumbai, Gurgaon, Hyderabad, or Kolkata?"
                    )
                elif not is_meaningful(session_attributes.get('budget', ''), 'budget'):
                    session_attributes['step'] = 'budget'
                    return reply("What is your budget?")
                else:
                    session_attributes['step'] = 'confirm'
                    return reply(
                        f"Let me confirm: {session_attributes.get('configuration', '')} {prop.title()} "
                        f"in {session_attributes.get('location', '')}, "
                        f"budget {session_attributes.get('budget', '')}. "
                        f"Shall I search? Yes or No."
                    )
        # No property type detected — ask for it
        return reply("What type of property are you looking for? "
                     "Apartment, Flat, Villa, House, or Bungalow?")

    # ── PROPERTY TYPE ──────────────────────────────────────────
    if current_step == 'property_type':
        value = extract_single_field('property_type', user_text_en)
        if is_meaningful(value, 'property_type'):
            session_attributes['property_type'] = value.strip().lower()
            session_attributes['step']          = 'configuration'
            return reply(
                f"Got it, a {value.title()}. "
                f"What configuration do you need? 1BHK, 2BHK, 3BHK, or 4BHK?"
            )
        return reply(
            "What type of property are you looking for? "
            "Apartment, Flat, Villa, House, or Bungalow?"
        )

    # ── CONFIGURATION ──────────────────────────────────────────
    if current_step == 'configuration':
        value = extract_single_field('configuration', user_text_en)
        if is_meaningful(value, 'configuration'):
            session_attributes['configuration'] = value.strip().upper()
            session_attributes['step']          = 'amenities'
            return reply(
                f"{value.upper()}, noted. "
                f"Any preferred amenities? Parking, Gym, Pool, Garden, or Lift? "
                f"Or say No preference."
            )
        return reply("Which configuration? 1BHK, 2BHK, 3BHK, or 4BHK?")

    # ── AMENITIES ──────────────────────────────────────────────
    if current_step == 'amenities':
        value = extract_amenities_locally(user_text_en)
        if value:
            session_attributes['amenities'] = value
            session_attributes['step']      = 'location'
            return reply(
                "Noted. Which city are you looking in? "
                "Mumbai, Gurgaon, Hyderabad, or Kolkata?"
            )
        return reply(
            "Any preferred amenities? Parking, Gym, Pool, Garden, or Lift? "
            "Or say No preference."
        )

    # ── LOCATION ───────────────────────────────────────────────
    if current_step == 'location':
        value = extract_single_field('location', user_text_en)
        if is_meaningful(value, 'location'):
            session_attributes['location'] = value.strip().title()
            session_attributes['step']     = 'budget'
            return reply(
                f"{value.title()}, great. What is your budget? "
                f"For example 50 Lakhs or 1 Crore."
            )
        return reply(
            "Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?"
        )

    # ── BUDGET (Fix #10: Handle ambiguous budget) ──────────────
    if current_step == 'budget':
        value = extract_single_field('budget', user_text_en)
        if is_meaningful(value, 'budget'):
            # Fix #10: Check if budget unit is ambiguous
            budget_range = budget_to_inr_range(value)
            if budget_range is None:
                return reply(
                    f"Is that {value} in Lakhs or Crores? Please specify."
                )
            session_attributes['budget'] = value.strip()
            session_attributes['step']   = 'confirm'
            prop   = session_attributes.get('property_type', '').title()
            config = session_attributes.get('configuration', '')
            amen   = session_attributes.get('amenities', 'None')
            loc    = session_attributes.get('location', '')
            return reply(
                f"Let me confirm. {config} {prop} in {loc}, "
                f"budget {value.strip()}, amenities {amen}. "
                f"Shall I search? Yes or No."
            )
        return reply("What is your budget? For example 50 Lakhs or 1 Crore.")

    # ── CONFIRM ────────────────────────────────────────────────
    if current_step == 'confirm':
        cleaned       = user_text_en.strip().lower()
        original_text = user_text_raw.strip().lower() if user_text_raw else ""

        yes_words = [
            # English
            "yes", "go ahead", "proceed", "sure", "okay", "ok",
            "yeah", "yep", "correct", "right", "confirmed", "please", "search",
            # Hindi
            "यस", "हाँ", "हां", "हा", "ठीक है", "ठीक", "बिल्कुल",
            "सर्च", "खोजो", "ढूंढो", "चलो", "जी हाँ", "जी",
            # Marathi
            "हो", "होय", "ठीक आहे", "चालेल", "शोधा",
            # Transliterated
            "haan", "ha", "theek", "bilkul", "ji", "ji haan",
            "ho", "hoy", "chalel",
        ]

        no_words = [
            # English
            "no", "change", "modify", "update", "nope",
            # Hindi
            "नहीं", "नही", "बदलो", "बदलें",
            # Marathi
            "नाही", "नको", "बदला",
            # Transliterated
            "nahi", "nako", "badlo",
        ]

        is_yes = any(w in cleaned for w in yes_words)
        if not is_yes:
            is_yes = any(w in original_text for w in yes_words)

        is_no = any(w in cleaned for w in no_words)
        if not is_no:
            is_no = any(w in original_text for w in no_words)

        if is_yes:
            prop   = session_attributes.get('property_type', 'apartment')
            config = session_attributes.get('configuration', '')
            amen   = session_attributes.get('amenities', '')
            loc    = session_attributes.get('location', '')
            bud    = session_attributes.get('budget', '')
            session_attributes['step'] = 'results'

            user_profile = json.loads(session_attributes.get("user_profile", "{}"))
            user_profile["last_location"] = loc
            user_profile["last_budget"]   = bud
            user_profile["preferences"]   = amen
            session_attributes["user_profile"] = json.dumps(user_profile)

            session_attributes["offset"] = "0"
            results = search_properties(loc, prop, config, bud, amen, 0)
            session_attributes["offset"] = "3"

            if results:
                response_text = format_property_results(results)
                if not response_text:
                    response_text = "I found some properties but couldn't format them properly."
                print(f"[FINAL RESULTS COUNT] {len(results)}")
                return reply(response_text)
            else:
                return reply(
                    "I searched our database but could not find properties matching your exact criteria. "
                    "Would you like to try a different city, increase your budget, "
                    "or change the configuration?"
                )

        elif is_no:
            return reply(
                "No problem! What would you like to change? "
                "Say Change location, Change budget, Change configuration, "
                "Change amenities, or Start over."
            )
        else:
            return reply(
                "Please say Yes to search or No to make changes. "
                "You can also say Haan or Ho to confirm."
            )

    # ── RESULTS ────────────────────────────────────────────────
    if current_step == 'results':
        if user_wants_more(user_text_en):
            prop   = session_attributes.get('property_type', 'apartment')
            config = session_attributes.get('configuration', '')
            amen   = session_attributes.get('amenities', '')
            loc    = session_attributes.get('location', '')
            bud    = session_attributes.get('budget', '')
            offset = int(session_attributes.get("offset", "0"))

            results = search_properties(loc, prop, config, bud, amen, offset)
            session_attributes["offset"] = str(offset + 3)

            if not results:
                return reply("No more properties available for your criteria.")
            return reply("Here are more options: " + format_property_results(results))

        elif user_wants_stop(user_text_en):
            session_attributes['step'] = 'done'
            return reply(
                "Alright. Our team will contact you with details on these properties. "
                "Anything else I can help with?"
            )

        elif any(w in user_text_en.strip().lower()
                 for w in ["yes", "great", "perfect", "good", "interested", "details"]):
            session_attributes['step'] = 'done'
            return reply(
                "Great! Our team will reach out shortly with details and arrange site visits. "
                "Anything else I can help with?"
            )
        else:
            # Check if it's garbage/unclear input — ask to repeat
            cleaned = user_text_en.strip().lower()
            if len(cleaned) <= 3 or not any(c.isalpha() for c in cleaned):
                return reply(
                    "Sorry, I didn't catch that. Would you like to see more options, "
                    "or shall I connect you with our team?"
                )
            session_attributes['step'] = 'done'
            return reply(
                "Our team will contact you soon. Anything else I can help with?"
            )

    # ── DONE ───────────────────────────────────────────────────
    if current_step == 'done':
        new_search = ["yes", "yeah", "sure", "another", "new search", "different",
                      "one more", "looking for", "property", "flat", "apartment",
                      "house", "villa", "bungalow"]
        if any(w in user_text_en.strip().lower() for w in new_search):
            session_attributes = {'user_lang': user_lang, 'step': 'property_type'}
            return reply(
                "Sure! What type of property this time? "
                "Apartment, Flat, Villa, House, or Bungalow?"
            )
        else:
            session_attributes = {'user_lang': user_lang}
            return reply(
                "Thank you! Have a great day. Goodbye!",
                close=True
            )

    # ── FALLBACK ───────────────────────────────────────────────
    session_attributes['step'] = 'property_type'
    return reply("What type of property are you looking for?")


# =============================================================
# BUILD LEX RESPONSE
# =============================================================

def build_response(intent_name, bot_reply, session_attributes, close=False):
    print(f"[BOT REPLY]     {bot_reply[:120]}")
    print(f"[SESSION AFTER] {session_attributes}")
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"} if close else {"type": "ElicitIntent"},
            "intent": {
                "name":  intent_name,
                "state": "Fulfilled" if close else "InProgress"
            },
            "sessionAttributes": {k: str(v) for k, v in session_attributes.items()}
        },
        "messages": [{"contentType": "PlainText", "content": bot_reply}]
    }
