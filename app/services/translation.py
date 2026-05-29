"""
Translation service using TranslateGemma models.

Provides translation between Indian languages and English using
TranslateGemma 27B base model deployed on vLLM.
"""

import os
import json
import re
import random
import aiohttp
import asyncio
from pathlib import Path
from typing import Literal, Optional
from openai import AsyncOpenAI
from helpers.utils import get_logger, normalize_voice_output
from dotenv import load_dotenv
from agents.tools.terms import get_mini_glossary_for_text, get_ambiguity_hints_for_query, TERM_PAIRS
from app.config import settings

try:
    from langfuse import get_client as get_langfuse_client
except ImportError:
    get_langfuse_client = None

load_dotenv()

logger = get_logger(__name__)


# Pretranslation provider — follows main LLM_PROVIDER by default.
# Override with PRETRANSLATION_PROVIDER if you want a different provider for pretranslation.
# Supported: "openai" | "vllm" (OpenAI-compatible endpoint, e.g. local Gemma 4 via vLLM).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
PRETRANSLATION_PROVIDER = os.getenv("PRETRANSLATION_PROVIDER", LLM_PROVIDER).lower()
if PRETRANSLATION_PROVIDER == "vllm":
    _PRETRANSLATION_MODEL_DEFAULT = os.getenv("LLM_MODEL_NAME", "gemma-4-31b-it")
else:
    _PRETRANSLATION_MODEL_DEFAULT = "gpt-5.1"
# OPENAI_PRETRANSLATION_MODEL kept as legacy alias; PRETRANSLATION_MODEL takes precedence.
OPENAI_PRETRANSLATION_MODEL = os.getenv(
    "PRETRANSLATION_MODEL",
    os.getenv("OPENAI_PRETRANSLATION_MODEL", _PRETRANSLATION_MODEL_DEFAULT),
)
_openai_client: Optional[AsyncOpenAI] = None

# OSS pretranslation (vLLM) — used per-request only for sticky 'oss' sessions,
# independent of the startup PRETRANSLATION_PROVIDER so legacy sessions are
# completely unaffected. Mirrors amul-oan-api's OSS pretranslation path.
OSS_INFERENCE_ENDPOINT_URL = (os.getenv("OSS_INFERENCE_ENDPOINT_URL", "") or "").rstrip("/")
OSS_INFERENCE_API_KEY = os.getenv("OSS_INFERENCE_API_KEY") or "dummy"
OSS_PRETRANSLATION_MODEL = os.getenv(
    "OSS_PRETRANSLATION_MODEL", os.getenv("OSS_LLM_MODEL_NAME", "gemma-4-31b-it")
)
_oss_pretrans_client: Optional[AsyncOpenAI] = None


def _get_oss_pretranslation_client() -> AsyncOpenAI:
    """OpenAI-compatible client pinned to the OSS vLLM endpoint."""
    global _oss_pretrans_client
    if _oss_pretrans_client is None:
        if not OSS_INFERENCE_ENDPOINT_URL:
            raise ValueError(
                "OSS_INFERENCE_ENDPOINT_URL is required for OSS pretranslation"
            )
        _oss_pretrans_client = AsyncOpenAI(
            api_key=OSS_INFERENCE_API_KEY, base_url=OSS_INFERENCE_ENDPOINT_URL
        )
    return _oss_pretrans_client


GU_PREFERRED_TRANSLATION_RULES = [
    "Use farmer-preferred Gujarati livestock terms.",
    "Address the caller respectfully with gender-neutral 'આપ' forms; never infer the caller's gender.",
    "Sarlaben must always use feminine self-reference in Gujarati (e.g. શકતી છું, કરૂં, આપી શકતી છું — never શકું, કરું, આવું).",
    "Keep the tone professional, cordial, and detached; do not become overly familiar or chatty.",
    "Do not translate English address markers such as sister, brother, bhai, ben, madam, or sir into caller labels like બહેન, ભાઈ, મેડમ, or સાહેબ. Use respectful gender-neutral 'આપ' wording instead.",
    "If the English source mentions 'sister' because the caller addressed Sarlaben, do not call the caller બહેન. Omit the address marker or render it as a neutral reference to સરલાબેન only when necessary.",
    "Never use slang body terms like 'બૈડા/બૈડું/બરડા/બરડું'. Prefer 'પીઠ' for back/flank context and 'શરીર' for general body context.",
    "Prefer 'બાવલું' over 'પાહો' for udder context.",
    "Prefer 'ધાર' over 'ટીપાં' for milk streams.",
    "Use 'ગાભણ' for pregnant livestock context.",
    "Use 'ફેટ' for fat/milk-fat (not 'ચરબી').",
    "Use 'એસ.એન.એફ.' for SNF (not 'ઘન પદાર્થો').",
    "Use 'બેક્ટેરિયા' for bacteria (not 'જંતુઓ').",
    "Use 'ધણ' for herd (not 'ટોળું').",
    "Use one mastitis term consistently: 'આંચળનો સોજો'. Do not combine 'આઉ નો સોજો' and 'બાવલાનો સોજો'.",
    "NEVER use 'સ્તન' for animal udder/teat. Use 'આંચળ' for teat and 'બાવલું' or 'આઉ' for udder.",
    "Use 'બુલ' for bull (not 'બળદ' which means bullock/ox).",
    "For bloat (આફરો), use 'ફુલેલા' (distended/puffed) not 'સોજેલા' (swollen) when describing the flank.",
    "Avoid brackets, markdown, list scaffolding, and repeated parenthetical restatements.",
    "Use 'માખણ' for butter, 'મલાઈ' for cream, 'વલોણું/વલોણાથી' for churning, and 'ઘી બનાવવું' for making ghee.",
    "Use 'ચીરો' for incision/cut (not 'ચૂભો' which is not a real word).",
    "Use 'માનસિક આઘાત' for mental trauma/stress in animals (not 'તણાવ').",
    "Use 'ફીણ' for foam (not 'ફી').",
    "Use 'દવા' for medicine (Gujarati does not pluralise as 'દવાઓ').",
    "For feed meant for a pregnant animal, say 'ગાભણ પશુ માટેનું દાણ' or 'ગાભણ દાણ'. Never invent 'ગર્ભચારો' and never say 'ગર્ભ માટેનો ચારો'.",
    "Never use the phrase 'સામાન્ય જાળવણી ચારો'. Always use natural farmer wording such as 'રોજિંદો ઘાસચારો' or 'નિયમિત સૂકો અને લીલો ચારો'.",
    "In dairy feed context, if ASR/transcription suggests 'સમુદ્રી' but livestock feed is the likely meaning, prefer asking or keeping the term conservative over drifting into marine feed or seaweed advice.",
    "Use 'તેને' (not archaic 'તેણીને') for 'to her/it'.",
    "Use 'ભૌતિક' for physical (examination/condition), not 'શારીરિક'.",
    "Never use the hallucinated fodder word 'બરબા'. Use 'બરસીમ' (or 'રજકો' where contextually better).",
    "Never output placeholder quantities like '-', '--', or '–' for feed or dose lines. If exact values are missing, keep the wording non-numeric rather than inventing a quantity.",
]


def _load_gu_term_policy() -> dict:
    candidates = [
        Path.cwd() / "assets/gu_term_policy.json",
        Path(__file__).resolve().parents[2] / "assets/gu_term_policy.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Failed loading Gujarati term policy at %s: %s", path, e)
                return {}
    return {}


def _build_gu_policy_replacements(policy: dict) -> list[tuple[str, str]]:
    forbidden = policy.get("forbidden", {}) if isinstance(policy, dict) else {}
    if not isinstance(forbidden, dict):
        return []
    # Longer keys first so phrase-level replacements win before single-word ones.
    items = sorted(
        [(str(k).strip(), str(v).strip()) for k, v in forbidden.items() if str(k).strip() and str(v).strip()],
        key=lambda kv: len(kv[0]),
        reverse=True,
    )
    out: list[tuple[str, str]] = []
    for src, dst in items:
        pattern = re.escape(src)
        out.append((pattern, dst))
    return out


GU_POST_REPLACEMENTS_BASE = [
    (r"(?i)red\s*colour\s*-?\s*delete", ""),
    (r"(?i)red\s*colour", ""),
    # Keep only script/format cleanup and a couple of safe transliteration fixes here.
    # Terminology ownership should live in the glossary/policy layers.
    (r"(?i)\bpaho\b", "બાવલું"),
    (r"ગર્ભવતી", "ગાભણ"),
    # Fix TranslateGemma ૫↔પ confusion: letter પ adjacent to Gujarati digits → ૫
    (r"(?<=[૦-૯])પ", "૫"),
    (r"પ(?=[૦-૯])", "૫"),
]
GU_TERM_POLICY = _load_gu_term_policy()
GU_POLICY_REPLACEMENTS = _build_gu_policy_replacements(GU_TERM_POLICY)
GU_POST_REPLACEMENTS = GU_POST_REPLACEMENTS_BASE + GU_POLICY_REPLACEMENTS

# ── Gender-neutral caller-address guard ─────────────────────────────────────
# Replace gendered address terms directed at the *caller* with neutral forms.
# Patterns are boundary-aware (e.g. "ભૂખ ભાઈ" is a common animal-behaviour
# phrase, but "ભાઈ," at the start of a greeting is a caller address).
# Each tuple: (compiled pattern, replacement).
GU_GENDER_NEUTRAL_POST: list[tuple[re.Pattern, str]] = [
    # "ભાઈ" or "ભૈ" as caller address (preceded by start-of-string, comma, space, or period)
    (re.compile(r"(?<![^\s,।.!?])ભ(?:ાઈ|ૈ)(?=\s*[,।!?]|\s|$)"), ""),
    # "બહેન" / "બેન" as caller address
    (re.compile(r"(?<![^\s,।.!?])બ(?:હેન|ેન)(?=\s*[,।!?]|\s|$)"), ""),
    # "સાહેબ" as caller address
    (re.compile(r"(?<![^\s,।.!?])સ(?:ા)?હ(?:ે)?બ(?=\s*[,।!?]|\s|$)"), ""),
    # "સર" as caller address
    (re.compile(r"(?<![^\s,।.!?])સર(?=\s*[,।!?]|\s|$)"), ""),
    # "મેડમ" / "મૅડમ" / "મૅડ" as caller address
    (re.compile(r"(?<![^\s,।.!?])મ(?:ે|ૅ|ૅ)ડ(?:મ|)(?=\s*[,।!?]|\s|$)"), ""),
]

# Enforce feminine self-reference for Sarlaben in Gujarati assistant output.
# Keep this intentionally narrow and deterministic to avoid semantic drift.
GU_FEMININE_SELF_REFERENCE_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"શકું\s+નથી"), "શકતી નથી"),
    (re.compile(r"શકું\s+નહીં"), "શકતી નથી"),
    (re.compile(r"શકું\s+નહિ"), "શકતી નથી"),
]

GU_WORD_BOUNDARY_START = r"(?<![\u0A80-\u0AFF])"
GU_WORD_BOUNDARY_END = r"(?![\u0A80-\u0AFF])"
GU_BODY_SLANG_VARIANTS = r"(?:બૈડા|બૈડું|બૈડુ|બરડા|બરડું|બરડુ)"
GU_BODY_BACK_SUFFIXES = r"(?:માં|મા|પર)"
GU_BODY_BACK_POSTPOSITIONS = r"(?:પર|માં|મા|પાછળ)"
GU_BODY_AGREEMENT_FIXES = [
    (r"શરીર\s+ઠંડા\s+લાગે\s+છે", "શરીર ઠંડું લાગે છે"),
    (r"શરીર\s+ઠંડી\s+લાગે\s+છે", "શરીર ઠંડું લાગે છે"),
    (r"પીઠ\s+ઠંડા\s+લાગે\s+છે", "પીઠ ઠંડી લાગે છે"),
    (r"પીઠ\s+ઠંડું\s+લાગે\s+છે", "પીઠ ઠંડી લાગે છે"),
]

_GU_PLACEHOLDER_RE = r"(?:[-–—]{1,3}|[‐‑‒―])"


def _fix_dandas(text: str) -> str:
    """Replace Devanagari dandas (।) with periods in TranslateGemma output."""
    return text.replace("।", ".")


def _normalize_gu_body_terms(text: str) -> str:
    """Normalize slang Gujarati body terms with contextual mapping."""
    out = text

    # Back/flank context: slang + attached locative suffix.
    out = re.sub(
        rf"{GU_WORD_BOUNDARY_START}(?P<lemma>{GU_BODY_SLANG_VARIANTS})(?P<suffix>{GU_BODY_BACK_SUFFIXES}){GU_WORD_BOUNDARY_END}",
        lambda m: f"પીઠ{m.group('suffix')}",
        out,
    )

    # Back/flank context: slang + spaced postposition/phrase.
    out = re.sub(
        rf"{GU_WORD_BOUNDARY_START}(?P<lemma>{GU_BODY_SLANG_VARIANTS})\s+(?P<post>{GU_BODY_BACK_POSTPOSITIONS}){GU_WORD_BOUNDARY_END}",
        lambda m: f"પીઠ {m.group('post')}",
        out,
    )
    out = re.sub(
        rf"{GU_WORD_BOUNDARY_START}(?P<lemma>{GU_BODY_SLANG_VARIANTS})\s+ની\s+બાજુ{GU_WORD_BOUNDARY_END}",
        "પીઠની બાજુ",
        out,
    )
    out = re.sub(
        rf"{GU_WORD_BOUNDARY_START}(?P<lemma>{GU_BODY_SLANG_VARIANTS})\s+ના\s+ભાગ(?P<post>{GU_BODY_BACK_SUFFIXES}){GU_WORD_BOUNDARY_END}",
        lambda m: f"પીઠના ભાગ{m.group('post')}",
        out,
    )

    # Default: generic body context.
    out = re.sub(
        rf"{GU_WORD_BOUNDARY_START}(?P<lemma>{GU_BODY_SLANG_VARIANTS})(?P<suffix>ના|ની|નું|નો|ને|થી)?{GU_WORD_BOUNDARY_END}",
        lambda m: f"શરીર{m.group('suffix') or ''}",
        out,
    )

    for pat, repl in GU_BODY_AGREEMENT_FIXES:
        out = re.sub(pat, repl, out)

    return out


def _post_normalize_gu_translation(
    text: str,
    target_lang: str,
    *,
    strip_outer: bool = False,
) -> str:
    if target_lang.lower() not in ("gujarati", "gu"):
        return text
    out = text
    out = _normalize_gu_body_terms(out)
    for pat, repl in GU_POST_REPLACEMENTS:
        out = re.sub(pat, repl, out)
    # Remove placeholder dashes without inventing a quantity.
    out = re.sub(rf"([:：]\s*){_GU_PLACEHOLDER_RE}(?=\s|$)", r"\1", out)

    # -- Gender-neutral caller-address guard --------------------------------
    # Strip gendered address terms (ભાઈ, બહેન, સાહેબ, મેડમ, સર) directed at
    # the caller before the text reaches TTS.
    for pat, repl in GU_GENDER_NEUTRAL_POST:
        out = pat.sub(repl, out)

    # -- Feminine self-reference guard --------------------------------------
    # Sarlaben must not leak masculine first-person modal forms.
    for pat, repl in GU_FEMININE_SELF_REFERENCE_REPLACEMENTS:
        out = pat.sub(repl, out)

    # -- Scaffold collapse: "Label: value\nLabel: value" → spoken flow ------
    # Collapse inline label patterns (short non-space word + colon at line start)
    # into a comma-space connector so they don't create list-like TTS artifacts.
    out = re.sub(r"(?m)^\s*[^\s:।.!?\n]{1,20}\s*:\s*", ", ", out)
    # Strip stray leading comma left by the above at the start of text
    out = re.sub(r"^\s*,\s*", "", out)

    # -- Unicode noise cleanup -----------------------------------------------
    out = out.replace("\u00A0", " ")   # NBSP → regular space
    out = out.replace("\u200D", "")    # ZWJ → removed
    out = out.replace("\u200C", "")    # ZWNJ → removed
    # Punctuation spacing: no space before ,।.!?
    out = re.sub(r"\s+([,।.!?])", r"\1", out)

    # collapse extra spaces introduced by removals
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = normalize_voice_output(out, target_lang, replace_slash=False)
    return out.strip() if strip_outer else out


TRANSLATION_ENDPOINTS = {
    "4b": os.getenv("TRANSLATEGEMMA_4B_ENDPOINT", "http://10.128.170.2:8081/v1"),
    "12b": os.getenv("TRANSLATEGEMMA_12B_ENDPOINT", "http://10.128.170.2:8082/v1"),
    "27b": os.getenv("TRANSLATEGEMMA_27B_ENDPOINT", "http://localhost:8085/v1"),
    "27b-base": os.getenv("TRANSLATEGEMMA_27B_BASE_ENDPOINT", "http://localhost:18002/v1"),
}

# Multi-endpoint support: comma-separated list for load-balanced 27b-base
_27b_base_ep_raw = os.getenv("TRANSLATEGEMMA_27B_BASE_ENDPOINTS", "").strip()
TRANSLATION_ENDPOINTS_27B_BASE: list[str] = (
    [e.strip() for e in _27b_base_ep_raw.split(",") if e.strip()]
    if _27b_base_ep_raw
    else [TRANSLATION_ENDPOINTS["27b-base"]]
)

DEFAULT_TRANSLATION_MODEL = os.getenv("DEFAULT_TRANSLATION_MODEL", "27b-base")

TRANSLATION_MODEL_IDS = {
    "4b": os.getenv("TRANSLATEGEMMA_4B_MODEL", "translategemma-4b"),
    "12b": os.getenv("TRANSLATEGEMMA_12B_MODEL", "translategemma-12b"),
    "27b": os.getenv("TRANSLATEGEMMA_27B_MODEL", "marathi-translategemma-27b-2250"),
    "27b-base": os.getenv("TRANSLATEGEMMA_27B_BASE_MODEL", "translategemma-27b-base"),
}

LANG_NAMES = {
    "marathi": "Marathi", "english": "English", "hindi": "Hindi",
    "gujarati": "Gujarati", "tamil": "Tamil", "kannada": "Kannada",
    "odia": "Oriya", "telugu": "Telugu", "punjabi": "Punjabi",
    "malayalam": "Malayalam", "bengali": "Bengali", "urdu": "Urdu",
    "assamese": "Assamese",
    "mr": "Marathi", "en": "English", "hi": "Hindi", "gu": "Gujarati",
    "ta": "Tamil", "kn": "Kannada", "or": "Oriya", "te": "Telugu",
    "pa": "Punjabi", "ml": "Malayalam", "bn": "Bengali", "ur": "Urdu",
    "as": "Assamese"
}

LANG_CODES = {
    "marathi": "mr", "english": "en", "hindi": "hi", "gujarati": "gu",
    "tamil": "ta", "kannada": "kn", "odia": "or", "telugu": "te",
    "punjabi": "pa", "malayalam": "ml", "bengali": "bn", "urdu": "ur",
    "assamese": "as",
    "mr": "mr", "en": "en", "hi": "hi", "gu": "gu", "ta": "ta",
    "kn": "kn", "or": "or", "te": "te", "pa": "pa", "ml": "ml",
    "bn": "bn", "ur": "ur", "as": "as"
}

INDIAN_LANGUAGES = [
    "marathi", "mr", "hindi", "hi", "gujarati", "gu", "tamil", "ta",
    "kannada", "kn", "odia", "or", "telugu", "te", "punjabi", "pa",
    "malayalam", "ml", "bengali", "bn", "urdu", "ur", "assamese", "as"
]


def _format_translation_prompt(
    text: str,
    source_lang: str,
    target_lang: str,
    mini_glossary: Optional[str] = None,
) -> str:
    """Format the translation prompt using TranslateGemma's official chat template.
    When target is Gujarati and mini_glossary is provided, injects a dynamic term list
    so the model uses consistent domain terminology."""
    source_name = LANG_NAMES.get(source_lang.lower(), source_lang.capitalize())
    target_name = LANG_NAMES.get(target_lang.lower(), target_lang.capitalize())
    source_code = LANG_CODES.get(source_lang.lower(), source_lang.lower())
    target_code = LANG_CODES.get(target_lang.lower(), target_lang.lower())

    instruction = (
        f"You are a professional {source_name} ({source_code}) to {target_name} ({target_code}) translator. "
        f"Your goal is to accurately convey the meaning and nuances of the original {source_name} text "
        f"while adhering to {target_name} grammar, vocabulary, and cultural sensitivities.\n"
        f"Produce only the {target_name} translation, without any additional explanations or commentary.\n"
        f"Prefer clear spoken language over literal formatting. Do not preserve markdown, bullets, numbered lists, or bracketed duplicates if they hurt voice clarity."
    )
    if mini_glossary and mini_glossary.strip():
        lines = mini_glossary.strip().splitlines()
        rules = []
        for line in lines:
            if " -> " in line:
                en_term, gu_term = line.split(" -> ", 1)
                rules.append(f"Rule: '{en_term.strip()}' must be translated as '{gu_term.strip()}'.")
        if rules:
            instruction += "\n\n**Terminology Rules (mandatory):**\n" + "\n".join(rules) + "\n"
    if target_code == "gu":
        instruction += (
            "\n\n**Gujarati Livestock Style Rules (mandatory):**\n- "
            + "\n- ".join(GU_PREFERRED_TRANSLATION_RULES)
            + "\n"
        )
    instruction += f"\n\nPlease translate the following {source_name} text into {target_name}:\n\n\n{text.strip()}"

    prompt = (
        f"<bos><start_of_turn>user\n"
        f"{instruction}<end_of_turn>\n"
        f"<start_of_turn>model\n"
    )
    return prompt


def _resolve_model(model_size: Optional[str], target_lang: str) -> tuple[str, Optional[str], Optional[str]]:
    """
    Resolve to 27b-base model/endpoint for all translations.
    Returns (model_size, endpoint, model_id).
    """
    model_size = "27b-base"
    endpoint = random.choice(TRANSLATION_ENDPOINTS_27B_BASE)
    model_id = TRANSLATION_MODEL_IDS.get(model_size)
    return model_size, endpoint, model_id


def _get_langfuse():
    if not get_langfuse_client:
        return None
    try:
        return get_langfuse_client()
    except Exception:
        return None


async def translate_text(
    text: str,
    source_lang: str,
    target_lang: str,
    model_size: Optional[Literal["4b", "12b", "27b", "27b-base"]] = None,
    temperature: float = 0.0,
    max_tokens: int = 2048
) -> str:
    """Translate text using TranslateGemma."""
    if not text or not text.strip():
        return text

    if source_lang.lower() == target_lang.lower():
        logger.info("Source and target languages are the same, skipping translation")
        return text

    model_size, endpoint, model_id = _resolve_model(model_size, target_lang)
    if not endpoint or not model_id:
        raise ValueError(f"Invalid translation model size: {model_size}")

    mini_glossary = ""
    if target_lang.lower() in ("gujarati", "gu"):
        mini_glossary = get_mini_glossary_for_text(text, threshold=0.90, max_terms=40)
        if mini_glossary:
            logger.info(f"Translation prompt: injected mini glossary ({len(mini_glossary.splitlines())} terms)")
    prompt = _format_translation_prompt(text, source_lang, target_lang, mini_glossary=mini_glossary)
    logger.info(f"Translating {source_lang} -> {target_lang} using {model_size} model")

    langfuse = _get_langfuse()

    try:
        if not langfuse:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{endpoint}/completions",
                    json={
                        "model": model_id,
                        "prompt": prompt,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Translation API error {response.status}: {error_text}")
                        raise Exception(f"Translation failed with status {response.status}")

                    result = await response.json()
                    translated_text = result["choices"][0]["text"].strip()
                    translated_text = _fix_dandas(translated_text)
                    translated_text = _post_normalize_gu_translation(
                        translated_text,
                        target_lang,
                    )
                    translated_text = normalize_voice_output(translated_text, target_lang)
                    logger.info(f"Translation successful ({len(text)} -> {len(translated_text)} chars)")
                    return translated_text

        with langfuse.start_as_current_observation(
            name="text_translation",
            as_type="generation",
            input={
                "source_lang": source_lang,
                "target_lang": target_lang,
                "text": text,
            },
            model=model_id,
            metadata={
                "translation_provider": "translategemma",
                "model_size": model_size,
                "pipeline_stage": "text_translation",
            },
        ) as observation:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{endpoint}/completions",
                    json={
                        "model": model_id,
                        "prompt": prompt,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Translation API error {response.status}: {error_text}")
                        raise Exception(f"Translation failed with status {response.status}")

                    result = await response.json()
                    translated_text = result["choices"][0]["text"].strip()
                    translated_text = _fix_dandas(translated_text)
                    translated_text = _post_normalize_gu_translation(
                        translated_text,
                        target_lang,
                    )
                    translated_text = normalize_voice_output(translated_text, target_lang)
                    observation.update(output=translated_text)
                    logger.info(f"Translation successful ({len(text)} -> {len(translated_text)} chars)")
                    return translated_text

    except aiohttp.ClientError as e:
        logger.error(f"Translation API connection error: {str(e)}")
        raise Exception(f"Failed to connect to translation service: {str(e)}")


def _get_openai_client() -> AsyncOpenAI:
    """Return an OpenAI-compatible async client.

    When PRETRANSLATION_PROVIDER=vllm, point the OpenAI client at the local
    vLLM `INFERENCE_ENDPOINT_URL` (e.g. http://10.185.25.198:8020/v1) so the
    same chat-completions call path serves an OSS model like Gemma 4 31B IT.
    """
    global _openai_client
    if _openai_client is None:
        if PRETRANSLATION_PROVIDER == "vllm":
            base_url = os.getenv("INFERENCE_ENDPOINT_URL", "").rstrip("/")
            if not base_url:
                raise ValueError(
                    "INFERENCE_ENDPOINT_URL is required when PRETRANSLATION_PROVIDER=vllm"
                )
            api_key = os.getenv("INFERENCE_API_KEY") or "dummy"
            _openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        else:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY is required for OpenAI pretranslation")
            _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


def _get_glossary_hints_for_gu_query(text: str, max_results: int = 7) -> str:
    """Fuzzy-match Gujarati input against glossary gu/transliteration fields.

    Returns a compact hint string like:
      આફરો = Bloat (rumen tympany)
      આંચળ = Udder / Teat
    """
    from rapidfuzz import fuzz as _fuzz

    if not text or not text.strip():
        return ""

    text_lower = text.lower().strip()
    scored: list[tuple[str, str, float]] = []

    for tp in TERM_PAIRS:
        scores: list[float] = []
        gu_lower = (tp.gu or "").lower().strip()
        translit_lower = (tp.transliteration or "").lower().strip()

        # Check substring containment first (fast path), ignoring empty fields.
        if gu_lower:
            scores.append(100.0 if gu_lower in text_lower else _fuzz.partial_ratio(gu_lower, text_lower))
        if translit_lower:
            scores.append(
                100.0 if translit_lower in text_lower else _fuzz.partial_ratio(translit_lower, text_lower)
            )
        if not scores:
            continue
        best = max(scores)

        if best >= 75:
            scored.append((tp.gu, tp.en, best))

    if not scored:
        return ""

    # Deduplicate by English term, keep highest score
    seen_en: dict[str, tuple[str, str, float]] = {}
    for gu, en, score in scored:
        en_key = en.lower()
        if en_key not in seen_en or score > seen_en[en_key][2]:
            seen_en[en_key] = (gu, en, score)

    top = sorted(seen_en.values(), key=lambda x: x[2], reverse=True)[:max_results]
    return "\n".join(f"  {gu} = {en}" for gu, en, _ in top)


def _whole_ascii_token_pattern(term: str) -> str:
    escaped = re.escape(term.strip())
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"


def _apply_exact_glossary_transliteration_replacements(source_text: str, translation: str) -> str:
    """Replace model transliterations with glossary labels for exact Gujarati term hits."""
    if not source_text or not translation:
        return translation

    source_lower = source_text.lower()
    cleaned = translation

    for tp in TERM_PAIRS:
        gu_term = (tp.gu or "").strip()
        transliteration = (tp.transliteration or "").strip()
        english_label = (tp.en or "").strip()
        if not gu_term or not transliteration or not english_label:
            continue
        if len(transliteration) < 3 or transliteration.lower() == english_label.lower():
            continue
        if gu_term.lower() not in source_lower:
            continue
        if re.search(_whole_ascii_token_pattern(english_label), cleaned, flags=re.IGNORECASE):
            continue

        cleaned = re.sub(
            _whole_ascii_token_pattern(transliteration),
            english_label,
            cleaned,
            flags=re.IGNORECASE,
        )

    return cleaned


def _build_openai_pretranslation_messages(source_name: str, source_code: str, text: str) -> list[dict[str, str]]:
    # -- Domain context ------------------------------------------------
    domain_preamble = (
        "You are translating messages from Indian dairy farmers calling the Amul AI helpline (voiced as 'Sarlaben' / સરલાબેન). "
        "The farmers speak Gujarati and ask about animal health, milk production, fodder, breeding, and dairy cooperative services.\n\n"
        "IMPORTANT translation rules:\n"
        "- Your job is faithful pretranslation for safe routing, not correction, completion, or advice.\n"
        "- Preserve uncertainty from the original speech. Do not repair missing words, fill missing slots, or choose a clean interpretation when the audio transcript is ambiguous.\n"
        "- Words that look like human names (e.g. સલાદ, સરલા, ગંગા) are almost always ANIMAL NAMES (cow/buffalo names). Transliterate them as-is, do NOT translate literally.\n"
        "- If a garbled token does not clearly map to a real medicine, feed, symptom, or service term, do NOT invent a meaning. Keep the translation conservative.\n"
        "- Kinship words like બેન, બહેન, ભાઈ are often address markers for Sarlaben or filler in phone speech. Do not turn them into the caller's gender. Use 'Sarlaben' only if the caller is clearly addressing the assistant; otherwise omit the address marker.\n"
        "- 'ભાઈ' in livestock context may refer to a male animal (bull/ox); keep it generic if the word could also be an address marker.\n"
        "- Prefer veterinary/agricultural meanings only when the term is clear in the original transcript. If choosing the agricultural meaning requires guessing, preserve the uncertain token.\n"
        "- Do not infer animal species. If cow/buffalo/sheep/goat is unclear, write 'unclear animal' or keep the uncertain token.\n"
    )

    # -- Ambiguity hints from ambiguity_terms.json ---------------------
    # include_ask=False so "ask" type entries (clarifying-question rules
    # meant for the answering agent) don't leak into the translator prompt
    # and get echoed back as appended follow-up questions.
    ambiguity_hints = get_ambiguity_hints_for_query(text, include_ask=False)
    if ambiguity_hints:
        domain_preamble += f"\nDomain-specific disambiguation rules for terms in this message:\n{ambiguity_hints}\n"

    # -- Glossary hints (top matching gu→en terms) ---------------------
    glossary_hints = _get_glossary_hints_for_gu_query(text, max_results=7)
    if glossary_hints:
        domain_preamble += (
            f"\nGlossary (Gujarati → English) for terms likely in this message:\n{glossary_hints}\n"
            "Glossary usage rule: If the user's term clearly matches a glossary line above, use the right-hand English label "
            "from that line instead of transliterating the Gujarati token. Do not output the romanized/transliterated form "
            "when a matching glossary English label is available. Domain-specific disambiguation rules above override "
            "glossary lines if they conflict.\n"
        )

    system_content = (
        f"{domain_preamble}\n"
        "Translate the user's message to faithful spoken English for an internal agent. "
        "Respond with JSON: {\"translation\": \"...\"}.\n\n"
        "Do not preserve markdown, bullets, bracketed duplicates, or other formatting clutter, but do preserve the meaning uncertainty.\n"
        "When the input is garbled noise, random syllables, fragmentary, contradictory, or when any key noun, animal species, medicine, feed, product, disease, symptom, or requested action is uncertain, still provide the most faithful translation possible, using markers such as 'unclear animal', 'unclear feed name', 'unclear symptom', or '[unclear token]' instead of inventing missing meaning.\n"
        "Never convert a doubtful token into a specific medicine, feed, disease, animal species, or service term just because it would make a plausible livestock question."
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": text.strip()},
    ]


def _build_structured_pretranslation_prompt(source_name: str, source_code: str, text: str) -> str:
    """Build the structured translation prompt for non-OpenAI fallback models."""
    messages = _build_openai_pretranslation_messages(source_name, source_code, text)
    system_content = messages[0]["content"]
    user_content = messages[1]["content"]
    return (
        "<bos><start_of_turn>user\n"
        f"{system_content}\n\nUser message:\n{user_content}\n\n"
        'Respond only with valid JSON: {"translation": "..."}.'
        "<end_of_turn>\n"
        "<start_of_turn>model\n"
    )


async def _create_openai_pretranslation_response(
    client: AsyncOpenAI,
    *,
    source_name: str,
    source_code: str,
    text: str,
    max_tokens: int,
):
    return await asyncio.wait_for(
        client.chat.completions.create(
            model=OPENAI_PRETRANSLATION_MODEL,
            messages=_build_openai_pretranslation_messages(source_name, source_code, text),
            max_completion_tokens=max_tokens,
            response_format={"type": "json_object"},
        ),
        timeout=settings.openai_pretranslation_timeout_seconds,
    )


def _extract_openai_message_diagnostics(response) -> dict:
    choice = response.choices[0] if getattr(response, "choices", None) else None
    message = getattr(choice, "message", None) if choice is not None else None
    usage = getattr(response, "usage", None)
    diagnostics = {
        "response_id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "finish_reason": getattr(choice, "finish_reason", None) if choice is not None else None,
        "content_present": bool(getattr(message, "content", None)) if message is not None else False,
        "refusal": getattr(message, "refusal", None) if message is not None else None,
        "tool_calls": len(getattr(message, "tool_calls", []) or []) if message is not None else 0,
        "usage": usage.model_dump() if hasattr(usage, "model_dump") else str(usage),
    }
    return diagnostics


def _raise_empty_pretranslation(
    response,
    *,
    source_lang: str,
    text: str,
) -> None:
    diagnostics = _extract_openai_message_diagnostics(response)
    logger.error(
        "OpenAI pretranslation returned empty output - source_lang=%s model=%s query_chars=%s query_preview=%r diagnostics=%s",
        source_lang,
        OPENAI_PRETRANSLATION_MODEL,
        len(text or ""),
        (text or "")[:160],
        diagnostics,
    )
    raise ValueError("GPT pretranslation returned empty output")


def _extract_translation_from_response(response) -> str:
    """Extract the translation string from an OpenAI JSON response."""
    raw = (response.choices[0].message.content or "").strip()
    return _extract_translation_from_raw(raw)


def _extract_translation_from_raw(raw: str) -> str:
    """Extract the translation string from raw model output."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        return (data.get("translation") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        # Fallback: use raw content if JSON parsing fails
        return raw


async def translate_to_english_with_structured_fallback(
    text: str,
    source_lang: str,
    *,
    max_tokens: int = 1024,
) -> str:
    """Fallback pretranslation with the same structured contract as the OpenAI path.

    Returns the translated text, or an empty string on empty/failed output.
    """
    if not text or not text.strip():
        return text

    if source_lang.lower() in {"english", "en"}:
        return text

    source_name = LANG_NAMES.get(source_lang.lower(), source_lang.capitalize())
    source_code = LANG_CODES.get(source_lang.lower(), source_lang.lower())
    prompt = _build_structured_pretranslation_prompt(source_name, source_code, text)
    model_size, endpoint, model_id = _resolve_model(None, "english")
    if not endpoint or not model_id:
        raise ValueError(f"Invalid translation model size: {model_size}")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{endpoint}/completions",
            json={
                "model": model_id,
                "prompt": prompt,
                "temperature": 0.0,
                "max_tokens": max_tokens,
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error("Structured fallback pretranslation API error %s: %s", response.status, error_text)
                raise Exception(f"Structured fallback pretranslation failed with status {response.status}")

            result = await response.json()
            raw_text = result["choices"][0]["text"].strip()
            translated_text = _extract_translation_from_raw(raw_text)
            translated_text = normalize_voice_output(translated_text, "english")
            translated_text = _apply_exact_glossary_transliteration_replacements(text, translated_text)
            if not translated_text:
                logger.warning(
                    "Structured fallback pretranslation returned empty - source_lang=%s query=%r",
                    source_lang,
                    (text or "")[:100],
                )
                return text
            return translated_text


async def translate_to_english_with_gpt5_mini(
    text: str,
    source_lang: str,
    *,
    max_tokens: int = 1024,
) -> str:
    """Translate input text to English using OpenAI for pipeline pre-translation.

    Returns the translated text, or an empty string on empty/failed output.
    """
    if not text or not text.strip():
        return text

    if source_lang.lower() in {"english", "en"}:
        return text

    client = _get_openai_client()
    source_name = LANG_NAMES.get(source_lang.lower(), source_lang.capitalize())
    source_code = LANG_CODES.get(source_lang.lower(), source_lang.lower())

    langfuse = _get_langfuse()

    try:
        if not langfuse:
            response = await _create_openai_pretranslation_response(
                client,
                source_name=source_name,
                source_code=source_code,
                text=text,
                max_tokens=max_tokens,
            )
            translated_text = _extract_translation_from_response(response)
            if not translated_text:
                logger.warning(
                    "OpenAI pretranslation returned empty - source_lang=%s query=%r",
                    source_lang, (text or "")[:100],
                )
                return text
            translated_text = _apply_exact_glossary_transliteration_replacements(text, translated_text)
            return translated_text

        with langfuse.start_as_current_observation(
            name="query_pretranslation",
            as_type="generation",
            input={
                "source_lang": source_lang,
                "target_lang": "english",
                "text": text,
            },
            model=OPENAI_PRETRANSLATION_MODEL,
            metadata={
                "translation_provider": PRETRANSLATION_PROVIDER,
                "pipeline_stage": "query_pretranslation",
            },
        ) as observation:
            response = await _create_openai_pretranslation_response(
                client,
                source_name=source_name,
                source_code=source_code,
                text=text,
                max_tokens=max_tokens,
            )
            translated_text = _extract_translation_from_response(response)
            if not translated_text:
                logger.warning(
                    "OpenAI pretranslation returned empty - source_lang=%s query=%r",
                    source_lang, (text or "")[:100],
                )
                observation.update(output="__EMPTY__")
                return text
            translated_text = _apply_exact_glossary_transliteration_replacements(text, translated_text)
            observation.update(output=translated_text)
            return translated_text
    except asyncio.TimeoutError as e:
        logger.error(
            "OpenAI pretranslation timed out - source_lang=%s model=%s timeout_seconds=%.2f query_chars=%s query_preview=%r",
            source_lang,
            OPENAI_PRETRANSLATION_MODEL,
            settings.openai_pretranslation_timeout_seconds,
            len(text or ""),
            (text or "")[:160],
        )
        raise TimeoutError("OpenAI pretranslation timed out") from e


async def _create_oss_pretranslation_response(
    client: AsyncOpenAI,
    *,
    source_name: str,
    source_code: str,
    text: str,
    max_tokens: int,
):
    """Mirror of _create_openai_pretranslation_response, pinned to the OSS vLLM endpoint."""
    return await asyncio.wait_for(
        client.chat.completions.create(
            model=OSS_PRETRANSLATION_MODEL,
            messages=_build_openai_pretranslation_messages(source_name, source_code, text),
            max_completion_tokens=max_tokens,
            response_format={"type": "json_object"},
        ),
        timeout=settings.openai_pretranslation_timeout_seconds,
    )


async def translate_to_english_with_oss_vllm(
    text: str,
    source_lang: str,
    *,
    max_tokens: int = 1024,
) -> str:
    """Pretranslate via the OSS vLLM endpoint (per-request, sticky 'oss' sessions).

    Same return contract as translate_to_english_with_gpt5_mini: the translated
    text, or an empty string on empty/failed output.

    Legacy sessions never hit this — the function is only called when the
    sticky pipeline router returns variant='oss'.
    """
    if not text or not text.strip():
        return text

    if source_lang.lower() in {"english", "en"}:
        return text

    client = _get_oss_pretranslation_client()
    source_name = LANG_NAMES.get(source_lang.lower(), source_lang.capitalize())
    source_code = LANG_CODES.get(source_lang.lower(), source_lang.lower())

    langfuse = _get_langfuse()

    try:
        if not langfuse:
            response = await _create_oss_pretranslation_response(
                client,
                source_name=source_name,
                source_code=source_code,
                text=text,
                max_tokens=max_tokens,
            )
            translated_text = _extract_translation_from_response(response)
            if not translated_text:
                logger.warning(
                    "OSS vLLM pretranslation returned empty - source_lang=%s query=%r",
                    source_lang, (text or "")[:100],
                )
                return text
            translated_text = _apply_exact_glossary_transliteration_replacements(text, translated_text)
            return translated_text

        with langfuse.start_as_current_observation(
            name="query_pretranslation",
            as_type="generation",
            input={
                "source_lang": source_lang,
                "target_lang": "english",
                "text": text,
            },
            model=OSS_PRETRANSLATION_MODEL,
            metadata={
                "translation_provider": "vllm",
                "pipeline_stage": "query_pretranslation",
                "pipeline_variant": "oss",
            },
        ) as observation:
            response = await _create_oss_pretranslation_response(
                client,
                source_name=source_name,
                source_code=source_code,
                text=text,
                max_tokens=max_tokens,
            )
            translated_text = _extract_translation_from_response(response)
            if not translated_text:
                logger.warning(
                    "OSS vLLM pretranslation returned empty - source_lang=%s query=%r",
                    source_lang, (text or "")[:100],
                )
                observation.update(output="__EMPTY__")
                return text
            translated_text = _apply_exact_glossary_transliteration_replacements(text, translated_text)
            observation.update(output=translated_text)
            return translated_text
    except asyncio.TimeoutError as e:
        logger.error(
            "OSS vLLM pretranslation timed out - source_lang=%s model=%s timeout_seconds=%.2f query_chars=%s query_preview=%r",
            source_lang,
            OSS_PRETRANSLATION_MODEL,
            settings.openai_pretranslation_timeout_seconds,
            len(text or ""),
            (text or "")[:160],
        )
        raise TimeoutError("OSS vLLM pretranslation timed out") from e


async def translate_text_stream_fast(
    text: str,
    source_lang: str,
    target_lang: str,
    model_size: Optional[Literal["4b", "12b", "27b", "27b-base"]] = None,
    temperature: float = 0.0,
    max_tokens: int = 2048
):
    """Stream translated text token by token (no artificial delay)."""
    if not text or not text.strip():
        return

    if source_lang.lower() == target_lang.lower():
        yield text
        return

    model_size, endpoint, model_id = _resolve_model(model_size, target_lang)
    if not endpoint or not model_id:
        raise ValueError(f"Invalid translation model size: {model_size}")

    mini_glossary = ""
    if target_lang.lower() in ("gujarati", "gu"):
        mini_glossary = get_mini_glossary_for_text(text, threshold=0.90, max_terms=40)
        if mini_glossary:
            logger.info(f"Translation prompt: injected mini glossary ({len(mini_glossary.splitlines())} terms)")
    prompt = _format_translation_prompt(text, source_lang, target_lang, mini_glossary=mini_glossary)
    logger.info(f"Fast streaming translation {source_lang} -> {target_lang} using {model_size} model")

    translated_parts: list[str] = []
    langfuse = _get_langfuse()

    try:
        if not langfuse:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{endpoint}/completions",
                    json={
                        "model": model_id,
                        "prompt": prompt,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "stream": True
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Translation API error {response.status}: {error_text}")
                        raise Exception(f"Translation failed with status {response.status}")

                    buffer = b''
                    async for chunk in response.content.iter_chunked(64):
                        buffer += chunk
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            line = line.decode('utf-8').rstrip('\r')
                            if line.startswith('data: '):
                                data = line[6:]
                                if data == '[DONE]':
                                    break
                                try:
                                    chunk_data = json.loads(data)
                                    content = chunk_data['choices'][0].get('text', '')
                                    if content:
                                        content = _fix_dandas(content)
                                        content = _post_normalize_gu_translation(
                                            content,
                                            target_lang,
                                            strip_outer=False,
                                        )
                                        content = normalize_voice_output(
                                            content,
                                            target_lang,
                                            streaming=True,
                                        )
                                        translated_parts.append(content)
                                        yield content
                                except json.JSONDecodeError:
                                    continue
            return

        with langfuse.start_as_current_observation(
            name="stream_translation",
            as_type="generation",
            input={
                "source_lang": source_lang,
                "target_lang": target_lang,
                "text": text,
            },
            model=model_id,
            metadata={
                "translation_provider": "translategemma",
                "model_size": model_size,
                "stream": "true",
                "pipeline_stage": "stream_translation",
            },
        ) as observation:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{endpoint}/completions",
                    json={
                        "model": model_id,
                        "prompt": prompt,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "stream": True
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Translation API error {response.status}: {error_text}")
                        raise Exception(f"Translation failed with status {response.status}")

                    buffer = b''
                    async for chunk in response.content.iter_chunked(64):
                        buffer += chunk
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            line = line.decode('utf-8').rstrip('\r')
                            if line.startswith('data: '):
                                data = line[6:]
                                if data == '[DONE]':
                                    break
                                try:
                                    chunk_data = json.loads(data)
                                    content = chunk_data['choices'][0].get('text', '')
                                    if content:
                                        content = _fix_dandas(content)
                                        content = _post_normalize_gu_translation(
                                            content,
                                            target_lang,
                                            strip_outer=False,
                                        )
                                        content = normalize_voice_output(
                                            content,
                                            target_lang,
                                            streaming=True,
                                        )
                                        translated_parts.append(content)
                                        yield content
                                except json.JSONDecodeError:
                                    continue
            observation.update(output="".join(translated_parts))

    except Exception as e:
        logger.error(f"Translation streaming error: {str(e)}")
        raise
