"""
One-shot script: generate 3 YouTube thumbnail samples for Fiverr portfolio.
Saves to assets/fiverr_samples/ at 1280x720.
"""
import os, sys, base64, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import openai
from PIL import Image
from pathlib import Path

client  = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
out_dir = Path(__file__).parent / "fiverr_samples"
out_dir.mkdir(parents=True, exist_ok=True)

_COMPOSITION_RULES = (
    "IMPORTANT COMPOSITION RULES: All text must be fully visible with at least 80px "
    "margin from every edge. Nothing should be cut off or cropped at any border. "
    "The full thumbnail must be contained within the frame with clear breathing room "
    "on all sides."
)

PROMPTS = [
    (
        "thumb_30days.png",
        (
            "YouTube thumbnail. Bold text on left: 'I ATE NOTHING BUT CHIPOTLE FOR 30 DAYS'. "
            "Shocked white male in his 20s on right side, mouth open, eyes wide, pointing at camera. "
            "Red and black split background. White and yellow bold text, thick black outlines. "
            "Looks exactly like a real MrBeast style YouTube thumbnail. "
            "Professional, high contrast, no borders. " + _COMPOSITION_RULES
        ),
    ),
    (
        "thumb_10things.png",
        (
            "YouTube thumbnail. Text: '10 THINGS CHATGPT WON'T TELL YOU'. "
            "Dark navy background. Glowing ChatGPT logo on right with a red X through it. "
            "Bold white and cyan text on left. Looks like a real tech YouTube channel thumbnail. "
            "High contrast, clean composition, professional. " + _COMPOSITION_RULES
        ),
    ),
    (
        "thumb_500day.png",
        (
            "YouTube thumbnail. Text: 'I BUILT A DROPSHIPPING STORE IN 24 HOURS'. "
            "Young entrepreneur at a laptop, looking excited, pointing at a laptop screen "
            "showing dollar signs. Split background: dark left side, bright green right side. "
            "Bold white text with yellow dollar amount. "
            "Looks like a real business/finance YouTube thumbnail. Professional, click-worthy. "
            + _COMPOSITION_RULES
        ),
    ),
]

total_cost = 0.0
print("[generate] --- 3 YouTube thumbnail samples ---")

for filename, prompt in PROMPTS:
    print(f"\n[generate]  Generating: {filename} ...")
    resp = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        n=1,
        size="1536x1024",   # max landscape size gpt-image-1 supports (1792x1024 not valid); resized below
        quality="high",
    )
    png_bytes = base64.b64decode(resp.data[0].b64_json)

    # Crop/resize to exact 1280x720 (YouTube standard)
    img = Image.open(io.BytesIO(png_bytes))
    img = img.resize((1280, 720), Image.LANCZOS)

    out_path = out_dir / filename
    img.save(str(out_path), "PNG")
    total_cost += 0.040
    print(f"[generate]  Saved: {out_path}  ({img.size[0]}x{img.size[1]})")

print(f"\n[generate] Done. 3 files in {out_dir}")
print(f"[generate] Approx cost: ${total_cost:.3f}")
