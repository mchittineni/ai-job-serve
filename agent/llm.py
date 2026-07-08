"""Gemini client wrapper: structured (JSON-schema) generation for the pipeline.

Both the evaluator and the tailor go through generate_structured(), which asks
Gemini for JSON matching a Pydantic model and returns the validated instance.
Transient API errors (429/5xx) are retried with backoff — without this, a blip
would demote a strong job to the keyword heuristic and it would be skipped
forever (the dedupe DB never revisits a job).
"""
import asyncio
import random

from google import genai
from google.genai import errors, types

from .config import Config

RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class LLMError(Exception):
    """Single error type callers handle (API failure, blocked/unparseable response)."""


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = genai.Client(api_key=cfg.gemini_api_key) if cfg.gemini_api_key else None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def generate_structured(self, *, system: str, user: str, schema,
                                  temperature: float | None = None):
        """Generate a response validated against `schema` (a Pydantic model class)."""
        if self.client is None:
            raise LLMError("GEMINI_API_KEY not configured")

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
        )

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self.client.aio.models.generate_content(
                    model=self.cfg.gemini_model, contents=user, config=config,
                )
                break
            except errors.APIError as e:
                last_error = e
                if e.code in RETRYABLE_CODES and attempt < MAX_ATTEMPTS:
                    delay = 2 ** attempt + random.uniform(0, 1)
                    print(f"[llm] {e.code} from Gemini, retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    continue
                raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
        else:  # pragma: no cover - loop always breaks or raises
            raise LLMError(f"Gemini API error: {last_error}")

        usage = getattr(response, "usage_metadata", None)
        if usage:
            print(f"[llm] {self.cfg.gemini_model}: "
                  f"in={getattr(usage, 'prompt_token_count', '?')} "
                  f"out={getattr(usage, 'candidates_token_count', '?')} tokens")

        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            return parsed
        try:
            return schema.model_validate_json(response.text)
        except Exception as e:  # blocked response (text=None) or malformed JSON
            raise LLMError(f"could not parse structured response: {e}") from e
