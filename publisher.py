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
    """关闭页面上的广告弹窗、AI 助手抽屉等遮挡元素"""
    # 1. 尝试点击常见的关闭按钮
    close_selectors = [
        '.byte-modal-close',
        '[class*="close"]',
        'svg[class*="close"]',
        '.modal-close',
        '[aria-label="关闭"]',
        '[aria-label="Close"]',
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

    # 2. 关闭 AI 助手抽屉（byte-drawer）
    drawer_close_selectors = [
        '.ai-assistant-drawer .byte-drawer-close',
        '.byte-drawer-wrapper .byte-drawer-close',
        '.ai-assistant-drawer [class*="close"]',
        '.byte-drawer-mask',            # 点击遮罩层通常可关闭抽屉
    ]
    for sel in drawer_close_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click()
                print(f"已关闭 AI 抽屉（选择器: {sel}）")
                time.sleep(1)
                break
        except:
            pass

    # 3. 按 ESC 键作为保底关闭
    page.keyboard.press("Escape")
    time.sleep(0.5)

    # 4. 向下滚动一点，避开顶部 banner 遮挡
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


def _set_cover(page: Page) -> None:
    """
    强制点击封面区域，并处理弹出的上传/选择界面。
    使用坐标点击和多种选择器兜底。
    """
    # 等待页面稳定
    time.sleep(2)
    page.wait_for_load_state("networkidle")
    time.sleep(1)

    # 再次确保没有弹窗遮挡
    _close_popups(page)

    cover_file = "cover.jpg"
    if not os.path.isfile(cover_file):
        print(f"未找到封面文件 {cover_file}")
        return

    # 先尝试直接通过 JS 找隐藏的 file input 并上传（最高优先级）
    if _upload_via_hidden_input(page, cover_file):
        return

    # 策略1：寻找任何与封面相关的可点击元素，包括纯文本
    click_targets = [
        'text=添加封面',
        'text=上传封面',
        'text=编辑封面',
        'text=封面',
        '[class*="cover"]:has-text("添加")',
        '[class*="cover"]:has-text("上传")',
        '.article-cover',
        '.cover-wrapper',
        '[class*="cover-upload"]',
        '[class*="coverImage"]',
        '[class*="cover"] img',  # 可能已有封面图，点击替换
    ]

    for target in click_targets:
        try:
            el = page.locator(target).first
            if el.is_visible(timeout=2000):
                el.scroll_into_view_if_needed()
                time.sleep(0.3)
                el.click()
                print(f"点击了封面元素: {target}")
                time.sleep(2)
                # 点击后可能出现上传面板，尝试找文件输入
                if _try_upload_after_click(page, cover_file):
                    return
        except:
            continue

    # 策略2：用坐标点击正文编辑器的上方区域（封面通常在那里）
    try:
        editor = page.locator('[contenteditable="true"]').first
        box = editor.bounding_box()
        if box:
            # 点击正文编辑器上方约100像素处
            x = box['x'] + box['width'] / 2
            y = box['y'] - 60
            page.mouse.click(x, y)
            print(f"坐标点击封面区域 ({x},{y})")
            time.sleep(2)
            if _try_upload_after_click(page, cover_file):
                return
    except:
        pass

    # 策略3：再次尝试默认封面按钮
    try:
        page.locator('button:has-text("默认封面")').first.click(timeout=2000)
        print("点击了默认封面按钮")
        time.sleep(2)
        return
    except:
        pass

    # 全部失败，保存截图用于调试
    screenshot_path = f"cover_fail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"未能设置封面，调试截图已保存: {screenshot_path}")


def _upload_via_hidden_input(page: Page, cover_file: str) -> bool:
    """
    通过 JS 直接查找页面中隐藏的 type=file 输入框并上传。
    头条的上传控件通常是隐藏的 input[type=file]，由点击触发。
    """
    # 用 JS 查找所有隐藏的 file input
    result = page.evaluate("""
        () => {
            const inputs = document.querySelectorAll('input[type="file"]');
            const infos = [];
            for (const input of inputs) {
                const rect = input.getBoundingClientRect();
                const style = window.getComputedStyle(input);
                infos.push({
                    id: input.id,
                    name: input.name,
                    class: input.className,
                    accept: input.accept,
                    visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden',
                    rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height },
                    parentTag: input.parentElement ? input.parentElement.tagName : null,
                    parentClass: input.parentElement ? input.parentElement.className : null,
                });
            }
            return JSON.stringify(infos);
        }
    """)
    print(f"页面中的 file input: {result}")

    # 方案A: 用 JS 直接触发隐藏的 file input（先找 accept 含 image 的）
    upload_success = page.evaluate("""
        (coverFileName) => {
            // 找所有 file input，优先找 accept 含 image 的
            const inputs = document.querySelectorAll('input[type="file"]');
            let target = null;

            // 优先找 accept 含 image 的
            for (const input of inputs) {
                if (input.accept && input.accept.includes('image')) {
                    target = input;
                    break;
                }
            }
            // 其次取第一个
            if (!target && inputs.length > 0) {
                target = inputs[0];
            }

            if (target) {
                // 移除 data 传输限制，让 Playwright 接管
                return 'FOUND:' + (target.id || 'no-id');
            }
            return 'NOT_FOUND';
        }
    """, os.path.basename(cover_file))

    if upload_success.startswith('FOUND:'):
        # 用 Playwright 的 set_input_files 设置文件（即使隐藏也能工作）
        try:
            # 尝试找 accept 含 image 的
            for sel in ['input[type="file"][accept*="image"]', 'input[type="file"]']:
                file_input = page.locator(sel).first
                if file_input.count() > 0:
                    # 直接用 Playwright 设置，不需要 visible
                    file_input.set_input_files(cover_file)
                    print(f"已通过隐藏 input 上传封面图: {cover_file}")
                    time.sleep(3)

                    # 上传后可能需要点击"确定"或"完成"
                    for confirm in ['button:has-text("确定")', 'button:has-text("完成")', 'span:has-text("确定")']:
                        try:
                            btn = page.locator(confirm).first
                            if btn.is_visible(timeout=2000):
                                btn.click()
                                print(f"点击了封面确认按钮: {confirm}")
                                time.sleep(1)
                                break
                        except:
                            pass
                    return True
        except Exception as e:
            print(f"隐藏 input 上传失败: {e}")

    return False


def _try_upload_after_click(page: Page, cover_file: str) -> bool:
    """在点击封面区域后，尝试寻找文件上传控件并上传"""
    time.sleep(1)

    # 先试试直接用 JS 找隐藏 file input 上传
    if _upload_via_hidden_input(page, cover_file):
        return True

    # 用 Playwright 的 locator（不检查可见性）
    file_input_selectors = [
        'input[type="file"][accept*="image"]',
        'input[type="file"]',
    ]
    for sel in file_input_selectors:
        try:
            file_input = page.locator(sel).first
            if file_input.count() > 0:
                # set_input_files 不需要元素可见
                file_input.set_input_files(cover_file)
                print(f"已上传封面图: {cover_file}")
                time.sleep(3)
                # 上传后可能需要点击"确定"或"完成"
                for confirm in ['button:has-text("确定")', 'button:has-text("完成")']:
                    try:
                        btn = page.locator(confirm).first
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            print(f"点击了封面确认按钮: {confirm}")
                            time.sleep(1)
                            break
                    except:
                        pass
                return True
        except:
            continue

    print("点击封面区域后未找到文件上传控件")
    return False


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

            # 处理"我知道了"等弹窗（使用 JS 强制点击，绕过遮挡和超时）
            time.sleep(2)
            js_click_button_texts = [
                "我知道了",
                "确定",
                "发布",
                "暂不",
                "跳过",
                "关闭",
                "保存",
            ]
            for text in js_click_button_texts:
                try:
                    page.evaluate(f"""
                        () => {{
                            const buttons = [...document.querySelectorAll('button, span, div[role="button"]')];
                            const target = buttons.find(el => el.innerText.includes('{text}'));
                            if (target) {{
                                target.click();
                                return true;
                            }}
                            return false;
                        }}
                    """)
                    time.sleep(0.5)
                except:
                    pass

            # 最后再按 ESC 清除残留
            page.keyboard.press("Escape")
            time.sleep(0.5)

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

            # 3.5 设置封面
            _set_cover(page)

            # 4. 点击发布
            _click_publish(page)

            # 5. 等待成功并截图
            _wait_for_success(page)

        finally:
            context.close()
            browser.close()
