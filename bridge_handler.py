import json
import urllib.parse
from xml.etree.ElementTree import Element, SubElement, tostring

from bridge.bridge_config import (
    lambda_client, EXISTING_BOT_LAMBDA, TWILIO_AUTH_TOKEN,
    MAX_NO_INPUT_RETRIES, session_table,
    validate_twilio_signature,
)
from bridge.twiml import (
    build_twiml, build_twiml_hangup, twiml_response,
    load_session, save_session, build_action_url,
)


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
