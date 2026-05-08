# LLM Layer Integration Guide

## How to Use

### 1. Drop-in replacement (minimal changes)

Replace in `voice-bot-function-sagar.py`:

```python
# OLD: Direct Bedrock call
response = bedrock.invoke_model(
    modelId="meta.llama3-8b-instruct-v1:0",
    body=json.dumps({"prompt": prompt, "max_gen_len": 80, "temperature": 0})
)
raw = json.loads(response['body'].read()).get('generation', '')

# NEW: Via LLM Layer
from llm_layer import LLMRouter
router = LLMRouter()
raw = router._call_llm(prompt, max_tokens=80)
```

### 2. For translation

```python
# OLD
result = translate.translate_text(Text=text, SourceLanguageCode="hi", TargetLanguageCode="en")

# NEW
result = router.translate(text, source="hi", target="en")
```

### 3. For field extraction

```python
# OLD: Custom prompt + Bedrock + JSON parsing
# NEW: One line
fields = router.extract_fields("I want a 2BHK flat in Mumbai for 1 crore")
# Returns: {'property_type': 'flat', 'configuration': '2BHK', 'location': 'Mumbai', 'budget': '1 crore'}
```

## Switching Models

### Via Environment Variables (no code change)

```bash
# Use Nova Lite (fast, cheap)
LLM_MODEL_ID=amazon.nova-lite-v1:0

# Use Claude Haiku (best quality)
LLM_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0

# Use Llama 70B (best Hindi)
LLM_MODEL_ID=meta.llama3-70b-instruct-v1:0

# Switch translation to LLM-based (better quality, slower)
TRANSLATION_METHOD=llm
```

### Via Code (hot-switch mid-session)

```python
router = LLMRouter()
router.switch_model('nova-lite')    # Fast mode
router.switch_model('claude-haiku') # Quality mode
router.switch_model('llama3-70b')   # Hindi mode
```

## Model Comparison for Your Use Case

| Model | Translation | Extraction | Latency | Cost | Hindi Quality |
|-------|------------|------------|---------|------|---------------|
| Llama 3 8B (current) | ⭐⭐ | ⭐⭐⭐ | ~800ms | Free tier | Good |
| Amazon Nova Lite | ⭐⭐⭐ | ⭐⭐⭐ | ~300ms | Very cheap | Good |
| Amazon Nova Micro | ⭐⭐ | ⭐⭐ | ~150ms | Cheapest | OK |
| Claude 3 Haiku | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ~500ms | Moderate | Excellent |
| Llama 3.1 70B | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ~2s | Free tier | Excellent |
| AWS Translate | ⭐⭐⭐ | N/A | ~80ms | Cheap | Good |

## Recommended Setup

```
Primary:   Amazon Nova Lite (fast, good Hindi)
Fallback:  Meta Llama 3 8B (current, proven)
Translation: AWS Translate (fast) + LLM fallback for complex phrases
```

## Deployment

1. Add `llm_layer.py` to your Lambda ZIP alongside `lambda_function.py`
2. Set environment variables for model selection
3. Import and use `LLMRouter` in your bot code

```bash
# Create ZIP with both files
zip voice-bot-function-sagar.zip lambda_function.py llm_layer.py
```
