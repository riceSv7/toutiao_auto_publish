import json
import random
import time
import re

import requests

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

SYSTEM_PROMPT = (
    "我是一个今日头条情感/职场类创作者，"
    "文章风格接地气，有金句，300-500字。"
)

TOPICS = ["职场心得", "中年感悟", "情感故事", "生活智慧"]

USER_MESSAGE_TEMPLATE = (
    "请生成一篇今日可发布的短文，主题从{}里随机选一个。"
    "输出格式要求：第一行为文章标题（以「# 」开头），"
    "空一行后为正文。正文必须是纯文本段落，不得出现任何 Markdown 标记"
    "（包括但不限于 **、*、#、>、-、1. 等）。"
    "正文内部不得再出现以 # 开头的行。"
)


def generate_article(api_key: str, max_retries: int = 3) -> tuple[str, str]:
    """调用 DeepSeek 生成文章，返回 (标题, 正文)"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    topic = random.choice(TOPICS)
    user_message = USER_MESSAGE_TEMPLATE.format("、".join(TOPICS))
    user_message += f"（本次随机选中的主题：{topic}）"

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.75,
        "max_tokens": 800,
    }

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                DEEPSEEK_URL,
                headers=headers,
                json=payload,
                timeout=60,
            )
            resp.raise_for_status()

            data = resp.json()
            content = data["choices"][0]["message"]["content"]

            title, body = _parse_content(content)
            if not title or not body:
                raise ValueError("标题或正文为空")
            return title, body

        except (requests.HTTPError, requests.RequestException,
                KeyError, json.JSONDecodeError, ValueError) as e:
            last_error = e
            status = (
                e.response.status_code
                if isinstance(e, requests.HTTPError)
                else "N/A"
            )
            print(f"[尝试 {attempt}/{max_retries}] 错误 ({status}): {e}")

        if attempt < max_retries:
            time.sleep(2 ** attempt)

    raise RuntimeError(
        f"生成失败，已重试{max_retries}次，最后错误：{last_error}"
    )


def _parse_content(raw: str) -> tuple[str, str]:
    """从 API 返回文本中提取标题和正文"""
    if not raw or not raw.strip():
        raise ValueError("API 返回内容为空")

    lines = raw.strip().split("\n")

    title = ""
    body_start = 0

    # 查找以 # 开头的标题行
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            body_start = i + 1
            break

    # 跳过标题行之后的空行
    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1

    body = "\n".join(lines[body_start:]).strip()

    # 如果没有找到标题，尝试用第一个长度>=5的非空行做标题
    if not title:
        for i, line in enumerate(lines):
            if line.strip() and len(line.strip()) >= 5:
                title = line.strip()
                body_start = i + 1
                body = "\n".join(lines[body_start:]).strip()
                break

    # 最后的 fallback：直接取第一行当标题
    if not title:
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()

    # 简单清理正文中残留的 Markdown 符号
    body = re.sub(r"[*#>`\-]", "", body)
    body = "\n".join(line.strip() for line in body.split("\n") if line.strip())

    return title, body