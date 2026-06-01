import json
import random
import time

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
    "空一行后为正文，正文纯文本不使用 markdown。"
)


def generate_article(api_key: str, max_retries: int = 3) -> tuple[str, str]:
    """调用 DeepSeek 生成文章，返回 (标题, 正文)。

    失败时自动重试，最多重试 max_retries 次。
    """
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
        "temperature": 0.8,
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
            return title, body

        except requests.HTTPError as e:
            last_error = e
            status = e.response.status_code if e.response is not None else "?"
            print(f"[尝试 {attempt}/{max_retries}] HTTP {status}：{e}")
        except (requests.RequestException, KeyError, json.JSONDecodeError) as e:
            last_error = e
            print(f"[尝试 {attempt}/{max_retries}] 请求异常：{e}")

        if attempt < max_retries:
            sleep_sec = 2 ** attempt
            time.sleep(sleep_sec)

    raise RuntimeError(f"生成失败，已重试{max_retries}次，最后错误：{last_error}")


def _parse_content(raw: str) -> tuple[str, str]:
    """从 API 返回文本中提取标题和正文。"""
    lines = raw.strip().split("\n")

    title = ""
    body_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            body_start = i + 1
            break

    # 跳过标题后的空行
    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1

    body = "\n".join(lines[body_start:]).strip()

    if not title:
        # 整段文本没有 # 标题时，取第一行作为标题
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()

    return title, body
