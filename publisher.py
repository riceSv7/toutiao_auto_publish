import json
import os
import time
from datetime import datetime

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"


def _load_cookies() -> list[dict]:
    """读取 TOUTIAO_COOKIE（{"name":"value",...} 格式），转为 Playwright cookie 列表。"""
    raw = os.environ.get("TOUTIAO_COOKIE", "")
    if not raw:
        raise RuntimeError("未配置 TOUTIAO_COOKIE 环境变量")
    flat = json.loads(raw)
    return [
        {"name": k, "value": v, "domain": ".toutiao.com", "path": "/"}
        for k, v in flat.items()
    ]


def _close_popups(page: Page) -> None:
    """关闭页面上的广告弹窗、活动对话框等遮挡元素"""
    # 1. 尝试点击常见的关闭按钮
    close_selectors = [
        '.byte-modal-close',           # 弹窗关闭按钮
        '[class*="close"]',            # 包含 close 的元素
        'svg[class*="close"]',         # SVG 关闭图标
        '.modal-close',                # 通用模态框关闭
        '[aria-label="关闭"]',         # 无障碍标签
        '[aria-label="Close"]',        # 英文无障碍标签
    ]
    for sel in close_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click()
                print(f"已关闭弹窗（选择器: {sel}）")
                time.sleep(1)
        except:
            pass

    # 2. 按 ESC 键尝试关闭弹窗
    page.keyboard.press("Escape")
    time.sleep(0.5)

    # 3. 如果页面顶部有大 banner 图遮挡，尝试滚动一点距离
    page.evaluate("window.scrollBy(0, 100)")
    time.sleep(0.5)


def _fill_title(page: Page, title: str) -> None:
    # 头条编辑器标题栏 —— 多种可能选择器
    selectors = [
        'input[placeholder*="标题"]',
        '[data-placeholder*="标题"]',
        'textarea[placeholder*="标题"]',
        ".title-input input",
    ]
    for sel in selectors:
        el = page.locator(sel).first
        if el.is_visible(timeout=1000):
            el.click()
            el.fill("")
            el.fill(title)
            print(f"标题已填入（选择器: {sel}）")
            return
    raise RuntimeError("未找到标题输入框")


def _fill_body(page: Page, body: str) -> None:
    editor = page.locator('[contenteditable="true"]').first
    editor.wait_for(state="visible", timeout=10000)
    editor.click()
    time.sleep(0.5)

    # 清空可能已有的占位内容
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")
    time.sleep(0.2)

    paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
    for i, para in enumerate(paragraphs):
        page.keyboard.type(para)
        if i < len(paragraphs) - 1:
            page.keyboard.press("Enter")
        time.sleep(0.2)

    print(f"正文已填入（{len(paragraphs)} 段）")


def _click_publish(page: Page) -> None:
    publish_selectors = [
        'button:has-text("发布")',
        'button:has-text("提交")',
        '[class*="publish"]:has-text("发布")',
        'button:has-text("确认发布")',
        'button:has-text("发表")',
        '.publish-btn',
    ]

    for sel in publish_selectors:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=3000)
            btn.click()
            print(f"点击了按钮（选择器: {sel}）")
            time.sleep(2)

            # 处理可能的二次确认弹窗
            confirm = page.locator('button:has-text("确认发布")').first
            if confirm.is_visible(timeout=2000):
                confirm.click()
                print("已点击确认发布")
                time.sleep(2)

            # 处理"确定"按钮
            confirm2 = page.locator('button:has-text("确定")').first
            if confirm2.is_visible(timeout=2000):
                confirm2.click()
                print("已点击确定")
                time.sleep(2)

            return
        except PlaywrightTimeout:
            continue

    # 所有选择器都未命中，打印 HTML 调试
    print("ERROR: 未找到发布按钮，打印页面 HTML（前 3000 字符）...")
    html = page.content()
    print(html[:3000])

    screenshot_path = f"debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"调试截图已保存: {screenshot_path}")

    raise RuntimeError("未找到发布按钮，已输出页面 HTML 和截图")


def _wait_for_success(page: Page, timeout: int = 30) -> str:
    success_hints = [
        "发布成功",
        "已发布",
        "审核",
        "提交成功",
        "操作成功",
    ]
    deadline = time.time() + timeout
    while time.time() < deadline:
        content = page.content()
        for hint in success_hints:
            if hint in content:
                path = f"success_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                page.screenshot(path=path, full_page=True)
                print(f"发布成功截图: {path}")
                return path
        time.sleep(1)

    # 超时也保存一张截图
    path = f"result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    page.screenshot(path=path, full_page=True)
    print(f"未检测到明确成功提示，已截图: {path}")
    return path


def publish(title: str, body: str) -> None:
    cookies = _load_cookies()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="zh-CN")
        page = context.new_page()

        try:
            # 1. 先访问域名以设置 cookie 作用域
            page.goto("https://mp.toutiao.com", wait_until="domcontentloaded")
            context.add_cookies(cookies)
            print(f"已注入 {len(cookies)} 个 Cookie")

            # 2. 跳转到发布页面
            page.goto(PUBLISH_URL, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle")
            time.sleep(3)
            print(f"已打开发布页面: {PUBLISH_URL}")

            # 2.5 清理可能出现的弹窗和遮挡
            _close_popups(page)

            # 3. 填写标题和正文
            _fill_title(page, title)
            time.sleep(0.5)
            _fill_body(page, body)
            time.sleep(1)

            # 4. 点击发布
            _click_publish(page)

            # 5. 等待成功并截图
            _wait_for_success(page)

        finally:
            context.close()
            browser.close()
