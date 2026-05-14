"""
publishers/etsy_printify.py — Printify product creation and Etsy publishing.

Printify handles Etsy integration natively — no Etsy API credentials needed.
After a product is created in Printify, one publish call pushes it directly to
the connected Etsy shop. The Printify product_id is returned as the
external_listing_id (there is no separate Etsy listing ID).

Credentials required (from .env):
  PRINTIFY_API_KEY  — Printify → My Profile → Connections → API Access
  PRINTIFY_SHOP_ID  — Printify → select your connected Etsy shop → URL ID

Blueprint / provider IDs can be overridden via env vars if you switch products:
  PRINTIFY_BLUEPRINT_ID  (default 68  = Ceramic Mug 11oz)
  PRINTIFY_PROVIDER_ID   (default 99  = Printify Choice)
"""

import base64
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

MODULE_NAME = "etsy_printify"
BASE_URL    = "https://api.printify.com/v1"

# ── Printify catalog defaults ─────────────────────────────────────────────────
# Blueprint 68 = Ceramic Mug 11oz  (verify at https://developers.printify.com/#catalog)
# Provider 99  = Printify Choice (routes to the fastest/cheapest available provider)
BLUEPRINT_ID   = int(os.getenv("PRINTIFY_BLUEPRINT_ID", "68"))
PRINT_PROVIDER = int(os.getenv("PRINTIFY_PROVIDER_ID",  "99"))

# Default variant: 11oz white mug, one size. Confirm variant ID by calling:
#   GET /v1/catalog/blueprints/{blueprint_id}/print_providers/{provider_id}/variants.json
DEFAULT_VARIANTS = [
    {"id": 18484, "price": 1999, "is_enabled": True},  # 11oz white, $19.99 retail
]

# Tell Printify to sync all fields when pushing to Etsy
PUBLISH_OPTIONS = {
    "title":             True,
    "description":       True,
    "images":            True,
    "variants":          True,
    "tags":              True,
    "keyFeatures":       True,
    "shipping_template": True,
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _headers() -> dict:
    key = os.getenv("PRINTIFY_API_KEY", "").strip()
    if not key:
        raise EnvironmentError("PRINTIFY_API_KEY not set in .env")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _shop_id() -> str:
    sid = os.getenv("PRINTIFY_SHOP_ID", "").strip()
    if not sid:
        raise EnvironmentError("PRINTIFY_SHOP_ID not set in .env")
    return sid


# ─── Step 1: upload image ─────────────────────────────────────────────────────

def upload_image(file_path: str) -> str:
    """
    Upload a design PNG to Printify's image library and return the image ID.
    Uses POST /v1/uploads/images.json with base64-encoded file contents.
    """
    data = Path(file_path).read_bytes()
    payload = {
        "file_name": Path(file_path).name,
        "contents":  base64.b64encode(data).decode(),
    }
    resp = requests.post(
        f"{BASE_URL}/uploads/images.json",
        headers=_headers(),
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    image_id = resp.json().get("id", "")
    print(f"[{MODULE_NAME}]   Image uploaded — printify_image_id: {image_id}")
    return image_id


# ─── Step 2: create product ───────────────────────────────────────────────────

def create_product(
    title: str,
    description: str,
    tags: list[str],
    image_file_path: str,
) -> str | None:
    """
    Create a Printify product from an uploaded design image.
    Returns the Printify product_id on success, None on failure.
    """
    shop_id  = _shop_id()
    image_id = upload_image(image_file_path)

    payload = {
        "title":             title,
        "description":       description,
        "tags":              tags[:13],
        "blueprint_id":      BLUEPRINT_ID,
        "print_provider_id": PRINT_PROVIDER,
        "variants":          DEFAULT_VARIANTS,
        "print_areas": [
            {
                "variant_ids": [v["id"] for v in DEFAULT_VARIANTS],
                "placeholders": [
                    {
                        "position": "front",
                        "images": [
                            {
                                "id":    image_id,
                                "x":    0.5,
                                "y":    0.5,
                                "scale": 1.0,
                                "angle": 0,
                            }
                        ],
                    }
                ],
            }
        ],
    }

    resp = requests.post(
        f"{BASE_URL}/shops/{shop_id}/products.json",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    product_id = resp.json().get("id", "")
    print(f"[{MODULE_NAME}]   Printify product created — product_id: {product_id}")
    return product_id


# ─── Step 3: publish to Etsy ──────────────────────────────────────────────────

def publish_to_etsy(product_id: str) -> bool:
    """
    Push a Printify product directly to the connected Etsy shop.
    Printify handles the Etsy listing creation — no Etsy API call needed.
    Returns True on success.
    """
    shop_id = _shop_id()
    resp = requests.post(
        f"{BASE_URL}/shops/{shop_id}/products/{product_id}/publish.json",
        headers=_headers(),
        json=PUBLISH_OPTIONS,
        timeout=30,
    )
    resp.raise_for_status()
    print(f"[{MODULE_NAME}]   Product {product_id} queued for Etsy publish via Printify")
    return True


# ─── Combined pipeline ────────────────────────────────────────────────────────

def publish_design(
    title: str,
    description: str,
    tags: list[str],
    image_file_path: str,
) -> str | None:
    """
    Full pipeline: upload image → create Printify product → publish to Etsy.

    Returns the Printify product_id (stored as external_listing_id in the
    listings table). Returns None on any failure.
    """
    try:
        product_id = create_product(title, description, tags, image_file_path)
        if not product_id:
            return None
        publish_to_etsy(product_id)
        return product_id
    except requests.HTTPError as e:
        status = e.response.status_code
        body   = e.response.text[:300]
        print(f"[{MODULE_NAME}]   Printify HTTP {status}: {body}")
        return None
    except Exception as e:
        print(f"[{MODULE_NAME}]   Error: {e}")
        return None
