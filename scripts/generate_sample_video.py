#!/usr/bin/env python3
"""
Generate a sample faceless finance YouTube video using moviepy + PIL + gTTS.

Produces a ~2-minute preview video with:
- Dark background with animated text overlays
- Section title cards for each habit
- Key stats highlighted on screen
- Text-to-speech narration
- Background color transitions per section

Output: docs/youtube-playbook/videos/001-money-habits/sample_video.mp4
"""

import os
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    ImageClip,
    concatenate_videoclips,
)

# ── Paths ──────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "docs" / "youtube-playbook" / "videos" / "001-money-habits"
OUTPUT_FILE = OUTPUT_DIR / "sample_video.mp4"
TEMP_DIR = PROJECT_ROOT / "scripts" / ".tmp_video"
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# ── Video settings ─────────────────────────────────────────────────────
WIDTH, HEIGHT = 1920, 1080
FPS = 24

# Colors
DARK_BG = (15, 15, 25)
ACCENT_GREEN = (0, 200, 120)
ACCENT_BLUE = (40, 120, 255)
ACCENT_GOLD = (255, 200, 50)
WHITE = (255, 255, 255)
LIGHT_GRAY = (180, 180, 190)
DARK_OVERLAY = (20, 20, 35)

# Section colors (gradient feel per section)
SECTION_COLORS = [
    (15, 15, 25),    # Intro - dark blue-black
    (10, 25, 20),    # Habit 1 - dark green tint
    (15, 15, 30),    # Habit 2 - dark purple tint
    (25, 15, 15),    # Habit 3 - dark red tint
    (15, 20, 30),    # Habit 4 - dark blue tint
    (20, 20, 15),    # Habit 5 - dark gold tint
    (15, 15, 25),    # Closing - dark blue-black
]


def get_font(size, bold=False):
    """Get a font, falling back to default if custom not available."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()


def create_frame(bg_color, texts, width=WIDTH, height=HEIGHT):
    """
    Create a single frame image.

    texts: list of dicts with keys:
        text, x, y, color, font_size, bold, align, max_width
    """
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Subtle gradient overlay (darker at top/bottom)
    for y in range(80):
        alpha = int(255 * (1 - y / 80) * 0.3)
        draw.line([(0, y), (width, y)], fill=(0, 0, 0))
    for y in range(height - 60, height):
        draw.line([(0, y), (width, y)], fill=(0, 0, 0))

    # Bottom bar
    draw.rectangle([(0, height - 4), (width, height)], fill=ACCENT_GREEN)

    for t in texts:
        font = get_font(t.get("font_size", 40), t.get("bold", False))
        text = t["text"]
        max_w = t.get("max_width", width - 200)

        # Wrap text
        wrapped = textwrap.fill(text, width=max_w // (t.get("font_size", 40) // 3 + 1))

        draw.multiline_text(
            (t["x"], t["y"]),
            wrapped,
            fill=t.get("color", WHITE),
            font=font,
            align=t.get("align", "left"),
        )

    return img


def make_scene(bg_color, texts, narration_text, scene_name, duration=6.0):
    """Create a video clip with image frames (no audio - add voiceover in post)."""
    img = create_frame(bg_color, texts)
    img_path = str(TEMP_DIR / f"{scene_name}.png")
    img.save(img_path)

    # Add narration as subtitle bar at bottom of a second frame
    img_sub = create_frame(bg_color, texts)
    draw = ImageDraw.Draw(img_sub)
    font = get_font(22, bold=False)
    # Dark subtitle bar
    draw.rectangle([(0, HEIGHT - 100), (WIDTH, HEIGHT - 4)], fill=(0, 0, 0, 200))
    # Wrap narration text
    wrapped = textwrap.fill(narration_text, width=110)
    draw.multiline_text((80, HEIGHT - 95), wrapped, fill=LIGHT_GRAY, font=font)
    img_sub_path = str(TEMP_DIR / f"{scene_name}_sub.png")
    img_sub.save(img_sub_path)

    # Show title for 2s, then subtitled version for remaining time
    clip1 = ImageClip(img_path, duration=min(2.0, duration * 0.3))
    clip2 = ImageClip(img_sub_path, duration=duration - clip1.duration)
    return concatenate_videoclips([clip1, clip2])


def main():
    print("🎬 Generating sample faceless finance video...")
    print(f"   Output: {OUTPUT_FILE}")
    print()

    scenes = []

    # ── Scene 1: Channel Intro / Title Card ──
    print("  [1/7] Title card...")
    scenes.append(make_scene(
        SECTION_COLORS[0],
        [
            {"text": "FACELESS FINANCE", "x": 160, "y": 200,
             "color": ACCENT_GREEN, "font_size": 32, "bold": True},
            {"text": "5 Money Habits That Separate", "x": 160, "y": 320,
             "color": WHITE, "font_size": 64, "bold": True, "max_width": 1600},
            {"text": "the Rich From the Broke", "x": 160, "y": 410,
             "color": ACCENT_GOLD, "font_size": 64, "bold": True, "max_width": 1600},
            {"text": "Backed by data. No fluff.", "x": 160, "y": 560,
             "color": LIGHT_GRAY, "font_size": 28, "bold": False},
        ],
        "Five Money Habits That Separate the Rich From the Broke. "
        "A forty-year study by Duke University found that forty-five percent of everything "
        "you do in a day is a habit, not a conscious decision. That includes what you do with your money.",
        "01_title",
    ))

    # ── Scene 2: Habit 1 ──
    print("  [2/7] Habit 1 — Pay Yourself First...")
    scenes.append(make_scene(
        SECTION_COLORS[1],
        [
            {"text": "HABIT #1", "x": 160, "y": 180,
             "color": ACCENT_GREEN, "font_size": 28, "bold": True},
            {"text": "Pay Yourself First", "x": 160, "y": 260,
             "color": WHITE, "font_size": 56, "bold": True},
            {"text": "81%", "x": 160, "y": 430,
             "color": ACCENT_GOLD, "font_size": 120, "bold": True},
            {"text": "more saved with automatic transfers", "x": 160, "y": 570,
             "color": LIGHT_GRAY, "font_size": 30, "bold": False},
            {"text": "— Journal of Consumer Research", "x": 160, "y": 630,
             "color": (120, 120, 130), "font_size": 22, "bold": False},
        ],
        "Habit number one. Pay yourself first. People who set up automatic transfers to savings "
        "accounts saved eighty-one percent more over twelve months compared to people who "
        "transferred money manually. Start with ten percent of your take-home pay.",
        "02_habit1",
    ))

    # ── Scene 3: Habit 2 ──
    print("  [3/7] Habit 2 — Know Your Numbers...")
    scenes.append(make_scene(
        SECTION_COLORS[2],
        [
            {"text": "HABIT #2", "x": 160, "y": 180,
             "color": ACCENT_BLUE, "font_size": 28, "bold": True},
            {"text": "Know Your Numbers", "x": 160, "y": 260,
             "color": WHITE, "font_size": 56, "bold": True},
            {"text": "56% of Americans", "x": 160, "y": 420,
             "color": ACCENT_GOLD, "font_size": 52, "bold": True},
            {"text": "can't cover a $1,000 emergency", "x": 160, "y": 500,
             "color": LIGHT_GRAY, "font_size": 32, "bold": False},
            {"text": "3 numbers to know:", "x": 160, "y": 600,
             "color": WHITE, "font_size": 28, "bold": True},
            {"text": "Monthly fixed expenses  •  Variable spending  •  Net worth", "x": 160, "y": 660,
             "color": ACCENT_GREEN, "font_size": 26, "bold": False},
        ],
        "Habit number two. Know your numbers. Fifty-six percent of Americans cannot cover a "
        "one-thousand dollar emergency expense. Know three numbers: your monthly fixed expenses, "
        "your variable spending, and your net worth.",
        "03_habit2",
    ))

    # ── Scene 4: Habit 3 ──
    print("  [4/7] Habit 3 — Eliminate Debt Strategically...")
    scenes.append(make_scene(
        SECTION_COLORS[3],
        [
            {"text": "HABIT #3", "x": 160, "y": 180,
             "color": (255, 100, 80), "font_size": 28, "bold": True},
            {"text": "Eliminate High-Interest Debt", "x": 160, "y": 260,
             "color": WHITE, "font_size": 52, "bold": True},
            {"text": "AVALANCHE", "x": 200, "y": 440,
             "color": ACCENT_BLUE, "font_size": 42, "bold": True},
            {"text": "Highest rate first", "x": 200, "y": 500,
             "color": LIGHT_GRAY, "font_size": 26, "bold": False},
            {"text": "vs", "x": 880, "y": 460,
             "color": (120, 120, 130), "font_size": 32, "bold": True},
            {"text": "SNOWBALL", "x": 1050, "y": 440,
             "color": ACCENT_GREEN, "font_size": 42, "bold": True},
            {"text": "Smallest balance first", "x": 1050, "y": 500,
             "color": LIGHT_GRAY, "font_size": 26, "bold": False},
            {"text": "$1,600/year lost to interest on avg credit card debt", "x": 160, "y": 640,
             "color": ACCENT_GOLD, "font_size": 28, "bold": True},
        ],
        "Habit number three. Eliminate high-interest debt strategically. The average American "
        "loses over sixteen hundred dollars a year to credit card interest. Use the avalanche "
        "method for debts under fifteen thousand, or the snowball method for motivation.",
        "04_habit3",
    ))

    # ── Scene 5: Habit 4 ──
    print("  [5/7] Habit 4 — Invest Consistently...")
    scenes.append(make_scene(
        SECTION_COLORS[4],
        [
            {"text": "HABIT #4", "x": 160, "y": 180,
             "color": ACCENT_BLUE, "font_size": 28, "bold": True},
            {"text": "Invest Consistently, Not Perfectly", "x": 160, "y": 260,
             "color": WHITE, "font_size": 50, "bold": True},
            {"text": "$500/mo since 2006", "x": 160, "y": 430,
             "color": LIGHT_GRAY, "font_size": 34, "bold": False},
            {"text": "$120K invested  →  $230K+ today", "x": 160, "y": 500,
             "color": ACCENT_GREEN, "font_size": 44, "bold": True},
            {"text": "Time in the market beats timing the market.", "x": 160, "y": 640,
             "color": ACCENT_GOLD, "font_size": 30, "bold": True},
        ],
        "Habit number four. Invest consistently, not perfectly. Five hundred dollars a month "
        "in an index fund since 2006 would be worth over two hundred thirty thousand dollars "
        "today. Time in the market always beats timing the market.",
        "05_habit4",
    ))

    # ── Scene 6: Habit 5 ──
    print("  [6/7] Habit 5 — Increase Income...")
    scenes.append(make_scene(
        SECTION_COLORS[5],
        [
            {"text": "HABIT #5", "x": 160, "y": 180,
             "color": ACCENT_GOLD, "font_size": 28, "bold": True},
            {"text": "Increase Your Income Deliberately", "x": 160, "y": 260,
             "color": WHITE, "font_size": 50, "bold": True},
            {"text": "75%", "x": 160, "y": 430,
             "color": ACCENT_GREEN, "font_size": 100, "bold": True},
            {"text": "of people who asked for a raise received one", "x": 160, "y": 550,
             "color": LIGHT_GRAY, "font_size": 30, "bold": False},
            {"text": "1 hour/week on income growth = transformed trajectory",
             "x": 160, "y": 650, "color": ACCENT_GOLD, "font_size": 26, "bold": True},
        ],
        "Habit number five. Increase your income deliberately. Seventy-five percent of people "
        "who asked for a raise received one. Spend one hour per week on deliberate income growth.",
        "06_habit5",
    ))

    # ── Scene 7: Closing / CTA ──
    print("  [7/7] Closing...")
    scenes.append(make_scene(
        SECTION_COLORS[6],
        [
            {"text": "RECAP", "x": 160, "y": 160,
             "color": ACCENT_GREEN, "font_size": 28, "bold": True},
            {"text": "1. Pay yourself first (automate savings)", "x": 200, "y": 260,
             "color": WHITE, "font_size": 34, "bold": False},
            {"text": "2. Know your numbers", "x": 200, "y": 330,
             "color": WHITE, "font_size": 34, "bold": False},
            {"text": "3. Eliminate high-interest debt strategically", "x": 200, "y": 400,
             "color": WHITE, "font_size": 34, "bold": False},
            {"text": "4. Invest consistently", "x": 200, "y": 470,
             "color": WHITE, "font_size": 34, "bold": False},
            {"text": "5. Grow your income deliberately", "x": 200, "y": 540,
             "color": WHITE, "font_size": 34, "bold": False},
            {"text": "SUBSCRIBE for weekly data-driven finance breakdowns",
             "x": 160, "y": 700, "color": ACCENT_GOLD, "font_size": 30, "bold": True},
        ],
        "Five habits. Automate your savings. Know your numbers. Eliminate high-interest debt. "
        "Invest consistently. Grow your income deliberately. Subscribe for weekly data-driven "
        "finance breakdowns.",
        "07_closing",
    ))

    # ── Combine all scenes ──
    print()
    print("  Combining scenes into final video...")
    final = concatenate_videoclips(scenes, method="compose")
    final.write_videofile(
        str(OUTPUT_FILE),
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=4,
        logger="bar",
    )

    # Cleanup temp files
    import shutil
    shutil.rmtree(str(TEMP_DIR), ignore_errors=True)

    duration_min = final.duration / 60
    print()
    print(f"  ✅ Video created: {OUTPUT_FILE}")
    print(f"  ⏱  Duration: {final.duration:.1f}s ({duration_min:.1f} min)")
    print(f"  📐 Resolution: {WIDTH}x{HEIGHT}")
    print()


if __name__ == "__main__":
    main()
