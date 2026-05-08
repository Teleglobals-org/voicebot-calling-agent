# Voicebot Calling Agent

Multilingual AI-powered voice bot for real estate property search.
Makes outbound calls via Twilio, collects property requirements through
natural conversation in English/Hindi/Marathi, and returns matching properties.

## Architecture

Twilio → API Gateway → Bridge Lambda → Bot Lambda → Bedrock/S3

## Features

- Mid-call language switching (English ↔ Hindi ↔ Marathi)
- Real-time property search from CSV datasets
- Speech-to-text error correction (30+ Twilio mishearings)
- Budget parsing (Lakhs/Crores, Hindi numerals)
- DynamoDB session management
- 132 unit tests

## Files

| File | Purpose |
|------|---------|
| `voice-bot-function-sagar.py` | Main bot Lambda (conversation, extraction, search) |
| `twilio-voicebot-bridge.py` | Twilio bridge Lambda (TwiML, sessions, call control) |
| `llm_layer.py` | LLM abstraction layer (swap models via env var) |
| `tests/` | 132 test cases |

## Deployment

1. Upload `voice-bot-function-sagar.py` as `lambda_function.py` to bot Lambda
2. Upload `twilio-voicebot-bridge.py` as `lambda_function.py` to bridge Lambda
3. Set environment variables (see INTEGRATION_GUIDE.md)

## Run Tests

```bash
pip install pytest moto boto3
python -m pytest tests/ -v
