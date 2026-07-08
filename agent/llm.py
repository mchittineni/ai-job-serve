"""Gemini client wrapper: structured (JSON-schema) generation for the pipeline.

Both the evaluator and the tailor go through generate_structured(), which asks
Gemini for JSON matching a Pydantic model and returns the validated instance.
Transient API errors (429/5xx) are retried with backoff — without this, a blip
would demote a strong job to the keyword heuristic and it would be skipped
forever (the dedupe DB never revisits a job).
"""

import asyncio
import random
import re

from google import genai
from google.genai import errors, types

from .config import Config

RETRYABLE_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 65.0  # free-tier rate windows are per-minute; never sleep longer than one window


class LLMError(Exception):
    """Single error type callers handle (API failure, blocked/unparseable response)."""


class LLMQuotaError(LLMError):
    """Gemini quota is gone for this run: 429s persisted through honored retry
    delays, or the model has no quota at all on this key's plan (429 with
    "limit: 0" — e.g. gemini-2.5-pro on the free tier). Callers must DEFER the
    work, not fall back to the heuristic: a heuristic evaluation gets recorded
    in the dedupe DB and the job is never looked at again."""


def _server_retry_delay(message: str) -> float | None:
    """Google 429 messages include 'Please retry in 47.4s' — honor it over blind backoff."""
    m = re.search(r"retry in ([0-9.]+)\s*s", message, re.IGNORECASE)
    return min(float(m.group(1)) + 1.0, MAX_RETRY_DELAY) if m else None


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = genai.Client(api_key=cfg.gemini_api_key) if cfg.gemini_api_key else None
        self.quota_exhausted = False

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def generate_structured(self, *, system: str, user: str, schema, temperature: float | None = None):
        """Generate a response validated against `schema` (a Pydantic model class)."""
        if self.client is None:
            raise LLMError("GEMINI_API_KEY not configured")
        if self.quota_exhausted:
            raise LLMQuotaError("Gemini quota already exhausted earlier in this run")

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
                    model=self.cfg.gemini_model,
                    contents=user,
                    config=config,
                )
                break
            except errors.APIError as e:
                last_error = e
                message = str(getattr(e, "message", None) or e)
                if e.code == 429 and "limit: 0" in message:
                    # this plan has NO quota for the model, ever — retrying is pointless
                    self.quota_exhausted = True
                    raise LLMQuotaError(
                        f"model {self.cfg.gemini_model} has zero quota on this API key's plan "
                        "(the free tier excludes gemini-2.5-pro) — set GEMINI_MODEL=gemini-2.5-flash "
                        "or enable billing"
                    ) from e
                if e.code in RETRYABLE_CODES and attempt < MAX_ATTEMPTS:
                    delay = _server_retry_delay(message) or 2**attempt + random.uniform(0, 1)
                    print(f"[llm] {e.code} from Gemini, retry {attempt}/{MAX_ATTEMPTS - 1} in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    continue
                if e.code == 429:
                    self.quota_exhausted = True
                    raise LLMQuotaError(
                        f"Gemini 429 persisted after {MAX_ATTEMPTS} attempts: {message[:300]}"
                    ) from e
                raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
        else:  # pragma: no cover - loop always breaks or raises
            raise LLMError(f"Gemini API error: {last_error}")

        usage = getattr(response, "usage_metadata", None)
        if usage:
            print(
                f"[llm] {self.cfg.gemini_model}: "
                f"in={getattr(usage, 'prompt_token_count', '?')} "
                f"out={getattr(usage, 'candidates_token_count', '?')} tokens"
            )

        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            return parsed
        try:
            return schema.model_validate_json(response.text)
        except Exception as e:  # blocked response (text=None) or malformed JSON
            raise LLMError(f"could not parse structured response: {e}") from e
