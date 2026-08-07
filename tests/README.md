# 测试说明

日常离线测试只需要 Python 标准库：

```text
python -m unittest discover -s tests -p "test_*.py"
```

`browser_qa.js` 是可选的视觉回归检查，需要另行安装 Node.js 与 Playwright，并安装 Chromium：

```text
npm install --no-save playwright
npx playwright install chromium
node tests/browser_qa.js
```

若希望使用本机已有浏览器，可把 `BROWSER_EXECUTABLE_PATH` 设为其完整路径。视觉检查会把截图写进本地 `data/` 目录；该目录默认不会进入 Git。

设置 `PUBLIC_DEMO_ONLY=1` 可以强制检查缺少本机 `data.js` 时的脱敏演示回退；设置 `QA_PREVIEW_PATH` 可以额外输出一张当前视口的 JPEG 预览图。
