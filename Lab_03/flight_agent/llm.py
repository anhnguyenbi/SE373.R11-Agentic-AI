"""Chọn backend model. Mặc định 'mock' (offline, tái lập được).

    --llm mock                         MockFlightLLM (không cần khoá API)
    --llm openai:gpt-4o-mini           cần OPENAI_API_KEY   (pip install langchain-openai)
    --llm anthropic:claude-haiku-4-5   cần ANTHROPIC_API_KEY (pip install langchain-anthropic)
    --llm google_genai:gemini-2.5-flash cần GOOGLE_API_KEY  (pip install langchain-google-genai)

Có thể đặt SE373_MODEL trong .env thay cho --llm (giống slide demo 01).
"""

from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

from .mock_llm import MockFlightLLM


def make_model(spec: str | None = None, *, noise: float = 0.0, seed: int = 0) -> BaseChatModel:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    spec = spec or os.getenv("SE373_MODEL") or "mock"
    if spec == "mock":
        return MockFlightLLM(noise=noise, seed=seed)
    from langchain.chat_models import init_chat_model

    return init_chat_model(spec, temperature=0)
