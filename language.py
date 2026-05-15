import re

from bot.config import (
    STT_CORRECTIONS, SUPPORTED_LANGUAGES,
    LANG_SWITCH_MIN_CONFIDENCE, translate
)


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
# LANGUAGE DETECTION (Fix #8: Confidence-based switching)
# =============================================================

def detect_language(text, current_session_lang=None):
    """
    Detect language with confidence scoring.
    Returns (language_code, confidence_score) tuple.

    Args:
        text: The user's input text
        current_session_lang: The current language in session (if any).
            Used to avoid false switches when user is already in Hindi
            and uses English loanwords (code-mixing).
    """
    if not text or not text.strip():
        return "en", 0.0
    tl = text.lower()
    word_count = len(text.split())

    # Layer 1: Devanagari script check
    devanagari_count = sum(1 for c in text if '\u0900' <= c <= '\u097F')
    if devanagari_count > 2:
        # KEY FIX: If user is ALREADY speaking Hindi (session says hi),
        # check if they switched to Marathi (Marathi-specific words present)
        # OR if they switched back to English (English-in-Devanagari pattern)
        if current_session_lang == 'hi':
            marathi_words = ["मला", "पाहिजे", "आहे", "नाही", "आम्हाला",
                            "कुठे", "किती", "काय", "चालेल", "होय", "नको"]
            if any(w in text for w in marathi_words):
                print(f"[LANG] Devanagari + session=hi but Marathi words found → switching to Marathi")
                return "mr", 0.95

            # Check if user switched back to English (full English sentence in Devanagari)
            english_in_devanagari = [
                "आई एम", "लुकिंग", "फॉर", "अपार्टमेंट",
                "हाउस", "विला", "प्रॉपर्टी",
                "हेलो", "गुड", "बाय",
                "आई वांट", "आई नीड", "माई नेम"
            ]
            true_hindi_words = [
                "मुझे", "चाहिए", "मेरा", "मेरी", "हमें", "ढूंढ", "बताओ",
                "अच्छा", "ठीक", "कोई", "कुछ", "दिखाओ", "खोजो",
                "बजट", "लाख", "करोड़", "करोड", "माय", "मैं",
                "नो थैंक", "धन्यवाद",
            ]
            eng_hits = sum(1 for w in english_in_devanagari if w in text)
            hi_hits = sum(1 for w in true_hindi_words if w in text)

            # Only switch to English if MANY English words and NO Hindi words
            if eng_hits >= 3 and hi_hits == 0:
                print(f"[LANG] Devanagari + session=hi but English sentence detected → switching to English")
                return "en", 0.7

            print(f"[LANG] Devanagari + session=hi → staying Hindi (code-mixing is normal)")
            return "hi", 0.95

        # Same for Marathi — if already in Marathi, check if switched to Hindi or English
        if current_session_lang == 'mr':
            hindi_words = ["मुझे", "चाहिए", "मेरा", "मेरी", "हमें"]
            if any(w in text for w in hindi_words):
                print(f"[LANG] Devanagari + session=mr but Hindi words found → switching to Hindi")
                return "hi", 0.95

            # Check if user switched back to English
            english_check = ["आई एम", "लुकिंग", "फॉर", "अपार्टमेंट",
                            "हाउस", "विला", "प्रॉपर्टी",
                            "हेलो", "गुड", "बाय",
                            "आई वांट", "आई नीड", "माई नेम"]
            eng_hits = sum(1 for w in english_check if w in text)
            if eng_hits >= 3:
                print(f"[LANG] Devanagari + session=mr but English sentence detected → switching to English")
                return "en", 0.7

            print(f"[LANG] Devanagari + session=mr → staying Marathi (code-mixing is normal)")
            return "mr", 0.95

        # Only do English-in-Devanagari detection when session is English or unset
        # (i.e., first turn where Gather was en-IN but somehow got Devanagari)
        english_in_devanagari = [
            "आई एम", "लुकिंग", "फॉर", "अपार्टमेंट",
            "हाउस", "विला", "प्रॉपर्टी",
            "ओके", "प्लीज",
            "हेलो", "गुड", "बाय", "स्टार्ट",
            "आई वांट", "आई नीड", "माई नेम"
        ]
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
        marathi_words = ["मला", "पाहिजे", "आहे", "नाही", "आम्हाला",
                        "कुठे", "किती", "काय", "चालेल", "होय",
                        "बघतो", "सांगा", "हवं", "हवय", "नको"]

        english_hits = sum(1 for w in english_in_devanagari if w in text)
        hindi_hits = sum(1 for w in true_hindi_words if w in text)
        marathi_hits = sum(1 for w in marathi_words if w in text)

        print(f"[LANG] Devanagari detected: english_hits={english_hits}, hindi_hits={hindi_hits}, marathi_hits={marathi_hits}")

        # Only classify as English if MANY English words and ZERO Hindi words
        if english_hits >= 3 and hindi_hits == 0 and marathi_hits == 0:
            print(f"[LANG] Devanagari text is English transliteration, treating as English")
            return "en", 0.7

        if marathi_hits > 0 and marathi_hits >= hindi_hits:
            return "mr", 0.95
        if hindi_hits > 0:
            return "hi", 0.95

        # Default for Devanagari: Hindi
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

    # Marathi exclusive words (romanized) — words that ONLY exist in Marathi
    mr_exclusive_words = ["namaskar", "mala", "pahije", "ahe", "aahe",
                          "shodhat", "havay", "kaay", "kuthe", "madhe",
                          "nakko", "nako", "marathi", "flat pahije",
                          "mumbai madhe", "punyat", "aahet", "havet",
                          "mhanje", "kasa", "kashi", "tumhi", "amhi",
                          "ghar pahije", "baghto", "baghte", "sangha",
                          "chalel", "chalta", "chalte", "hoy", "hoye",
                          "kay", "kiti", "kuthe", "kontya", "tyat",
                          "aahe ka", "nahi ka", "pahijet", "dya",
                          "ghya", "bola", "sangha", "disat",
                          "changla", "changlay", "bara", "theek ahe"]

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

    # DEFAULT: English (unless proven otherwise by keyword scoring above)

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


# ===========================
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
