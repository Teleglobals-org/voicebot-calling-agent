import re
import io
import csv
import ast
import json
import time

from bot.config import (
    DATASET_BUCKET, DATASET_PREFIX, CACHE_TTL_SECONDS,
    CITY_FILE_MAP, VALID_PROPERTY_TYPES, VALID_CONFIGURATIONS,
    AMENITY_KEYWORDS, NO_PREFERENCE_PHRASES, HINDI_AMENITY_MAP,
    PROPERTY_TYPE_MAP, UNSUPPORTED_PROPERTY_TYPES,
    _dataset_cache, _amenity_map, _prop_type_map,
    s3, bedrock, translate,
)

# We need to reference the module-level cache variables
import bot.config as _config


def _is_cache_expired():
    """Fix #7: Check if cache has expired."""
    return (time.time() - _config._cache_timestamp) > CACHE_TTL_SECONDS


def _reset_cache_if_expired():
    """Fix #7: Reset caches if TTL has expired."""
    if _is_cache_expired():
        print("[CACHE] TTL expired, clearing caches")
        _config._dataset_cache.clear()
        _config._amenity_map.clear()
        _config._prop_type_map.clear()
        _config._cache_timestamp = time.time()


def s3_get(filename):
    key = DATASET_PREFIX + filename
    print(f"[S3] Getting: s3://{DATASET_BUCKET}/{key}")
    return s3.get_object(Bucket=DATASET_BUCKET, Key=key)


# =============================================================
# DATASET LOADING (Fix #7: TTL-based cache)
# =============================================================

def load_lookup_maps():
    _reset_cache_if_expired()
    if _config._amenity_map:
        return
    for fname, target_map, id_col, label_col in [
        ('AMENITIES.csv',     _config._amenity_map,   'id', 'label'),
        ('PROPERTY_TYPE.csv', _config._prop_type_map, 'id', 'label'),
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
    if not _config._amenity_map:
        print("[FALLBACK] Using default amenity map")
        _config._amenity_map.update({"1": "Gym", "2": "Parking", "3": "Garden"})


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
    if filename in _config._dataset_cache:
        print(f"[DATASET] Cache hit: {filename} ({len(_config._dataset_cache[filename])} rows)")
        return _config._dataset_cache[filename]
    try:
        obj    = s3_get(filename)
        text   = obj['Body'].read().decode('utf-8', errors='ignore')
        rows   = list(csv.DictReader(io.StringIO(text)))
        _config._dataset_cache[filename] = rows
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
    labels = [_config._amenity_map.get(i, '') for i in ids]
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
            for aid, alabel in _config._amenity_map.items():
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

    # Property type — match longest first to avoid "house" matching inside "farmhouse"
    for pt in sorted(VALID_PROPERTY_TYPES, key=len, reverse=True):
        if re.search(r'\b' + re.escape(pt) + r'\b', text_lower):
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
            modelId="us.meta.llama3-1-8b-instruct-v1:0",
            body=json.dumps({
                "prompt": prompt,
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
        for pt in sorted(VALID_PROPERTY_TYPES, key=len, reverse=True):
            if re.search(r'\b' + re.escape(pt) + r'\b', text_lower):
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
            modelId="us.meta.llama3-1-8b-instruct-v1:0",
            body=json.dumps({
                "prompt": prompts[field_name],
                "max_gen_len": 40,
                "temperature": 0
            })
        )
        raw = json.loads(response['body'].read()).get('generation', '').strip()
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
        # Accept if it's in VALID_PROPERTY_TYPES or PROPERTY_TYPE_MAP
        return (any(pt in cleaned for pt in VALID_PROPERTY_TYPES) or
                any(pt in cleaned for pt in PROPERTY_TYPE_MAP.keys()))
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


def resolve_property_type(raw_type):
    """
    Resolve a user-provided property type to a supported type or None.

    Returns:
        tuple: (resolved_type, is_unsupported)
        - resolved_type: The mapped property type (e.g., "residential" → "apartment")
        - is_unsupported: True if the type is commercial/industrial (we don't support it)

    Examples:
        resolve_property_type("flat") → ("flat", False)
        resolve_property_type("residential") → ("apartment", False)
        resolve_property_type("commercial") → (None, True)
        resolve_property_type("xyz") → (None, False)  # unknown, not unsupported
    """
    if not raw_type:
        return None, False

    cleaned = raw_type.strip().lower()

    # Check if it's an unsupported type (commercial, office, etc.)
    if cleaned in UNSUPPORTED_PROPERTY_TYPES or any(u in cleaned for u in UNSUPPORTED_PROPERTY_TYPES):
        return None, True

    # Check PROPERTY_TYPE_MAP for aliases
    for key, mapped_value in PROPERTY_TYPE_MAP.items():
        if key in cleaned:
            if mapped_value is None:
                return None, True  # Unsupported
            return mapped_value, False

    # Check if it directly matches a valid type
    for pt in VALID_PROPERTY_TYPES:
        if pt in cleaned:
            return pt, False

    return None, False
