"""下载前端依赖到 web/vendor/（约 2.8 MB），之后页面完全离线可用。

不下载也能用：index.html 在本地文件缺失时会自动从 npmmirror 的 CDN 加载同版本文件。
用法：python scripts/fetch_vendor.py
"""
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "web" / "vendor"
BASE = "https://registry.npmmirror.com"
FILES = {
    "vue.global.prod.js": "vue/3.5.46/files/dist/vue.global.prod.js",
    "element-plus.min.js": "element-plus/2.14.8/files/dist/index.full.min.js",
    "element-plus.css": "element-plus/2.14.8/files/dist/index.css",
    "element-plus-zh-cn.min.js": "element-plus/2.14.8/files/dist/locale/zh-cn.min.js",
    "icons-vue.min.js": "@element-plus/icons-vue/2.3.2/files/dist/index.iife.min.js",
    "echarts.min.js": "echarts/5.6.0/files/dist/echarts.min.js",
    "marked.min.js": "marked/12.0.2/files/marked.min.js",
}


def main():
    VENDOR.mkdir(parents=True, exist_ok=True)
    for name, path in FILES.items():
        target = VENDOR / name
        if target.exists() and target.stat().st_size > 1000:
            print(f"已存在 {name}")
            continue
        print(f"下载 {name} ……", end="", flush=True)
        with urllib.request.urlopen(f"{BASE}/{path}", timeout=60) as r:
            target.write_bytes(r.read())
        print(f" {target.stat().st_size // 1024} KB")
    print("完成")


if __name__ == "__main__":
    main()
