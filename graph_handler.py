"""
Lambda Handler with LangGraph-style Memory Architecture.

Implements short-term memory (DynamoDB checkpointer) and long-term memory
(DynamoDB store) WITHOUT requiring langgraph/langchain-core/pydantic packages.

This is a lightweight implementation that provides the same memory capabilities:
- Short-term: Full conversation state persisted per call (thread_id = CallSid)
- Long-term: User preferences and search history across calls (user_id = phone)

The interface (event format, response format) is identical to the original
handler.py — the bridge Lambda needs no changes.
"""
import json
import os

from bot.config import (
    DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES,
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
from bot.memory import DynamoDBCheckpointerLite, DynamoDBLongTermMemory


# =============================================================
# MODULE-LEVEL INSTANCES (reused across Lambda invocations)
# =============================================================

_checkpointer = DynamoDBCheckpointerLite(
    table_name=os.environ.get("CHECKPOINT_TABLE_NAME", "voicebot-checkpoints"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
    ttl_seconds=86400,
)

_long_term_memory = DynamoDBLongTermMemory(
    table_name=os.environ.get("LONG_TERM_MEMORY_TABLE", "voicebot-long-term-memory"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
    ttl_days=90,
)


# =============================================================
# INTENT HELPERS (same as original handler.py)
# =============================================================

def user_is_done(text_en):
    text = text_en.strip().lower()
    strong_exit = ["bye", "goodbye", "see you", "that's all", "i'm done",
                   "im done", "all set", "no need", "nothing else", "no more"]
    if any(p in text for p in strong_exit):
        return True
    if "thank" in text and len(text.split()) <= 4:
        return True
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
    phrases = ["stop", "enough", "that's enough", "no more", "sufficient",
               "bas", "ruk", "band karo"]
    return any(p in text_en.strip().lower() for p in phrases)


def _is_off_topic(text_en):
    text = text_en.strip().lower()
    if len(text.split()) <= 2:
        return None
    vulgar_words = ["fuck", "shit", "ass", "bitch", "bastard", "damn", "idiot", "stupid",
                    "madarchod", "behenchod", "chutiya", "gaali", "gandu", "harami"]
    if any(w in text for w in vulgar_words):
        return "vulgar"
    complaint_phrases = ["not a good bot", "not good", "useless", "waste of time",
                         "terrible", "worst", "not satisfied", "not helpful",
                         "talk to a real person", "talk to human", "real agent"]
    if any(p in text for p in complaint_phrases):
        return "complaint"
    off_topic_phrases = ["what is your name", "how old are you", "tell me a joke",
                         "what is the weather", "book a flight", "order food",
                         "who is the president", "who made you"]
    if any(p in text for p in off_topic_phrases):
        return "off_topic"
    property_keywords = ["flat", "apartment", "house", "villa", "bungalow", "plot", "studio",
                         "bhk", "bedroom", "property", "budget", "lakh", "crore",
                         "mumbai", "gurgaon", "hyderabad", "kolkata",
                         "parking", "gym", "pool", "garden", "lift", "security",
                         "search", "looking", "want", "need", "prefer", "find",
                         "yes", "no", "change", "more", "stop", "start over"]
    if len(text.split()) >= 5 and not any(kw in text for kw in property_keywords):
        return "off_topic"
    return None


# =============================================================
# MAIN HANDLER
# =============================================================

def lambda_handler(event, context):
    """
    Lambda handler with DynamoDB-backed short-term and long-term memory.

    Short-term memory: Conversation state (step, fields, language) is saved
    to DynamoDB after each turn, keyed by thread_id (= CallSid).

    Long-term memory: User preferences and search history are persisted
    across calls, keyed by user_id (= phone number).
    """
    print(f"[GRAPH HANDLER] Event keys: {list(event.keys())}")

    session_attributes = event.get('sessionState', {}).get('sessionAttributes', {})
    intent_name = event.get('sessionState', {}).get('intent', {}).get('name', 'FallbackIntent')
    input_mode = event.get('inputMode', 'Text')

    # ── Identity ──────────────────────────────────────────────
    thread_id = session_attributes.get('thread_id', session_attributes.get('call_sid', ''))
    if not thread_id:
        import uuid
        thread_id = str(uuid.uuid4())

    user_id = session_attributes.get('user_id', session_attributes.get('from_number', 'unknown'))

    print(f"[GRAPH HANDLER] thread_id={thread_id}, user_id={user_id}, mode={input_mode}")

    # ── SHORT-TERM MEMORY: Load state from DynamoDB ───────────
    # This replaces session_attributes from the bridge — we now have
    # our own persistent state that survives even if bridge loses it.
    saved_state = _checkpointer.load(thread_id)
    if saved_state:
        # Merge: saved state is the source of truth, but allow
        # bridge session_attributes to override for backward compat
        for key in ['step', 'user_lang', 'property_type', 'configuration',
                    'amenities', 'location', 'budget', 'offset']:
            if key not in session_attributes or not session_attributes.get(key):
                if key in saved_state:
                    session_attributes[key] = saved_state[key]
        print(f"[SHORT-TERM] Loaded state for {thread_id}: step={saved_state.get('step')}")

    user_lang = session_attributes.get('user_lang', DEFAULT_LANGUAGE)
    user_text_raw = event.get('inputTranscript', '').strip()
    user_text_en = ""

    # ── Process user input ────────────────────────────────────
    if user_text_raw:
        if input_mode == 'Speech':
            user_text_raw = correct_stt_errors(user_text_raw)

        detected_lang, confidence = detect_language(user_text_raw, current_session_lang=user_lang)
        word_count = len(user_text_raw.split())

        if should_switch_language(detected_lang, confidence, user_lang, word_count):
            if user_lang != detected_lang:
                print(f"[LANG SWITCH] {user_lang} → {detected_lang}")
            user_lang = detected_lang

        if not user_lang:
            user_lang = detected_lang if detected_lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

        session_attributes['user_lang'] = user_lang
        user_text_en = translate_to_english(user_text_raw, user_lang)
    else:
        if not user_lang:
            user_lang = DEFAULT_LANGUAGE
            session_attributes['user_lang'] = user_lang

    print(f"[LANG] {user_lang} | [USER EN] {user_text_en[:60]}")

    # ── LONG-TERM MEMORY: Load context for new calls ──────────
    long_term_context = {}
    current_step = session_attributes.get('step', 'greet')
    if current_step == 'greet' and user_id != 'unknown':
        try:
            long_term_context = _long_term_memory.get_context_for_session(user_id)
            if long_term_context:
                print(f"[LONG-TERM] Loaded for {user_id}: {list(long_term_context.keys())}")
        except Exception as e:
            print(f"[LONG-TERM] Load error: {e}")

    # ── Reply helper ──────────────────────────────────────────
    def reply(message, close=False):
        if user_lang != 'en':
            message = translate_reply(message, user_lang)
        # Save state to DynamoDB (short-term memory)
        _checkpointer.save(thread_id, session_attributes)
        print(f"[BOT REPLY] {message[:100]}")
        return build_response(intent_name, message, session_attributes, close=close)

    # ── Global checks ─────────────────────────────────────────
    if user_is_done(user_text_en):
        # Save long-term memory before ending
        if user_id != 'unknown':
            try:
                _long_term_memory.update_from_session(user_id, session_attributes)
            except Exception as e:
                print(f"[LONG-TERM] Save error: {e}")
        session_attributes = {'user_lang': user_lang, 'thread_id': thread_id}
        _checkpointer.save(thread_id, session_attributes)
        return reply("Thank you! Have a great day. Goodbye!", close=True)

    if user_wants_restart(user_text_en):
        session_attributes = {'user_lang': user_lang, 'step': 'property_type', 'thread_id': thread_id}
        return reply("Sure, let's start fresh. What type of property are you looking for?")

    # ── Off-topic handling ────────────────────────────────────
    if user_text_en:
        off_topic_type = _is_off_topic(user_text_en)
        if off_topic_type:
            if off_topic_type == "vulgar":
                return reply("I understand your frustration. Let me help you find the right property. What type of property are you looking for?")
            elif off_topic_type == "complaint":
                return reply("I'm sorry for the inconvenience. Would you like to continue with property search, or shall I connect you with our team?")
            else:
                return reply("I can only help with property search. Could we get back to finding your ideal property?")

    # ── STEP-BASED FLOW ───────────────────────────────────────

    # ── GREET ─────────────────────────────────────────────────
    if current_step == 'greet' or not user_text_en:
        session_attributes['step'] = 'property_type'
        session_attributes['thread_id'] = thread_id

        words = user_text_en.split()
        if len(words) >= 3:
            fields = extract_all_fields(user_text_en)
            prop = fields.get('property_type', '')
            if not is_meaningful(prop, 'property_type'):
                prop = extract_single_field('property_type', user_text_en)

            if is_meaningful(prop, 'property_type'):
                session_attributes['property_type'] = prop.strip().lower()
                config = fields.get('configuration', '')
                if is_meaningful(config, 'configuration'):
                    session_attributes['configuration'] = config.strip().upper()
                loc = fields.get('location', '')
                if is_meaningful(loc, 'location'):
                    session_attributes['location'] = loc.strip().title()
                bud = fields.get('budget', '')
                if is_meaningful(bud, 'budget'):
                    session_attributes['budget'] = bud.strip()

                if not is_meaningful(session_attributes.get('configuration', ''), 'configuration'):
                    session_attributes['step'] = 'configuration'
                    return reply(f"Got it, a {prop.title()}. What configuration? 1BHK, 2BHK, 3BHK, or 4BHK?")
                elif not session_attributes.get('amenities'):
                    session_attributes['step'] = 'amenities'
                    return reply(f"Got it, {session_attributes.get('configuration', '')} {prop.title()}. Any preferred amenities? Parking, Gym, Pool, Garden, or Lift? Or say No preference.")
                elif not is_meaningful(session_attributes.get('location', ''), 'location'):
                    session_attributes['step'] = 'location'
                    return reply("Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?")
                elif not is_meaningful(session_attributes.get('budget', ''), 'budget'):
                    session_attributes['step'] = 'budget'
                    return reply("What is your budget?")
                else:
                    session_attributes['step'] = 'confirm'
                    return reply(f"Let me confirm: {session_attributes.get('configuration', '')} {prop.title()} in {session_attributes.get('location', '')}, budget {session_attributes.get('budget', '')}. Shall I search? Yes or No.")

        # Personalized greeting with long-term memory
        prefs = long_term_context.get('preferences', {})
        if prefs.get('last_city'):
            return reply(f"Welcome back! Last time you searched in {prefs['last_city']}. What type of property are you looking for today?")
        return reply("What type of property are you looking for? Apartment, Flat, Villa, House, or Bungalow?")

    # ── PROPERTY TYPE ─────────────────────────────────────────
    if current_step == 'property_type':
        value = extract_single_field('property_type', user_text_en)
        if is_meaningful(value, 'property_type'):
            session_attributes['property_type'] = value.strip().lower()
            session_attributes['step'] = 'configuration'
            return reply(f"Got it, a {value.title()}. What configuration? 1BHK, 2BHK, 3BHK, or 4BHK?")
        return reply("What type of property are you looking for? Apartment, Flat, Villa, House, or Bungalow?")

    # ── CONFIGURATION ─────────────────────────────────────────
    if current_step == 'configuration':
        value = extract_single_field('configuration', user_text_en)
        if is_meaningful(value, 'configuration'):
            session_attributes['configuration'] = value.strip().upper()
            session_attributes['step'] = 'amenities'
            return reply(f"{value.upper()}, noted. Any preferred amenities? Parking, Gym, Pool, Garden, or Lift? Or say No preference.")
        return reply("Which configuration? 1BHK, 2BHK, 3BHK, or 4BHK?")

    # ── AMENITIES ─────────────────────────────────────────────
    if current_step == 'amenities':
        value = extract_amenities_locally(user_text_en)
        if value:
            session_attributes['amenities'] = value
            session_attributes['step'] = 'location'
            return reply("Noted. Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?")
        return reply("Any preferred amenities? Parking, Gym, Pool, Garden, or Lift? Or say No preference.")

    # ── LOCATION ──────────────────────────────────────────────
    if current_step == 'location':
        value = extract_single_field('location', user_text_en)
        if is_meaningful(value, 'location'):
            session_attributes['location'] = value.strip().title()
            session_attributes['step'] = 'budget'
            return reply(f"{value.title()}, great. What is your budget? For example 50 Lakhs or 1 Crore.")
        return reply("Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?")

    # ── BUDGET ────────────────────────────────────────────────
    if current_step == 'budget':
        value = extract_single_field('budget', user_text_en)
        if is_meaningful(value, 'budget'):
            budget_range = budget_to_inr_range(value)
            if budget_range is None:
                return reply(f"Is that {value} in Lakhs or Crores? Please specify.")
            session_attributes['budget'] = value.strip()
            session_attributes['step'] = 'confirm'
            prop = session_attributes.get('property_type', '').title()
            config = session_attributes.get('configuration', '')
            amen = session_attributes.get('amenities', 'None')
            loc = session_attributes.get('location', '')
            return reply(f"Let me confirm. {config} {prop} in {loc}, budget {value.strip()}, amenities {amen}. Shall I search? Yes or No.")
        return reply("What is your budget? For example 50 Lakhs or 1 Crore.")

    # ── CONFIRM ───────────────────────────────────────────────
    if current_step == 'confirm':
        cleaned = user_text_en.strip().lower()
        original_text = user_text_raw.strip().lower() if user_text_raw else ""

        yes_words = ["yes", "go ahead", "proceed", "sure", "okay", "ok", "yeah", "yep",
                     "correct", "right", "confirmed", "please", "search",
                     "haan", "ha", "theek", "bilkul", "ji", "ho", "hoy", "chalel"]
        no_words = ["no", "change", "modify", "update", "nope", "nahi", "nako", "badlo"]

        is_yes = any(w in cleaned for w in yes_words) or any(w in original_text for w in yes_words)
        is_no = any(w in cleaned for w in no_words) or any(w in original_text for w in no_words)

        if is_yes:
            prop = session_attributes.get('property_type', 'apartment')
            config = session_attributes.get('configuration', '')
            amen = session_attributes.get('amenities', '')
            loc = session_attributes.get('location', '')
            bud = session_attributes.get('budget', '')
            session_attributes['step'] = 'results'
            session_attributes['offset'] = '0'

            results = search_properties(loc, prop, config, bud, amen, 0)
            session_attributes['offset'] = '3'

            # Save to long-term memory
            if user_id != 'unknown':
                try:
                    _long_term_memory.update_from_session(user_id, session_attributes)
                except Exception as e:
                    print(f"[LONG-TERM] Save error: {e}")

            if results:
                response_text = format_property_results(results)
                if not response_text:
                    response_text = "I found some properties but couldn't format them properly."
                return reply(response_text)
            else:
                return reply("I searched our database but could not find properties matching your exact criteria. Would you like to try a different city, increase your budget, or change the configuration?")

        elif is_no:
            return reply("No problem! What would you like to change? Say Change location, Change budget, Change configuration, Change amenities, or Start over.")
        else:
            return reply("Please say Yes to search or No to make changes.")

    # ── RESULTS ───────────────────────────────────────────────
    if current_step == 'results':
        if user_wants_more(user_text_en):
            prop = session_attributes.get('property_type', 'apartment')
            config = session_attributes.get('configuration', '')
            amen = session_attributes.get('amenities', '')
            loc = session_attributes.get('location', '')
            bud = session_attributes.get('budget', '')
            offset = int(session_attributes.get('offset', '0'))

            results = search_properties(loc, prop, config, bud, amen, offset)
            session_attributes['offset'] = str(offset + 3)

            if not results:
                return reply("No more properties available for your criteria.")
            return reply("Here are more options: " + format_property_results(results))

        elif user_wants_stop(user_text_en):
            session_attributes['step'] = 'done'
            return reply("Alright. Our team will contact you with details. Anything else I can help with?")

        elif any(w in user_text_en.strip().lower() for w in ["change", "modify", "different city", "change city", "change location", "change budget", "increase budget"]):
            text_lower = user_text_en.strip().lower()
            if any(w in text_lower for w in ["city", "location"]):
                session_attributes['step'] = 'location'
                return reply("Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?")
            elif any(w in text_lower for w in ["budget", "increase", "price"]):
                session_attributes['step'] = 'budget'
                return reply("What is your new budget?")
            elif any(w in text_lower for w in ["config", "bhk"]):
                session_attributes['step'] = 'configuration'
                return reply("Which configuration? 1BHK, 2BHK, 3BHK, or 4BHK?")
            else:
                session_attributes['step'] = 'confirm'
                return reply("What would you like to change? Say Change location, Change budget, or Change configuration.")

        elif any(w in user_text_en.strip().lower() for w in ["yes", "great", "perfect", "interested", "details"]):
            session_attributes['step'] = 'done'
            return reply("Great! Our team will reach out shortly with details. Anything else I can help with?")
        else:
            session_attributes['step'] = 'done'
            return reply("Our team will contact you soon. Anything else I can help with?")

    # ── DONE ──────────────────────────────────────────────────
    if current_step == 'done':
        text_lower = user_text_en.strip().lower()
        if any(w in text_lower for w in ["change city", "change location", "different city"]):
            session_attributes['step'] = 'location'
            return reply("Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?")
        if any(w in text_lower for w in ["change budget", "increase budget"]):
            session_attributes['step'] = 'budget'
            return reply("What is your new budget?")

        new_search = ["yes", "yeah", "sure", "another", "new search", "one more",
                      "property", "flat", "apartment", "house", "villa", "bungalow"]
        if any(w in text_lower for w in new_search):
            session_attributes = {'user_lang': user_lang, 'step': 'property_type', 'thread_id': thread_id}
            return reply("Sure! What type of property this time? Apartment, Flat, Villa, House, or Bungalow?")
        else:
            if user_id != 'unknown':
                try:
                    _long_term_memory.update_from_session(user_id, session_attributes)
                except Exception as e:
                    print(f"[LONG-TERM] Save error: {e}")
            session_attributes = {'user_lang': user_lang, 'thread_id': thread_id}
            return reply("Thank you! Have a great day. Goodbye!", close=True)

    # ── FALLBACK ──────────────────────────────────────────────
    session_attributes['step'] = 'property_type'
    return reply("What type of property are you looking for?")


# =============================================================
# BUILD RESPONSE
# =============================================================

def build_response(intent_name, bot_reply, session_attributes, close=False):
    return {
        "sessionState": {
            "dialogAction": {"type": "Close"} if close else {"type": "ElicitIntent"},
            "intent": {
                "name": intent_name,
                "state": "Fulfilled" if close else "InProgress"
            },
            "sessionAttributes": {k: str(v) for k, v in session_attributes.items()}
        },
        "messages": [{"contentType": "PlainText", "content": bot_reply}]
    }
