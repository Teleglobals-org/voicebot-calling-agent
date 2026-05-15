# AI-Powered Real Estate Voice Bot

An AI-driven multilingual real estate voice calling bot built using AWS services, Twilio Voice APIs, Amazon Bedrock, and intelligent conversational workflows.

This bot can:
- Make outbound property search calls
- Converse naturally in English, Hindi, and Marathi
- Detect and switch languages dynamically during calls
- Search real estate datasets intelligently
- Maintain conversation memory
- Handle retries, interruptions, and off-topic conversations
- Provide personalized property recommendations

---

# Features

## Voice Calling
- Outbound calling using Twilio
- Dynamic TwiML generation
- Speech-to-text conversational flow
- Retry handling
- Session persistence

## Multilingual AI Conversations
Supported languages:
- English
- Hindi
- Marathi

Capabilities:
- Automatic language detection
- Mid-call language switching
- Translation-based response generation
- STT correction handling

## AI Property Search
Supports:
- Apartments
- Flats
- Villas
- Houses
- Bungalows
- Studio Apartments
- Builder Floors
- Farmhouses
- Plots/Land

Search filters:
- Configuration (1BHK–5BHK)
- Budget
- City
- Amenities
- Property type

## Memory Architecture

### Short-Term Memory
- Conversation state stored in DynamoDB
- Thread/session-based checkpointing

### Long-Term Memory
- User preferences stored across calls
- Historical search tracking
- Personalized conversations

## Smart Conversational Features
- Off-topic handling
- Vulgarity filtering
- Complaint handling
- Restart flow
- Pagination for more results
- Budget ambiguity clarification
- Smart relaxation search logic

---

# Project Structure

```bash
project-root/
│
├── bot/
│   ├── config.py
│   ├── graph.py
│   ├── graph_handler.py
│   ├── handler.py
│   ├── language.py
│   ├── memory.py
│   ├── search.py
│
├── bridge/
│   ├── bridge_handler.py
│   ├── bridge_config.py
│   ├── twiml.py
│
├── trigger/
│   ├── lambda_function.py
│
├── datasets/
│   ├── mumbai.csv
│   ├── gurgaon_10k.csv
│   ├── hyderabad.csv
│   ├── kolkata.csv
│   ├── AMENITIES.csv
│   ├── PROPERTY_TYPE.csv
│
├── requirements.txt
├── README.md
└── .env
```

---

# Architecture Overview

```text
User Call
   │
   ▼
Twilio Voice
   │
   ▼
API Gateway
   │
   ▼
Bridge Lambda
   │
   ▼
Main AI Bot Lambda
   │
   ├── Amazon Bedrock
   ├── Amazon Translate
   ├── DynamoDB
   ├── Amazon S3
   │
   ▼
Property Dataset Search
   │
   ▼
AI Response
   │
   ▼
Twilio Voice Response
```

---

# Core Modules

## `config.py`
Contains:
- AWS clients
- Property configurations
- Language settings
- Supported amenities
- Property type mappings
- Dataset configurations

---

## `language.py`
Handles:
- Language detection
- Mid-call language switching
- STT corrections
- Translation pipelines
- Romanized Hindi/Marathi handling

---

## `search.py`
Core property search engine.

Features:
- Dataset loading from S3
- Budget normalization
- Smart search ranking
- Amenity matching
- Cache-based dataset loading
- Relaxed search fallback logic

---

## `handler.py`
Primary conversational Lambda handler.

Responsibilities:
- Conversation orchestration
- Session flow management
- User intent routing
- Search triggering
- Reply generation

---

## `graph.py`
LangGraph-based conversational architecture.

Implements:
- Stateful conversation graph
- Node-based dialogue routing
- Typed agent state
- Memory integration

---

## `graph_handler.py`
Advanced memory-enabled Lambda handler.

Features:
- DynamoDB checkpointing
- Long-term memory retrieval
- Persistent conversations
- Session restoration

---

## `memory.py`
Handles:
- DynamoDB short-term memory
- Long-term user memory
- Session persistence

---

## `bridge_handler.py`
Twilio bridge Lambda.

Responsibilities:
- Twilio webhook handling
- TwiML generation
- Session loading/saving
- Call lifecycle handling
- Retry management

---

## `twiml.py`
Dynamic TwiML response generation.

Supports:
- Gather input
- Voice playback
- Language-specific responses
- Hangup flows

---

## `lambda_function.py`
Outbound call trigger Lambda.

Responsibilities:
- Triggering Twilio outbound calls
- API-based call initiation
- Number validation
- CORS handling

---

# AWS Services Used

| Service | Purpose |
|---|---|
| AWS Lambda | Serverless compute |
| Amazon Bedrock | LLM processing |
| Amazon Translate | Translation |
| DynamoDB | Session + memory storage |
| Amazon S3 | Dataset storage |
| API Gateway | Webhook endpoints |
| CloudWatch | Logging & monitoring |

---

# Twilio Services Used

| Service | Purpose |
|---|---|
| Twilio Voice API | Calling |
| Twilio Webhooks | Call routing |
| TwiML | Voice instructions |
| Speech Recognition | STT |

---

# Environment Variables

## Main Bot Lambda

```env
AWS_REGION=us-east-1

AUDIO_BUCKET=voicebot-audio
DATASET_BUCKET=voicebot-audio

CHECKPOINT_TABLE_NAME=voicebot-checkpoints
LONG_TERM_MEMORY_TABLE=voicebot-long-term-memory

TWILIO_AUTH_TOKEN=xxxxxxxx

BOT_LAMBDA_NAME=voice-bot-function
```

---

## Bridge Lambda

```env
BOT_LAMBDA_NAME=voice-bot-function

API_GATEWAY_URL=https://your-api-url.amazonaws.com/prod/voice

SESSION_TABLE_NAME=voicebot-sessions

ENABLE_TWILIO_VALIDATION=false
```

---

## Call Trigger Lambda

```env
TWILIO_ACCOUNT_SID=xxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxx

TWILIO_FROM_NUMBER=+1xxxxxxxxxx

WEBHOOK_URL=https://your-api-url.amazonaws.com/prod/voice
```

---

# DynamoDB Tables

## Session Table

```text
voicebot-sessions
```

Stores:
- Call session
- User language
- Current step
- Retry count

---

## Checkpoint Table

```text
voicebot-checkpoints
```

Stores:
- Full conversational state
- Thread memory

---

## Long-Term Memory Table

```text
voicebot-long-term-memory
```

Stores:
- User preferences
- Search history
- Last searched city
- Preferred property type

---

# Dataset Requirements

Upload these CSVs to S3:

```text
AMENITIES.csv
PROPERTY_TYPE.csv
mumbai.csv
gurgaon_10k.csv
hyderabad.csv
kolkata.csv
```

---

# Installation

## Clone Repository

```bash
git clone https://github.com/your-username/real-estate-voice-bot.git

cd real-estate-voice-bot
```

---

## Install Dependencies

```bash
pip install -r requirements.txt
```

---

# Deployment

## Deploy AWS Lambda Functions

Deploy:
- Main bot Lambda
- Bridge Lambda
- Call trigger Lambda

---

## Configure API Gateway

Create routes:

```text
POST /voice
POST /call
```

---

## Configure Twilio

Set webhook URL:

```text
https://your-api-url.amazonaws.com/prod/voice
```

---

# Example API Request

## Trigger Outbound Call

```bash
curl -X POST https://your-api-url.amazonaws.com/prod/call \
-H "Content-Type: application/json" \
-d '{
  "phone_number": "**********"
}'
```

---

# Example Conversation

```text
Bot:
Hello, this is your Property Assistant.
What type of property are you looking for?

User:
2 BHK apartment in Mumbai under 80 lakhs.

Bot:
Got it. Any preferred amenities?

User:
Gym and parking.

Bot:
I found 3 matching properties for you...
```

---

# Intelligent Features

## Language Intelligence
- Hindi ↔ English switching
- Marathi ↔ English switching
- Romanized Hindi support
- Transliteration handling

## Search Intelligence
- Smart ranking
- Budget expansion
- Relaxed search fallback
- Amenity scoring

## Conversation Intelligence
- Restart handling
- Goodbye detection
- Complaint handling
- Off-topic redirection

---

# Security

Implemented:
- Twilio signature validation
- DynamoDB session isolation
- Lambda invocation isolation
- API Gateway protection

---

# Performance Optimizations

- Dataset caching
- TTL-based cache invalidation
- Single-pass field extraction
- Reduced Bedrock calls
- Smart dataset loading

---

# Future Improvements

- WhatsApp integration
- CRM integration
- Real-time MLS integration
- Voice cloning
- Agent handoff
- Recommendation engine
- Analytics dashboard
- Multi-city scaling
- Vector search/RAG
- AI negotiation assistant

---

# Tech Stack

| Category | Technologies |
|---|---|
| Backend | Python |
| Cloud | AWS |
| Voice | Twilio |
| AI/LLM | Amazon Bedrock |
| Translation | Amazon Translate |
| Storage | DynamoDB, S3 |
| API | API Gateway |
| State Management | LangGraph |

---

# Author
Sagar Lad
