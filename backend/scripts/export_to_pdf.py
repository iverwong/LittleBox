#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# make_src_pdf.py — 生成软著源代码 PDF（全量版 + 提交版）
# 依赖：pip install reportlab fonttools

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# ==================== 配置 ====================
LIST_FILE = "/tmp/cloc-files.txt"  # 每行一个源文件路径（cloc 用的那份清单）
TITLE = "小盒子青少年 AI 安全聊天 APP V0.7"  # 页眉左侧：软件全称 + 版本号
OUT_FULL = "源代码_全量.pdf"
OUT_SUBMIT = "源代码_提交版.pdf"

CODE_PER_PAGE = 60  # 每页代码行数（软著要求每页 >=50 行）
FONT_SIZE = 9
LEADING = 12  # 行距（pt）
MAX_CHARS = 120  # 单行最大字符数，超出硬换行
MARGIN_X = 50  # 左右页边距（pt）
MARGIN_TOP = 60  # 正文距顶部（pt，页眉在其上方）
MARGIN_BOTTOM = 40

# 提交版：源码页数超过阈值时，只取前 N + 后 N 页
SUBMIT_THRESHOLD = 60
SUBMIT_HEAD = 30
SUBMIT_TAIL = 30

# 核心代码优先靠前，保证审查在前 30 页能看到关键逻辑
PRIORITY = [
    "backend/app/main.py",
    "backend/app/domain/accounts",
    "backend/app/domain/chat/",
    "backend/app/domain/audit/",
]

# 字体
FONT_NAME = "code"
TTC_PATH = "/Users/iverwong/Library/Fonts/Sarasa-SuperTTC.ttc"
TTC_TARGET = "Sarasa-Mono-SC-Regular"  # PostScript 名称
FALLBACK = "/System/Library/Fonts/Supplemental/Songti.ttc"
FALLBACK_SUBFONT = 0


# ==================== 字体注册 ====================
# 直接把 PostScript 名称作为 subfontIndex 传给 reportlab，由它在 .ttc 内部按名字定位；
# 不再用 fontTools.TTCollection（那一步会把整个超级合集解析进内存，是内存爆炸的源头）。
def register_code_font():
    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, TTC_PATH, subfontIndex=TTC_TARGET))
        print("[字体] 已加载 %s" % TTC_TARGET)
    except Exception as e:
        print("[字体] Sarasa 加载失败（%s），回退宋体" % e)
        pdfmetrics.registerFont(TTFont(FONT_NAME, FALLBACK, subfontIndex=FALLBACK_SUBFONT))


# ==================== 读取并排序文件 ====================
def load_file_list(list_file):
    with open(list_file, encoding="utf-8") as f:
        files = [ln.strip() for ln in f if ln.strip()]
    seen, uniq = set(), []
    for p in files:  # 去重保序
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def priority_key(path):
    norm = path.replace("\\", "/")
    for i, pref in enumerate(PRIORITY):
        if pref in norm:
            return (0, i, norm)  # 命中优先列表，靠前
    return (1, 0, norm)  # 其余按路径字典序


def sort_files(files):
    return sorted(files, key=priority_key)


# ==================== 展开成行 ====================
def wrap_line(line, max_chars):
    line = line.rstrip("\n").replace("\t", "    ")
    if not line:
        return [""]
    return [line[i : i + max_chars] for i in range(0, len(line), max_chars)]


def build_lines(files):
    # 把所有源码展开成一维行列表，每个文件前加一行分隔头
    lines = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                raw = f.readlines()
        except Exception as e:
            print("[跳过] 无法读取 %s: %s" % (path, e))
            continue
        sep = "=" * 8
        lines.append("%s %s %s" % (sep, path, sep))
        for ln in raw:
            lines.extend(wrap_line(ln, MAX_CHARS))
    return lines


def paginate(lines, per_page):
    return [lines[i : i + per_page] for i in range(0, len(lines), per_page)]


# ==================== 渲染 PDF ====================
def render_pdf(out_path, pages, page_indices, total_pages):
    # pages: 要渲染的页；page_indices: 每页的“原始页码”(0起)，用于页眉；total_pages: 原始总页数
    width, height = A4
    c = canvas.Canvas(out_path, pagesize=A4)
    for page, orig_idx in zip(pages, page_indices):
        # 页眉：左=软件全称+版本号，右=页码
        c.setFont(FONT_NAME, 8)
        c.drawString(MARGIN_X, height - 40, TITLE)
        c.drawRightString(
            width - MARGIN_X, height - 40, "源代码 第%d页 / 共%d页" % (orig_idx + 1, total_pages)
        )
        c.line(MARGIN_X, height - 45, width - MARGIN_X, height - 45)
        # 正文
        c.setFont(FONT_NAME, FONT_SIZE)
        y = height - MARGIN_TOP
        for text in page:
            c.drawString(MARGIN_X, y, text)
            y -= LEADING
        c.showPage()
    c.save()
    print("[输出] %s  共 %d 页" % (out_path, len(pages)))


# ==================== 主流程 ====================
def main():
    register_code_font()

    files = sort_files(load_file_list(LIST_FILE))
    print("[文件] 共 %d 个源文件" % len(files))

    lines = build_lines(files)
    pages = paginate(lines, CODE_PER_PAGE)
    total = len(pages)
    print("[分页] 全量 %d 页（每页 %d 行）" % (total, CODE_PER_PAGE))

    # 全量版
    render_pdf(OUT_FULL, pages, list(range(total)), total)

    # 提交版
    if total > SUBMIT_THRESHOLD:
        idxs = list(range(SUBMIT_HEAD)) + list(range(total - SUBMIT_TAIL, total))
        sub_pages = [pages[i] for i in idxs]
        render_pdf(OUT_SUBMIT, sub_pages, idxs, total)  # 页码保留原始编号
        print("[提交版] 取前 %d + 后 %d 页，页码沿用原始编号" % (SUBMIT_HEAD, SUBMIT_TAIL))
    else:
        render_pdf(OUT_SUBMIT, pages, list(range(total)), total)
        print("[提交版] 总页数 <= %d，提交全部" % SUBMIT_THRESHOLD)


if __name__ == "__main__":
    main()
