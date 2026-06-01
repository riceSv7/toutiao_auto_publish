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
    """关闭可能打开的封面上传/选择面板"""
    time.sleep(1)
    # 1. 按 ESC 键关闭
    page.keyboard.press("Escape")
    time.sleep(0.5)
    # 2. 点击页面空白区域（正文编辑器的上方标题附近）
    try:
        title_input = page.locator('input[placeholder*="标题"]').first
        if title_input.is_visible(timeout=1000):
            # 点击标题旁边的空白区域
            box = title_input.bounding_box()
            if box:
                page.mouse.click(box['x'] + box['width'] + 50, box['y'] + box['height'] / 2)
                time.sleep(0.5)
    except:
        pass
    # 3. JS 强制关闭所有 modal/drawer
    page.evaluate("""
        () => {
            // 关闭所有 byte-modal
            document.querySelectorAll('.byte-modal').forEach(m => m.style.display = 'none');
            // 关闭所有 drawer
            document.querySelectorAll('.byte-drawer').forEach(d => d.style.display = 'none');
            // 关闭所有遮罩层
            document.querySelectorAll('.byte-modal-mask, .byte-drawer-mask').forEach(m => m.style.display = 'none');
            // 移除 body 上的 overflow hidden
            document.body.style.overflow = '';
        }
    """)
    time.sleep(0.5)
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
    策略：以「无封面」为锚点找右侧加号大框。
    """
    time.sleep(2)
    page.wait_for_load_state("networkidle")
    time.sleep(1)
    _close_popups(page)

    if _is_cover_already_set(page):
        return

    # ★ 诊断：打印封面区域 DOM
    _dump_cover_ui(page)
    time.sleep(3)  # 等待封面组件异步加载
    _dump_cover_ui(page)

    # ========== 步骤1：以「无封面」为锚点找右侧加号 ==========
    print("封面流程：以「无封面」为锚点找加号大框...")
    click_result = page.evaluate("""
        () => {
            // 找「无封面」文本元素
            const all = [...document.querySelectorAll('*')];
            const noCover = all.find(e => (e.textContent||'').trim()==='无封面' && e.offsetParent && e.children.length<=1);
            if (!noCover) return JSON.stringify({status:'NO_ANCHOR'});
            const ar = noCover.getBoundingClientRect();
            // 在「无封面」右侧找候选：left在ar.right+5到ar.right+350, top在ar.top-30到ar.bottom+80
            const candidates = all.filter(e => {
                if (!e.offsetParent || e===noCover || e.contains(noCover)) return false;
                const r = e.getBoundingClientRect();
                return r.left>=ar.right+5 && r.right<=ar.right+350 && r.top>=ar.top-30 && r.bottom<=ar.bottom+80 && r.width>30 && r.height>30;
            }).map(e => {
                const r = e.getBoundingClientRect(); const txt = (e.textContent||'').trim();
                const isPlus = txt==='' || txt==='+' || txt==='＋' || e.tagName==='A' || (e.className||'').includes('add') || (e.className||'').includes('cover');
                return {tag:e.tagName, cls:(e.className||'').substring(0,60), txt:txt.substring(0,20), w:Math.round(r.w), h:Math.round(r.h), x:Math.round(r.x), y:Math.round(r.y), plus:isPlus};
            });
            if (!candidates.length) return JSON.stringify({status:'NO_CANDIDATES'});
            // 选最像加号的：尺寸>50x50 且距离最近
            candidates.sort((a,b) => a.x - b.x);
            const best = candidates.find(c => c.plus && c.w>50 && c.h>50) || candidates.find(c => c.plus) || candidates[0];
            // 点击
            const clickEl = all.find(e => {
                const r = e.getBoundingClientRect();
                return Math.round(r.x)===best.x && Math.round(r.y)===best.y;
            });
            if (clickEl) { clickEl.scrollIntoView({block:'center'}); clickEl.click(); }
            return JSON.stringify({status:clickEl?'CLICKED':'FAILED', best, candidates});
        }
    """)
    print(f"锚点定位结果: {click_result}")
    try:
        res = json.loads(click_result)
        if res.get('status') != 'CLICKED':
            # 坐标兜底: 正文编辑器上方80px, 偏右450px
            editor = page.locator('[contenteditable="true"]').first
            box = editor.bounding_box()
            if box:
                page.mouse.click(box['x']+450, box['y']-80)
                print(f"坐标兜底加号 ({box['x']+450}, {box['y']-80})")
    except:
        page.mouse.click(640, 280)
        print("绝对兜底 (640,280)")
    time.sleep(3)

    # ========== 步骤 2：面板中找「免费正版图片」 ==========
    # 面板中可能有 Tab 标签切换，找"免费正版图片"或"正版图库"等
    free_selectors = [
        'text=免费正版图片',
        'text=正版图库',
        'text=免费图片',
        'text=正版图片',
        'span:has-text("免费正版图片")',
        'div:has-text("免费正版图片")',
        '[class*="tab"]:has-text("免费")',
        '[class*="tab"]:has-text("正版")',
        '[role="tab"]:has-text("免费")',
        '[role="tab"]:has-text("正版")',
    ]
    free_clicked = False
    for sel in free_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=3000):
                btn.click()
                print(f"✅ 步骤1：点击图片来源标签 ({sel})")
                time.sleep(2)
                free_clicked = True
                break
        except:
            continue

    # 如果没找到tab，可能默认就是免费图片列表，直接跳到选图
    if not free_clicked:
        print("⚠ 步骤1：未找到「免费正版图片」标签，可能已在免费图片列表，尝试直接选图")
        # 截图保存当前面板
        page.screenshot(path=f"cover_panel_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        # Dump 面板内部元素
        panel_html = page.evaluate("""
            () => {
                const modals = document.querySelectorAll('.byte-modal, .byte-drawer, [class*="modal"], [class*="dialog"], [class*="drawer"]');
                const texts = [];
                modals.forEach(m => {
                    const style = window.getComputedStyle(m);
                    if (style.display !== 'none') {
                        texts.push('=== 面板 HTML ===');
                        texts.push(m.outerHTML.substring(0, 3000));
                        // 面板内所有可见文本
                        const all = [...m.querySelectorAll('*')];
                        const uniqueTexts = [...new Set(all.filter(e => e.children.length === 0 && e.offsetParent !== null).map(e => e.textContent.trim()).filter(t => t && t.length < 30))];
                        texts.push('=== 面板内可见文本 ===');
                        texts.push(JSON.stringify(uniqueTexts.slice(0, 50)));
                    }
                });
                return texts.join('\\n');
            }
        """)
        print(f"[面板内容]\n{panel_html}")

    # ========== 步骤 3：选第一张图片 ==========
    time.sleep(3)  # 等图片列表加载完

    img_clicked = False
    # 策略 A: 在弹窗/modal 中找第一张图片
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
                    print(f"✅ 步骤2：点击封面图片 ({sel}, 共{count}张)")
                    time.sleep(1)
                    img_clicked = True
                    break
        except:
            continue

    # 策略 B: JS 在弹窗内找合适尺寸的图片
    if not img_clicked:
        result = page.evaluate("""
            () => {
                // 先找弹窗容器
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
            print(f"✅ 步骤2-JS：点击了封面图片 ({result})")
            img_clicked = True
        else:
            print(f"⚠ 步骤2：所有策略都未找到可选图片。JS结果: {result}")
            page.screenshot(path=f"cover_no_img_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")

    # ========== 步骤 4：确认 ==========
    if img_clicked:
        time.sleep(1)
        confirm_selectors = [
            'button:has-text("确定")',
            'button:has-text("完成")',
            'span:has-text("确定")',
            'button:has-text("设为封面")',
            'button:has-text("使用")',
            '.byte-btn-primary',
            '[class*="btn-primary"]',
        ]
        for sel in confirm_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible(timeout=2000):
                    btn.click()
                    print(f"✅ 步骤3：点击确认按钮 ({sel})")
                    time.sleep(1)
                    break
            except:
                continue

    # 关闭可能残留的面板
    _dismiss_cover_panel(page)
    print("封面设置流程完成")


def _click_publish(page: Page) -> None:
    """
    点击发布按钮。
    头条的流程是：在编辑页面点击"预览并发布" → 弹出预览对话框 → 在对话框中点击"发布"。
    """
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
