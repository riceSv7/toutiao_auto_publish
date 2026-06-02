import json
import os
import time
from datetime import datetime

try:
    import pyperclip
    _HAS_PYPERCLIP = True
except ImportError:
    pyperclip = None  # type: ignore
    _HAS_PYPERCLIP = False

from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout

PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"

HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"


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
    # 头条编辑器标题栏 —— 多种可能选择器（textarea 优先，实际 DOM 为 textarea）
    selectors = [
        'textarea[placeholder*="标题"]',
        'input[placeholder*="标题"]',
        '[data-placeholder*="标题"]',
        ".title-input input",
        ".title-input textarea",
        "textarea",  # 最后兜底：找任意 textarea
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
    """使用 ProseMirror 编辑器 API 填入正文，兼容头条的富文本编辑器"""
    editor = page.locator('.ProseMirror[contenteditable="true"]').first
    # 兜底：如果 ProseMirror 选择器没找到，回退到通用 contenteditable
    if editor.count() == 0:
        editor = page.locator('[contenteditable="true"]').first
    editor.wait_for(state="visible", timeout=10000)
    editor.click()
    time.sleep(0.5)

    paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
    if not paragraphs:
        print("警告：正文为空，跳过填入")
        return

    # 先用 JS 通过 ProseMirror 内部 API 直接设置内容（更可靠）
    try:
        escaped_paragraphs = [p.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") for p in paragraphs]
        js_paragraphs = "', '".join(escaped_paragraphs)
        result = page.evaluate(f"""
            () => {{
                const editor = document.querySelector('.ProseMirror');
                if (!editor || !editor.__vue__) {{
                    // 尝试通过 DOM 事件触发
                    return 'NO_VUE';
                }}
                try {{
                    const view = editor.__vue__.$el ? editor.__vue__ : null;
                    return 'HAS_VUE_BUT_NO_DIRECT_API';
                }} catch(e) {{
                    return 'ERROR:' + e.message;
                }}
            }}
        """)
        print(f"ProseMirror 检测结果: {result}")
    except Exception as e:
        print(f"ProseMirror JS 检测异常: {e}")

    # 方案A：清空 + 逐段用 keyboard.type 输入
    page.keyboard.press("Control+a")
    page.keyboard.press("Backspace")
    time.sleep(0.3)

    for i, para in enumerate(paragraphs):
        page.keyboard.type(para, delay=10)  # 加点延迟避免输入太快丢失
        if i < len(paragraphs) - 1:
            page.keyboard.press("Enter")
            time.sleep(0.3)
        time.sleep(0.3)

    print(f"正文已填入（{len(paragraphs)} 段）")

    # 方案B（兜底）：如果内容未成功填入，用 clipboard + paste
    time.sleep(1)
    actual_text = page.evaluate("""
        () => {
            const editor = document.querySelector('.ProseMirror');
            return editor ? editor.textContent.trim() : '';
        }
    """)
    if len(actual_text) < 10:
        print(f"正文填入可能失败（当前长度={len(actual_text)}），尝试剪贴板方案...")
        try:
            pyperclip.copy(body)
            page.keyboard.press("Control+v")
            time.sleep(1)
            actual_text2 = page.evaluate("document.querySelector('.ProseMirror')?.textContent?.trim() || ''")
            print(f"剪贴板方案后正文长度: {len(actual_text2)}")
        except Exception as e:
            print(f"剪贴板方案失败: {e}")
    else:
        print(f"正文验证通过（长度={len(actual_text)}）")


def _is_cover_already_set(page: Page) -> bool:
    """检测封面是否已经设置好了（已有真实大图），如果是则跳过封面上传流程"""
    time.sleep(1)
    # 用 JS 检查封面区域是否有真实尺寸的图片（排除占位符小图标）
    has_real_cover = page.evaluate("""
        () => {
            // 检查封面区域所有 img，找到任何一个 >= 80px 宽的真实图片
            const imgs = document.querySelectorAll('[class*="cover"] img[src]');
            for (const img of imgs) {
                if (!img.offsetParent) continue;
                const src = (img.src || '').toLowerCase();
                // 排除明显是小图标/占位符的
                if (src.includes('data:')) continue;
                if (src.includes('.svg')) continue;
                if (src.includes('icon') || src.includes('avatar') || src.includes('logo')) continue;
                const rect = img.getBoundingClientRect();
                if (rect.width >= 80 && rect.height >= 60) {
                    return true;
                }
            }
            // 检查背景图（排除占位符）
            const coverEls = document.querySelectorAll('[class*="cover"]');
            for (const el of coverEls) {
                const bg = window.getComputedStyle(el).backgroundImage;
                if (bg && bg !== 'none' && bg.includes('url') && !bg.includes('data:') && !bg.includes('.svg')) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width >= 80 && rect.height >= 60) {
                        return true;
                    }
                }
            }
            return false;
        }
    """)
    if has_real_cover:
        print("检测到封面已设置（有真实大图），跳过封面上传步骤")
        return True

    # 额外检查：看页面上有没有"编辑封面"或"更换封面"文字
    try:
        for text in ['编辑封面', '更换封面']:
            btn = page.locator(f'text={text}').first
            if btn.count() > 0 and btn.is_visible(timeout=500):
                print(f"检测到封面已设置（有'{text}'按钮），跳过封面上传步骤")
                return True
    except:
        pass

    return False


def _dismiss_cover_panel(page: Page) -> None:
    """关闭可能打开的封面上传/选择面板 —— 只用安全方式，不销毁 modal DOM"""
    time.sleep(1)
    # 1. 多次按 ESC 关闭（覆盖不同层级的弹窗）
    for i in range(3):
        page.keyboard.press("Escape")
        time.sleep(0.5)
    # 2. 点击遮罩层（如果还存在且可见）
    try:
        mask = page.locator('.byte-modal-mask, .byte-drawer-mask').first
        if mask.count() > 0 and mask.is_visible(timeout=1000):
            mask.click()
            time.sleep(0.5)
    except:
        pass
    # 3. 点击面板自身的关闭按钮
    try:
        close_sel = page.locator('.byte-modal-close, .byte-drawer-close, [aria-label="关闭"]').first
        if close_sel.count() > 0 and close_sel.is_visible(timeout=1000):
            close_sel.click()
            time.sleep(0.5)
    except:
        pass
    # 4. 点击页面边缘空白区域（正文编辑器上方标题附近）
    try:
        title_input = page.locator('textarea[placeholder*="标题"]').first
        if title_input.count() > 0 and title_input.is_visible(timeout=1000):
            box = title_input.bounding_box()
            if box:
                page.mouse.click(box['x'] + box['width'] + 50, box['y'] + box['height'] / 2)
                time.sleep(0.5)
    except:
        pass
    # 5. 最后确认面板是否真的关闭了
    try:
        still_open = page.locator('.byte-modal, .byte-drawer').first
        if still_open.count() > 0 and still_open.is_visible(timeout=2000):
            print("[!] 警告：封面面板可能未完全关闭，但继续执行")
        else:
            print("封面面板已关闭")
    except:
        print("已尝试关闭封面上传面板")


def _dump_cover_ui(page: Page) -> None:
    """诊断日志：打印页面上与封面相关的真实 DOM 元素（帮助调试选择器）"""
    time.sleep(1)
    info = page.evaluate("""
        () => {
            function getPath(el) {
                if (!el || el === document.body) return 'body';
                let path = el.tagName.toLowerCase();
                if (el.className && typeof el.className === 'string') path += '.' + el.className.split(' ').filter(c => c).join('.');
                if (el.id) path += '#' + el.id;
                return path;
            }
            const results = [];

            // 1. 找所有 class 含 cover 的元素
            const coverEls = document.querySelectorAll('[class*="cover"]');
            coverEls.forEach(el => {
                const rect = el.getBoundingClientRect();
                results.push({
                    type: 'cover_class',
                    tag: getPath(el),
                    text: (el.textContent || '').trim().substring(0, 80),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    visible: rect.width > 0 && rect.height > 0 && el.offsetParent !== null
                });
            });

            // 2. 找所有含"封面"文字的元素
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            let node;
            while (node = walker.nextNode()) {
                if (!node.textContent) continue;
                const txt = node.textContent.trim();
                if ((txt.includes('封面') || txt.includes('正版') || txt.includes('图库')) && txt.length < 50) {
                    results.push({
                        type: 'text_match',
                        tag: getPath(node),
                        text: txt.substring(0, 80),
                        visible: node.offsetParent !== null
                    });
                }
            }

            // 3. 所有可见 button（前20个）
            const buttons = [...document.querySelectorAll('button')].filter(b => b.offsetParent !== null).slice(0, 20);
            buttons.forEach(b => {
                results.push({
                    type: 'visible_button',
                    tag: getPath(b),
                    text: (b.textContent || '').trim().substring(0, 40)
                });
            });

            // 4. 所有 iframe
            const iframes = [...document.querySelectorAll('iframe')].map(f => ({
                type: 'iframe',
                src: (f.src || '').substring(0, 120),
                visible: f.offsetParent !== null
            }));
            results.push(...iframes);

            return JSON.stringify(results, null, 2);
        }
    """)
    print(f"[封面诊断] \n{info}")


def _set_cover(page: Page) -> None:
    """
    封面设置（头条真实 UI 逻辑）：
    - 面板是主页面 DOM 中的 modal 弹窗（非 iframe）
    - 顶部 4 个 tab：上传图片 / 免费正版图片 / 热点图库 / 我的素材
    - 中间是图片网格，点击任意图片 = 自动选中并关闭面板（无需确认按钮）
    - 有搜索框，可输入关键词筛选免费正版图片
    """
    time.sleep(2)
    page.wait_for_load_state("networkidle")
    time.sleep(1)
    _close_popups(page)

    if _is_cover_already_set(page):
        return

    # 步骤 0：确保「单图」被选中
    print("[封面] 步骤0 - 确保「单图」单选被选中...")
    _ensure_single_image_checked(page)
    time.sleep(1)
    _dump_cover_ui(page)
    time.sleep(2)

    # 步骤 1：点击 + 号按钮打开素材面板
    print("[封面] 步骤1 - 点击 + 号打开素材面板...")
    if not _click_cover_add_button(page):
        print("[封面] 无法点击 + 号按钮，封面设置失败")
        page.screenshot(path="cover_no_add_{}.png".format(datetime.now().strftime('%Y%m%d_%H%M%S')))
        return
    time.sleep(3)

    # 步骤 2：切换到「免费正版图片」tab
    print("[封面] 步骤2 - 切换到「免费正版图片」tab...")
    _switch_to_free_tab_in_modal(page)
    time.sleep(2)

    # 步骤 3：搜索关键词 + 点击图片
    print("[封面] 步骤3 - 搜索关键词并点击图片...")
    img_clicked = _search_and_click_image_in_modal(page, keywords=["情感", "深夜", "女性", "婚姻", "伤感"])

    # 步骤 4：点击「确定」按钮确认选图
    if img_clicked:
        print("[封面] 步骤4 - 点击确定按钮...")
        _click_confirm_in_modal(page)
        time.sleep(2)

    # 步骤 5：终极兜底 —— JS 深度查找任意大尺寸图片
    if not img_clicked:
        print("[封面] 步骤5 - 兜底：JS 深度搜索大尺寸图片...")

    if not img_clicked:
        print("[封面] 所有方式都未找到可选图片！")
        page.screenshot(path="cover_no_img_{}.png".format(datetime.now().strftime('%Y%m%d_%H%M%S')))

    # 步骤 6：验证封面是否设置成功（图片选中后面板通常自动关闭）
    time.sleep(3)
    # 处理面板未自动关闭的情况
    try:
        panel = page.locator('.byte-modal, .byte-drawer, [class*="modal"], [class*="dialog"]').first
        if panel.count() > 0 and panel.is_visible(timeout=2000):
            print("[封面] 面板仍可见，点击面板内任意区域关闭...")
            _dismiss_cover_panel(page)
    except:
        pass

    time.sleep(1)
    if _is_cover_already_set(page):
        print("[封面] 封面设置验证通过")
    else:
        print("[封面] 警告：封面可能未设置成功，继续流程...")
        page.screenshot(path="cover_verify_fail_{}.png".format(datetime.now().strftime('%Y%m%d_%H%M%S')))

    print("[封面] 完成")


def _ensure_single_image_checked(page: Page) -> None:
    """确保「单图」单选按钮被选中（位于封面设置区域）"""
    single_cover_selectors = [
        'text=单图',
        'span:has-text("单图")',
        'label:has-text("单图")',
        'input[type="radio"][value*="单图"]',
        'input[type="radio"][value*="single"]',
    ]
    import json as _json
    for sel in single_cover_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                # 检查是否已选中（通过 aria-checked 或 checked 属性）
                safe_sel = _json.dumps(sel)
                is_checked = page.evaluate("""
                    (sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        // 找到最近的 radio input
                        const radio = el.closest('label')?.querySelector('input[type="radio"]') || el;
                        return !!(radio.checked || radio.getAttribute('aria-checked') === 'true');
                    }
                """, sel)
                if not is_checked:
                    el.click()
                    print("[OK] 已选中「单图」")
                else:
                    print("[OK] 「单图」已处于选中状态")
                return
        except:
            continue
    print("[!] 未明确找到「单图」选择器，假定默认选中")


def _click_cover_add_button(page: Page) -> bool:
    """
    点击封面 + 号按钮打开素材面板。
    直接 force-click .article-cover-add（诊断确认：154x120 的可见 div）。
    如果失败，回退到 Tab 键盘导航。
    """
    # 方案A：直接点击 .article-cover-add（诊断数据确认这个元素就是封面添加区）
    try:
        add_div = page.locator('.article-cover-add').first
        if add_div.count() > 0 and add_div.is_visible(timeout=3000):
            add_div.scroll_into_view_if_needed()
            add_div.click(force=True)
            print("[封面] 直接点击 .article-cover-add 成功")
            time.sleep(2)
            # 检查弹窗是否打开
            try:
                if page.locator('.byte-modal, .muse-dialog, [class*="modal"]').first.is_visible(timeout=3000):
                    print("[封面] 素材弹窗已打开")
                    return True
            except:
                pass
            # 也检查是否有图片选择面板出现（可能不是 byte-modal）
            if page.locator('[class*="dialog"]:visible, [class*="panel"]:visible, [class*="popup"]:visible').first.count() > 0:
                print("[封面] 检测到弹窗/面板（非 byte-modal），假定已打开")
                return True
    except Exception as e:
        print(f"[封面] 直接点击 .article-cover-add 失败: {e}")

    # 方案B：点击包含"预览"文字的封面区域
    try:
        preview_area = page.locator('div.article-cover-images-wrap, div.article-cover-preview, div.pgc-figure-cover-preview').first
        if preview_area.count() > 0:
            preview_area.scroll_into_view_if_needed()
            preview_area.click(force=True)
            print("[封面] 点击预览区域成功")
            time.sleep(2)
            try:
                if page.locator('.byte-modal, .muse-dialog, [class*="dialog"], [class*="panel"]').first.is_visible(timeout=3000):
                    print("[封面] 素材弹窗已打开（通过预览区域）")
                    return True
            except:
                pass
    except Exception as e:
        print(f"[封面] 点击预览区域失败: {e}")

    # 方案C：Tab 键盘导航兜底
    print("[封面] 直接点击未成功，尝试 Tab 导航兜底...")
    try:
        page.locator('textarea[placeholder*="标题"]').first.click()
        time.sleep(0.5)
    except:
        pass

    tab_count = int(os.environ.get("COVER_TAB_COUNT", "8"))
    for i in range(tab_count):
        page.keyboard.press("Tab")
        time.sleep(0.15)
    page.keyboard.press("Enter")
    time.sleep(2)
    try:
        if page.locator('.byte-modal, .muse-dialog, [class*="modal"]').first.is_visible(timeout=3000):
            print("[封面] Tab 导航兜底成功，素材弹窗已打开")
            return True
    except:
        pass

    print("[封面] 所有方案均未能打开弹窗")
    page.screenshot(path=f"cover_add_fail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    return False


def _click_cover_image_in_modal(page: Page) -> bool:
    """
    在主页面 DOM 的 modal 面板中搜索并点击第一张合适的封面图片。
    封面素材面板是主页面内的弹窗（非 iframe），图片点击后自动选中。
    """
    # 等待弹窗出现，确保图片加载完成
    try:
        page.wait_for_selector('.byte-modal', state='visible', timeout=5000)
        print("[封面] 弹窗已打开")
    except:
        print("[封面] 等待弹窗超时，继续尝试...")
    time.sleep(2)  # 等图片加载

    # 简化点击：直接点击弹窗内第一张可见的图片
    first_img = page.locator('.byte-modal img').first
    if first_img.count() > 0:
        try:
            first_img.click(force=True)
            print("[封面] 已点击弹窗内第一张图片")
            time.sleep(2)
            return True
        except Exception as e:
            print(f"[封面] 点击弹窗内第一张图片失败: {e}")

    # 先找「免费正版图片」tab 并确保选中
    free_tab_selectors = [
        'text=免费正版图片',
        'text=正版图片',
        'text=正版图库',
        'text=免费图片',
        '[class*="tab"]:has-text("免费正版")',
        '[class*="tab"]:has-text("正版")',
        '[role="tab"]:has-text("免费")',
        '[class*="tab-item"]:has-text("免费")',
    ]
    for sel in free_tab_selectors:
        try:
            tab = page.locator(sel).first
            if tab.count() > 0 and tab.is_visible(timeout=3000):
                # 确认它是否已经是选中状态（检查 class 是否有 active/selected）
                is_active = page.evaluate("""
                    (sel) => {
                        try {
                            const el = document.evaluate(
                                "//*[contains(text(), '免费')]",
                                document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                            ).singleNodeValue;
                            if (!el) return false;
                            const parent = el.closest('[class*="tab"]') || el.parentElement;
                            return parent && (parent.className.includes('active') || parent.className.includes('selected') || parent.getAttribute('aria-selected') === 'true');
                        } catch(e) { return false; }
                    }
                """, sel)
                if not is_active:
                    tab.click()
                    print(f"[封面] 已切换到「免费正版图片」tab ({sel})")
                else:
                    print(f"[封面] 「免费正版图片」tab 已处于选中状态")
                time.sleep(1.5)
                break
        except:
            continue

    time.sleep(1.5)

    # 在 modal 面板中找图片
    img_selectors = [
        # 卡片容器级别的选择器（包围 img 的可点击 div）
        '[class*="card"]',
        '[class*="item"]',
        '[class*="list"] [class*="item"]',
        '[class*="pic"]',
        '[class*="image"]',
        # 直接的 img 标签
        'img[src]',
        '[class*="card"] img',
        '[class*="item"] img',
    ]

    for sel in img_selectors:
        try:
            items = page.locator(sel)
            count = items.count()
            if count == 0:
                continue
            for i in range(min(count, 20)):
                el = items.nth(i)
                if not el.is_visible(timeout=1000):
                    continue
                box = el.bounding_box()
                if not box:
                    continue

                # 如果是 img 标签且尺寸合适
                tag = page.evaluate(f"document.querySelectorAll('{sel}')[{i}]?.tagName?.toLowerCase() || ''")
                if tag == 'img':
                    if box['width'] < 80 or box['height'] < 60:
                        continue
                    src = el.get_attribute('src') or ''
                    if any(s in src.lower() for s in ['avatar', 'icon', 'logo', 'favicon']):
                        continue
                else:
                    # 卡片/容器元素：尺寸应该 >= 100x100
                    if box['width'] < 100 or box['height'] < 100:
                        continue
                    # 确保里面有 img
                    has_img = page.evaluate(f"""
                        (idx) => {{
                            const el = document.querySelectorAll('{sel}')[idx];
                            if (!el) return false;
                            const imgs = el.querySelectorAll('img');
                            return imgs.length > 0;
                        }}
                    """, i)
                    if not has_img:
                        continue

                # 可见且是图片/图片容器 → 可点击
                el.scroll_into_view_if_needed()
                time.sleep(0.2)
                try:
                    el.click(force=True)
                    print(f"[封面] 点击素材图片 ({sel}[{i}], {box['width']}x{box['height']})")
                    time.sleep(1.5)
                    return True
                except Exception as ce:
                    print(f"[封面] {sel}[{i}] 点击失败: {ce}")
                    continue
        except Exception as e:
            print(f"[封面] 选择器 {sel} 异常: {e}")
            continue

    # JS 兜底 —— 在所有可见 modal 中找大尺寸图片
    try:
        result = page.evaluate("""
            () => {
                // 只在可见的 modal/dialog/drawer 内查找
                const containers = document.querySelectorAll('[class*="modal"]:not([class*="mask"]), [class*="drawer"], [class*="dialog"], [class*="popup"], [class*="panel"]');
                let searchRoot = document.body;
                for (const c of containers) {
                    const style = window.getComputedStyle(c);
                    if (style.display !== 'none' && style.visibility !== 'hidden') {
                        const rect = c.getBoundingClientRect();
                        if (rect.width > 300 && rect.height > 300) {
                            searchRoot = c;
                            break;
                        }
                    }
                }
                const imgs = searchRoot.querySelectorAll('img[src]');
                for (const img of imgs) {
                    const rect = img.getBoundingClientRect();
                    if (rect.width >= 100 && rect.height >= 75 && img.offsetParent !== null) {
                        const src = (img.src || '').toLowerCase();
                        if (src.includes('avatar') || src.includes('icon') || src.includes('logo') || src.includes('favicon') || src.includes('data:')) continue;
                        img.click();
                        return 'CLICKED:' + rect.width + 'x' + rect.height + ' ' + src.substring(0, 60);
                    }
                }
                // 也尝试找大尺寸的 div（可能是图片容器）
                const divs = searchRoot.querySelectorAll('div');
                for (const div of divs) {
                    const rect = div.getBoundingClientRect();
                    if (rect.width >= 150 && rect.height >= 120 && div.offsetParent !== null) {
                        const childImgs = div.querySelectorAll('img');
                        if (childImgs.length > 0) {
                            div.click();
                            return 'CLICKED_DIV:' + rect.width + 'x' + rect.height;
                        }
                    }
                }
                return 'NOT_FOUND';
            }
        """)
        if result.startswith('CLICKED'):
            print(f"[封面] JS 兜底选择图片 ({result})")
            time.sleep(1)
            return True
    except Exception as e:
        print(f"[封面] JS 兜底选图异常: {e}")

    return False


def _switch_to_free_tab_in_modal(page: Page) -> bool:
    """在素材弹窗内切换到「免费正版图片」tab"""
    free_tab_selectors = [
        'text=免费正版图片',
        'text=正版图片',
        'text=正版图库',
        'text=免费图片',
        '[class*="tab"]:has-text("免费")',
        '[class*="tab"]:has-text("正版")',
        '[role="tab"]:has-text("免费")',
        'div:has-text("免费正版")',
        'span:has-text("免费正版")',
    ]
    for sel in free_tab_selectors:
        try:
            tab = page.locator(sel).first
            if tab.count() > 0 and tab.is_visible(timeout=3000):
                tab.click()
                print(f"[封面] 已切换到「免费正版图片」tab ({sel})")
                time.sleep(2)
                return True
        except:
            continue
    print("[封面] 未找到「免费正版图片」tab，假定已在正确 tab")
    return False


def _search_and_click_image_in_modal(page: Page, keywords: list) -> bool:
    """在弹窗内搜索关键词并点击第一张图片"""
    # 先尝试搜索
    _search_cover_image(page, keywords)
    time.sleep(2)

    # 在弹窗内查找并点击图片
    try:
        page.wait_for_selector('[class*="modal"] img, [class*="dialog"] img, [class*="panel"] img, [class*="drawer"] img', state='visible', timeout=8000)
        time.sleep(2)
        result = page.evaluate("""
            () => {
                const containers = document.querySelectorAll('[class*="modal"], [class*="dialog"], [class*="panel"], [class*="drawer"]');
                for (const c of containers) {
                    if (c.offsetParent === null) continue;
                    const rect = c.getBoundingClientRect();
                    if (rect.width < 200 || rect.height < 200) continue;
                    const imgs = c.querySelectorAll('img[src]');
                    for (const img of imgs) {
                        if (img.offsetParent === null) continue;
                        const ir = img.getBoundingClientRect();
                        if (ir.width < 80 || ir.height < 60) continue;
                        const src = (img.src || '').toLowerCase();
                        if (src.includes('data:') || src.includes('.svg') || src.includes('icon') || src.includes('avatar')) continue;
                        img.click();
                        return 'clicked ' + ir.width + 'x' + ir.height;
                    }
                }
                return 'not found';
            }
        """)
        if result.startswith('clicked'):
            print(f"[封面] 已点击弹窗内图片 ({result})")
            time.sleep(2)
            return True
        print(f"[封面] 弹窗内未找到合适图片: {result}")
        return False
    except Exception as e:
        print(f"[封面] 弹窗内选图异常: {e}")
        return False


def _click_confirm_in_modal(page: Page) -> bool:
    """在弹窗内点击「确定」按钮确认选图"""
    confirm_selectors = [
        'button:has-text("确定")',
        'button:has-text("确认")',
        'span:has-text("确定")',
        'div:has-text("确定")',
        '[class*="modal"] button:has-text("确定")',
        '[class*="dialog"] button:has-text("确定")',
        '[class*="footer"] button:has-text("确定")',
        '.byte-modal button:has-text("确定")',
    ]
    for sel in confirm_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=3000):
                btn.click()
                print(f"[封面] 已点击确定按钮 ({sel})")
                time.sleep(2)
                return True
        except:
            continue
    print("[封面] 未找到确定按钮，假定图片已自动选中")
    return False


def _search_cover_image(page: Page, keywords: list) -> bool:
    """
    在素材面板的搜索框中输入关键词搜索图片。
    头条免费正版图库顶部有搜索框。
    """
    search_selectors = [
        'input[placeholder*="搜索"]',
        'input[placeholder*="关键词"]',
        'input[type="search"]',
        '.byte-input input',
        '[class*="search"] input',
        'input:not([type="hidden"])',
    ]

    input_found = False
    for sel in search_selectors:
        try:
            search_input = page.locator(sel).first
            if search_input.count() > 0 and search_input.is_visible(timeout=2000):
                input_found = True
                break
        except:
            continue

    if not input_found:
        print("[封面] 未找到搜索框，跳过关键词搜索")
        return False

    for kw in keywords:
        try:
            search_input = page.locator(search_selectors[0]).first
            if search_input.count() == 0 or not search_input.is_visible(timeout=1000):
                # 重新定位
                for sel in search_selectors:
                    si = page.locator(sel).first
                    if si.count() > 0 and si.is_visible(timeout=1000):
                        search_input = si
                        break

            search_input.click()
            time.sleep(0.3)
            search_input.fill("")
            time.sleep(0.2)
            search_input.type(kw, delay=50)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            print(f"[封面] 搜索关键词: {kw}")
            time.sleep(2)
            return True
        except Exception as e:
            print(f"[封面] 搜索关键词 '{kw}' 失败: {e}")
            continue

    return False


def _click_any_large_image_js(page: Page) -> bool:
    """
    最终兜底：用 JS 在主页面内找任意大尺寸图片（不限 modal 范围）并点击。
    这是最后的尝试，任何可见的大图都会尝试点击。
    """
    try:
        result = page.evaluate("""
            () => {
                // 策略1：优先在 visible modal/dialog 内查找
                const containers = document.querySelectorAll('[class*="modal"]:not([class*="mask"]), [class*="drawer"], [class*="dialog"], [class*="popup"], [class*="panel"]');
                for (const c of containers) {
                    const s = window.getComputedStyle(c);
                    if (s.display === 'none' || s.visibility === 'hidden') continue;
                    const r = c.getBoundingClientRect();
                    if (r.width < 200 || r.height < 200) continue;
                    // 找容器内最大的 img 或包含 img 的 div
                    const imgs = c.querySelectorAll('img[src]');
                    for (const img of imgs) {
                        const ir = img.getBoundingClientRect();
                        if (ir.width >= 100 && ir.height >= 75 && img.offsetParent !== null) {
                            const src = (img.src || '').toLowerCase();
                            if (src.includes('avatar') || src.includes('icon') || src.includes('logo') || src.includes('favicon') || src.includes('data:') || src.includes('.svg')) continue;
                            img.click();
                            return 'MODAL_IMG:' + ir.width + 'x' + ir.height;
                        }
                    }
                    const divs = c.querySelectorAll('div');
                    for (const d of divs) {
                        const dr = d.getBoundingClientRect();
                        if (dr.width >= 150 && dr.height >= 120 && d.offsetParent !== null && d.querySelector('img[src]')) {
                            d.click();
                            return 'MODAL_DIV:' + dr.width + 'x' + dr.height;
                        }
                    }
                }
                // 策略2：全页面找大尺寸图片（排除已知的 UI 图标区域）
                const allImgs = document.querySelectorAll('img[src]');
                for (const img of allImgs) {
                    const ir = img.getBoundingClientRect();
                    if (ir.width >= 120 && ir.height >= 100 && img.offsetParent !== null) {
                        const src = (img.src || '').toLowerCase();
                        if (src.includes('avatar') || src.includes('icon') || src.includes('logo') || src.includes('favicon') || src.includes('data:') || src.includes('.svg')) continue;
                        // 排除编辑区内的图片
                        if (img.closest('.ProseMirror') || img.closest('[contenteditable]')) continue;
                        img.click();
                        return 'GLOBAL_IMG:' + ir.width + 'x' + ir.height + ' ' + src.substring(0, 40);
                    }
                }
                return 'NOT_FOUND';
            }
        """)
        if result.startswith('MODAL') or result.startswith('GLOBAL'):
            print(f"[封面] 最终兜底 JS 点击图片 ({result})")
            time.sleep(1.5)
            return True
        elif result == 'NOT_FOUND':
            print("[封面] 最终兜底 JS 也未找到合适图片")
            return False
        else:
            print(f"[封面] 最终兜底 JS: {result}")
            return 'CLICKED' in result
    except Exception as e:
        print(f"[封面] 最终兜底 JS 异常: {e}")
        return False


def _click_publish(page: Page) -> None:
    """
    点击发布按钮。
    头条的流程是：在编辑页面点击"预览并发布" → 弹出预览对话框 → 在对话框中点击"发布"。
    """
    # ========== 第零步：发布前清理，确保没有遮挡 ==========
    time.sleep(1)
    # 按几次 ESC 确保没有残留弹窗/面板遮挡发布按钮
    for _ in range(2):
        page.keyboard.press("Escape")
        time.sleep(0.5)
    # 点击页面空白处确保焦点不在编辑器内
    try:
        page.mouse.click(100, 300)
        time.sleep(0.5)
    except:
        pass
    # 检查是否有可见的遮罩层遮挡按钮，如果有就点击遮罩层关闭
    try:
        mask = page.locator('.byte-modal-mask, .byte-drawer-mask').first
        if mask.count() > 0 and mask.is_visible(timeout=1000):
            mask.click()
            print("发布前清理：点击了残留遮罩层")
            time.sleep(0.5)
    except:
        pass
    # 重要：不再使用 JS 设置 display:none，否则会破坏后续预览弹窗

    # ========== 第一步：点击"预览并发布"按钮（编辑页面上的主按钮） ==========
    preview_publish_selectors = [
        'button.publish-btn',                       # * 实测头条发布按钮 class
        'button.publish-btn-last',                  # * 实测备选
        'button:has-text("预览并发布")',
        'span:has-text("预览并发布")',
        '[class*="preview"]:has-text("发布")',
        '[class*="publish"]:has-text("发布")',
        'text=预览并发布',
    ]

    found_preview = False
    for sel in preview_publish_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.scroll_into_view_if_needed()
                time.sleep(0.3)
                btn.click()
                print(f"点击了「预览并发布」（选择器: {sel}）")
                time.sleep(3)
                found_preview = True
                break
        except:
            continue

    if not found_preview:
        # 如果没有"预览并发布"按钮，尝试直接找"发布"按钮（老版本或特殊情况）
        for sel in ['button:has-text("发布")', 'button:has-text("提交")']:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    print(f"直接点击了「发布」（选择器: {sel}）")
                    time.sleep(3)
                    break
            except:
                continue

    # ========== 第二步：在预览/发布对话框中点击真正的"发布"按钮 ==========
    time.sleep(1)

    # 在对话框中找发布按钮（多种可能性）
    dialog_publish_selectors = [
        '.byte-modal button:has-text("发布")',
        '.byte-dialog button:has-text("发布")',
        'button:has-text("确认发布")',
        'button:has-text("发布")',
        '[class*="dialog"] button:has-text("发布")',
    ]
    for sel in dialog_publish_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                btn.click()
                print(f"在对话框中点击了发布（选择器: {sel}）")
                time.sleep(2)
                break
        except:
            continue

    # ========== 第三步：处理后续的各种弹窗 ==========
    time.sleep(2)

    # 使用 JS 点击各类确认弹窗按钮
    js_click_button_texts = [
        "我知道了",
        "确定",
        "确认发布",
        "发布",
        "暂不",
        "跳过",
        "关闭",
        "保存",
        "完成",
    ]
    for text in js_click_button_texts:
        try:
            clicked = page.evaluate("""
                (text) => {
                    const buttons = [...document.querySelectorAll('button, span, div[role="button"], a')];
                    const target = buttons.find(el => el.innerText.trim() === text || el.innerText.includes(text));
                    if (target) {
                        target.click();
                        return true;
                    }
                    return false;
                }
            """, text)
            if clicked:
                print(f"JS 点击了按钮: {text}")
                time.sleep(1)
        except:
            pass

    # 最后再按 ESC 清除残留弹窗
    page.keyboard.press("Escape")
    time.sleep(0.5)
    page.keyboard.press("Escape")
    time.sleep(0.5)

    # ========== 第四步：验证是否成功（检测成功提示） ==========
    success_hints = ["发布成功", "已发布", "提交成功", "操作成功"]
    content = page.content()
    for hint in success_hints:
        if hint in content:
            print(f"检测到发布成功提示: {hint}")
            return

    print("发布操作完成，等待后续确认...")


def _click_publish_fallback(page: Page) -> None:
    """备用：使用 JS 直接查找并点击页面中所有可能的发布按钮"""
    clicked = page.evaluate("""
        () => {
            const texts = ['预览并发布', '发布', '确认发布', '提交'];
            const allElements = [...document.querySelectorAll('button, span, a, div')];
            for (const text of texts) {
                const target = allElements.find(el =>
                    el.innerText.trim() === text || el.innerText.includes(text)
                );
                if (target) {
                    target.click();
                    return text;
                }
            }
            return null;
        }
    """)
    if clicked:
        print(f"JS 备用点击: {clicked}")
    else:
        print("JS 备用未找到任何发布按钮")


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
        browser = pw.chromium.launch(headless=HEADLESS)
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
            # 保存填写完成截图
            screenshot_after_fill = f"step1_filled_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            page.screenshot(path=screenshot_after_fill, full_page=True)
            print(f"填写完成截图: {screenshot_after_fill}")

            # 3.5 设置封面
            _set_cover(page)
            time.sleep(1)
            # 保存封面设置后截图
            screenshot_after_cover = f"step2_cover_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            page.screenshot(path=screenshot_after_cover, full_page=True)
            print(f"封面设置后截图: {screenshot_after_cover}")

            # 4. 点击发布
            _click_publish(page)

            # 5. 等待成功并截图
            result_path = _wait_for_success(page)
            if "success" not in result_path:
                print(f"[!] 未检测到明确成功提示，请检查截图: {result_path}")
            else:
                print(f"[OK] 发布成功！截图: {result_path}")

        finally:
            context.close()
            browser.close()
