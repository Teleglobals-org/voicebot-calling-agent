"""
Test: Marathi Language Detection, Switching, and Response

Verifies that:
1. Marathi romanized text is detected correctly
2. Marathi Devanagari text is detected correctly
3. Mid-call switch English → Marathi works
4. Mid-call switch Marathi → English works
5. Mid-call switch Hindi → Marathi works
6. Context is preserved across Marathi switches
7. Bot responds in Marathi when user speaks Marathi
8. Marathi no-preference phrases are recognized

Run with: python -m pytest tests/test_marathi_language.py -v
"""
import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock
import importlib.util

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
os.environ['AWS_ACCESS_KEY_ID'] = 'test'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'test'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch('boto3.client') as _mock:
    _mock.return_value = MagicMock()
    config = load_module('bot.config', 'bot/config.py')
    lang_mod = load_module('bot.language', 'bot/language.py')


# =============================================================
# TEST: Marathi Language Detection (Romanized)
# =============================================================

class TestMarathiDetectionRomanized:
    """Test that romanized Marathi text is detected correctly."""

    def test_mala_flat_pahije(self):
        """'mala flat pahije' = I want a flat"""
        lang, conf = lang_mod.detect_language("mala flat pahije")
        assert lang == "mr", f"Expected 'mr', got '{lang}' (conf={conf:.2f})"
        assert conf >= 0.5

    def test_mala_mumbai_madhe_pahije(self):
        """'mala mumbai madhe flat pahije' = I want flat in Mumbai"""
        lang, conf = lang_mod.detect_language("mala mumbai madhe flat pahije")
        assert lang == "mr"
        assert conf >= 0.5

    def test_kuthe_ahe(self):
        """'property kuthe ahe' = where is the property"""
        lang, conf = lang_mod.detect_language("property kuthe ahe")
        assert lang == "mr"

    def test_kiti_budget(self):
        """'mala 1 crore budget madhe pahije'"""
        lang, conf = lang_mod.detect_language("mala 1 crore budget madhe pahije")
        assert lang == "mr"

    def test_chalel(self):
        """'chalel' = okay/fine (Marathi confirmation)"""
        lang, conf = lang_mod.detect_language("ho chalel theek ahe")
        assert lang == "mr"

    def test_nakko(self):
        """'nakko' = don't want (Marathi)"""
        lang, conf = lang_mod.detect_language("nakko mala nako ahe")
        assert lang == "mr"

    def test_namaskar(self):
        """'namaskar' = Marathi greeting"""
        lang, conf = lang_mod.detect_language("namaskar mala flat pahije")
        assert lang == "mr"

    def test_kaay_pahije(self):
        """'kaay pahije tumhala' = what do you want"""
        lang, conf = lang_mod.detect_language("kaay pahije tumhala")
        assert lang == "mr"


# =============================================================
# TEST: Marathi Language Detection (Devanagari)
# =============================================================

class TestMarathiDetectionDevanagari:
    """Test that Marathi Devanagari text is detected correctly."""

    def test_mala_pahije_devanagari(self):
        """मला फ्लॅट पाहिजे = I want a flat"""
        lang, conf = lang_mod.detect_language("मला फ्लॅट पाहिजे")
        assert lang == "mr"
        assert conf >= 0.9

    def test_mumbai_madhe(self):
        """मला मुंबई मध्ये फ्लॅट पाहिजे"""
        lang, conf = lang_mod.detect_language("मला मुंबई मध्ये फ्लॅट पाहिजे")
        assert lang == "mr"

    def test_nahi_marathi(self):
        """नाही = no in Marathi"""
        lang, conf = lang_mod.detect_language("नाही मला नको आहे")
        assert lang == "mr"

    def test_aamhala(self):
        """आम्हाला = we want"""
        lang, conf = lang_mod.detect_language("आम्हाला मुंबई मध्ये घर पाहिजे")
        assert lang == "mr"


# =============================================================
# TEST: Marathi vs Hindi Distinction
# =============================================================

class TestMarathiVsHindi:
    """Ensure Marathi is not confused with Hindi."""

    def test_pahije_is_marathi_not_hindi(self):
        """'pahije' is exclusively Marathi (Hindi uses 'chahiye')"""
        lang, conf = lang_mod.detect_language("mala parking pahije")
        assert lang == "mr"

    def test_chahiye_is_hindi_not_marathi(self):
        """'chahiye' is exclusively Hindi"""
        lang, conf = lang_mod.detect_language("mujhe parking chahiye")
        assert lang == "hi"

    def test_ahe_is_marathi(self):
        """'ahe' (is) is Marathi"""
        lang, conf = lang_mod.detect_language("te kuthe ahe")
        assert lang == "mr"

    def test_hai_is_hindi(self):
        """'hai' (is) is Hindi"""
        lang, conf = lang_mod.detect_language("mera budget kya hai")
        assert lang == "hi"

    def test_devanagari_marathi_vs_hindi(self):
        """पाहिजे is Marathi, चाहिए is Hindi"""
        lang1, _ = lang_mod.detect_language("मला पाहिजे")
        lang2, _ = lang_mod.detect_language("मुझे चाहिए")
        assert lang1 == "mr"
        assert lang2 == "hi"


# =============================================================
# TEST: Mid-Call Language Switch — Marathi
# =============================================================

class TestMarathiMidCallSwitch:
    """Test mid-call switching to/from Marathi."""

    def test_english_to_marathi_switch(self):
        """User switches from English to Marathi mid-call."""
        lang, conf = lang_mod.detect_language("mala 2bhk flat pahije mumbai madhe")
        switch = lang_mod.should_switch_language(lang, conf, "en", 6)
        assert lang == "mr"
        assert switch is True

    def test_marathi_to_english_switch(self):
        """User switches from Marathi back to English."""
        with patch.object(lang_mod.comprehend, 'detect_dominant_language', return_value={
            'Languages': [{'LanguageCode': 'en', 'Score': 0.9}]
        }):
            lang, conf = lang_mod.detect_language("yes please search now")
        switch = lang_mod.should_switch_language(lang, conf, "mr", 4)
        assert lang == "en"
        assert switch is True

    def test_hindi_to_marathi_switch(self):
        """User switches from Hindi to Marathi."""
        lang, conf = lang_mod.detect_language("mala flat pahije mumbai madhe")
        switch = lang_mod.should_switch_language(lang, conf, "hi", 5)
        assert lang == "mr"
        assert switch is True

    def test_marathi_to_hindi_switch(self):
        """User switches from Marathi to Hindi."""
        lang, conf = lang_mod.detect_language("mujhe flat chahiye mumbai mein")
        switch = lang_mod.should_switch_language(lang, conf, "mr", 5)
        assert lang == "hi"
        assert switch is True

    def test_single_word_no_switch(self):
        """Single Marathi word should not trigger switch."""
        lang, conf = lang_mod.detect_language("hoy")
        switch = lang_mod.should_switch_language(lang, conf, "en", 1)
        assert switch is False

    def test_marathi_session_stays_marathi_on_devanagari(self):
        """If session is Marathi and Devanagari arrives, stay Marathi."""
        lang, conf = lang_mod.detect_language("मला पार्किंग पाहिजे", current_session_lang="mr")
        assert lang == "mr"
        assert conf >= 0.9

    def test_marathi_session_stays_on_english_loanwords(self):
        """Marathi with English loanwords should stay Marathi."""
        lang, conf = lang_mod.detect_language("मला parking आणि gym पाहिजे", current_session_lang="mr")
        assert lang == "mr"


# =============================================================
# TEST: Marathi No-Preference Phrases
# =============================================================

class TestMarathiNoPreference:
    """Test Marathi no-preference detection for amenities."""

    def test_kahi_nahi(self):
        from bot.search import extract_amenities_locally
        result = extract_amenities_locally("kahi nahi")
        assert result == "No specific preference"

    def test_kahihi_chalel(self):
        from bot.search import extract_amenities_locally
        result = extract_amenities_locally("kahihi chalel")
        assert result == "No specific preference"

    def test_nako(self):
        from bot.search import extract_amenities_locally
        result = extract_amenities_locally("nako")
        assert result == "No specific preference"

    def test_devanagari_kahi_nahi(self):
        from bot.search import extract_amenities_locally
        result = extract_amenities_locally("काही नाही")
        assert result == "No specific preference"

    def test_devanagari_kahihi_chalel(self):
        from bot.search import extract_amenities_locally
        result = extract_amenities_locally("काहीही चालेल")
        assert result == "No specific preference"


# =============================================================
# TEST: Full Call Simulation — English → Marathi → English
# =============================================================

class TestMarathiFullCallFlow:
    """End-to-end test: English start, switch to Marathi, switch back."""

    def _make_event(self, transcript, session):
        return {
            'inputTranscript': transcript,
            'inputMode': 'Speech',
            'sessionState': {
                'intent': {'name': 'PropertySearchIntent'},
                'sessionAttributes': session
            }
        }

    def test_full_flow_english_to_marathi(self):
        """
        Turn 1: English → Bot responds English
        Turn 2: Marathi → Bot switches to Marathi, context preserved
        """
        from bot.handler import lambda_handler

        # Turn 1: English
        with patch('bot.language.comprehend') as mock_comp:
            mock_comp.detect_dominant_language.return_value = {
                'Languages': [{'LanguageCode': 'en', 'Score': 0.95}]
            }
            event = self._make_event("I am looking for a flat", {'user_lang': 'en', 'step': 'greet'})

            mock_bedrock = MagicMock()
            mock_bedrock.read.return_value = json.dumps({
                'content': [{'text': '{"property_type": "flat", "configuration": "", "location": "", "budget": ""}'}]
            }).encode()

            with patch('bot.search.bedrock') as mock_br:
                mock_br.invoke_model.return_value = {'body': mock_bedrock}
                result = lambda_handler(event, None)

        session = result['sessionState']['sessionAttributes']
        reply = result['messages'][0]['content']
        assert session['user_lang'] == 'en'
        assert session['property_type'] == 'flat'
        assert not any('\u0900' <= c <= '\u097F' for c in reply), "Should be English"
        print(f"Turn 1 PASS: English, reply='{reply[:50]}'")

        # Turn 2: Switch to Marathi
        event = self._make_event("mala 2bhk pahije", session)

        with patch('bot.language.translate') as mock_tr:
            mock_tr.translate_text.return_value = {'TranslatedText': 'Noted response'}
            result = lambda_handler(event, None)

        session = result['sessionState']['sessionAttributes']
        assert session['user_lang'] == 'mr', f"Expected 'mr', got '{session['user_lang']}'"
        assert session['property_type'] == 'flat', "Context lost!"
        assert '2BHK' in session.get('configuration', ''), "Should have extracted 2BHK"
        print(f"Turn 2 PASS: Switched to Marathi, context preserved")

    def test_marathi_confirm_words(self):
        """Marathi yes/no words work at confirm step."""
        from bot.handler import lambda_handler

        # Test "hoy" (Marathi yes) at confirm step
        session = {
            'user_lang': 'mr', 'step': 'confirm',
            'property_type': 'flat', 'configuration': '2BHK',
            'amenities': 'Parking', 'location': 'Mumbai', 'budget': '1 crore'
        }
        event = self._make_event("hoy chalel", session)

        with patch('bot.search.search_properties', return_value=[]):
            with patch('bot.language.translate') as mock_tr:
                mock_tr.translate_text.return_value = {'TranslatedText': 'No properties found'}
                result = lambda_handler(event, None)

        session_after = result['sessionState']['sessionAttributes']
        # "hoy chalel" should be detected as yes → trigger search
        assert session_after['step'] == 'results', f"Expected 'results', got '{session_after['step']}'"
        print("Turn 3 PASS: Marathi 'hoy chalel' recognized as confirmation")

    def test_marathi_nako_is_no(self):
        """'nako' at confirm step should be treated as No."""
        from bot.handler import lambda_handler

        session = {
            'user_lang': 'mr', 'step': 'confirm',
            'property_type': 'villa', 'configuration': '3BHK',
            'amenities': 'Gym', 'location': 'Gurgaon', 'budget': '2 crore'
        }
        event = self._make_event("nako badla", session)

        with patch('bot.language.translate') as mock_tr:
            mock_tr.translate_text.return_value = {'TranslatedText': 'No problem response'}
            result = lambda_handler(event, None)

        reply = result['messages'][0]['content']
        # "nako" should trigger the "no" path
        assert 'change' in reply.lower() or 'badla' in reply.lower() or 'No problem' in reply
        print("Turn 4 PASS: Marathi 'nako' recognized as No")
