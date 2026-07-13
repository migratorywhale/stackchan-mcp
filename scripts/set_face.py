#!/usr/bin/env python3
"""
set_face.py — 快速替换 Stack-chan 表情图

用法（单文件模式）：
  python3 set_face.py path/to/image.png
  → 把这张图 resize 到 320×240 后覆盖全部 7 个表情文件

用法（目录模式）：
  python3 set_face.py path/to/faces/
  → 扫描目录，按文件名关键词匹配 7 个表情（calm/thinking/happy/sleepy/shy/smug/pouty）
  → 只替换匹配到的表情，允许部分替换

支持格式：PNG / JPG / JPEG / WEBP

在替换前会自动把原文件备份到 firmware/data/backup_faces/
（如果 backup 目录已存在则跳过备份，避免覆盖之前的备份）

依赖：Pillow
  pip install Pillow
"""

import argparse
import shutil
import sys
from pathlib import Path

# 7 个表情文件映射：表情关键词 → 实际文件名
FACE_MAP = {
    "calm": "A_calm_320x240.png",
    "thinking": "B_thinking_320x240.png",
    "happy": "C_happy_320x240.png",
    "sleepy": "D_sleepy_320x240.png",
    "shy": "E_shy_320x240.png",
    "smug": "F_smug_320x240.png",
    "pouty": "G_pouty_320x240.png",
}

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
TARGET_SIZE = (320, 240)

# 项目根目录相对于本脚本的位置（scripts/ → 项目根 → firmware/data/）
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "firmware" / "data"
BACKUP_DIR = DATA_DIR / "backup_faces"


def check_pillow():
    try:
        from PIL import Image  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        print("错误：未安装 Pillow。请先运行：", file=sys.stderr)
        print("  pip install Pillow", file=sys.stderr)
        sys.exit(1)


def resize_crop(src_path: Path, target_w: int, target_h: int):
    """把图片裁切+缩放到目标尺寸（保持比例，居中裁切，不拉伸）。"""
    from PIL import Image  # type: ignore[import-untyped]

    img = Image.open(src_path).convert("RGB")
    src_w, src_h = img.size
    ratio_w = target_w / src_w
    ratio_h = target_h / src_h
    ratio = max(ratio_w, ratio_h)  # 放大到刚好覆盖目标尺寸

    new_w = round(src_w * ratio)
    new_h = round(src_h * ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)

    # 居中裁切
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    img = img.crop((left, top, left + target_w, top + target_h))
    return img


def do_backup():
    """把 firmware/data/ 里的原始表情文件备份到 backup_faces/。
    如果 backup 目录已存在，跳过备份（避免覆盖之前的备份）。
    返回 (是否执行了备份, 备份了几个文件)。
    """
    if BACKUP_DIR.exists():
        print("[备份] backup_faces/ 目录已存在，跳过备份（保留上次备份）。")
        return False, 0

    BACKUP_DIR.mkdir(parents=True)
    count = 0
    for fname in FACE_MAP.values():
        src = DATA_DIR / fname
        if src.exists():
            shutil.copy2(src, BACKUP_DIR / fname)
            count += 1
    print(f"[备份] 已备份 {count} 个原始表情文件 → {BACKUP_DIR}")
    return True, count


def write_face(img, dest_path: Path):
    """把 PIL Image 存成 PNG 写入目标路径。"""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest_path, format="PNG")


def replace_all(src: Path):
    """单文件模式：把一张图覆盖全部 7 个表情。"""
    did_backup, _ = do_backup()
    img = resize_crop(src, *TARGET_SIZE)
    count = 0
    for key, fname in FACE_MAP.items():
        dest = DATA_DIR / fname
        write_face(img, dest)
        print(f"  [替换] {key:10s} → {fname}")
        count += 1
    print(f"\n完成：替换了 {count} 个表情文件，来源：{src.name}")
    if did_backup:
        print(f"原文件已备份至：{BACKUP_DIR}")


def replace_dir(src_dir: Path):
    """目录模式：按文件名关键词匹配，逐个替换。"""
    # 扫描目录里支持的图片文件
    candidates = [
        f for f in src_dir.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS
    ]

    if not candidates:
        print(f"错误：目录 {src_dir} 里没有找到 PNG/JPG/WEBP 文件。", file=sys.stderr)
        sys.exit(1)

    # 匹配：文件名（不含扩展名）包含表情关键词
    matches = {}  # key → src_path
    for f in candidates:
        stem = f.stem.lower()
        for key in FACE_MAP:
            if key in stem and key not in matches:
                matches[key] = f

    if not matches:
        print("错误：目录里的文件名没有匹配到任何表情关键词。", file=sys.stderr)
        print("关键词：" + ", ".join(FACE_MAP.keys()), file=sys.stderr)
        sys.exit(1)

    did_backup, _ = do_backup()
    count = 0
    for key, src in matches.items():
        fname = FACE_MAP[key]
        dest = DATA_DIR / fname
        img = resize_crop(src, *TARGET_SIZE)
        write_face(img, dest)
        print(f"  [替换] {key:10s} ← {src.name:30s} → {fname}")
        count += 1

    skipped = [k for k in FACE_MAP if k not in matches]
    print(f"\n完成：替换了 {count} 个表情文件。")
    if skipped:
        print(f"未匹配（保留原文件）：{', '.join(skipped)}")
    if did_backup:
        print(f"原文件已备份至：{BACKUP_DIR}")


def main():
    parser = argparse.ArgumentParser(
        prog="set_face.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input",
        metavar="IMAGE_OR_DIR",
        help="图片文件（PNG/JPG/WEBP）或包含表情图的目录",
    )
    args = parser.parse_args()

    check_pillow()

    src = Path(args.input).resolve()
    if not src.exists():
        print(f"错误：路径不存在：{src}", file=sys.stderr)
        sys.exit(1)

    if not DATA_DIR.exists():
        print(f"错误：找不到目标目录：{DATA_DIR}", file=sys.stderr)
        print("请确认脚本在 stackchan/scripts/ 下运行，且 firmware/data/ 存在。", file=sys.stderr)
        sys.exit(1)

    if src.is_dir():
        replace_dir(src)
    elif src.is_file():
        if src.suffix.lower() not in SUPPORTED_EXTS:
            print(f"错误：不支持的文件格式：{src.suffix}（支持：PNG/JPG/WEBP）", file=sys.stderr)
            sys.exit(1)
        replace_all(src)
    else:
        print(f"错误：{src} 既不是文件也不是目录。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
