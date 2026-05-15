"""
LangGraph-based Property Search Voice Bot.

Implements the conversation flow as a StateGraph with:
- Typed state (AgentState) with message history
- Node-per-step architecture (greet, property_type, configuration, etc.)
- Short-term memory via DynamoDB checkpointer (within a call)
- Long-term memory via DynamoDB store (across calls for same user)
"""
import json
import os
from typing import Annotated, Optional
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from bot.config import (
    SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE,
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
    budget_to_inr_range, resolve_property_type,
)
from bot.memory import DynamoDBCheckpointer, DynamoDBLongTermMemory


# =============================================================
# GRAPH STATE DEFINITION
# =============================================================

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    step: str
    user_lang: str
    property_type: str
    configuration: str
    amenities: str
    location: str
    budget: str
    offset: int
    user_id: str
    long_term_context: dict
    user_input_raw: str
    user_input_en: str
    bot_reply: str
    should_close: bool


# =============================================================
# HELPER
# =============================================================

def _reply(message: str, user_lang: str) -> str:
    if user_lang and user_lang != "en":
        return translate_reply(message, user_lang)
    return message


# =============================================================
# NODE: Process Input
# =============================================================

def process_input(state: AgentState) -> dict:
    raw_text = state.get("user_input_raw", "")
    current_lang = state.get("user_lang", DEFAULT_LANGUAGE)

    if not raw_text:
        return {"user_input_en": "", "user_lang": current_lang or DEFAULT_LANGUAGE}

    corrected = correct_stt_errors(raw_text)
    detected_lang, confidence = detect_language(corrected, current_session_lang=current_lang)
    word_count = len(corrected.split())

    if should_switch_language(detected_lang, confidence, current_lang, word_count):
        if current_lang != detected_lang:
            print(f"[GRAPH] Language switch: {current_lang} → {detected_lang}")
        current_lang = detected_lang

    if not current_lang:
        current_lang = detected_lang if detected_lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    text_en = translate_to_english(corrected, current_lang)
    return {"user_input_en": text_en, "user_lang": current_lang}


# =============================================================
# NODE: Route
# =============================================================

def route_step(state: AgentState) -> str:
    text_en = state.get("user_input_en", "").strip().lower()
    step = state.get("step", "greet")

    strong_exit = ["bye", "goodbye", "see you", "that's all", "i'm done",
                   "im done", "all set", "no need", "nothing else", "no more"]
    if any(p in text_en for p in strong_exit):
        return "handle_goodbye"
    if "thank" in text_en and len(text_en.split()) <= 4:
        return "handle_goodbye"
    if text_en in ("done", "i am done", "im done", "all done"):
        return "handle_goodbye"

    restart_phrases = ["start over", "restart", "reset", "begin again",
                       "new search", "change requirements", "different property", "start again"]
    if any(p in text_en for p in restart_phrases):
        return "handle_restart"

    step_map = {
        "greet": "handle_greet",
        "property_type": "handle_property_type",
        "configuration": "handle_configuration",
        "amenities": "handle_amenities",
        "location": "handle_location",
        "budget": "handle_budget",
        "confirm": "handle_confirm",
        "results": "handle_results",
        "done": "handle_done",
    }
    return step_map.get(step, "handle_greet")


# =============================================================
# NODE: Greet
# =============================================================

def handle_greet(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "")
    user_lang = state.get("user_lang", "en")
    long_term = state.get("long_term_context", {})

    if not text_en:
        prefs = long_term.get("preferences", {})
        if prefs.get("last_city"):
            msg = (f"Welcome back! Last time you searched in {prefs['last_city']}. "
                   f"What type of property are you looking for today?")
        else:
            msg = ("What type of property are you looking for? "
                   "Apartment, Flat, Villa, House, or Bungalow?")
        return {
            "step": "property_type",
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    words = text_en.split()
    if len(words) >= 3:
        fields = extract_all_fields(text_en)
        prop = fields.get("property_type", "")
        if not is_meaningful(prop, "property_type"):
            prop = extract_single_field("property_type", text_en)

        if is_meaningful(prop, "property_type"):
            # Resolve property type (handle aliases and unsupported types)
            resolved, is_unsupported = resolve_property_type(prop)
            if is_unsupported:
                msg = ("I currently help with residential properties only — "
                       "Apartment, Flat, Villa, House, or Bungalow. "
                       "What type of residential property are you looking for?")
                return {
                    "step": "property_type",
                    "bot_reply": _reply(msg, user_lang),
                    "messages": [AIMessage(content=msg)],
                }
            if resolved:
                prop = resolved

            updates = {"property_type": prop.strip().lower(), "step": "configuration"}

            config = fields.get("configuration", "")
            if is_meaningful(config, "configuration"):
                updates["configuration"] = config.strip().upper()
            loc = fields.get("location", "")
            if is_meaningful(loc, "location"):
                updates["location"] = loc.strip().title()
            bud = fields.get("budget", "")
            if is_meaningful(bud, "budget"):
                updates["budget"] = bud.strip()

            if not is_meaningful(updates.get("configuration", ""), "configuration"):
                updates["step"] = "configuration"
                msg = f"Got it, a {prop.title()}. What configuration? 1BHK, 2BHK, 3BHK, or 4BHK?"
            elif not updates.get("amenities"):
                updates["step"] = "amenities"
                msg = (f"Got it, {updates.get('configuration', '')} {prop.title()}. "
                       f"Any preferred amenities? Parking, Gym, Pool, Garden, or Lift? Or say No preference.")
            elif not is_meaningful(updates.get("location", ""), "location"):
                updates["step"] = "location"
                msg = "Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?"
            elif not is_meaningful(updates.get("budget", ""), "budget"):
                updates["step"] = "budget"
                msg = "What is your budget?"
            else:
                updates["step"] = "confirm"
                msg = (f"Let me confirm: {updates.get('configuration', '')} {prop.title()} "
                       f"in {updates.get('location', '')}, budget {updates.get('budget', '')}. "
                       f"Shall I search? Yes or No.")

            updates["bot_reply"] = _reply(msg, user_lang)
            updates["messages"] = [AIMessage(content=msg)]
            return updates

    msg = "What type of property are you looking for? Apartment, Flat, Villa, House, or Bungalow?"
    return {
        "step": "property_type",
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Property Type
# =============================================================

def handle_property_type(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "")
    user_lang = state.get("user_lang", "en")

    value = extract_single_field("property_type", text_en)
    if is_meaningful(value, "property_type"):
        # Resolve property type (handle aliases and unsupported types)
        resolved, is_unsupported = resolve_property_type(value)
        if is_unsupported:
            msg = ("I currently help with residential properties only — "
                   "Apartment, Flat, Villa, House, or Bungalow. "
                   "What would you like?")
            return {
                "bot_reply": _reply(msg, user_lang),
                "messages": [AIMessage(content=msg)],
            }
        if resolved:
            value = resolved

        msg = f"Got it, a {value.title()}. What configuration? 1BHK, 2BHK, 3BHK, or 4BHK?"
        return {
            "property_type": value.strip().lower(),
            "step": "configuration",
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    msg = "What type of property are you looking for? Apartment, Flat, Villa, House, or Bungalow?"
    return {
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Configuration
# =============================================================

def handle_configuration(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "")
    user_lang = state.get("user_lang", "en")

    value = extract_single_field("configuration", text_en)
    if is_meaningful(value, "configuration"):
        msg = (f"{value.upper()}, noted. Any preferred amenities? "
               f"Parking, Gym, Pool, Garden, or Lift? Or say No preference.")
        return {
            "configuration": value.strip().upper(),
            "step": "amenities",
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    msg = "Which configuration? 1BHK, 2BHK, 3BHK, or 4BHK?"
    return {
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Amenities
# =============================================================

def handle_amenities(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "")
    user_lang = state.get("user_lang", "en")

    value = extract_amenities_locally(text_en)
    if value:
        msg = "Noted. Which city are you looking in? Mumbai, Gurgaon, Hyderabad, or Kolkata?"
        return {
            "amenities": value,
            "step": "location",
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    msg = "Any preferred amenities? Parking, Gym, Pool, Garden, or Lift? Or say No preference."
    return {
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Location
# =============================================================

def handle_location(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "")
    user_lang = state.get("user_lang", "en")

    value = extract_single_field("location", text_en)
    if is_meaningful(value, "location"):
        msg = f"{value.title()}, great. What is your budget? For example 50 Lakhs or 1 Crore."
        return {
            "location": value.strip().title(),
            "step": "budget",
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    msg = "Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?"
    return {
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Budget
# =============================================================

def handle_budget(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "")
    user_lang = state.get("user_lang", "en")

    value = extract_single_field("budget", text_en)
    if is_meaningful(value, "budget"):
        budget_range = budget_to_inr_range(value)
        if budget_range is None:
            msg = f"Is that {value} in Lakhs or Crores? Please specify."
            return {
                "bot_reply": _reply(msg, user_lang),
                "messages": [AIMessage(content=msg)],
            }

        prop = state.get("property_type", "").title()
        config = state.get("configuration", "")
        amen = state.get("amenities", "None")
        loc = state.get("location", "")

        msg = (f"Let me confirm. {config} {prop} in {loc}, "
               f"budget {value.strip()}, amenities {amen}. Shall I search? Yes or No.")
        return {
            "budget": value.strip(),
            "step": "confirm",
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    msg = "What is your budget? For example 50 Lakhs or 1 Crore."
    return {
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Confirm
# =============================================================

def handle_confirm(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "").strip().lower()
    user_lang = state.get("user_lang", "en")
    raw_text = state.get("user_input_raw", "").strip().lower()

    yes_words = ["yes", "go ahead", "proceed", "sure", "okay", "ok",
                 "yeah", "yep", "correct", "right", "confirmed", "please", "search",
                 "haan", "ha", "theek", "bilkul", "ji", "ho", "hoy", "chalel"]
    no_words = ["no", "change", "modify", "update", "nope",
                "nahi", "nako", "badlo", "badla"]

    is_yes = any(w in text_en for w in yes_words) or any(w in raw_text for w in yes_words)
    is_no = any(w in text_en for w in no_words) or any(w in raw_text for w in no_words)

    if is_yes:
        prop = state.get("property_type", "apartment")
        config = state.get("configuration", "")
        amen = state.get("amenities", "")
        loc = state.get("location", "")
        bud = state.get("budget", "")

        results = search_properties(loc, prop, config, bud, amen, 0)

        if results:
            response_text = format_property_results(results)
            if not response_text:
                response_text = "I found some properties but couldn't format them properly."
        else:
            response_text = ("I searched our database but could not find properties matching your exact criteria. "
                             "Would you like to try a different city, increase your budget, or change the configuration?")

        return {
            "step": "results",
            "offset": 3,
            "bot_reply": _reply(response_text, user_lang),
            "messages": [AIMessage(content=response_text)],
        }

    elif is_no:
        msg = ("No problem! What would you like to change? "
               "Say Change location, Change budget, Change configuration, Change amenities, or Start over.")
        return {
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    msg = "Please say Yes to search or No to make changes."
    return {
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Results
# =============================================================

def handle_results(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "").strip().lower()
    user_lang = state.get("user_lang", "en")

    more_phrases = ["more", "other options", "another", "show more", "more options", "next"]
    if any(p in text_en for p in more_phrases):
        prop = state.get("property_type", "apartment")
        config = state.get("configuration", "")
        amen = state.get("amenities", "")
        loc = state.get("location", "")
        bud = state.get("budget", "")
        offset = state.get("offset", 0)

        results = search_properties(loc, prop, config, bud, amen, offset)

        if not results:
            msg = "No more properties available for your criteria."
        else:
            msg = "Here are more options: " + format_property_results(results)

        return {
            "offset": offset + 3,
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    stop_phrases = ["stop", "enough", "that's enough", "no more", "bas", "ruk"]
    if any(p in text_en for p in stop_phrases):
        msg = "Alright. Our team will contact you with details. Anything else I can help with?"
        return {
            "step": "done",
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    if any(w in text_en for w in ["change", "modify", "different", "increase"]):
        if any(w in text_en for w in ["city", "location"]):
            msg = "Which city would you like to search in? Mumbai, Gurgaon, Hyderabad, or Kolkata?"
            return {"step": "location", "bot_reply": _reply(msg, user_lang), "messages": [AIMessage(content=msg)]}
        elif any(w in text_en for w in ["budget", "price"]):
            msg = "What is your new budget?"
            return {"step": "budget", "bot_reply": _reply(msg, user_lang), "messages": [AIMessage(content=msg)]}
        elif any(w in text_en for w in ["config", "bhk"]):
            msg = "Which configuration? 1BHK, 2BHK, 3BHK, or 4BHK?"
            return {"step": "configuration", "bot_reply": _reply(msg, user_lang), "messages": [AIMessage(content=msg)]}

    if any(w in text_en for w in ["yes", "great", "perfect", "interested", "details"]):
        msg = "Great! Our team will reach out shortly with details. Anything else I can help with?"
        return {
            "step": "done",
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    msg = "Our team will contact you soon. Anything else I can help with?"
    return {
        "step": "done",
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Done
# =============================================================

def handle_done(state: AgentState) -> dict:
    text_en = state.get("user_input_en", "").strip().lower()
    user_lang = state.get("user_lang", "en")

    if any(w in text_en for w in ["change city", "change location", "different city"]):
        msg = "Which city? Mumbai, Gurgaon, Hyderabad, or Kolkata?"
        return {"step": "location", "bot_reply": _reply(msg, user_lang), "messages": [AIMessage(content=msg)]}

    if any(w in text_en for w in ["change budget", "increase budget"]):
        msg = "What is your new budget?"
        return {"step": "budget", "bot_reply": _reply(msg, user_lang), "messages": [AIMessage(content=msg)]}

    new_search = ["yes", "yeah", "sure", "another", "new search", "one more",
                  "property", "flat", "apartment", "house", "villa"]
    if any(w in text_en for w in new_search):
        msg = "Sure! What type of property this time? Apartment, Flat, Villa, House, or Bungalow?"
        return {
            "step": "property_type",
            "property_type": "", "configuration": "", "amenities": "",
            "location": "", "budget": "", "offset": 0,
            "bot_reply": _reply(msg, user_lang),
            "messages": [AIMessage(content=msg)],
        }

    msg = "Thank you! Have a great day. Goodbye!"
    return {
        "bot_reply": _reply(msg, user_lang),
        "should_close": True,
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Goodbye
# =============================================================

def handle_goodbye(state: AgentState) -> dict:
    user_lang = state.get("user_lang", "en")
    msg = "Thank you! Have a great day. Goodbye!"
    return {
        "bot_reply": _reply(msg, user_lang),
        "should_close": True,
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# NODE: Restart
# =============================================================

def handle_restart(state: AgentState) -> dict:
    user_lang = state.get("user_lang", "en")
    msg = "Sure, let's start fresh. What type of property are you looking for?"
    return {
        "step": "property_type",
        "property_type": "", "configuration": "", "amenities": "",
        "location": "", "budget": "", "offset": 0,
        "bot_reply": _reply(msg, user_lang),
        "messages": [AIMessage(content=msg)],
    }


# =============================================================
# BUILD THE GRAPH
# =============================================================

def build_graph(checkpointer=None):
    builder = StateGraph(AgentState)

    builder.add_node("process_input", process_input)
    builder.add_node("handle_greet", handle_greet)
    builder.add_node("handle_property_type", handle_property_type)
    builder.add_node("handle_configuration", handle_configuration)
    builder.add_node("handle_amenities", handle_amenities)
    builder.add_node("handle_location", handle_location)
    builder.add_node("handle_budget", handle_budget)
    builder.add_node("handle_confirm", handle_confirm)
    builder.add_node("handle_results", handle_results)
    builder.add_node("handle_done", handle_done)
    builder.add_node("handle_goodbye", handle_goodbye)
    builder.add_node("handle_restart", handle_restart)

    builder.add_edge(START, "process_input")
    builder.add_conditional_edges("process_input", route_step)

    for node in ["handle_greet", "handle_property_type", "handle_configuration",
                 "handle_amenities", "handle_location", "handle_budget",
                 "handle_confirm", "handle_results", "handle_done",
                 "handle_goodbye", "handle_restart"]:
        builder.add_edge(node, END)

    return builder.compile(checkpointer=checkpointer)


# =============================================================
# GRAPH INSTANCE (Module-level for Lambda reuse)
# =============================================================

if os.environ.get("LANGGRAPH_CHECKPOINTER") == "memory":
    from langgraph.checkpoint.memory import MemorySaver
    _checkpointer = MemorySaver()
    print("[GRAPH] Using in-memory checkpointer (dev mode)")
else:
    _checkpointer = DynamoDBCheckpointer(
        table_name=os.environ.get("CHECKPOINT_TABLE_NAME", "voicebot-checkpoints"),
        writes_table_name=os.environ.get("CHECKPOINT_WRITES_TABLE_NAME", "voicebot-checkpoint-writes"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        ttl_seconds=86400,
    )
    print("[GRAPH] Using DynamoDB checkpointer (production)")

_long_term_memory = DynamoDBLongTermMemory(
    table_name=os.environ.get("LONG_TERM_MEMORY_TABLE", "voicebot-long-term-memory"),
    region_name=os.environ.get("AWS_REGION", "us-east-1"),
    ttl_days=90,
)

graph = build_graph(checkpointer=_checkpointer)
