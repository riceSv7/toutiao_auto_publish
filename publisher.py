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
    """关闭可能打开的封面上传/选择面板 —— 多策略组合"""
    time.sleep(1)

    # 1. JS 派发原生 MouseEvent 点击遮罩层（绕过 Playwright 点击拦截）
    page.evaluate("""
        () => {
            const masks = document.querySelectorAll('.byte-drawer-mask, .byte-modal-mask');
            for (const mask of masks) {
                if (mask.offsetParent !== null) {
                    const rect = mask.getBoundingClientRect();
                    ['mousedown', 'mouseup', 'click'].forEach(name => {
                        mask.dispatchEvent(new MouseEvent(name, {
                            bubbles: true, cancelable: true,
                            clientX: rect.left + 10, clientY: rect.top + 10, button: 0
                        }));
                    });
                }
            }
        }
    """)
    time.sleep(0.5)

    # 2. 按 ESC 多次（比之前更多，确保 Vue 响应）
    for i in range(5):
        page.keyboard.press("Escape")
        time.sleep(0.4)

    # 3. 点击 close 按钮
    try:
        close_sel = page.locator('.byte-modal-close, .byte-drawer-close, [aria-label="关闭"]').first
        if close_sel.count() > 0 and close_sel.is_visible(timeout=1000):
            close_sel.click(force=True)
            time.sleep(0.5)
    except:
        pass

    # 4. 隐藏所有遮挡元素（mask + wrapper，它们会拦截后续点击）
    hidden_count = page.evaluate("""
        () => {
            let count = 0;
            const els = document.querySelectorAll('.byte-drawer-mask, .byte-modal-mask, .byte-drawer-wrapper');
            for (const el of els) {
                if (el.offsetParent !== null) {
                    el.style.display = 'none';
                    count++;
                }
            }
            return count;
        }
    """)
    if hidden_count > 0:
        print(f"封面遮挡层已强制隐藏 ({hidden_count}个)")

    # 5. 确认面板是否真的关闭了
    time.sleep(0.5)
    still_open = page.evaluate("""
        () => {
            // 检查有没有仍可见的大尺寸抽屉或模态框
            const els = document.querySelectorAll('.byte-drawer, .byte-modal');
            for (const el of els) {
                if (el.offsetParent !== null && window.getComputedStyle(el).display !== 'none') {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 200 && rect.height > 200) return true;
                }
            }
            return false;
        }
    """)
    if still_open:
        print("[!] 警告：封面面板可能未完全关闭")
    else:
        print("封面面板已关闭")


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

    # 步骤 2：切换到「免费正版图片」tab 并搜索选图
    print("[封面] 步骤2 - 切换到「免费正版图片」tab...")
    _switch_to_free_tab_in_modal(page)
    time.sleep(2)
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
    # 方案A：用 JS 原生事件点击封面添加区域
    try:
        result = page.evaluate("""
            () => {
                // 尝试多个可能的封面触发元素
                const selectors = [
                    '.article-cover-add',
                    '.article-cover-images',
                    '.article-cover-images-wrap',
                    '.article-cover-preview',
                    '.pgc-figure-cover-preview',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    // 用原生 MouseEvent 触发（兼容 Vue/React）
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    ['mousedown', 'mouseup', 'click'].forEach(name => {
                        el.dispatchEvent(new MouseEvent(name, {
                            bubbles: true, cancelable: true,
                            clientX: cx, clientY: cy, button: 0
                        }));
                    });
                    return 'CLICKED:' + sel + ' ' + rect.width + 'x' + rect.height;
                }
                return 'NOT_FOUND';
            }
        """)
        print(f"[封面] JS 事件点击结果: {result}")
        time.sleep(3)

        # 诊断：检查点击后是否有弹窗出现
        modal_info = page.evaluate("""
            () => {
                const results = [];
                const selectors = ['modal', 'dialog', 'drawer', 'popup', 'overlay', 'muse'];
                for (const s of selectors) {
                    const els = document.querySelectorAll('[class*="' + s + '"]');
                    for (const el of els) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 200 || rect.height > 200) {
                            results.push({
                                tag: el.tagName.toLowerCase(),
                                cls: (el.className || '').toString().substring(0, 80),
                                text: (el.textContent || '').trim().substring(0, 80),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                                vis: el.offsetParent !== null
                            });
                        }
                    }
                }
                return JSON.stringify(results, null, 2);
            }
        """)
        print(f"[封面] 点击后弹窗诊断: {modal_info}")
        page.screenshot(path=f"cover_after_click_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")

        # 检查弹窗是否打开
        if page.locator('[class*="modal"]:visible, [class*="dialog"]:visible, [class*="drawer"]:visible').first.count() > 0:
            # 确认不是页面本身的 garr-panel
            is_real_modal = page.evaluate("""() => {
                const els = document.querySelectorAll('[class*="modal"]:not([class*="garr"]), [class*="dialog"]:not([class*="garr"]), [class*="drawer"]:not([class*="sticky"])');
                for (const el of els) {
                    if (el.offsetParent !== null) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 200 && r.height > 200 && r.width < window.innerWidth) {
                            return true;
                        }
                    }
                }
                return false;
            }""")
            if is_real_modal:
                print("[封面] 素材弹窗已打开")
                return True
    except Exception as e:
        print(f"[封面] 方案A JS 事件点击失败: {e}")

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


def _upload_local_cover(page: Page) -> bool:
    """在打开的抽屉中直接上传本地 cover.jpg"""
    import os as _os
    cover_path = _os.path.join(_os.path.dirname(__file__), "cover.jpg")
    if not _os.path.exists(cover_path):
        print(f"[封面] 本地图片不存在: {cover_path}")
        return False

    print(f"[封面] 准备上传: {cover_path}")

    # 方法1：找到隐藏的 file input 直接传文件
    try:
        file_input = page.locator('input[type="file"]').first
        if file_input.count() > 0:
            file_input.set_input_files(cover_path)
            print("[封面] 已选择本地文件，等待上传...")
            time.sleep(5)
            # 检查是否上传成功（看看页面有没有变化）
            return True
    except Exception as e:
        print(f"[封面] file input 上传失败: {e}")

    # 方法2：点击"本地上传"按钮触发文件选择
    try:
        upload_btn = page.locator('text=本地上传').first
        if upload_btn.count() > 0 and upload_btn.is_visible(timeout=3000):
            # 监听文件选择对话框
            with page.expect_file_chooser() as fc_info:
                upload_btn.click()
            file_chooser = fc_info.value
            file_chooser.set_files(cover_path)
            print("[封面] 通过文件选择器上传")
            time.sleep(5)
            return True
    except Exception as e:
        print(f"[封面] 本地上传按钮失败: {e}")

    # 方法3：找所有 file input（包括隐藏的）
    try:
        result = page.evaluate("""
            (path) => {
                const inputs = document.querySelectorAll('input[type="file"]');
                if (inputs.length > 0) {
                    // 返回找到的 input 信息
                    const info = [];
                    inputs.forEach((inp, i) => {
                        info.push('input[' + i + ']: ' + (inp.className || '') + ' accept=' + (inp.accept || ''));
                    });
                    return JSON.stringify(info);
                }
                return 'no file inputs';
            }
        """, cover_path)
        print(f"[封面] 页面 file inputs: {result}")
    except:
        pass

    return False


def _switch_to_free_tab_in_modal(page: Page) -> bool:
    """在素材弹窗内切换到「免费正版图片」tab"""
    # 使用 JS MouseEvent 直接点击 .byte-tabs-header-title 元素
    result = page.evaluate("""
        () => {
            const titles = document.querySelectorAll('.byte-tabs-header-title');
            for (const el of titles) {
                if (el.textContent.trim() === '免费正版图片') {
                    const rect = el.getBoundingClientRect();
                    ['mousedown', 'mouseup', 'click'].forEach(name => {
                        el.dispatchEvent(new MouseEvent(name, {
                            bubbles: true, cancelable: true,
                            clientX: rect.left + rect.width/2,
                            clientY: rect.top + rect.height/2,
                            button: 0
                        }));
                    });
                    return 'CLICKED free tab';
                }
            }
            return 'NOT_FOUND';
        }
    """)
    print(f"[封面] 切换 tab: {result}")
    time.sleep(2)
    return 'CLICKED' in result


def _search_and_click_image_in_modal(page: Page, keywords: list) -> bool:
    """在抽屉内找图片并点击。先等默认图片加载，不行再搜索。"""
    # 先不用搜索，等默认图片自然加载
    print("[封面] 等待抽屉内图片加载...")
    time.sleep(5)  # 给足时间让默认图片加载

    # 在抽屉内滚动一下触发懒加载
    try:
        page.evaluate("""
            () => {
                const scroll = document.querySelector('.byte-drawer-scroll');
                if (scroll) scroll.scrollTop += 300;
            }
        """)
        time.sleep(1)
    except:
        pass

    # 诊断：检查 iframe、canvas、所有 img
    dump = page.evaluate("""
        () => {
            const drawer = document.querySelector('.byte-drawer');
            if (!drawer) return 'no drawer';
            const results = [];

            // 检查 iframe
            const iframes = drawer.querySelectorAll('iframe');
            results.push('iframes: ' + iframes.length);
            iframes.forEach((f, i) => {
                results.push('  iframe[' + i + '] src=' + (f.src || '').substring(0, 80) + ' ' + f.getBoundingClientRect().width + 'x' + f.getBoundingClientRect().height);
            });

            // 检查 canvas
            const canvases = drawer.querySelectorAll('canvas');
            results.push('canvases: ' + canvases.length);
            canvases.forEach((c, i) => {
                results.push('  canvas[' + i + '] ' + c.getBoundingClientRect().width + 'x' + c.getBoundingClientRect().height);
            });

            // 检查所有 img（包括 0x0 的）
            const imgs = drawer.querySelectorAll('img');
            results.push('all imgs: ' + imgs.length);
            imgs.forEach((img, i) => {
                const r = img.getBoundingClientRect();
                const ns = img.naturalWidth || 0;
                results.push('  img[' + i + ']: dom=' + r.width + 'x' + r.height + ' natural=' + ns + 'x' + (img.naturalHeight||0) + ' src=' + (img.src || '').substring(0, 80));
            });

            // 检查所有带 background-image 的元素
            const all = drawer.querySelectorAll('*');
            let bgCount = 0;
            for (const el of all) {
                const bg = window.getComputedStyle(el).backgroundImage;
                if (bg && bg !== 'none' && bg.includes('url') && !bg.includes('data:')) {
                    const r = el.getBoundingClientRect();
                    results.push('  bg: ' + el.tagName + '.' + (el.className||'').substring(0,40) + ' ' + r.width + 'x' + r.height + ' ' + bg.substring(0, 80));
                    bgCount++;
                    if (bgCount >= 5) break;
                }
            }

            return JSON.stringify(results);
        }
    """)
    print(f"[封面] 抽屉内容诊断: {dump}")

    # 在抽屉内查找真实图片
    result = _find_and_click_image_in_container(page, '.byte-drawer')
    if result:
        return True

    # 没找到 → 在抽屉内搜索
    print("[封面] 默认无图片，在抽屉内搜索...")
    _search_in_drawer(page, keywords)
    time.sleep(5)  # 等搜索结果加载
    # 再滚动触发懒加载
    try:
        page.evaluate("() => { const s = document.querySelector('.byte-drawer-scroll'); if (s) s.scrollTop += 300; }")
        time.sleep(1)
    except:
        pass
    return _find_and_click_image_in_container(page, '.byte-drawer')


def _find_and_click_image_in_container(page: Page, container_sel: str) -> bool:
    """在抽屉内找带真实背景图的图片并点击（头条用 CSS background-image 显示图片）"""
    result = page.evaluate("""
        (sel) => {
            const container = document.querySelector(sel);
            if (!container) return 'no container';

            // 策略1: 点击 LI.item 元素（头条免费正版图库使用 LI.item + CSS background-image）
            const items = container.querySelectorAll('li.item, li[class*="item"]');
            for (const item of items) {
                const bg = window.getComputedStyle(item).backgroundImage;
                if (!bg || bg === 'none' || bg.includes('data:')) continue;
                if (!bg.includes('url(')) continue;
                const r = item.getBoundingClientRect();
                if (r.width >= 60 && r.height >= 40 && item.offsetParent !== null) {
                    item.click();
                    return 'clicked li.item ' + Math.round(r.width) + 'x' + Math.round(r.height) + ' ' + bg.substring(0, 60);
                }
            }

            // 策略2: 点击 IMG.btn.image-border（旧版头条图库使用）
            const imgs = container.querySelectorAll('IMG.btn.image-border');
            for (const img of imgs) {
                const bg = window.getComputedStyle(img).backgroundImage;
                if (!bg || bg === 'none' || bg.includes('data:')) continue;
                if (!bg.includes('url(') || !bg.includes('tuchong')) continue;
                img.click();
                return 'clicked IMG.btn.image-border ' + bg.substring(0, 60);
            }

            // 策略3: 点击任意带 tuchong 背景图的元素（div/span/li/a）
            const all = container.querySelectorAll('div, span, li, a');
            for (const el of all) {
                const bg = window.getComputedStyle(el).backgroundImage;
                if (!bg || bg === 'none' || bg.includes('data:')) continue;
                if (!bg.includes('tuchong')) continue;
                const r = el.getBoundingClientRect();
                if (r.width >= 60 && r.height >= 40 && el.offsetParent !== null) {
                    el.click();
                    return 'fallback_clicked ' + el.tagName + ' ' + Math.round(r.width) + 'x' + Math.round(r.height);
                }
            }

            return 'not found';
        }
    """, container_sel)
    print(f"[封面] 背景图查找: {result}")
    if result and result.startswith(('clicked', 'fallback_clicked')):
        time.sleep(2)
        return True
    return False


def _search_in_drawer(page: Page, keywords: list) -> bool:
    """在抽屉内搜索关键词，直接复用已验证的通用搜索"""
    # 直接使用已存在的通用搜索函数（之前测试中确认可用）
    return _search_cover_image(page, keywords)


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


def _select_no_cover(page: Page) -> None:
    """选择「无封面」模式，跳过封面上传流程直接发布"""
    time.sleep(1)
    no_cover = page.locator('text=无封面').first
    if no_cover.count() > 0 and no_cover.is_visible(timeout=3000):
        no_cover.click()
        print("已选择「无封面」")
        time.sleep(2)
    else:
        print("[!] 未找到「无封面」选项，继续流程")


def _click_publish(page: Page) -> None:
    """
    点击发布按钮。
    头条的流程取决于是否有封面：
    - 无封面：按钮为「确认发布」，一键直接发布
    - 有封面：按钮为「预览并发布」，可能弹出预览对话框再确认
    关键：必须点击按钮内部的 <span> 元素并派发原生 MouseEvent，才能触发 Vue 事件处理器。
    """
    # ========== 第零步：清理遮挡 ==========
    time.sleep(1)
    for _ in range(3):
        page.keyboard.press("Escape")
        time.sleep(0.5)
    page.evaluate("""
        () => {
            document.querySelectorAll('.byte-drawer-mask, .byte-modal-mask, .byte-drawer-wrapper').forEach(m => {
                if (m.offsetParent !== null) m.style.display = 'none';
            });
        }
    """)
    time.sleep(0.5)

    # ========== 第一步：点击主发布按钮内部的 SPAN ==========
    # 经验证：点击 SPAN 派发原生 MouseEvent 能正确触发发布 API
    print("正在点击发布按钮...")
    clicked_text = page.evaluate("""
        () => {
            const btn = [...document.querySelectorAll('button')].find(b =>
                b.offsetParent !== null &&
                b.className.includes('primary') &&
                (b.innerText.includes('发布') || b.innerText.includes('预览并发布'))
            );
            if (!btn) return 'NOT_FOUND';
            // 点击内部 SPAN（而非 button 本身），这是触发 Vue 事件的关键
            const span = btn.querySelector('span');
            const target = span || btn;
            const rect = target.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            ['mousedown', 'mouseup', 'click'].forEach(name => {
                target.dispatchEvent(new MouseEvent(name, {
                    bubbles: true, cancelable: true,
                    clientX: cx, clientY: cy, button: 0
                }));
            });
            return btn.innerText.trim().slice(0, 30);
        }
    """)
    print(f"点击了: {clicked_text}")
    time.sleep(3)

    # ========== 第二步：如果是「预览并发布」，轮询「确认发布」按钮 ==========
    if '预览' not in clicked_text:
        print("发布按钮已点击（非预览模式），等待结果...")
        return

    # 不依赖弹窗容器检测——直接轮询可见的「确认发布」按钮
    second_click = None
    for _ in range(12):
        time.sleep(0.5)
        second_click = page.evaluate("""
            () => {
                const buttons = [...document.querySelectorAll('button')].filter(b =>
                    b.offsetParent !== null
                );
                // 精确匹配「确认发布」
                let btn = buttons.find(b => b.innerText.trim() === '确认发布');
                // 兜底：包含「发布」但不含「预览」
                if (!btn) btn = buttons.find(b => {
                    const t = b.innerText.trim();
                    return t.includes('确认发布') || (t.includes('发布') && !t.includes('预览'));
                });
                if (!btn) return null;
                const span = btn.querySelector('span');
                const target = span || btn;
                const rect = target.getBoundingClientRect();
                ['mousedown', 'mouseup', 'click'].forEach(name => {
                    target.dispatchEvent(new MouseEvent(name, {
                        bubbles: true, cancelable: true,
                        clientX: rect.left + rect.width / 2,
                        clientY: rect.top + rect.height / 2,
                        button: 0
                    }));
                });
                return btn.innerText.trim().slice(0, 30);
            }
        """)
        if second_click:
            print(f"二次点击: {second_click}")
            time.sleep(3)
            break
    if not second_click:
        print("未找到二次确认按钮（可能已直接发布）")

    print("发布按钮已点击，等待结果...")


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
    """等待发布成功。只用真正表示成功的一次性提示，不依赖导航菜单常驻文字。"""
    success_hints = [
        "发布成功",
        "提交成功",
        "操作成功",
    ]
    start_url = page.url
    print(f"等待发布结果（当前 URL: {start_url}）...")

    deadline = time.time() + timeout
    while time.time() < deadline:
        current_url = page.url

        # URL 跳转到管理页 = 发布成功（排除仅 hash 变化）
        if current_url != start_url:
            management_paths = [
                "/content/manage", "/article/manage", "/content/article",
                "/content/video", "/content",
                "/profile_v4/content", "/profile_v4/article",
            ]
            is_management = any(p in current_url for p in management_paths)
            is_different_base = current_url.split("?")[0].split("#")[0] != start_url.split("?")[0].split("#")[0]

            if is_management or is_different_base:
                path = f"success_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                page.screenshot(path=path, full_page=True)
                print(f"页面已跳转: {start_url} -> {current_url}")
                print(f"发布成功截图: {path}")
                return path

        # 在弹窗/toast/通知区域检查一次性成功提示（不匹配导航栏）
        try:
            has_success_toast = page.evaluate("""
                (hints) => {
                    const containers = document.querySelectorAll(
                        '.byte-modal, .byte-dialog, .byte-toast, .byte-notification, ' +
                        '.muse-dialog, .el-message, .el-notification, ' +
                        '[class*="toast"], [class*="notification"], [class*="message"], ' +
                        '[class*="dialog"]:not([class*="garr"])'
                    );
                    for (const el of containers) {
                        if (!el.offsetParent && el.tagName !== 'DIV') continue;
                        const text = el.textContent || '';
                        for (const hint of hints) {
                            if (text.includes(hint)) return true;
                        }
                    }
                    return false;
                }
            """, success_hints)
            if has_success_toast:
                path = f"success_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                page.screenshot(path=path, full_page=True)
                print(f"检测到成功 toast/弹窗，截图: {path}")
                return path
        except:
            pass

        # 检查"我知道了"按钮（出现即表示成功）
        try:
            has_btn = page.evaluate("""
                () => {
                    const buttons = [...document.querySelectorAll('button, span, div[role="button"], a')];
                    return buttons.some(el => el.innerText.trim() === '我知道了');
                }
            """)
            if has_btn:
                # 点掉"我知道了"
                page.evaluate("""
                    () => {
                        const buttons = [...document.querySelectorAll('button, span, div[role="button"], a')];
                        const target = buttons.find(el => el.innerText.trim() === '我知道了');
                        if (target) target.click();
                    }
                """)
                time.sleep(1)
                path = f"success_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                page.screenshot(path=path, full_page=True)
                print(f"检测到「我知道了」按钮，发布成功。截图: {path}")
                return path
        except:
            pass

        time.sleep(1)

    # 超时也保存一张截图
    path = f"result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    page.screenshot(path=path, full_page=True)
    print(f"超时未检测到成功（当前 URL: {page.url}），已截图: {path}")
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
