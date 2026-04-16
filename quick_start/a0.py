"""Langfuse OpenAI integration with Qwen via DashScope.

Usage:
    conda run -n performance --no-capture-output python quick_start/a0.py
"""

from dotenv import load_dotenv
import os

from langfuse import get_client
from langfuse.openai import OpenAI

load_dotenv()
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "sk-xxx")
QWEN_BASE_URL_CN = os.getenv(
    "QWEN_BASE_URL_CN", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
QWEN_MODEL_NAME = "qwen3-max-2026-01-23"

client = OpenAI(
    api_key=DASHSCOPE_API_KEY,
    base_url=QWEN_BASE_URL_CN,
)

completion = client.chat.completions.create(
    name="test-chat",
    model=QWEN_MODEL_NAME,
    messages=[
        {
            "role": "system",
            "content": "You are a very accurate calculator. You output only the result of the calculation.",
        },
        {"role": "user", "content": "1 + 11 = "},
    ],
    metadata={"someMetadataKey": "someValue"},
)

print(completion.choices[0].message.content)
get_client().flush()
