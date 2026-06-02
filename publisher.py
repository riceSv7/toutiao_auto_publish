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
    """检测封面是否已经设置好了（已有图片显示），如果是则跳过封面上传流程"""
    time.sleep(1)
    # 检查页面中是否存在已设置的封面图
    checks = [
        # 封面区域内的 img 标签且已经加载
        "document.querySelector('[class*=\"cover\"] img') !== null",
        # 封面容器内含有 src 属性的图片
        "document.querySelector('[class*=\"cover\"] img[src]') !== null",
        # 某些头条版本中封面是 background-image
        """() => {
            const el = document.querySelector('[class*=\"cover\"]');
            if (!el) return false;
            const bg = window.getComputedStyle(el).backgroundImage;
            return bg && bg !== 'none' && bg.includes('url');
        }""",
        # 封面预览区域（class 含 preview 或 show 的图片）
        "document.querySelector('[class*=\"preview\"] img[src]') !== null",
    ]
    for js_code in checks:
        try:
            if page.evaluate(js_code):
                print("检测到封面已设置，跳过封面上传步骤")
                return True
        except:
            continue

    # 额外检查：看页面上有没有 text=添加封面，如果没有（只有编辑封面），说明已经有封面了
    try:
        add_btn = page.locator('text=添加封面').first
        if not add_btn.is_visible(timeout=1000):
            # "添加封面"按钮不可见，但可能存在"编辑封面"或"更换封面"
            edit_btn = page.locator('text=编辑封面').first
            change_btn = page.locator('text=更换封面').first
            has_edit = edit_btn.is_visible(timeout=500) if edit_btn.count() else False
            has_change = change_btn.is_visible(timeout=500) if change_btn.count() else False
            if has_edit or has_change:
                print("检测到封面已设置（有编辑封面按钮），跳过封面上传步骤")
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
            print("⚠ 警告：封面面板可能未完全关闭，但继续执行")
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
    封面布局（用户提供）：
      展示封面
      单图 | 三图 | 无封面 | [加号大框] | 预览
    加号在「无封面」和「预览」之间，单图/三图正下方。

    关键修正：
    - 确保「单图」单选按钮选中
    - 点击加号后弹出 iframe（免费正版图库），需要在 iframe 内选图
    - iframe 内有 Tab 标签（免费正版图片/热点图库等）和图片列表
    """
    time.sleep(2)
    page.wait_for_load_state("networkidle")
    time.sleep(1)
    _close_popups(page)

    if _is_cover_already_set(page):
        return

    # ========== 步骤 0：确保「单图」被选中 ==========
    print("封面流程：步骤0 - 确保「单图」单选被选中...")
    _ensure_single_image_checked(page)
    time.sleep(1)

    # ★ 诊断：打印封面区域 DOM
    _dump_cover_ui(page)
    time.sleep(2)  # 等待封面组件异步加载
    _dump_cover_ui(page)

    # ========== 步骤 1：点击 .article-cover-add 加号大框 ==========
    print("封面流程：步骤1 - 点击 .article-cover-add 加号...")
    clicked = False
    try:
        plus_btn = page.locator('.article-cover-add').first
        if plus_btn.count() > 0:
            plus_btn.wait_for(state="visible", timeout=5000)
            plus_btn.scroll_into_view_if_needed()
            plus_btn.click()
            print("✅ 已点击 .article-cover-add 加号大框")
            clicked = True
    except Exception as e:
        print(f".article-cover-add 点击失败: {e}")

    if not clicked:
        try:
            page.evaluate("document.querySelector('.article-cover-add')?.click()")
            print("JS 兜底点击 .article-cover-add")
            clicked = True
        except:
            pass

    if not clicked:
        print("⚠ 未找到 .article-cover-add，使用坐标兜底")
        page.mouse.click(640, 280)

    time.sleep(4)  # 等待 iframe 加载

    # ========== 步骤 2：检测并进入 iframe（封面图库弹窗） ==========
    print("封面流程：步骤2 - 检测 iframe 并选图...")

    # 先获取所有可见 iframe 信息
    iframe_info = page.evaluate("""
        () => {
            const iframes = document.querySelectorAll('iframe');
            const results = [];
            iframes.forEach((f, i) => {
                const rect = f.getBoundingClientRect();
                results.push({
                    index: i,
                    src: (f.src || '').substring(0, 200),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    visible: rect.width > 100 || rect.height > 100
                });
            });
            return results;
        }
    """)
    print(f"[iframe 检测]\n{json.dumps(iframe_info, indent=2, ensure_ascii=False)}")

    img_clicked = False
    confirm_clicked = False

    # ---- 策略 A：在 iframe 内操作（头条免费正版图库常用 iframe 加载） ----
    if any(f.get('visible') for f in iframe_info):
        print("策略A: 在 iframe 中搜索图片...")
        for iframe_data in iframe_info:
            if not iframe_data.get('visible'):
                continue
            idx = iframe_data.get('index', 0)

            try:
                # 获取 iframe 的 frame 对象
                frame = page.frames[idx + 1] if idx + 1 < len(page.frames) else None
                if frame is None:
                    # 通过 src 匹配
                    for f in page.frames:
                        if f.url and 'toutiao' not in f.url and 'mp.' not in f.url:
                            frame = f
                            break

                if frame is None:
                    print(f"  无法获取 iframe[{idx}] 的 frame 对象")
                    continue

                print(f"  进入 iframe[{idx}]: {iframe_data.get('src', '')[:80]}")

                # 等 iframe 内容加载
                try:
                    frame.wait_for_load_state("networkidle", timeout=10000)
                except:
                    pass
                time.sleep(2)

                # 在 iframe 内点击图片
                img_clicked = _click_image_in_iframe(page, frame)
                if img_clicked:
                    # 在 iframe 内找确认按钮
                    confirm_clicked = _click_confirm_in_iframe(page, frame)
                    if confirm_clicked:
                        print("✅ iframe 内操作完成")
                    break  # 无论确认是否点击，图片已选中就跳出
            except Exception as e:
                print(f"  iframe[{idx}] 操作失败: {e}")
                continue

    # ---- 策略 B：主页面 DOM 选图（兜底） ----
    if not img_clicked:
        print("策略B: 在主页面 DOM 中搜索图片（兜底）...")
        img_clicked = _click_image_in_main_page(page)

        if img_clicked:
            time.sleep(1)
            confirm_clicked = _click_confirm_in_main_page(page)

    # ---- 策略 C：JS 深度搜索（最终兜底） ----
    if not img_clicked:
        print("策略C: JS 深度搜索所有 frame 中的图片...")
        img_clicked, clicked_frame = _click_image_js_deep_search(page)
        if img_clicked:
            confirm_clicked = _click_confirm_js_deep_search(page, clicked_frame)

    # 如果所有策略都未找到图片，截图诊断
    if not img_clicked:
        print("⚠ 所有策略都未找到可选图片！")
        page.screenshot(path=f"cover_no_img_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")

    # ========== 步骤 5：验证封面是否设置成功 ==========
    time.sleep(2)
    # 等待面板自然关闭
    try:
        page.wait_for_timeout(3000)
        panel = page.locator('.byte-modal, .byte-drawer').first
        if panel.count() > 0 and panel.is_visible(timeout=2000):
            print("面板仍可见，手动关闭...")
            _dismiss_cover_panel(page)
    except:
        pass

    time.sleep(1)
    if _is_cover_already_set(page):
        print("✅ 封面设置验证通过")
    else:
        print("⚠ 警告：封面可能未设置成功，继续流程...")
        page.screenshot(path=f"cover_verify_fail_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")

    print("封面设置流程完成")


def _ensure_single_image_checked(page: Page) -> None:
    """确保「单图」单选按钮被选中（位于封面设置区域）"""
    single_cover_selectors = [
        'text=单图',
        'span:has-text("单图")',
        'label:has-text("单图")',
        'input[type="radio"][value*="单图"]',
        'input[type="radio"][value*="single"]',
    ]
    for sel in single_cover_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                # 检查是否已选中（通过 aria-checked 或 checked 属性）
                is_checked = page.evaluate(f"""
                    () => {{
                        const el = document.querySelector('{sel.replace("'", "\\'")}');
                        if (!el) return false;
                        // 找到最近的 radio input
                        const radio = el.closest('label')?.querySelector('input[type="radio"]') || el;
                        return radio.checked || radio.getAttribute('aria-checked') === 'true';
                    }}
                """)
                if not is_checked:
                    el.click()
                    print("✅ 已选中「单图」")
                else:
                    print("✅ 「单图」已处于选中状态")
                return
        except:
            continue
    print("⚠ 未明确找到「单图」选择器，假定默认选中")


def _click_image_in_iframe(page: Page, frame) -> bool:
    """在 iframe 内找并点击第一张可用的封面图片"""
    # 先尝试点击「免费正版图片」tab（可能在 iframe 内）
    free_tab_selectors = [
        'text=免费正版图片',
        'text=正版图库',
        'text=免费图片',
        'text=正版图片',
        '[class*="tab"]:has-text("免费")',
        '[class*="tab"]:has-text("正版")',
        '[role="tab"]:has-text("免费")',
    ]
    for sel in free_tab_selectors:
        try:
            btn = frame.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=2000):
                btn.click()
                print(f"  ✅ iframe 内点击「免费正版图片」tab ({sel})")
                time.sleep(2)
                break
        except:
            continue

    # 等图片列表加载
    time.sleep(2)

    # 在 iframe 内找图片
    img_selectors = [
        'img[src]',
        '.image-item img',
        '[class*="image-item"] img',
        '[class*="pic-item"] img',
        '[class*="card"] img',
        '[class*="list"] img',
    ]
    for sel in img_selectors:
        try:
            imgs = frame.locator(sel)
            count = imgs.count()
            if count > 0:
                # 跳过小图标，找尺寸合理的图片（宽 >= 100, 高 >= 75）
                for i in range(min(count, 15)):  # 最多检查前 15 张
                    img = imgs.nth(i)
                    if img.is_visible(timeout=2000):
                        box = img.bounding_box()
                        if box and box['width'] >= 100 and box['height'] >= 75:
                            # 还要检查 src 不是 avatar/icon/logo
                            src = img.get_attribute('src') or ''
                            if any(skip in src.lower() for skip in ['avatar', 'icon', 'logo', 'favicon']):
                                continue
                            img.click()
                            print(f"  ✅ iframe 内点击封面图片 ({sel}[{i}], {box['width']}x{box['height']})")
                            time.sleep(1)
                            return True
        except:
            continue

    # 兜底：在 iframe 内 JS 查找
    try:
        result = frame.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img[src]');
                for (const img of imgs) {
                    const rect = img.getBoundingClientRect();
                    if (rect.width >= 100 && rect.height >= 75 && img.offsetParent !== null) {
                        const src = (img.src || '').toLowerCase();
                        if (src.includes('avatar') || src.includes('icon') || src.includes('logo') || src.includes('favicon')) continue;
                        img.click();
                        return 'CLICKED:' + rect.width + 'x' + rect.height;
                    }
                }
                return 'NOT_FOUND';
            }
        """)
        if result.startswith('CLICKED:'):
            print(f"  ✅ iframe 内 JS 点击封面图片 ({result})")
            time.sleep(1)
            return True
    except Exception as e:
        print(f"  iframe 内 JS 选图失败: {e}")

    return False


def _click_confirm_in_iframe(page: Page, frame) -> bool:
    """在 iframe 内找确认/使用按钮"""
    confirm_selectors = [
        'button:has-text("确定")',
        'button:has-text("使用")',
        'button:has-text("使用图片")',
        'button:has-text("选好了")',
        'button:has-text("确认")',
        'button:has-text("完成")',
        'button:has-text("设为封面")',
        'span:has-text("确定")',
        'span:has-text("使用")',
        '.byte-btn-primary',
        '[class*="confirm"]',
        '[class*="btn-primary"]',
    ]
    for sel in confirm_selectors:
        try:
            btn = frame.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=2000):
                btn.click()
                print(f"  ✅ iframe 内点击确认按钮 ({sel})")
                time.sleep(1)
                return True
        except:
            continue

    # JS 兜底
    try:
        result = frame.evaluate("""
            () => {
                const btns = document.querySelectorAll('button, span[role="button"], div[role="button"]');
                const keywords = ['确定', '确认', '使用', '完成', '选好了', '设为封面'];
                for (const kw of keywords) {
                    for (const b of btns) {
                        if (b.offsetParent !== null && b.textContent.trim().includes(kw)) {
                            b.click();
                            return 'CLICKED:' + kw;
                        }
                    }
                }
                return 'NOT_FOUND';
            }
        """)
        if result.startswith('CLICKED:'):
            print(f"  ✅ iframe 内 JS 点击确认按钮 ({result})")
            time.sleep(1)
            return True
    except Exception as e:
        print(f"  iframe 内 JS 确认按钮查找失败: {e}")

    return False


def _click_image_in_main_page(page: Page) -> bool:
    """在主页面 DOM 中找并点击封面图片（兜底策略）"""
    image_selectors = [
        '.byte-modal img[src]',
        '[class*="modal"] img[src]',
        '[class*="dialog"] img[src]',
        '[class*="drawer"] img[src]',
        '.image-item img',
        '.image-item',
        '[class*="image-list"] [class*="item"]:first-child img',
        '[class*="image-list"] [class*="item"]:first-child',
        '[class*="pic-list"] img:first-child',
        '[class*="pic-list"] [class*="item"]:first-child',
    ]
    for sel in image_selectors:
        try:
            imgs = page.locator(sel)
            count = imgs.count()
            if count > 0:
                img = imgs.first
                if img.is_visible(timeout=2000):
                    img.scroll_into_view_if_needed()
                    time.sleep(0.3)
                    img.click()
                    print(f"  ✅ 主页面点击封面图片 ({sel}, 共{count}张)")
                    time.sleep(1)
                    return True
        except:
            continue

    # JS 兜底
    result = page.evaluate("""
        () => {
            const containers = document.querySelectorAll('.byte-modal, .byte-drawer, [class*="modal"]:not([class*="mask"]), [class*="dialog"]');
            let searchRoot = document.body;
            for (const c of containers) {
                if (window.getComputedStyle(c).display !== 'none') {
                    searchRoot = c;
                    break;
                }
            }
            const imgs = searchRoot.querySelectorAll('img[src]');
            for (const img of imgs) {
                const rect = img.getBoundingClientRect();
                if (rect.width >= 100 && rect.height >= 75 && img.offsetParent !== null) {
                    if (img.src.includes('avatar') || img.src.includes('icon') || img.src.includes('logo') || img.src.includes('favicon')) continue;
                    img.click();
                    return 'CLICKED:' + rect.width + 'x' + rect.height + ' ' + img.src.substring(0, 60);
                }
            }
            return 'NOT_FOUND';
        }
    """)
    if result.startswith('CLICKED:'):
        print(f"  ✅ 主页面 JS 点击封面图片 ({result})")
        time.sleep(1)
        return True
    return False


def _click_confirm_in_main_page(page: Page) -> bool:
    """在主页面 DOM 中找确认按钮"""
    confirm_selectors = [
        'button:has-text("确定")',
        'button:has-text("确认")',
        'button:has-text("完成")',
        'button:has-text("使用")',
        'button:has-text("设为封面")',
        '.byte-btn-primary',
    ]
    for sel in confirm_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=2000):
                btn.click()
                print(f"  ✅ 主页面点击确认按钮 ({sel})")
                time.sleep(1)
                return True
        except:
            continue

    result = page.evaluate("""
        () => {
            const containers = document.querySelectorAll('.byte-modal, .byte-drawer, [class*="modal"]:not([class*="mask"]), [class*="dialog"]');
            let searchRoot = document.body;
            for (const c of containers) {
                if (window.getComputedStyle(c).display !== 'none') {
                    searchRoot = c;
                    break;
                }
            }
            const btns = searchRoot.querySelectorAll('button, span[role="button"], div[role="button"]');
            const keywords = ['确定', '确认', '完成', '使用', '设为封面'];
            for (const kw of keywords) {
                for (const b of btns) {
                    if (b.offsetParent !== null && b.textContent.trim().includes(kw)) {
                        b.click();
                        return 'CLICKED:' + kw;
                    }
                }
            }
            return 'NOT_FOUND';
        }
    """)
    if result.startswith('CLICKED:'):
        print(f"  ✅ 主页面 JS 点击确认按钮 ({result})")
        return True
    return False


def _click_image_js_deep_search(page: Page):
    """JS 深度搜索：遍历所有 frame 找图片并点击"""
    # 尝试 main frame
    try:
        result = page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img[src]');
                for (const img of imgs) {
                    const rect = img.getBoundingClientRect();
                    if (rect.width >= 100 && rect.height >= 75 && img.offsetParent !== null) {
                        const src = (img.src || '').toLowerCase();
                        if (src.includes('avatar') || src.includes('icon') || src.includes('logo') || src.includes('favicon') || src.includes('data:')) continue;
                        img.click();
                        return 'CLICKED_MAIN:' + rect.width + 'x' + rect.height + ' ' + src.substring(0, 60);
                    }
                }
                return 'NOT_FOUND_MAIN';
            }
        """)
        if result.startswith('CLICKED_MAIN:'):
            print(f"  ✅ JS 深度搜索(主页面)点击图片 ({result})")
            time.sleep(1)
            return True, None
    except:
        pass

    # 遍历所有子 frame
    for i, frame in enumerate(page.frames):
        if frame == page.main_frame:
            continue
        try:
            result = frame.evaluate("""
                () => {
                    const imgs = document.querySelectorAll('img[src]');
                    for (const img of imgs) {
                        const rect = img.getBoundingClientRect();
                        if (rect.width >= 100 && rect.height >= 75 && img.offsetParent !== null) {
                            const src = (img.src || '').toLowerCase();
                            if (src.includes('avatar') || src.includes('icon') || src.includes('logo') || src.includes('favicon') || src.includes('data:')) continue;
                            img.click();
                            return 'CLICKED:' + rect.width + 'x' + rect.height + ' ' + src.substring(0, 60);
                        }
                    }
                    return 'NOT_FOUND';
                }
            """)
            if result.startswith('CLICKED:'):
                print(f"  ✅ JS 深度搜索(frame[{i}])点击图片 ({result})")
                time.sleep(1)
                return True, frame
        except:
            continue

    return False, None


def _click_confirm_js_deep_search(page: Page, clicked_frame=None) -> bool:
    """JS 深度搜索确认按钮"""
    frames_to_search = [clicked_frame] if clicked_frame else [page.main_frame]
    if not clicked_frame:
        for frame in page.frames:
            if frame != page.main_frame:
                frames_to_search.append(frame)

    for frame in frames_to_search:
        if frame is None:
            continue
        try:
            result = frame.evaluate("""
                () => {
                    const btns = document.querySelectorAll('button, span[role="button"], div[role="button"], a.btn, a[class*="btn"]');
                    const keywords = ['确定', '确认', '使用', '完成', '选好了', '设为封面', '保存'];
                    for (const kw of keywords) {
                        for (const b of btns) {
                            if (b.offsetParent !== null && b.textContent.trim().includes(kw)) {
                                b.click();
                                return 'CLICKED:' + kw;
                            }
                        }
                    }
                    return 'NOT_FOUND';
                }
            """)
            if result.startswith('CLICKED:'):
                print(f"  ✅ JS 深度搜索点击确认按钮 ({result})")
                time.sleep(1)
                return True
        except:
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
    preview_publish_selectors = [
        'button.publish-btn',                       # ★ 实测头条发布按钮 class
        'button.publish-btn-last',                  # ★ 实测备选
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
            clicked = page.evaluate(f"""
                () => {{
                    const buttons = [...document.querySelectorAll('button, span, div[role="button"], a')];
                    const target = buttons.find(el => el.innerText.trim() === '{text}' || el.innerText.includes('{text}'));
                    if (target) {{
                        target.click();
                        return true;
                    }}
                    return false;
                }}
            """)
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
                print(f"⚠ 未检测到明确成功提示，请检查截图: {result_path}")
            else:
                print(f"✅ 发布成功！截图: {result_path}")

        finally:
            context.close()
            browser.close()
