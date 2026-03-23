import requests
import re
import os
import time
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont


def get_font(size):
    font_candidates = [
        r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
        r"C:\Windows\Fonts\msyhbd.ttc",    # 微软雅黑粗体
        r"C:\Windows\Fonts\simhei.ttf",    # 黑体
        r"C:\Windows\Fonts\simsun.ttc",    # 宋体
    ]
    for path in font_candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def has_choice_answer(ans) -> bool:
    if ans is None:
        return False

    ans = str(ans).strip().upper().replace(" ", "")
    return (
        1 <= len(ans) <= 4
        and set(ans) <= set("ABCD")
        and len(ans) == len(set(ans))
    )


# =================================================
# ⭐ 只需要改这两处 ⭐
# =================================================

PAGE_URL = "https://www.yuketang.cn/v2/web/studentQuiz/4114223/2?pageIndex=1"

cookie_str = """csrftoken=7vQBFRlV6EcSD5IQCaeUQWbFeAQ0YXbp; sensorsdata2015jssdkcross=xxx; sessionid=1uqxd966bwhuvk5onta247ptduqci54l; django_language=zh-cn; platform_id=3; classroomId=28773638; classroom_id=28773638; uv_id=0; university_id=0"""

# 需要的关键字段
needed_keys = {"sessionid", "csrftoken", "classroom_id", "classroomId"}

cookies_dict = {}

for item in cookie_str.split("; "):
    if "=" in item:
        k, v = item.split("=", 1)
        if k in needed_keys:
            cookies_dict[k] = v

COOKIES_JSON = [
    {"name": k, "value": v, "domain": "www.yuketang.cn"}
    for k, v in cookies_dict.items()
]

# =================================================
# Session + Cookie
# =================================================
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json, text/plain, */*",
    "Referer": PAGE_URL,
})

for c in COOKIES_JSON:
    session.cookies.set(c["name"], c["value"], domain=c.get("domain", "www.yuketang.cn"))

# =================================================
# 解析 quiz_id
# =================================================
m = re.search(r"/studentQuiz/(\d+)", PAGE_URL)
if not m:
    raise RuntimeError("❌ URL 中未找到 quiz_id")
QUIZ_ID = int(m.group(1))

CLASSROOM_ID = session.cookies.get("classroom_id") or session.cookies.get("classroomId")
if not CLASSROOM_ID:
    raise RuntimeError("❌ Cookie 中未找到 classroom_id")
CLASSROOM_ID = int(CLASSROOM_ID)

# =================================================
# ① 获取试卷信息 + 答案
# =================================================
print("📥 获取试卷信息与答案...")
ANS_API = "https://www.yuketang.cn/v2/api/web/quiz/personal_result"

ans_resp = session.get(ANS_API, params={
    "classroom_id": CLASSROOM_ID,
    "quiz_id": QUIZ_ID
})
ans_resp.raise_for_status()

ans_data = ans_resp.json()["data"]
quiz_title = ans_data.get("title", f"quiz_{QUIZ_ID}").replace("/", "_")

objective = ans_data.get("objective_result_list", [])

answers = {}
problem_map = {}

for item in objective:
    idx = item["problem_index"]
    problem_map[idx] = item["problem_id"]
    answers[idx] = item.get("answer")

print(f"✔ 已获取 {len(problem_map)} 道题（来自成绩接口）")

# =================================================
# ② 扫描补漏 problem_id（关键兜底）
# =================================================
print("🔍 扫描补漏题目（problem_shape）...")

SHAPE_API = "https://www.yuketang.cn/v2/api/web/quiz/problem_shape"
known_ids = set(problem_map.values())
scanned_ids = set()

pid_min = min(known_ids)
pid_max = max(known_ids) + 20  # 稍微多扫一点，防止尾巴题

for pid in range(pid_min, pid_max):
    try:
        r = session.get(SHAPE_API, params={
            "classroom_id": CLASSROOM_ID,
            "quiz_id": QUIZ_ID,
            "problem_id": pid
        }, timeout=8)

        if r.status_code != 200:
            continue

        j = r.json()
        if j.get("errcode") == 0 and j.get("data", {}).get("Shapes"):
            scanned_ids.add(pid)

        time.sleep(0.15)
    except Exception:
        pass

missing_ids = scanned_ids - known_ids
if missing_ids:
    print("🧩 发现缺失题目：", sorted(missing_ids))
    max_index = max(problem_map.keys())
    for i, pid in enumerate(sorted(missing_ids), start=1):
        problem_map[max_index + i] = pid
        answers[max_index + i] = None
else:
    print("✔ 无缺失题目")

# =================================================
# ③ 下载并拼接题目（真正的一题一页）
# =================================================
print("📥 下载题目图片并生成页面...")
pages = []

for idx in sorted(problem_map):
    pid = problem_map[idx]
    print(f"▶ 第 {idx} 题（problem_id={pid}）")

    r = session.get(SHAPE_API, params={
        "classroom_id": CLASSROOM_ID,
        "quiz_id": QUIZ_ID,
        "problem_id": pid
    })

    if r.status_code != 200:
        print("  ❌ 获取失败")
        continue

    data = r.json().get("data", {})
    shapes = data.get("Shapes", [])
    if not shapes:
        print("  ⚠ 无图片，跳过")
        continue

    ANSWER_BAR = 180
    loaded_shapes = []

    option_font = get_font(36)
    answer_font = get_font(42)

    for s in shapes:
        resp = session.get(s["URL"])
        raw_img = Image.open(BytesIO(resp.content))

        if raw_img.mode == "P":
            try:
                raw_img.apply_transparency()
            except Exception:
                pass
            img = raw_img.convert("RGBA")
        else:
            img = raw_img.convert("RGBA")

        left = int(float(s["Left"]))
        top = int(float(s["Top"]))
        loaded_shapes.append((img, left, top))

    if not loaded_shapes:
        print("  ⚠ 无有效图片，跳过")
        continue

    # 按原始纵坐标排序
    loaded_shapes.sort(key=lambda x: x[2])

    answer_value = answers.get(idx)
    choice_mode = has_choice_answer(answer_value)

    # ===== 可调参数 =====
    LEFT_MARGIN = 90
    TOP_MARGIN = 50
    QUESTION_GAP = 40
    OPTION_GAP = 28
    LABEL_OFFSET = 42
    # ===================

    option_labels = ["A", "B", "C", "D", "E", "F", "G", "H"]

    if choice_mode and len(loaded_shapes) >= 2:
        # 选择题：第一个当题干，后面当选项
        question_img, _, _ = loaded_shapes[0]
        option_shapes = loaded_shapes[1:]

        # 计算高度
        current_y = TOP_MARGIN + question_img.height + QUESTION_GAP
        for img, left, top in option_shapes:
            current_y += img.height + OPTION_GAP

        max_width = max(
            [question_img.width] + [img.width for img, _, _ in option_shapes]
        )
        canvas_width = LEFT_MARGIN + max_width + 80
        canvas_height = current_y + ANSWER_BAR + 40

        canvas = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 255))
        draw = ImageDraw.Draw(canvas)

        # 贴题干
        current_y = TOP_MARGIN
        canvas.paste(question_img, (LEFT_MARGIN, current_y), question_img)
        current_y += question_img.height + QUESTION_GAP

        # 贴选项，并补 A/B/C/D
        for i, (img, left, top) in enumerate(option_shapes):
            label = option_labels[i] if i < len(option_labels) else f"选项{i+1}"

            label_x = LEFT_MARGIN - LABEL_OFFSET
            label_y = current_y + max(0, img.height // 4)

            draw.text((label_x, label_y), label, fill="black", font=option_font)
            canvas.paste(img, (LEFT_MARGIN, current_y), img)

            current_y += img.height + OPTION_GAP

    else:
        # 非选择题：所有 shape 按顺序排，不补选项
        current_y = TOP_MARGIN
        max_width = 0

        for img, left, top in loaded_shapes:
            current_y += img.height + OPTION_GAP
            max_width = max(max_width, img.width)

        canvas_width = LEFT_MARGIN + max_width + 80
        canvas_height = current_y + ANSWER_BAR + 40

        canvas = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 255))
        draw = ImageDraw.Draw(canvas)

        current_y = TOP_MARGIN
        for img, left, top in loaded_shapes:
            canvas.paste(img, (LEFT_MARGIN, current_y), img)
            current_y += img.height + OPTION_GAP

    answer_text = answers.get(idx)
    if answer_text is None or str(answer_text).strip() == "":
        answer_text = "—"

    draw.text(
        (30, current_y + 20),
        f"第 {idx} 题 正确答案：{answer_text}",
        fill="black",
        font=answer_font
    )

    pages.append(canvas.convert("RGB"))

# =================================================
# ④ 保存 PDF
# =================================================
if not pages:
    raise RuntimeError("❌ 没有生成任何页面")

OUT_PDF = f"{quiz_title}_一题一页_题目+答案.pdf"
pages[0].save(OUT_PDF, save_all=True, append_images=pages[1:])

print("\n🎉 完成！")
print(f"📄 文件名：{OUT_PDF}")
print(f"📘 共 {len(pages)} 页（一题一页）")
print("📍 保存路径：", os.path.abspath(OUT_PDF))