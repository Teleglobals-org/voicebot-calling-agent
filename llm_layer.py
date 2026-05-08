"""
LLM Abstraction Layer for Voice Bot.

Provides a unified interface to switch between different LLMs
for translation, extraction, and conversation tasks.

Usage:
    from llm_layer import LLMRouter

    router = LLMRouter()
    result = router.translate("मुझे फ्लैट चाहिए", source="hi", target="en")
    fields = router.extract_fields("I want a 2BHK in Mumbai")
    reply  = router.generate_reply("Ask for budget", context={...})

To switch LLMs, change the config — no business logic changes needed.
"""
import boto3
import json
import os
import time

# ── Configuration ─────────────────────────────────────────────
# Set via environment variables to switch without code changes
LLM_PROVIDER       = os.environ.get('LLM_PROVIDER', 'bedrock')  # bedrock, openai, custom
LLM_MODEL_ID       = os.environ.get('LLM_MODEL_ID', 'meta.llama3-8b-instruct-v1:0')
TRANSLATION_METHOD = os.environ.get('TRANSLATION_METHOD', 'aws_translate')  # aws_translate, llm, google
FALLBACK_MODEL_ID  = os.environ.get('FALLBACK_MODEL_ID', 'amazon.nova-lite-v1:0')

# ── Available Models ──────────────────────────────────────────
BEDROCK_MODELS = {
    'llama3-8b':    'meta.llama3-8b-instruct-v1:0',
    'llama3-70b':   'meta.llama3-70b-instruct-v1:0',
    'nova-lite':    'amazon.nova-lite-v1:0',
    'nova-micro':   'amazon.nova-micro-v1:0',
    'nova-pro':     'amazon.nova-pro-v1:0',
    'claude-haiku': 'anthropic.claude-3-haiku-20240307-v1:0',
    'claude-sonnet':'anthropic.claude-3-5-sonnet-20241022-v2:0',
}


class LLMRouter:
    """
    Unified interface for all LLM operations.
    Handles provider switching, fallback, and retry logic.
    """

    def __init__(self, region='us-east-1'):
        self.region = region
        self.bedrock = boto3.client('bedrock-runtime', region_name=region)
        self.translate_client = boto3.client('translate', region_name=region)
        self.model_id = LLM_MODEL_ID
        self.fallback_model_id = FALLBACK_MODEL_ID
        self._call_count = 0
        self._total_latency = 0

    # ═════════════════════════════════════════════════════════
    # TRANSLATION
    # ═════════════════════════════════════════════════════════

    def translate(self, text, source='auto', target='en'):
        """
        Translate text between languages.
        Routes to configured translation method.
        """
        if not text or not text.strip():
            return text
        if source == target:
            return text

        method = TRANSLATION_METHOD

        if method == 'aws_translate':
            return self._translate_aws(text, source, target)
        elif method == 'llm':
            return self._translate_llm(text, source, target)
        else:
            # Default fallback
            return self._translate_aws(text, source, target)

    def _translate_aws(self, text, source, target):
        """Fast translation via AWS Translate (~80ms)."""
        try:
            result = self.translate_client.translate_text(
                Text=text,
                SourceLanguageCode=source,
                TargetLanguageCode=target
            )
            return result.get('TranslatedText', text)
        except Exception as e:
            print(f"[LLM LAYER] AWS Translate error: {e}")
            return text

    def _translate_llm(self, text, source, target):
        """
        Context-aware translation via LLM (~300-500ms).
        Better for conversational text where literal translation fails.
        """
        lang_names = {'en': 'English', 'hi': 'Hindi', 'mr': 'Marathi'}
        source_name = lang_names.get(source, source)
        target_name = lang_names.get(target, target)

        prompt = (
            f"Translate the following from {source_name} to {target_name}. "
            f"Keep it natural and conversational. Return ONLY the translation.\n\n"
            f"Text: {text}\n"
            f"Translation:"
        )

        result = self._call_llm(prompt, max_tokens=200)
        return result.strip() if result else text

    # ═════════════════════════════════════════════════════════
    # FIELD EXTRACTION
    # ═════════════════════════════════════════════════════════

    def extract_fields(self, text, fields=None):
        """
        Extract structured fields from user text.
        Returns dict with extracted values.
        """
        if fields is None:
            fields = ['property_type', 'configuration', 'location', 'budget']

        prompt = f"""Extract property search details from the user message.
Only extract values that are EXPLICITLY mentioned. Use empty string "" for anything not mentioned.

Property types: apartment, flat, house, villa, bungalow, penthouse, studio, plot, farmhouse
Configurations: 1BHK, 2BHK, 3BHK, 4BHK, 5BHK, Studio
Cities: Mumbai, Gurgaon, Hyderabad, Kolkata, Pune, Delhi, Thane, Navi Mumbai
Budget: Include unit (lakhs/crore). Examples: "50 lakhs", "1 crore"

Return ONLY valid JSON on ONE line. No explanation.
Format: {{"property_type": "", "configuration": "", "location": "", "budget": ""}}

User: {text}
Output:"""

        result = self._call_llm(prompt, max_tokens=80)
        return self._parse_json(result, fields)

    def extract_single_field(self, field_name, text):
        """Extract a single field from text."""
        prompts = {
            'property_type': f'Extract property type from: "{text}". Return ONLY the type (apartment/flat/villa/house/bungalow/studio/plot) or empty string. No explanation.',
            'configuration': f'Extract BHK from: "{text}". Return ONLY like "2BHK" or empty string. No explanation.',
            'location': f'Extract Indian city from: "{text}". Return ONLY city name or empty string. No explanation.',
            'budget': f'Extract budget from: "{text}". Return ONLY amount with unit like "1 crore" or "50 lakhs" or empty string. No explanation.',
        }

        prompt = prompts.get(field_name, '')
        if not prompt:
            return ''

        result = self._call_llm(prompt, max_tokens=30)
        return result.strip().strip('"').strip("'") if result else ''

    # ═════════════════════════════════════════════════════════
    # CONVERSATION / REPLY GENERATION
    # ═════════════════════════════════════════════════════════

    def generate_reply(self, instruction, context=None, language='en'):
        """
        Generate a conversational reply.
        Useful for dynamic responses beyond template-based replies.
        """
        ctx = json.dumps(context) if context else '{}'
        lang_name = {'en': 'English', 'hi': 'Hindi', 'mr': 'Marathi'}.get(language, 'English')

        prompt = (
            f"You are a professional property search assistant on a phone call. "
            f"Respond in {lang_name}. Be concise (1-2 sentences max). "
            f"Context: {ctx}\n"
            f"Instruction: {instruction}\n"
            f"Response:"
        )

        return self._call_llm(prompt, max_tokens=150)

    # ═════════════════════════════════════════════════════════
    # CORE LLM CALL (with fallback and retry)
    # ═════════════════════════════════════════════════════════

    def _call_llm(self, prompt, max_tokens=80, temperature=0):
        """
        Call the configured LLM with automatic fallback.
        Tries primary model first, falls back to secondary on failure.
        """
        start = time.time()

        # Try primary model
        result = self._invoke_bedrock(self.model_id, prompt, max_tokens, temperature)

        # Fallback if primary fails
        if result is None and self.fallback_model_id != self.model_id:
            print(f"[LLM LAYER] Primary failed, trying fallback: {self.fallback_model_id}")
            result = self._invoke_bedrock(self.fallback_model_id, prompt, max_tokens, temperature)

        latency = (time.time() - start) * 1000
        self._call_count += 1
        self._total_latency += latency
        print(f"[LLM LAYER] Call #{self._call_count} latency: {latency:.0f}ms")

        return result or ''

    def _invoke_bedrock(self, model_id, prompt, max_tokens, temperature):
        """Invoke a Bedrock model with the appropriate request format."""
        try:
            # Different models need different request formats
            if 'llama' in model_id or 'meta' in model_id:
                body = json.dumps({
                    "prompt": prompt,
                    "max_gen_len": max_tokens,
                    "temperature": temperature
                })
            elif 'nova' in model_id or 'amazon' in model_id:
                body = json.dumps({
                    "inputText": prompt,
                    "textGenerationConfig": {
                        "maxTokenCount": max_tokens,
                        "temperature": temperature
                    }
                })
            elif 'claude' in model_id or 'anthropic' in model_id:
                body = json.dumps({
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}]
                })
            else:
                body = json.dumps({
                    "prompt": prompt,
                    "max_gen_len": max_tokens,
                    "temperature": temperature
                })

            response = self.bedrock.invoke_model(
                modelId=model_id,
                body=body
            )

            response_body = json.loads(response['body'].read())

            # Parse response based on model type
            if 'llama' in model_id or 'meta' in model_id:
                return response_body.get('generation', '')
            elif 'nova' in model_id or 'amazon' in model_id:
                results = response_body.get('results', [{}])
                return results[0].get('outputText', '') if results else ''
            elif 'claude' in model_id or 'anthropic' in model_id:
                content = response_body.get('content', [{}])
                return content[0].get('text', '') if content else ''
            else:
                return response_body.get('generation', response_body.get('outputText', ''))

        except Exception as e:
            print(f"[LLM LAYER] Error with {model_id}: {e}")
            return None

    # ═════════════════════════════════════════════════════════
    # UTILITIES
    # ═════════════════════════════════════════════════════════

    def _parse_json(self, raw, fields):
        """Parse JSON from LLM output, handling common issues."""
        import re
        if not raw:
            return {f: '' for f in fields}

        match = re.search(r'\{[^{}]*\}', raw, re.DOTALL)
        if not match:
            return {f: '' for f in fields}

        try:
            json_str = match.group(0)
            json_str = re.sub(r':\s*(None|null|undefined)', ': ""', json_str)
            parsed = json.loads(json_str)

            result = {}
            for field in fields:
                value = parsed.get(field, '')
                if isinstance(value, list):
                    value = ', '.join(str(v) for v in value)
                if value is None or str(value).lower() in ('none', 'null', 'undefined', 'n/a'):
                    value = ''
                result[field] = str(value).strip()
            return result
        except Exception:
            return {f: '' for f in fields}

    def get_stats(self):
        """Return performance stats."""
        avg = self._total_latency / max(self._call_count, 1)
        return {
            'calls': self._call_count,
            'total_latency_ms': round(self._total_latency),
            'avg_latency_ms': round(avg),
            'model': self.model_id,
            'fallback': self.fallback_model_id,
        }

    def switch_model(self, model_key):
        """
        Hot-switch to a different model mid-session.
        Useful for A/B testing or degraded mode.
        """
        if model_key in BEDROCK_MODELS:
            old = self.model_id
            self.model_id = BEDROCK_MODELS[model_key]
            print(f"[LLM LAYER] Switched: {old} → {self.model_id}")
            return True
        return False
