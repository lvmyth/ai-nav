#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 导航站 · 自动适配原生 logo 脚本
=================================
扫描 index.html 的 TOOLS 数组，找出「仍用 emoji 占位(非图片路径)」或
「图片引用但文件缺失」的工具条目，按其官方 url 域名用 icon.horse 抓取
原生品牌 favicon/logo，转 PNG 存入 images/logos/{id}.png，并替换该条目的
icon 字段。

特性：
- 幂等可重复运行：已有原生 logo 且文件存在的条目不重复抓取
- 术语表(gl-*)无品牌 logo，自动跳过
- 抓取失败的条目保留原 emoji 并报告，不阻塞后续流程
- 纯本地文件操作，不触碰 git

用法：
    python3 fetch_missing_logos.py            # 处理并写回 index.html
    python3 fetch_missing_logos.py --dry-run  # 只扫描报告，不改文件
"""
import os
import re
import sys
import io
import subprocess
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(ROOT, "index.html")
LOGOS = os.path.join(ROOT, "images", "logos")


def get_tools(html):
    """从 index.html 提取 TOOLS 数组中每个条目的 id / icon / url / region。

    锚定在 `const TOOLS` 与下一个顶层声明 `const RECENT_CAP` 之间，
    避免被 TOOLS 内部 tags:['...'] 的方括号或数组之后的数据干扰。
    """
    s = html.index("const TOOLS")
    try:
        e = html.index("const RECENT_CAP", s)
    except ValueError:
        e = len(html)
    scope = html[s:e]
    # 每个条目以 { id:'...' 开头，到本条目最近的 icon:'...' 结束（条目内无嵌套 {}）
    pat = re.compile(r"\{ id:'([^']*)'.*?icon:'([^']*)'", re.S)
    tools = []
    for m in pat.finditer(scope):
        seg = scope[m.start():m.end()]
        gid = m.group(1)
        ic = m.group(2)
        ur = re.search(r"url:'([^']*)'", seg)
        rg = re.search(r"region:'([^']*)'", seg)
        tools.append({
            "id": gid,
            "icon": ic,
            "url": ur.group(1) if ur else "",
            "region": rg.group(1) if rg else "",
        })
    return tools


def is_image_icon(ic):
    return ic.startswith("images/") or ic.startswith("http")


def is_glossary(t):
    return t["id"].startswith("gl-") or t["region"] == "glossary"


def domain_of(url):
    m = re.search(r"https?://([^/]+)/?", url)
    if not m:
        return None
    host = m.group(1).lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def fetch_icon(domain):
    url = f"https://icon.horse/icon/{domain}"
    try:
        r = subprocess.run(
            ["curl", "-sSL", "-m", "30", "-w", "%{http_code}",
             "-o", "/tmp/_ico_bin", url],
            capture_output=True, text=True,
        )
        code = r.stdout.strip()
        if code != "200" or not os.path.exists("/tmp/_ico_bin"):
            return None
        data = open("/tmp/_ico_bin", "rb").read()
        if len(data) < 500:
            return None
        return data
    except Exception:
        return None


def to_png(data, out):
    try:
        im = Image.open(io.BytesIO(data))
        best = None
        maxs = 0
        if getattr(im, "n_frames", 1) > 1:
            for k in range(im.n_frames):
                im.seek(k)
                cur = im.convert("RGBA").copy()
                s = cur.size[0] * cur.size[1]
                if s > maxs:
                    maxs = s
                    best = cur
        else:
            best = im.convert("RGBA").copy()
        best.save(out, "PNG")
        return best.size
    except Exception:
        return None


def replace_icon(html, tid, new_path):
    pat = re.compile(r"(\{ id:'" + re.escape(tid) + r"'.*?icon:')[^']*(')", re.S)
    return pat.sub(r"\1" + new_path + r"\2", html, count=1)


def main():
    dry = "--dry-run" in sys.argv
    html = open(HTML, encoding="utf-8").read()
    tools = get_tools(html)
    targets = []
    for t in tools:
        if is_glossary(t):
            continue
        if is_image_icon(t["icon"]):
            if t["icon"].startswith("images/"):
                if os.path.exists(os.path.join(ROOT, t["icon"])):
                    continue
                t["reason"] = "图片文件缺失"
                targets.append(t)
            # http 外链图标跳过
        else:
            t["reason"] = "emoji 占位"
            targets.append(t)

    print(f"[扫描] TOOLS 共 {len(tools)} 条，需补原生 logo：{len(targets)} 条")
    if not targets:
        print("[结果] 无需适配，全部已是原生 logo ✅")
        return 0

    for t in targets:
        print(f"  · {t['id']:22} 原因={t['reason']:10} url={t['url'][:45]}")

    if dry:
        print("[dry-run] 未改动文件，退出。")
        return 0

    os.makedirs(LOGOS, exist_ok=True)
    changed = []
    failed = []
    for t in targets:
        dom = domain_of(t["url"])
        if not dom:
            failed.append((t["id"], "无有效 url"))
            continue
        data = fetch_icon(dom)
        if not data:
            failed.append((t["id"], f"icon.horse 抓取失败({dom})"))
            continue
        out = os.path.join(LOGOS, t["id"] + ".png")
        sz = to_png(data, out)
        if not sz:
            failed.append((t["id"], "转 PNG 失败"))
            continue
        html = replace_icon(html, t["id"], f"images/logos/{t['id']}.png")
        changed.append((t["id"], f"{sz[0]}x{sz[1]}", dom))

    open(HTML, "w", encoding="utf-8").write(html)

    print(f"\n[结果] 已适配 {len(changed)} 个原生 logo：")
    for rid, wh, dom in changed:
        print(f"  ✅ {rid:22} ({wh}) 来自 {dom}")
    if failed:
        print(f"\n[警告] {len(failed)} 个未能适配（保留原 emoji，待人工补）：")
        for rid, why in failed:
            print(f"  ⚠️  {rid:22} {why}")
    print(f"\n[汇总] 成功 {len(changed)} / 失败 {len(failed)} / 总计 {len(targets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
