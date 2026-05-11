import json
import re

from bot.config import (
    bedrock, translate, s3,
    AUDIO_BUCKET, SUPPORTED_LANGUAGES,
    DEFAULT_LANGUAGE,
    CITY_FILE_MAP, VALID_PROPERTY_TYPES, VALID_CONFIGURATIONS,
)
from bot.language import (
    correct_stt_errors, detect_language, should_switch_language,
    translate_to_english, translate_reply,
)
from bot.search import (
    search_properties, format_property_results,
    extract_all_fields, extract_single_field,
    extract_amenities_locally, is_meaningful,
    budget_to_inr_range,
)


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
# MAIN HANDLER
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

    # ── Process user input (from Twilio Bridge or Lex) ────────
    user_text_raw = event.get('inputTranscript', '').strip()
    if input_mode == 'Speech':
        user_text_raw = correct_stt_errors(user_text_raw)

    if user_text_raw:
        # Detect language and switch if user changed language mid-call
        detected_lang, confidence = detect_language(user_text_raw, current_session_lang=user_lang)
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
                # Also grab other fields if mentioned (no word count restriction)
                config = fields.get('configuration', '')
                if is_meaningful(config, 'configuration'):
                    session_attributes['configuration'] = config.strip().upper()
                loc = fields.get('location', '')
                if is_meaningful(loc, 'location'):
                    session_attributes['location'] = loc.strip().title()
                bud = fields.get('budget', '')
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
            # Transliterated (Hindi + Marathi)
            "haan", "ha", "theek", "bilkul", "ji", "ji haan",
            "ho", "hoy", "chalel", "barobar", "nakki",
        ]

        no_words = [
            # English
            "no", "change", "modify", "update", "nope",
            # Hindi
            "नहीं", "नही", "बदलो", "बदलें",
            # Marathi
            "नाही", "नको", "बदला",
            # Transliterated (Hindi + Marathi)
            "nahi", "nako", "badlo", "badla",
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
                 for w in ["change", "modify", "different city", "different location",
                           "try another", "change city", "change location",
                           "change budget", "increase budget"]):
            # User wants to modify search criteria — route to correct step
            text_lower = user_text_en.strip().lower()
            if any(w in text_lower for w in ["city", "location", "different city"]):
                session_attributes['step'] = 'location'
                return reply("Which city would you like to search in? Mumbai, Gurgaon, Hyderabad, or Kolkata?")
            elif any(w in text_lower for w in ["budget", "increase", "price"]):
                session_attributes['step'] = 'budget'
                return reply("What is your new budget?")
            elif any(w in text_lower for w in ["config", "bhk", "bedroom"]):
                session_attributes['step'] = 'configuration'
                return reply("Which configuration? 1BHK, 2BHK, 3BHK, or 4BHK?")
            else:
                session_attributes['step'] = 'confirm'
                return reply(
                    "What would you like to change? "
                    "Say Change location, Change budget, or Change configuration."
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
        text_lower = user_text_en.strip().lower()

        # Check if user wants to change specific criteria (keep existing data)
        if any(w in text_lower for w in ["change city", "change location", "different city"]):
            session_attributes['step'] = 'location'
            return reply("Which city would you like to search in? Mumbai, Gurgaon, Hyderabad, or Kolkata?")
        if any(w in text_lower for w in ["change budget", "increase budget"]):
            session_attributes['step'] = 'budget'
            return reply("What is your new budget?")

        new_search = ["yes", "yeah", "sure", "another", "new search",
                      "one more", "looking for", "property", "flat", "apartment",
                      "house", "villa", "bungalow"]
        if any(w in text_lower for w in new_search):
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
