"""
Voice Bot Call Trigger Lambda

API Endpoint: POST /call
Purpose: Initiates an outbound call via Twilio to the given phone number.
The bot will call the user and start the property search conversation.

Request Body:
    {"phone_number": "7066498822"}
    or
    {"phone_number": "+917066498822"}

Response:
    {"message": "Call initiated successfully", "call_sid": "CA...", "status": "queued"}

Environment Variables Required:
    TWILIO_ACCOUNT_SID - Twilio Account SID
    TWILIO_AUTH_TOKEN  - Twilio Auth Token
    TWILIO_FROM_NUMBER - Twilio phone number (e.g., +14243737052)
    WEBHOOK_URL        - Your bot's API Gateway URL
"""
import json
import urllib.request
import urllib.parse
import base64
import os

ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_FROM = os.environ.get('TWILIO_FROM_NUMBER', '+14243737052')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', 'https://4r7upj5i2h.execute-api.us-east-1.amazonaws.com/prod/voice')


def lambda_handler(event, context):
    # Handle CORS preflight
    if event.get('httpMethod') == 'OPTIONS':
        return {
            'statusCode': 200,
            'headers': cors_headers(),
            'body': ''
        }

    # Parse request body
    body = event.get('body', '{}')
    if isinstance(body, str):
        body = json.loads(body)

    to_number = body.get('phone_number', '')

    # Validation
    if not to_number:
        return response(400, {'error': 'Phone number is required'})

    # Clean and format number
    to_number = to_number.strip().replace(' ', '').replace('-', '')
    if not to_number.startswith('+'):
        to_number = '+91' + to_number

    if len(to_number) < 12:
        return response(400, {'error': 'Invalid phone number format'})

    # Call Twilio API
    url = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Calls.json"
    data = urllib.parse.urlencode({
        'To': to_number,
        'From': TWILIO_FROM,
        'Url': WEBHOOK_URL
    }).encode()

    credentials = base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', f'Basic {credentials}')

    try:
        resp = urllib.request.urlopen(req)
        result = json.loads(resp.read())
        return response(200, {
            'message': 'Call initiated successfully',
            'call_sid': result.get('sid'),
            'status': result.get('status'),
            'to': to_number
        })
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"[TWILIO ERROR] {e.code}: {error_body}")
        return response(500, {'error': f'Twilio error: {error_body}'})
    except Exception as e:
        print(f"[ERROR] {e}")
        return response(500, {'error': str(e)})


def response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': cors_headers(),
        'body': json.dumps(body)
    }


def cors_headers():
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type',
        'Access-Control-Allow-Methods': 'POST, OPTIONS'
    }
