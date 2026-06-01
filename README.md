# 头条自动发布

用 AI 生成文章并自动发布到头条号。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
playwright install chromium

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入头条账号和 DeepSeek API Key

# 3. 运行
python main.py
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `main.py` | 主流程：生成文章 → 发布 |
| `content_generator.py` | 调用 DeepSeek API 生成文章 |
| `publisher.py` | Playwright 浏览器自动化发布到头条号后台 |

## 注意事项

- 首次运行需手动登录（可能触发验证码），后续浏览器会保留 cookie
- 头条编辑器页面结构可能更新，若选择器失效需调整 `publisher.py` 中的定位逻辑
- DeepSeek API 按量计费，请确保账户余额充足
