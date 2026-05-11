import json
import boto3
import os
import time
import hmac
import hashlib
import urllib.parse

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
