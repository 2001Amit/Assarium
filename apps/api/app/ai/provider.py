from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger("assarium.ai")

_client: Any = None


class LLMUnavailableError(Exception):
    """The language model is not configured, or refused the request."""


def _get_client() -> Any:
    """
    Build the Azure OpenAI client against the v1 GA surface.

    The v1 API is reached by pointing the standard OpenAI client at
    `{endpoint}/openai/v1`; `api-version` is not a parameter there. That keeps the
    client on one code path whether it talks to Azure or to OpenAI directly, and means
    new features arrive without a dated version string to chase.
    """
    global _client  # noqa: PLW0603
    if _client is not None:
        return _client

    settings = get_settings()
    if not settings.azure_openai_endpoint:
        raise LLMUnavailableError(
            "Azure OpenAI is not configured. Set ASSARIUM_AZURE_OPENAI_ENDPOINT, plus "
            "ASSARIUM_AZURE_OPENAI_API_KEY or ASSARIUM_AZURE_OPENAI_USE_ENTRA_ID=true."
        )

    from openai import OpenAI

    base_url = settings.azure_openai_endpoint.rstrip("/") + "/openai/v1"

    if settings.azure_openai_use_entra_id:
        # Managed identity or the ambient Azure credential: nothing is stored, and the
        # token provider refreshes on expiry without a separate Azure client.
        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailableError(
                "Entra ID authentication needs the 'azure-identity' package."
            ) from exc

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        _client = OpenAI(base_url=base_url, api_key=token_provider)
    else:
        if not settings.azure_openai_api_key:
            raise LLMUnavailableError(
                "Azure OpenAI is not configured. Set ASSARIUM_AZURE_OPENAI_API_KEY, or "
                "ASSARIUM_AZURE_OPENAI_USE_ENTRA_ID=true to use a managed identity."
            )
        _client = OpenAI(base_url=base_url, api_key=settings.azure_openai_api_key)

    return _client


def is_available() -> bool:
    """Whether the provider is configured, without raising or building a client."""
    settings = get_settings()
    if not settings.azure_openai_endpoint:
        return False
    return bool(settings.azure_openai_api_key or settings.azure_openai_use_entra_id)


def complete(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    response_format: dict[str, str] | None = None,
) -> str:
    """Run a chat completion and return the assistant's text."""
    client = _get_client()
    settings = get_settings()

    kwargs: dict[str, Any] = {
        "model": settings.azure_openai_deployment,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    if response_format:
        kwargs["response_format"] = response_format

    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as unavailability
        logger.warning("Azure OpenAI request failed: %s", exc)
        raise LLMUnavailableError(
            f"The language model could not be reached: {str(exc).splitlines()[0][:200]}"
        ) from exc

    return response.choices[0].message.content or ""


def complete_json(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """Run a completion that must return JSON, and parse it."""
    raw = complete(
        messages,
        temperature=temperature,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("The model returned non-JSON: %s", raw[:200])
        return {"error": "The model did not return valid JSON.", "raw": raw[:500]}
