import datetime
import random
import time
import re

import requests

DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"

SYSTEM_PROMPT = (
    "我是一个今日头条创作者，文章风格接地气，有金句。"
    "金句中可以融入哲理和古诗词，增强文章厚度和传播力。"
)

# 常规主题（轮换）
ROTATION_TOPICS = [
    "职场心得", "副业选择", "失业过渡", "如何与异性交流",
    "生活智慧", "心灵鸡汤", "躺平主义", "古诗词和古文名句点评",
]

# 前沿知识科普（概率触发，不在轮换中）
SCIENCE_TOPIC = "前沿知识科普"
SCIENCE_PROBABILITY = 0.15


USER_MESSAGE_TEMPLATE = (
    "请生成一篇今日可发布的短文，主题：{topic}。"
    "字数要求：600字左右。"
    "输出格式要求：第一行为文章标题（以「# 」开头），"
    "空一行后为正文。正文必须是纯文本段落，不得出现任何 Markdown 标记"
    "（包括但不限于 **、*、#、>、-、1. 等）。"
    "正文内部不得再出现以 # 开头的行。"
)

SCIENCE_USER_MESSAGE = (
    "请生成一篇前沿知识科普短文，主题：{topic}。"
    "字数要求：600字左右。"
    "重要：内容务必真实可靠，基于已知的科学事实或公认的研究成果。"
    "不得编造数据、研究结论或人物言论。如涉及具体数字或研究，需是可查证的。"
    "风格要通俗易懂，让普通读者也能理解和受益。"
    "输出格式要求：第一行为文章标题（以「# 」开头），"
    "空一行后为正文。正文必须是纯文本段落，不得出现任何 Markdown 标记"
    "（包括但不限于 **、*、#、>、-、1. 等）。"
    "正文内部不得再出现以 # 开头的行。"
)


def _pick_topic() -> tuple[str, bool]:
    """
    选择文章主题。返回 (主题, 是否为科普类)。
    常规主题按日期轮换（一年中的第几天 mod 主题数），科普类 15% 概率触发。
    """
    if random.random() < SCIENCE_PROBABILITY:
        return SCIENCE_TOPIC, True

    idx = datetime.date.today().timetuple().tm_yday % len(ROTATION_TOPICS)
    topic = ROTATION_TOPICS[idx]
    return topic, False


def generate_article(api_key: str, max_retries: int = 3) -> tuple[str, str]:
    """调用 DeepSeek 生成文章，返回 (标题, 正文)"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    topic, is_science = _pick_topic()
    if is_science:
        user_message = SCIENCE_USER_MESSAGE.format(topic=topic)
    else:
        user_message = USER_MESSAGE_TEMPLATE.format(topic=topic)

    print(f"选中主题：{topic}{'（科普类，需验证内容真实性）' if is_science else ''}")

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.75,
        "max_tokens": 1200,
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

            # 科普类主题加一道内容验证（多次查询交叉验证）
            if is_science:
                if not _verify_science_content(api_key, title, body):
                    print("[科普验证] 内容存疑，退回常规主题重试...")
                    topic = random.choice(ROTATION_TOPICS)
                    is_science = False
                    user_message = USER_MESSAGE_TEMPLATE.format(topic=topic)
                    payload["messages"][1]["content"] = user_message
                    print(f"回退到常规主题：{topic}")
                    continue

            return title, body

        except (requests.HTTPError, requests.RequestException,
                KeyError, ValueError) as e:
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


def _verify_science_content(api_key: str, title: str, body: str) -> bool:
    """
    通过多次查询交叉验证科普类内容的可靠性。
    把文章中的关键事实主张提取出来，另开一次 API 调用专门核查。
    返回 True 表示通过验证，False 表示存疑应跳过。
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    full_text = f"标题：{title}\n正文：{body}"

    # 第一次核查：让模型审查内容中的事实性主张
    verify_prompt = (
        "你是一个严谨的事实核查员。请审查以下文章中的关键事实主张和数据。\n\n"
        f"{full_text}\n\n"
        "请逐一列出文章中提到的事实主张（包括数据、研究结论、历史事件、科学原理等），"
        "对每一项给出判断：\n"
        "- 「可靠」：广泛公认的事实，或你可以确认其准确性\n"
        "- 「存疑」：你无法确认，或没有足够信息判断其真伪\n"
        "- 「错误」：你明确知道与事实不符\n\n"
        "最后给出总体结论，格式为：「综合判断：通过」或「综合判断：不通过」。\n"
        "如果「存疑」或「错误」项超过 1 个，则综合判断应为不通过。"
    )

    try:
        resp = requests.post(
            DEEPSEEK_URL,
            headers=headers,
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": verify_prompt}],
                "temperature": 0.1,
                "max_tokens": 800,
            },
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()["choices"][0]["message"]["content"]
        print(f"[科普验证] 第一次核查结果:\n{result[:300]}...")

        if "不通过" in result:
            print("[科普验证] 第一次核查不通过")
            return False
        if "通过" not in result:
            print("[科普验证] 第一次核查结论不明确，视为存疑")
            return False

    except Exception as e:
        print(f"[科普验证] 第一次核查请求失败: {e}")
        return False

    # 第二次核查：换一个角度再问一次（交叉验证）
    verify_prompt_2 = (
        "请以搜索引擎般的精确度，快速判断以下文章内容是否可靠。\n\n"
        f"{full_text}\n\n"
        "重点关注：\n"
        "1. 文中提到的数据是否有夸大或编造的痕迹？\n"
        "2. 科学原理解释是否正确？\n"
        "3. 有没有与常识相悖的说法？\n\n"
        "只需回答「可靠」或「不可靠」，并简要说明理由（不超过 100 字）。"
    )

    try:
        resp2 = requests.post(
            DEEPSEEK_URL,
            headers=headers,
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": verify_prompt_2}],
                "temperature": 0.1,
                "max_tokens": 300,
            },
            timeout=60,
        )
        resp2.raise_for_status()
        result2 = resp2.json()["choices"][0]["message"]["content"]
        print(f"[科普验证] 第二次核查结果: {result2[:200]}")

        if "不可靠" in result2:
            print("[科普验证] 第二次核查不通过")
            return False

    except Exception as e:
        print(f"[科普验证] 第二次核查请求失败: {e}")
        # 第二次失败不算致命，第一次通过了就算过
        return True

    print("[科普验证] 两次交叉验证通过")
    return True


def _parse_content(raw: str) -> tuple[str, str]:
    """从 API 返回文本中提取标题和正文"""
    if not raw or not raw.strip():
        raise ValueError("API 返回内容为空")

    lines = raw.strip().split("\n")

    title = ""
    body_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            body_start = i + 1
            break

    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1

    body = "\n".join(lines[body_start:]).strip()

    if not title:
        for i, line in enumerate(lines):
            if line.strip() and len(line.strip()) >= 5:
                title = line.strip()
                body_start = i + 1
                body = "\n".join(lines[body_start:]).strip()
                break

    if not title:
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()

    body = re.sub(r"[*#>`\-]", "", body)
    body = "\n".join(line.strip() for line in body.split("\n") if line.strip())

    return title, body
