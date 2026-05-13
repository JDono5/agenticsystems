"""
Generate 6 Fiverr portfolio thumbnails across different niches.
Saves to assets/fiverr_samples/ at 1536x1024 (native gpt-image-1 landscape size).
"""
import os, sys, base64
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import openai
from pathlib import Path

client  = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
out_dir = Path(__file__).parent / "fiverr_samples"
out_dir.mkdir(parents=True, exist_ok=True)

PROMPTS = [
    (
        "portfolio_gaming.png",
        "YouTube thumbnail. Bold text: 'I FOUND THE RAREST ITEM IN THE GAME'. "
        "Dark background with purple and blue neon glow. Shocked gamer face on left "
        "pointing at a glowing rare item on the right. Epic dramatic lighting. "
        "Bold white and yellow text with thick outlines. Professional gaming YouTube thumbnail. "
        "100px margin from all edges, nothing cropped.",
    ),
    (
        "portfolio_finance.png",
        "YouTube thumbnail. Bold text: 'HOW I SAVED $10,000 IN 6 MONTHS'. "
        "Clean split background dark left bright green right. Professional young person "
        "on right side smiling and pointing at text. Bold white and yellow text on left. "
        "Finance/money YouTube channel style. Professional, high contrast. "
        "100px margin from all edges, nothing cropped.",
    ),
    (
        "portfolio_fitness.png",
        "YouTube thumbnail. Bold text: '30 DAY TRANSFORMATION RESULTS'. "
        "High energy orange and black background. Athletic person flexing on right side. "
        "Bold white text with orange accents on left. Fitness motivation YouTube thumbnail style. "
        "Dramatic lighting, professional. "
        "100px margin from all edges, nothing cropped.",
    ),
    (
        "portfolio_food.png",
        "YouTube thumbnail. Bold text: 'I MADE GORDON RAMSAY'S SECRET RECIPE'. "
        "Warm red and orange background. Person looking shocked holding a delicious looking dish. "
        "Bold white text. Food/cooking YouTube thumbnail style. Warm appetizing feel, professional. "
        "100px margin from all edges, nothing cropped.",
    ),
    (
        "portfolio_tech.png",
        "YouTube thumbnail. Bold text: 'THIS AI TOOL CHANGED EVERYTHING'. "
        "Dark navy background with glowing blue circuit lines. Person looking amazed at a glowing "
        "laptop screen. Bold white and cyan text. Tech YouTube channel style. "
        "Clean modern professional. "
        "100px margin from all edges, nothing cropped.",
    ),
    (
        "portfolio_lifestyle.png",
        "YouTube thumbnail. Bold text: 'I QUIT MY JOB AND THIS HAPPENED'. "
        "Bright airy background split warm yellow and white. Young person looking confident and happy. "
        "Bold dark text with yellow accents. Lifestyle vlog YouTube thumbnail style. "
        "Relatable, warm, professional. "
        "100px margin from all edges, nothing cropped.",
    ),
]

COST_PER_IMAGE = 0.040
total_cost = 0.0

print(f"[portfolio] --- Generating {len(PROMPTS)} Fiverr portfolio thumbnails ---")

for filename, prompt in PROMPTS:
    niche = filename.replace("portfolio_", "").replace(".png", "")
    print(f"\n[portfolio] Generating: {niche} ({filename}) ...")
    try:
        resp = client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            n=1,
            size="1536x1024",
            quality="high",
        )
        png_bytes = base64.b64decode(resp.data[0].b64_json)
        out_path = out_dir / filename
        out_path.write_bytes(png_bytes)
        total_cost += COST_PER_IMAGE
        print(f"[portfolio] Saved: {out_path}")
    except Exception as e:
        print(f"[portfolio] ERROR generating {filename}: {e}")

print(f"\n[portfolio] --- Done ---")
print(f"[portfolio] Files in: {out_dir}")
print(f"[portfolio] Approx cost: ${total_cost:.3f}")
for f in sorted(out_dir.glob("portfolio_*.png")):
    print(f"  {f}")
