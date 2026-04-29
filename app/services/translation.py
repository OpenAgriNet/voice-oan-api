import os
import re
import asyncio
import httpx
from typing import List, Optional
from app.config import settings


class BhashiniTranslationService:
    """Service for translating text using Bhashini API."""
    
    def __init__(self):
        self.api_key = os.getenv("MEITY_API_KEY_VALUE") or settings.meity_api_key_value
        self.base_url = 'https://dhruva-api.bhashini.gov.in/services/inference/pipeline'
        self.max_parallel_requests = int(os.getenv("BHASHINI_TRANSLATE_MAX_PARALLEL", "6"))
        self.chunk_max_sentences = int(os.getenv("BHASHINI_TRANSLATE_CHUNK_MAX_SENTENCES", "2"))
        self.chunking_min_chars = int(os.getenv("BHASHINI_TRANSLATE_CHUNKING_MIN_CHARS", "180"))

    def _split_sentences(self, text: str) -> List[str]:
        # Split on common sentence terminators including Devanagari danda.
        parts = re.split(r"(?<=[\.\!\?।])\s+", text.strip())
        return [p.strip() for p in parts if p and p.strip()]

    def _chunk_paragraph(self, paragraph: str, max_sentences: int) -> List[str]:
        sentences = self._split_sentences(paragraph)
        if not sentences:
            cleaned = paragraph.strip()
            return [cleaned] if cleaned else []

        chunks: List[str] = []
        i = 0
        while i < len(sentences):
            chunk_sents = sentences[i : i + max_sentences]
            chunks.append(" ".join(chunk_sents).strip())
            i += max_sentences
        return [c for c in chunks if c]

    def _chunk_text_for_translation(self, text: str, max_sentences: int) -> List[str]:
        # Preserve paragraph boundaries to avoid merging unrelated context.
        paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
        if not paragraphs:
            return []
        chunks: List[str] = []
        for p in paragraphs:
            chunks.extend(self._chunk_paragraph(p, max_sentences=max_sentences))
        return chunks

    async def _translate_texts_with_client(
        self,
        client: httpx.AsyncClient,
        texts: List[str],
        source_lang: str,
        target_lang: str,
    ) -> List[str]:
        headers = {
            "Accept": "/",
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        data = {
            "pipelineTasks": [
                {
                    "taskType": "translation",
                    "config": {
                        "language": {
                            "sourceLanguage": source_lang,
                            "targetLanguage": target_lang,
                        },
                        "serviceId": "bhashini/ai4b/bhili-nmt",
                    },
                }
            ],
            "inputData": {
                "input": [{"source": text} for text in texts],
            },
        }

        response = await client.post(self.base_url, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()

        pipeline_response = result.get("pipelineResponse", [])
        if pipeline_response and "output" in pipeline_response[0]:
            return [item.get("target", "") for item in pipeline_response[0]["output"]]

        raise ValueError("Unexpected response format from translation API")
    
    async def translate_texts(
        self, 
        texts: List[str], 
        source_lang: str, 
        target_lang: str
    ) -> List[str]:
        """Translate a list of texts using the Bhashini API."""
        if not self.api_key:
            raise ValueError("Translation API key not configured")
        if not texts:
            return []

        async with httpx.AsyncClient(timeout=30.0) as client:
            return await self._translate_texts_with_client(
                client=client,
                texts=texts,
                source_lang=source_lang,
                target_lang=target_lang,
            )
    
    async def translate_text(
        self, 
        text: str, 
        source_lang: str, 
        target_lang: str
    ) -> str:
        """Translate a single text using the Bhashini API."""
        if source_lang == target_lang:
            return text
        cleaned = (text or "").strip()
        if not cleaned:
            return text

        # Chunking helps NMT quality; keep chunks small (1–2 sentences) and translate concurrently.
        should_chunk = len(cleaned) >= self.chunking_min_chars
        max_sentences = max(1, self.chunk_max_sentences)
        chunks = self._chunk_text_for_translation(cleaned, max_sentences=max_sentences) if should_chunk else [cleaned]
        chunks = [c for c in chunks if c.strip()]

        if len(chunks) <= 1:
            results = await self.translate_texts([cleaned], source_lang, target_lang)
            return results[0] if results else text

        semaphore = asyncio.Semaphore(max(1, self.max_parallel_requests))

        async def _translate_one(chunk: str) -> str:
            async with semaphore:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    out = await self._translate_texts_with_client(
                        client=client,
                        texts=[chunk],
                        source_lang=source_lang,
                        target_lang=target_lang,
                    )
                    return out[0] if out else ""

        translated_chunks = await asyncio.gather(*[_translate_one(c) for c in chunks])
        # Keep a simple join; paragraphs are already split before chunking.
        return " ".join([t.strip() for t in translated_chunks if t and t.strip()]).strip() or text


translation_service = BhashiniTranslationService()
