import json
import time
from xml.etree.ElementTree import Element, SubElement, tostring

from bridge.bridge_config import (
    session_table, API_GATEWAY_URL,
)


# =============================================================
# TWIML BUILDERS
# =============================================================

def get_gather_language(lang):
    """Dynamically set Gather language based on user's detected language.
    Note: Twilio doesn't support mr-IN for STT, so Marathi uses hi-IN
    (both use Devanagari script, Twilio captures it fine)."""
    lang_map = {
        'en': 'en-IN',
        'hi': 'hi-IN',
        'mr': 'hi-IN'  # Marathi uses hi-IN (Devanagari) for speech recognition
    }
    return lang_map.get(lang, 'hi-IN')


def build_twiml(bot_reply, gather_action_url, session_id, lang='en'):
    """Build TwiML XML — speak bot reply and listen for user speech."""
    response = Element('Response')

    gather = SubElement(response, 'Gather',
        input='speech',
        action=gather_action_url,
        method='POST',
        speechTimeout='auto',
        speechModel='phone_call',
        enhanced='true',
        language=get_gather_language(lang)  # Fix #11: Dynamic language
    )

    polly_lang = {
        'en': 'en-IN',
        'hi': 'hi-IN',
        'mr': 'hi-IN'
    }.get(lang, 'en-IN')

    # Use Aditi for Hindi (supports hi-IN), Aditi also supports en-IN
    say = SubElement(gather, 'Say', voice='Polly.Aditi', language=polly_lang)
    say.text = bot_reply

    # Fallback redirect if no speech detected
    redirect      = SubElement(response, 'Redirect', method='POST')
    redirect.text = gather_action_url + '&no_input=true' if '?' in gather_action_url else gather_action_url + '?no_input=true'

    return '<?xml version="1.0" encoding="UTF-8"?>' + tostring(response, encoding='unicode')


def build_twiml_hangup(bot_reply, lang='en'):
    """Build TwiML to speak a final message and hang up."""
    response   = Element('Response')
    polly_lang = {'en': 'en-IN', 'hi': 'hi-IN', 'mr': 'hi-IN'}.get(lang, 'en-IN')
    say        = SubElement(response, 'Say', voice='Polly.Aditi', language=polly_lang)
    say.text   = bot_reply
    SubElement(response, 'Hangup')
    return '<?xml version="1.0" encoding="UTF-8"?>' + tostring(response, encoding='unicode')


def twiml_response(body):
    return {
        'statusCode': 200,
        'headers':    {'Content-Type': 'text/xml'},
        'body':       body
    }


# =============================================================
# SESSION MANAGEMENT via DynamoDB (Fix #1: No more URL params)
# =============================================================

def load_session(call_sid):
    """Load session from DynamoDB by CallSid."""
    try:
        response = session_table.get_item(Key={'call_sid': call_sid})
        item = response.get('Item', {})
        session = item.get('session_data', {})
        if isinstance(session, str):
            session = json.loads(session)
        return session
    except Exception as e:
        print(f"[SESSION] Error loading session for {call_sid}: {e}")
        return {}


def save_session(call_sid, session_attributes):
    """Save session to DynamoDB with TTL (24 hours)."""
    try:
        ttl = int(time.time()) + 86400  # 24 hour TTL
        session_table.put_item(Item={
            'call_sid': call_sid,
            'session_data': session_attributes,
            'ttl': ttl
        })
    except Exception as e:
        print(f"[SESSION] Error saving session for {call_sid}: {e}")


def build_action_url(call_sid):
    """Build action URL with only the CallSid as identifier."""
    return f"{API_GATEWAY_URL}?call_sid={call_sid}"
