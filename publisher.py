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

    # 清空 + 逐段用 keyboard.type 输入
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
    time.sleep(2)

    # 步骤 1：点击 + 号按钮打开素材面板
    print("[封面] 步骤1 - 点击 + 号打开素材面板...")
    if not _click_cover_add_button(page):
        print("[封面] 无法点击 + 号按钮，封面设置失败")
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

    if not img_clicked:
        print("[封面] 所有方式都未找到可选图片！")

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

            // 先诊断第一个 IMG.btn.image-border 的父链
            const firstImg = container.querySelector('IMG.btn.image-border') || container.querySelector('img[style*="background"]');
            if (firstImg) {
                let chain = [];
                let p = firstImg;
                for (let i = 0; i < 8 && p && p !== document.body; i++) {
                    const r = p.getBoundingClientRect();
                    chain.push(p.tagName + '.' + (p.className||'').substring(0,30) + ' ' + r.width + 'x' + r.height + ' cs=' + window.getComputedStyle(p).display);
                    p = p.parentElement;
                }
                console.log('IMG parent chain:', JSON.stringify(chain));
            }

            // 直接找所有 IMG.btn.image-border 并点击（即使0x0也尝试）
            const imgs = container.querySelectorAll('IMG.btn.image-border');
            for (const img of imgs) {
                const bg = window.getComputedStyle(img).backgroundImage;
                if (!bg || bg === 'none' || bg.includes('data:')) continue;
                if (!bg.includes('url(') || !bg.includes('tuchong')) continue;
                // 直接点击 IMG 本身（JS click 不依赖 DOM 尺寸）
                img.click();
                return 'clicked IMG.btn.image-border ' + bg.substring(0, 60);
            }

            // 回退：找所有带 tuchong 背景图的元素
            const all = container.querySelectorAll('*');
            for (const el of all) {
                const bg = window.getComputedStyle(el).backgroundImage;
                if (!bg || bg === 'none' || bg.includes('data:')) continue;
                if (!bg.includes('tuchong')) continue;
                el.click();
                return 'fallback_clicked ' + el.tagName + '.' + (el.className||'').substring(0,40);
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
    # 注意：页面有两个 button.publish-btn（"预览并发布"和"定时发布"），封面区也有"预览"按钮
    # 必须用完整文字"预览并发布"精确匹配，避免误点封面预览或定时发布
    preview_publish_selectors = [
        'button.publish-btn:has-text("预览并发布")',
        'button.publish-btn >> text=预览并发布',
        'button:has-text("预览并发布")',
        'span:has-text("预览并发布")',
        'text=预览并发布',
        'button.publish-btn-last',
        '[class*="preview"]:has-text("发布")',
        '[class*="publish"]:has-text("发布")',
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

    dialog_publish_selectors = [
        'button.publish-btn.byte-btn-primary:has-text("预览并发布")',
        'button.byte-btn-primary:has-text("预览并发布")',
        'button.publish-btn.byte-btn-primary:has-text("发布")',
        '.byte-modal button:has-text("发布")',
        '.byte-dialog button:has-text("发布")',
        'button:has-text("确认发布")',
        'button:has-text("发布")',
        '[class*="dialog"] button:has-text("发布")',
    ]
    dialog_clicked = False
    for sel in dialog_publish_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=3000):
                # 用 JS 原生 MouseEvent 派发，确保 Vue 事件处理器能响应
                result = page.evaluate(f"""
                    () => {{
                        const sel = {sel!r};
                        const el = document.querySelector(sel) || [...document.querySelectorAll('button')].find(b => b.innerText.includes('发布'));
                        if (!el) return 'NOT_FOUND';
                        const rect = el.getBoundingClientRect();
                        const cx = rect.left + rect.width / 2;
                        const cy = rect.top + rect.height / 2;
                        ['mousedown', 'mouseup', 'click'].forEach(name => {{
                            el.dispatchEvent(new MouseEvent(name, {{
                                bubbles: true, cancelable: true,
                                clientX: cx, clientY: cy, button: 0
                            }}));
                        }});
                        return 'CLICKED:' + el.innerText.trim().slice(0, 20);
                    }}
                """)
                print(f"在对话框中点击了发布（选择器: {sel}）, 结果: {result}")
                time.sleep(3)
                dialog_clicked = True
                break
        except:
            continue

    if not dialog_clicked:
        # JS 兜底：找所有含"发布"的 button，点击 primary 的那个
        result = page.evaluate("""
            () => {
                const buttons = [...document.querySelectorAll('button')].filter(b =>
                    b.offsetParent !== null && b.innerText.includes('发布')
                );
                // 优先点 primary 按钮
                const primary = buttons.find(b => b.className.includes('primary'));
                const target = primary || buttons[0];
                if (!target) return 'NOT_FOUND';
                const rect = target.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                ['mousedown', 'mouseup', 'click'].forEach(name => {
                    target.dispatchEvent(new MouseEvent(name, {
                        bubbles: true, cancelable: true,
                        clientX: cx, clientY: cy, button: 0
                    }));
                });
                return 'FALLBACK_CLICKED:' + target.innerText.trim().slice(0, 30);
            }
        """)
        print(f"对话框发布按钮 JS 兜底: {result}")
        time.sleep(3)

    # ========== 第三步：等待发布结果，先检测成功再关弹窗 ==========
    time.sleep(3)

    # 先检查是否发布成功，成功的话截图保存证据
    success_hints = ["发布成功", "已发布", "提交成功", "操作成功", "审核"]
    content = page.content()
    already_success = False
    for hint in success_hints:
        if hint in content:
            print(f"检测到发布成功提示: {hint}")
            already_success = True
            break

    if already_success:
        # 成功后如果有"我知道了"按钮，点掉即可
        try:
            page.evaluate("""
                () => {
                    const buttons = [...document.querySelectorAll('button, span, div[role="button"], a')];
                    const target = buttons.find(el => el.innerText.trim() === '我知道了');
                    if (target) target.click();
                }
            """)
        except:
            pass
        return

    # 还没检测到成功，逐个处理弹窗按钮（但"我知道了"可能是成功弹窗，先跳过它检测成功）
    js_click_button_texts = [
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

    # 再次检查成功提示（"我知道了"按钮出现往往意味着成功）
    time.sleep(2)
    for hint in success_hints:
        if hint in page.content():
            print(f"二次检测到发布成功提示: {hint}")
            already_success = True
            break

    # 检查是否有"我知道了"按钮（通常是成功后的确认按钮）
    try:
        clicked = page.evaluate("""
            () => {
                const buttons = [...document.querySelectorAll('button, span, div[role="button"], a')];
                const target = buttons.find(el => el.innerText.trim() === '我知道了');
                if (target) {
                    target.click();
                    return true;
                }
                return false;
            }
        """)
        if clicked:
            print("JS 点击了按钮: 我知道了（成功确认）")
            time.sleep(1)
    except:
        pass

    # 最后再按 ESC 清除残留弹窗
    page.keyboard.press("Escape")
    time.sleep(0.5)
    page.keyboard.press("Escape")
    time.sleep(0.5)

    if already_success:
        return

    # ========== 第四步：最终验证 ==========
    for hint in success_hints:
        if hint in page.content():
            print(f"最终检测到发布成功提示: {hint}")
            return

    print("发布操作完成，等待后续确认...")



def _wait_for_success(page: Page, timeout: int = 30) -> str:
    success_hints = [
        "发布成功",
        "已发布",
        "审核",
        "提交成功",
        "操作成功",
        "内容管理",       # 发布后可能跳转到内容管理页
        "作品管理",
        "文章管理",
    ]
    start_url = page.url
    print(f"等待发布结果（当前 URL: {start_url}）...")

    deadline = time.time() + timeout
    while time.time() < deadline:
        current_url = page.url

        # 检查 URL 是否变化（发布成功后通常会跳转）
        if current_url != start_url:
            path = f"success_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            page.screenshot(path=path, full_page=True)
            print(f"页面已跳转: {start_url} -> {current_url}")
            print(f"发布成功截图: {path}")
            return path

        # 检查页面内容中的成功提示
        content = page.content()
        for hint in success_hints:
            if hint in content:
                path = f"success_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                page.screenshot(path=path, full_page=True)
                print(f"检测到成功提示「{hint}」，截图: {path}")
                return path

        # 检查"我知道了"按钮（出现即表示成功）
        try:
            has_btn = page.evaluate("""
                () => {
                    const buttons = [...document.querySelectorAll('button, span, div[role="button"], a')];
                    return buttons.some(el => el.innerText.trim() === '我知道了');
                }
            """)
            if has_btn:
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

            _set_cover(page)
            time.sleep(1)

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
