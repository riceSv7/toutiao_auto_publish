import os
from dotenv import load_dotenv

from content_generator import generate_article
from publisher import publish

load_dotenv()


def main():
    api_key = os.environ["DEEPSEEK_API_KEY"]

    print("正在生成文章...")
    title, body = generate_article(api_key)

    print(f"标题：{title}")
    print(f"正文（{len(body)}字）：{body[:80]}...")

    print("正在打开浏览器发布到头条...")
    publish(title, body)


if __name__ == "__main__":
    main()
