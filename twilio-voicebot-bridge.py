import json
import boto3
import os
import time
import hmac
import hashlib
import urllib.parse
from xml.etree.ElementTree import Element, SubElement, tostring

# ── AWS Clients ───────────────────────────────────────────────
lambda_client = boto3.client('lambda', region_name='us-east-1')
dynamodb      = boto3.resource('dynamodb', region_name='us-east-1')

# ── Config ────────────────────────────────────────────────────
EXISTING_BOT_LAMBDA = os.environ.get('BOT_LAMBDA_NAME', 'voice-bot-function-sagar')
API_GATEWAY_URL     = os.environ.get('API_GATEWAY_URL', '')
TWILIO_AUTH_TOKEN   = os.environ.get('TWILIO_AUTH_TOKEN', '')
SESSION_TABLE_NAME  = os.environ.get('SESSION_TABLE_NAME', 'voicebot-sessions')
MAX_NO_INPUT_RETRIES = 3

# ── DynamoDB Session Table ────────────────────────────────────
session_table = dynamodb.Table(SESSION_TABLE_NAME)


# =============================================================
# TWILIO SIGNATURE VALIDATION
# =============================================================

def validate_twilio_signature(event):
    """
    Validate that the request is genuinely from Twilio.
    NOTE: Disabled by default for initial testing. Set ENABLE_TWILIO_VALIDATION=true
    to enable once the bot is working end-to-end.
    """
    enable_validation = os.environ.get('ENABLE_TWILIO_VALIDATION', 'false').lower() == 'true'
    if not enable_validation:
        print("[SECURITY] Twilio signature validation disabled (set ENABLE_TWILIO_VALIDATION=true to enable)")
        return True

    if not TWILIO_AUTH_TOKEN:
        print("[SECURITY] No TWILIO_AUTH_TOKEN configured, skipping validation")
        return True

    signature = (event.get('headers') or {}).get('X-Twilio-Signature', '')
    if not signature:
        signature = (event.get('headers') or {}).get('x-twilio-signature', '')

    if not signature:
        print("[SECURITY] Missing Twilio signature header")
        return False

    # Reconstruct the full URL
    url = API_GATEWAY_URL

    # Get POST params
    body = event.get('body', '') or ''
    if event.get('isBase64Encoded'):
        import base64
        body = base64.b64decode(body).decode('utf-8')

    params = dict(urllib.parse.parse_qsl(body))

    # Build the validation string: URL + sorted POST params
    validation_str = url
    for key in sorted(params.keys()):
        validation_str += key + params[key]

    # Compute expected signature
    expected = hmac.HMAC(
        TWILIO_AUTH_TOKEN.encode('utf-8'),
        validation_str.encode('utf-8'),
        hashlib.sha1
    ).digest()

    import base64 as b64
    expected_b64 = b64.b64encode(expected).decode('utf-8')

    return hmac.compare_digest(signature, expected_b64)


# =============================================================
# TWIML BUILDERS
# =============================================================

def get_gather_language(lang):
    """Fix #11: Dynamically set Gather language based on user's detected language."""
    lang_map = {
        'en': 'en-IN',
        'hi': 'hi-IN',
        'mr': 'mr-IN'
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


# =============================================================
# CALL EXISTING BOT LAMBDA (Fix #2: Timeout handling)
# =============================================================

def call_your_bot(user_text, session_attributes, intent_name='PropertySearchIntent'):
    """Invoke the existing bot Lambda with timeout handling."""
    event = {
        'inputTranscript': user_text,
        'inputMode':       'Speech',
        'sessionState': {
            'intent':            {'name': intent_name},
            'sessionAttributes': session_attributes
        }
    }

    try:
        response = lambda_client.invoke(
            FunctionName=EXISTING_BOT_LAMBDA,
            InvocationType='RequestResponse',
            Payload=json.dumps(event)
        )

        # Check for Lambda errors
        if response.get('FunctionError'):
            print(f"[BOT LAMBDA] Function error: {response.get('FunctionError')}")
            return "I'm having trouble processing your request. Could you please repeat?", session_attributes

        result = json.loads(response['Payload'].read())
        bot_reply = ''
        if result.get('messages'):
            bot_reply = result['messages'][0].get('content', '')

        new_session = result.get('sessionState', {}).get('sessionAttributes', {})
        return bot_reply, new_session

    except Exception as e:
        error_name = type(e).__name__
        if 'TooManyRequests' in error_name or 'Throttl' in error_name:
            print("[BOT LAMBDA] Throttled")
            return "I'm experiencing high traffic. Please wait a moment and try again.", session_attributes
        print(f"[BOT LAMBDA] Error: {e}")
        return "I'm sorry, something went wrong. Could you please repeat?", session_attributes


# =============================================================
# MAIN HANDLER
# =============================================================

def lambda_handler(event, context):
    print(f"[TWILIO EVENT] {json.dumps(event)[:500]}")

    # ── Fix #15: Validate Twilio signature ────────────────────
    if TWILIO_AUTH_TOKEN and not validate_twilio_signature(event):
        print("[SECURITY] Invalid Twilio signature - rejecting request")
        return {'statusCode': 403, 'body': 'Forbidden'}

    # ── Parse Twilio's form-encoded POST body ─────────────────
    body = event.get('body', '') or ''
    if event.get('isBase64Encoded'):
        import base64
        body = base64.b64decode(body).decode('utf-8')

    params    = dict(urllib.parse.parse_qsl(body))
    qs_params = event.get('queryStringParameters') or {}
    print(f"[TWILIO PARAMS] {params}")

    call_sid    = params.get('CallSid', qs_params.get('call_sid', 'unknown'))
    from_number = params.get('From',         '')
    to_number   = params.get('To',           '')
    speech_text = params.get('SpeechResult', '').strip()
    call_status = params.get('CallStatus',   '')
    no_input    = qs_params.get('no_input',  'false')

    print(f"[CALL SID]    {call_sid}")
    print(f"[FROM]        {from_number}")
    print(f"[SPEECH TEXT] {speech_text}")
    print(f"[CALL STATUS] {call_status}")

    # ── Handle call end events ────────────────────────────────
    if call_status in ('completed', 'busy', 'no-answer', 'failed', 'canceled'):
        print(f"[CALL ENDED] {call_sid} - {call_status}")
        # Clean up session on call end
        try:
            session_table.delete_item(Key={'call_sid': call_sid})
        except Exception:
            pass
        return twiml_response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>')

    # ── Load session from DynamoDB (Fix #1) ───────────────────
    session = load_session(call_sid)
    lang    = session.get('user_lang', 'en')  # Default language is English

    # ── FIRST TURN: new call (Default language: English) ─────────
    if not speech_text and no_input != 'true':
        session['no_input_count'] = 0
        session['user_lang'] = 'en'  # Default language is English
        save_session(call_sid, session)

        response = Element('Response')
        gather = SubElement(
            response, 'Gather',
            input='speech',
            action=build_action_url(call_sid),
            method='POST',
            speechTimeout='auto',
            speechModel='phone_call',
            enhanced='true',
            language='en-IN'  # Default: English. Will switch after language is detected.
        )
        # Greet in English (default language) — concise, professional
        say = SubElement(gather, 'Say', voice='Polly.Aditi', language='en-IN')
        say.text = "Hello, this is your Property Assistant. What type of property are you looking for?"

        return twiml_response(
            '<?xml version="1.0" encoding="UTF-8"?>' +
            tostring(response, encoding='unicode')
        )

    # ── NO INPUT DETECTED (Fix #12: Limit retries) ────────────
    if no_input == 'true':
        no_input_count = int(session.get('no_input_count', 0)) + 1
        session['no_input_count'] = no_input_count
        save_session(call_sid, session)

        if no_input_count >= MAX_NO_INPUT_RETRIES:
            print(f"[NO INPUT] Max retries ({MAX_NO_INPUT_RETRIES}) reached, hanging up")
            return twiml_response(build_twiml_hangup(
                "I haven't heard from you. Ending the call now. Goodbye!",
                lang
            ))

        action_url = build_action_url(call_sid)
        return twiml_response(build_twiml(
            "I couldn't hear you. Could you please repeat?",
            action_url, call_sid, lang
        ))

    # ── NORMAL TURN: user said something ──────────────────────
    print(f"[USER SAID] {speech_text}")

    # Reset no-input counter on successful speech
    session['no_input_count'] = 0

    bot_reply, new_session = call_your_bot(speech_text, session)

    if not bot_reply:
        bot_reply = "I'm sorry, I didn't understand that. Could you please repeat?"

    # The bot Lambda detects the user's language and switches user_lang
    # in the session. This flows back here so the next Gather uses the
    # correct language for speech recognition. Context is fully preserved.
    lang = new_session.get('user_lang', 'en')
    print(f"[LANG AFTER BOT] {lang} (was: {session.get('user_lang', 'en')})")

    # Save updated session to DynamoDB
    new_session['no_input_count'] = 0
    save_session(call_sid, new_session)

    # ── Check if call should end (Fix #14: Only end on explicit 'done' step)
    is_done = (
        new_session.get('step') == 'done' and
        any(phrase in speech_text.lower()
            for phrase in ['bye', 'goodbye', 'thank you', 'done',
                           'नो थैंक यू', 'धन्यवाद', 'alvida'])
    )

    if is_done:
        # Clean up session
        try:
            session_table.delete_item(Key={'call_sid': call_sid})
        except Exception:
            pass
        return twiml_response(build_twiml_hangup(bot_reply, lang))

    action_url = build_action_url(call_sid)
    return twiml_response(build_twiml(bot_reply, action_url, call_sid, lang))
