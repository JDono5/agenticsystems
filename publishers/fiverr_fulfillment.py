"""
publishers/fiverr_fulfillment.py — Main Fiverr fulfillment orchestrator.

Supports three gig types with automatic routing:
  thumbnail  — YouTube thumbnail (1536x1024, FiverrPromptBuilder + FiverrQA)
  logo       — Minimalist brand logo (1024x1024, LogoPromptBuilder + LogoQA)
  social_media — Instagram post graphic (1024x1024, SocialPromptBuilder + SocialQA)

Full pipeline per order:
  parse -> detect gig type -> download attachments -> analyze buyer images ->
  build prompt -> generate image (gpt-image-1) -> QA loop (up to 3 attempts) ->
  save to disk -> log to Supabase -> send delivery email -> log to memory

Also handles IMAP polling and review detection.
"""

import base64
import email as email_lib
import imaplib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai
from dotenv import load_dotenv

from core.cost_logger import log_cost, calc_openai_cost
from core.error_handler import api_call_with_retry
from core.spend_monitor import check_cap
from core.supabase_client import save_design
from core.emailer import send_alert

from publishers.fiverr_parser              import parse_order, is_revision, extract_attachments, detect_package_tier
from publishers.fiverr_analyzer            import analyze_buyer_images, is_face_photo
from publishers.fiverr_prompt_builder      import build_thumbnail_prompt, build_background_only_prompt
from publishers.fiverr_qa                  import qa_thumbnail
from publishers.fiverr_learning            import log_order_to_memory, log_review_to_memory, get_niche_memory
from publishers.fiverr_logo_prompt_builder import build_logo_prompt
from publishers.fiverr_logo_qa             import evaluate as logo_qa_evaluate
from publishers.fiverr_social_prompt_builder import build_social_prompt
from publishers.fiverr_social_qa           import evaluate as social_qa_evaluate

load_dotenv()

MODULE_NAME        = "fiverr_fulfillment"
IMAGE_MODEL        = "gpt-image-1"
VISION_MODEL       = "gpt-4o"
THUMBNAIL_COST     = 0.040      # gpt-image-1 high quality per image
MAX_QA_RETRIES     = 3          # thumbnails + social media
MAX_QA_RETRIES_LOGO = 5         # logos need more attempts — white background is hard to enforce
ROOT               = Path(__file__).parent.parent


# ─── Gig type detection ───────────────────────────────────────────────────────

_LOGO_KEYWORDS    = {"logo", "brand", "icon", "wordmark", "brand identity", "logotype"}
_SOCIAL_KEYWORDS  = {"instagram", "post", "social", "graphic", "content", "ig post",
                     "social media", "social post", "content graphic"}

def detect_gig_type(order: dict) -> str:
    """
    Detect which Fiverr gig an order belongs to by scanning all text fields.

    Precedence:
      1. Already set 'gig_type' key in the order (from Claude parser or email header)
      2. Keyword scan of requirements, subject, video_title, buyer_answers
      3. Default to 'thumbnail'
    """
    if order.get("gig_type") in ("logo", "social_media", "thumbnail"):
        return order["gig_type"]

    search_text = " ".join(filter(None, [
        order.get("requirements", ""),
        order.get("subject", ""),
        order.get("video_title", ""),
        order.get("raw_requirements", ""),
        str(order.get("buyer_answers", "")),
    ])).lower()

    for kw in _LOGO_KEYWORDS:
        if kw in search_text:
            return "logo"
    for kw in _SOCIAL_KEYWORDS:
        if kw in search_text:
            return "social_media"
    return "thumbnail"


# ─── Image generation ─────────────────────────────────────────────────────────

def _generate_image(prompt: str, size: str = "1536x1024") -> bytes:
    """Generate an image via gpt-image-1 and return raw PNG bytes."""
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        n=1,
        size=size,
        quality="high",
    )
    return base64.b64decode(response.data[0].b64_json)


def _generate_thumbnail(prompt: str) -> bytes:
    """Backwards-compatible wrapper for thumbnail generation (1536x1024)."""
    return _generate_image(prompt, size="1536x1024")


def _composite_buyer_photo(
    background_bytes: bytes,
    buyer_photo_path: str,
    order: dict,
) -> bytes | None:
    """
    Stage 2 of Case A: composite buyer's actual photo onto the background.
    Uses OpenAI images.edit endpoint. Returns PNG bytes on success, None on failure.
    """
    niche  = order.get("channel_niche", "lifestyle")
    text   = order.get("text_to_include") or order.get("video_title", "")
    prompt = (
        f"YouTube thumbnail for {niche} channel. "
        f"The person in this photo appears on the right third of a 1536x1024 thumbnail. "
        f"Large bold text '{text}' on the left side. "
        f"Keep the person's face and appearance exactly as in the original photo. "
        f"Professional, high-contrast, click-worthy composition."
    )
    try:
        client  = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        with open(buyer_photo_path, "rb") as photo_file:
            response = client.images.edit(
                model=IMAGE_MODEL,
                image=photo_file,
                prompt=prompt,
                n=1,
                size="1536x1024",
            )
        return base64.b64decode(response.data[0].b64_json)
    except Exception as e:
        print(f"[{MODULE_NAME}]   Buyer photo composite failed: {e}")
        return None


# ─── Delivery email ───────────────────────────────────────────────────────────

def _send_delivery_email(
    order: dict,
    file_paths: list[str],
    total_cost: float,
    used_buyer_photo: bool = False,
    composite_failed: bool = False,
) -> None:
    """
    Email the generated thumbnail(s) as attachments to REPORT_EMAIL.
    Subject: FIVERR DELIVERY READY: {order_id} — {video_title}
    Gracefully skips if SendGrid is not configured.
    """
    report_email = os.getenv("REPORT_EMAIL", "").strip()
    sg_key       = os.getenv("SENDGRID_API_KEY", "").strip()

    if not report_email or not sg_key:
        print(f"[{MODULE_NAME}]   Delivery email skipped (SendGrid not configured).")
        print(f"[{MODULE_NAME}]   Files ready at: {', '.join(file_paths)}")
        return

    order_id    = order.get("order_id", "unknown")
    video_title = order.get("video_title", "")
    buyer       = order.get("buyer_username", "unknown")
    package     = order.get("package_tier", "basic")
    niche       = order.get("channel_niche", "")

    photo_note = ""
    if used_buyer_photo:
        photo_note = (
            "\nNOTE: Your thumbnail was created using your provided photo. "
            "Background and text are AI-generated and your image was composited in. "
            "If anything looks off reply with specific feedback and we will fix it "
            "within 24 hours.\n"
        )
    elif composite_failed:
        photo_note = (
            "\nNOTE: We used an illustrated version of your photo - the automatic "
            "photo composite failed. Reply if you want your exact photo manually "
            "composited and we will fix it within 24 hours.\n"
        )

    body = (
        f"Fiverr thumbnail order ready for delivery.\n\n"
        f"Order ID:   {order_id}\n"
        f"Buyer:      {buyer}\n"
        f"Package:    {package}\n"
        f"Video:      {video_title}\n"
        f"Niche:      {niche}\n"
        f"Files:      {len(file_paths)} thumbnail(s)\n"
        f"AI Cost:    ${total_cost:.4f}\n"
        f"{photo_note}\n"
        f"File locations:\n" + "\n".join(f"  {p}" for p in file_paths) + "\n\n"
        "To deliver: go to fiverr.com/orders, open this order, click Deliver, "
        "and attach the PNG file(s) above."
    )

    try:
        import sendgrid
        from sendgrid.helpers.mail import (
            Mail, Attachment, FileContent, FileName, FileType, Disposition,
        )
        sg  = sendgrid.SendGridAPIClient(api_key=sg_key)
        msg = Mail(
            from_email="noreply@agent.local",
            to_emails=report_email,
            subject=f"FIVERR DELIVERY READY: {order_id} - {video_title}",
            plain_text_content=body,
        )
        for fp in file_paths:
            png_bytes  = Path(fp).read_bytes()
            attachment = Attachment(
                FileContent(base64.b64encode(png_bytes).decode()),
                FileName(Path(fp).name),
                FileType("image/png"),
                Disposition("attachment"),
            )
            msg.add_attachment(attachment)

        resp = sg.send(msg)
        print(f"[{MODULE_NAME}]   Delivery email sent ({resp.status_code}) -> {report_email}")
    except Exception as e:
        print(f"[{MODULE_NAME}]   Delivery email failed: {e}")
        print(f"[{MODULE_NAME}]   Files: {', '.join(file_paths)}")


# ─── Main fulfillment ─────────────────────────────────────────────────────────

def fulfill_order(order: dict) -> bool:
    """
    Full Fiverr order fulfillment with gig-type routing.

    Detects gig type (thumbnail / logo / social_media) from the order and
    dispatches to the appropriate prompt builder and QA module.

      1. Detect gig type and set order['gig_type']
      2. Read niche/style memory for any prior successful styles
      3. Analyze buyer images if present
      4. Build prompt using gig-specific builder
      5. Generate image (size depends on gig type) with QA loop
      6. Save PNG to designs/fiverr/{date}/{order_id}.png
      7. Log to Supabase / send delivery email
      8. Write to learning memory

    Returns True on success.
    """
    if not check_cap():
        print(f"[{MODULE_NAME}] Spend cap reached — cannot fulfill order.")
        return False

    # Detect and lock in the gig type
    gig_type = detect_gig_type(order)
    order["gig_type"] = gig_type

    order_id    = order.get("order_id", f"fvr_{uuid.uuid4().hex[:8]}")
    video_title = order.get("video_title", "YouTube Video")
    niche       = order.get("channel_niche", "lifestyle")
    package     = order.get("package_tier", "basic")

    print(f"\n[{MODULE_NAME}] Fulfilling order {order_id} ({package}) [{gig_type}]")
    print(f"[{MODULE_NAME}]   Video: \"{video_title}\"")
    print(f"[{MODULE_NAME}]   Niche: {niche}")

    total_cost = 0.0

    # ── 1. Read niche memory ───────────────────────────────────────────────
    niche_memory   = get_niche_memory(niche)
    prior_orders   = niche_memory.get("orders", [])
    if prior_orders:
        print(f"[{MODULE_NAME}]   Found {len(prior_orders)} prior order(s) for niche '{niche}' in memory")

    # ── 2. Analyze buyer images — distinguish Case A (face photo) vs Case B (reference) ──
    buyer_images     = order.get("buyer_images", []) or []
    style_context: dict = {}
    buyer_face_paths: list[str] = []   # Case A: actual face photos for compositing
    reference_paths:  list[str] = []   # Case B: reference thumbnails for style only

    if buyer_images:
        print(f"[{MODULE_NAME}]   Analyzing {len(buyer_images)} buyer image(s)...")
        for img_path in buyer_images:
            try:
                if is_face_photo(img_path):
                    buyer_face_paths.append(img_path)
                    print(f"[{MODULE_NAME}]   -> Face photo detected: {Path(img_path).name}")
                else:
                    reference_paths.append(img_path)
                    print(f"[{MODULE_NAME}]   -> Reference image detected: {Path(img_path).name}")
            except Exception as e:
                print(f"[{MODULE_NAME}]   Image classification failed: {e}")
                reference_paths.append(img_path)

        # Analyze reference images (Case B) for style context
        if reference_paths:
            style_context = api_call_with_retry(
                lambda: analyze_buyer_images(reference_paths),
                max_retries=2,
                agent_name=MODULE_NAME,
            ) or {}
            img_cost = style_context.get("analysis_cost", 0.0)
            total_cost += img_cost
            if img_cost:
                log_cost(MODULE_NAME, "openai", VISION_MODEL,
                         tokens_used=0, cost_usd=img_cost)
            print(f"[{MODULE_NAME}]   Reference style: {style_context.get('aesthetic', 'extracted')}")
    else:
        print(f"[{MODULE_NAME}]   No buyer images - using niche defaults")

    # ── 3. Determine quantity + image size from gig config ─────────────────
    qty        = 1
    image_size = "1536x1024"   # thumbnail default

    if gig_type == "logo":
        image_size = "1024x1024"
        try:
            cfg = json.loads((ROOT / "platform_config" / "fiverr_logo.json").read_text())
            qty = cfg.get("package_tiers", {}).get(package, {}).get("deliverables", 1)
        except Exception:
            qty = {"basic": 1, "standard": 3, "premium": 5}.get(package, 1)
    elif gig_type == "social_media":
        image_size = "1024x1024"
        try:
            cfg = json.loads((ROOT / "platform_config" / "fiverr_social.json").read_text())
            qty = cfg.get("package_tiers", {}).get(package, {}).get("deliverables", 3)
        except Exception:
            qty = {"basic": 3, "standard": 8, "premium": 20}.get(package, 3)
    else:
        try:
            cfg = json.loads((ROOT / "platform_config" / "fiverr.json").read_text())
            qty = cfg.get("gig", {}).get("packages", {}).get(package, {}).get("quantity", 1)
        except Exception:
            qty = {"basic": 1, "standard": 2, "premium": 3}.get(package, 1)

    print(f"[{MODULE_NAME}]   Generating {qty} image(s) for {package} {gig_type} package")

    date_str   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    output_dir = ROOT / "designs" / "fiverr" / date_str
    output_dir.mkdir(parents=True, exist_ok=True)

    saved_paths:           list[str] = []
    last_prompt                     = ""
    used_buyer_photo                = False
    buyer_photo_composite_failed    = False

    # ── 4-5. Generate + QA loop ────────────────────────────────────────────
    for img_num in range(qty):
        revision_feedback = order.get("revision_feedback") if order.get("revision_of") else None
        image_bytes       = None
        qa_fix            = revision_feedback  # carries QA-suggested fix across attempts

        print(f"[{MODULE_NAME}]   Image {img_num+1}/{qty} ({gig_type})")

        # ── Thumbnail Case A: buyer has a face photo to composite ──────────
        if gig_type == "thumbnail" and buyer_face_paths and order.get("has_face"):
            buyer_photo = buyer_face_paths[0]
            print(f"[{MODULE_NAME}]   Case A: buyer face photo — 2-stage pipeline")

            bg_prompt   = build_background_only_prompt(order, style_context)
            last_prompt = bg_prompt
            bg_bytes    = api_call_with_retry(
                lambda p=bg_prompt: _generate_image(p, image_size),
                max_retries=2, agent_name=MODULE_NAME,
            )
            if bg_bytes:
                log_cost(MODULE_NAME, "openai", IMAGE_MODEL,
                         tokens_used=0, cost_usd=THUMBNAIL_COST)
                total_cost += THUMBNAIL_COST

                composite = _composite_buyer_photo(bg_bytes, buyer_photo, order)
                if composite:
                    image_bytes          = composite
                    used_buyer_photo     = True
                    log_cost(MODULE_NAME, "openai", IMAGE_MODEL,
                             tokens_used=0, cost_usd=THUMBNAIL_COST)
                    total_cost += THUMBNAIL_COST
                    print(f"[{MODULE_NAME}]   Buyer photo composited successfully")
                else:
                    image_bytes                  = bg_bytes
                    buyer_photo_composite_failed = True
                    print(f"[{MODULE_NAME}]   Composite failed — using background-only fallback")

        # ── Normal generation path (all gig types) ─────────────────────────
        if not image_bytes:
            retries = MAX_QA_RETRIES_LOGO if gig_type == "logo" else MAX_QA_RETRIES
            for attempt in range(retries):
                # Build prompt via gig-specific builder
                if gig_type == "logo":
                    prompt = build_logo_prompt(order, rejection_feedback=qa_fix or "")
                elif gig_type == "social_media":
                    # Enrich order with theme label for QA context
                    from publishers.fiverr_social_prompt_builder import generate_post_theme
                    theme = generate_post_theme(order, img_num)
                    order["post_theme"]   = theme["label"]
                    order["theme_label"]  = theme["label"]
                    prompt = build_social_prompt(order, post_index=img_num, rejection_feedback=qa_fix or "")
                else:
                    prompt = build_thumbnail_prompt(order, style_context, qa_fix)
                last_prompt = prompt

                print(f"[{MODULE_NAME}]   Attempt {attempt+1}/{retries}")
                image_bytes = api_call_with_retry(
                    lambda p=prompt: _generate_image(p, image_size),
                    max_retries=2, agent_name=MODULE_NAME,
                )
                if not image_bytes:
                    print(f"[{MODULE_NAME}]   Generation failed on attempt {attempt+1}")
                    continue

                log_cost(MODULE_NAME, "openai", IMAGE_MODEL,
                         tokens_used=0, cost_usd=THUMBNAIL_COST)
                total_cost += THUMBNAIL_COST

                # Save temp file for QA
                tmp_path = output_dir / f"_qa_tmp_{uuid.uuid4().hex[:6]}.png"
                tmp_path.write_bytes(image_bytes)

                # Run gig-specific QA
                if gig_type == "logo":
                    qa_fn = lambda p=str(tmp_path): logo_qa_evaluate(p, order)
                elif gig_type == "social_media":
                    qa_fn = lambda p=str(tmp_path): social_qa_evaluate(p, order)
                else:
                    qa_fn = lambda p=str(tmp_path): qa_thumbnail(p, order)

                qa = api_call_with_retry(
                    qa_fn, max_retries=2, agent_name=MODULE_NAME,
                ) or {"pass": True, "reason": "QA skipped", "suggested_fix": None,
                      "cost": 0.0, "tokens": 0}

                qa_cost = qa.get("cost", 0.0)
                total_cost += qa_cost
                if qa_cost:
                    log_cost(MODULE_NAME, "openai", VISION_MODEL,
                             tokens_used=qa.get("tokens", 0), cost_usd=qa_cost)

                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

                if qa.get("pass"):
                    print(f"[{MODULE_NAME}]   QA passed: {qa.get('reason', 'ok')}")
                    break
                else:
                    qa_fix = qa.get("suggested_fix") or qa.get("reason", "fix composition issues")
                    print(f"[{MODULE_NAME}]   QA FAIL ({attempt+1}): {qa.get('reason')} -> fix: {qa_fix}")
                    image_bytes = None

        if not image_bytes:
            print(f"[{MODULE_NAME}]   Could not produce passing image {img_num+1}")
            continue

        # ── Save PNG ───────────────────────────────────────────────────────
        file_id   = f"{order_id}_{img_num+1}" if qty > 1 else order_id
        png_path  = output_dir / f"{file_id}.png"
        meta_path = output_dir / f"{file_id}.json"

        png_path.write_bytes(image_bytes)
        meta = {
            "order_id":      order_id,
            "gig_type":      gig_type,
            "video_title":   video_title,
            "channel_niche": niche,
            "package":       package,
            "prompt_used":   last_prompt[:1000],
            "style_context": {k: v for k, v in style_context.items() if k != "analysis_cost"},
            "cost":          total_cost,
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        }
        meta_path.write_text(json.dumps(meta, indent=2))
        saved_paths.append(str(png_path))
        print(f"[{MODULE_NAME}]   Saved: {png_path}")

        # ── Log to cost_log (Fiverr thumbnails skip the designs table —
        #    that table's brief_id is NOT NULL and is for Etsy POD only)
        log_cost(MODULE_NAME, "openai", IMAGE_MODEL,
                 tokens_used=0, cost_usd=0.0)   # already logged above; no-op here

    if not saved_paths:
        print(f"[{MODULE_NAME}]   Order {order_id} failed — no images generated ({gig_type})")
        return False

    # ── 6. Delivery email ──────────────────────────────────────────────────
    _send_delivery_email(
        order, saved_paths, total_cost,
        used_buyer_photo=used_buyer_photo,
        composite_failed=buyer_photo_composite_failed,
    )

    # ── 7. Write to learning memory ────────────────────────────────────────
    try:
        log_order_to_memory(order, last_prompt, style_context)
    except Exception as e:
        print(f"[{MODULE_NAME}]   Learning memory write failed: {e}")

    print(
        f"[{MODULE_NAME}]   Order {order_id} done — "
        f"{len(saved_paths)}/{qty} {gig_type} image(s), cost ${total_cost:.4f}"
    )
    return True


# ─── IMAP helpers ─────────────────────────────────────────────────────────────

def _imap_connect():
    """
    Return an authenticated imaplib.IMAP4_SSL connection using the Gmail /
    IMAP env vars, or None if credentials are not configured.
    Reads: GMAIL_IMAP_SERVER, GMAIL_IMAP_PORT, FIVERR_NOTIFICATION_EMAIL,
           GMAIL_APP_PASSWORD.
    Falls back to legacy IMAP_* names for backward compatibility.
    """
    imap_server = os.getenv("GMAIL_IMAP_SERVER") or os.getenv("IMAP_SERVER", "imap.gmail.com")
    imap_port   = int(os.getenv("GMAIL_IMAP_PORT") or os.getenv("IMAP_PORT", "993"))
    imap_email  = os.getenv("FIVERR_NOTIFICATION_EMAIL") or os.getenv("IMAP_EMAIL") or os.getenv("REPORT_EMAIL", "")
    imap_pass   = os.getenv("GMAIL_APP_PASSWORD") or os.getenv("IMAP_PASSWORD", "")

    if not imap_email or not imap_pass or imap_pass == "paste-your-16-char-password-here":
        return None, imap_email

    mail = imaplib.IMAP4_SSL(imap_server, imap_port)
    mail.login(imap_email, imap_pass)
    return mail, imap_email


# ─── IMAP order checking ──────────────────────────────────────────────────────

def check_fiverr_orders() -> list[dict]:
    """
    Connect to IMAP and collect unread Fiverr order notification emails.
    Returns a list of fully parsed order dicts (via fiverr_parser.parse_order).
    Marks each email as read after processing.
    """
    mail, imap_email = _imap_connect()
    if mail is None:
        print(f"[{MODULE_NAME}] IMAP not configured — set FIVERR_NOTIFICATION_EMAIL and GMAIL_APP_PASSWORD")
        return []

    orders: list[dict] = []
    try:
        mail.select("INBOX")

        _, data = mail.search(
            None, '(UNSEEN FROM "noreply@fiverr.com" SUBJECT "New Order")'
        )
        msg_ids = data[0].split()
        print(f"[{MODULE_NAME}] Found {len(msg_ids)} unread Fiverr order email(s)")

        for msg_id in msg_ids:
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw_email   = msg_data[0][1]
            msg         = email_lib.message_from_bytes(raw_email)

            subject = msg.get("Subject", "")
            body    = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

            # Download image attachments
            attachments = extract_attachments(msg)

            # Parse order with full intelligence
            order = parse_order(body, attachments)

            # Extract order_id from subject + body
            id_match = re.search(r"#?(FO\d{7,}|\d{7,})", subject + " " + body)
            order["order_id"]       = id_match.group(1) if id_match else f"fvr_{msg_id.decode()}"
            order["subject"]        = subject

            # Buyer username from subject "New Order from username"
            buyer_match = re.search(r"from\s+([a-zA-Z0-9_]+)", subject, re.IGNORECASE)
            order["buyer_username"] = buyer_match.group(1) if buyer_match else "unknown"

            # Detect if revision
            if is_revision(body):
                order["revision_feedback"] = body[:500]

            orders.append(order)
            mail.store(msg_id, "+FLAGS", "\\Seen")

        mail.logout()
    except imaplib.IMAP4.error as e:
        print(f"[{MODULE_NAME}] IMAP error: {e}")
    except Exception as e:
        print(f"[{MODULE_NAME}] Order check error: {e}")

    return orders


# ─── Review checking ──────────────────────────────────────────────────────────

def check_for_reviews() -> list[dict]:
    """
    Check IMAP for Fiverr review notification emails.
    Parses star rating + review text and writes to learning memory.
    Returns list of parsed review dicts.
    """
    mail, _ = _imap_connect()
    if mail is None:
        return []

    reviews: list[dict] = []
    try:
        mail.select("INBOX")

        # Fiverr sends "left you a review" or "rated your gig" in review emails
        _, data = mail.search(
            None, '(UNSEEN FROM "noreply@fiverr.com")'
        )
        msg_ids = data[0].split()

        for msg_id in msg_ids:
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw         = msg_data[0][1]
            msg         = email_lib.message_from_bytes(raw)
            subject     = msg.get("Subject", "")
            body        = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="replace")

            # Only process review emails
            review_signals = ["review", "rated", "left you", "feedback", "star"]
            if not any(s in (subject + body).lower() for s in review_signals):
                continue

            # Extract star rating: "5 stars", "4-star", "rated 3"
            rating_match = re.search(
                r"(\d)\s*[-\s]?\s*star|rated\s+(\d)|(\d)\s+out\s+of\s+5",
                body, re.IGNORECASE,
            )
            rating = 5  # default to 5 if can't parse
            if rating_match:
                raw_r = rating_match.group(1) or rating_match.group(2) or rating_match.group(3)
                rating = int(raw_r) if raw_r else 5

            # Extract written review text
            review_text_match = re.search(
                r'(?:wrote|left a review|says?|commented?)[:\s]+["\']?(.{10,300})["\']?',
                body, re.IGNORECASE,
            )
            review_text = review_text_match.group(1).strip() if review_text_match else body[:200]

            # Extract order ID
            id_match = re.search(r"#?(FO\d{7,}|\d{7,})", body + subject)
            order_id = id_match.group(1) if id_match else "unknown"

            review = {
                "order_id":    order_id,
                "rating":      rating,
                "review_text": review_text,
                "subject":     subject,
            }
            reviews.append(review)

            # Write to memory (niche unknown at this point — logged without niche context)
            try:
                log_review_to_memory(order_id, rating, review_text, "unknown", "")
            except Exception as e:
                print(f"[{MODULE_NAME}]   Review memory write failed: {e}")

            mail.store(msg_id, "+FLAGS", "\\Seen")
            print(f"[{MODULE_NAME}]   Review processed: order {order_id} — {rating} stars")

        mail.logout()
    except Exception as e:
        print(f"[{MODULE_NAME}] Review check error: {e}")

    return reviews


# ─── Mock test order ──────────────────────────────────────────────────────────

MOCK_ORDER = {
    "order_id":           "TEST_FO000001",
    "buyer_username":     "testbuyer",
    "package_tier":       "standard",
    "video_title":        "I Tried Living on $5 a Day for 30 Days",
    "channel_niche":      "finance",
    "style_preference":   "bold, MrBeast energy, high contrast",
    "has_face":           True,
    "buyer_images":       [],
    "color_preferences":  "bright green and black",
    "text_to_include":    "I TRIED $5/DAY FOR 30 DAYS",
    "special_instructions": "Make it look viral — like a video that gets 10 million views",
    "revision_of":        None,
    "requirements":       "Finance channel. Video about living on $5 a day. Bold, MrBeast style.",
}

MOCK_LOGO_ORDER = {
    "order_id":           "TEST_LOGO_001",
    "buyer_username":     "testbuyer",
    "gig_type":           "logo",
    "package_tier":       "standard",
    "business_name":      "NovaBuild",
    "business_type":      "construction and renovation company",
    "industry":           "real_estate",
    "colors":             "navy blue and gold",
    "style_preferences":  "modern, professional, trustworthy",
    "special_instructions": "Keep it clean and minimal — no fancy effects",
    "requirements":       "Logo for construction company called NovaBuild. Navy and gold. Modern.",
}

MOCK_SOCIAL_ORDER = {
    "order_id":           "TEST_SOCIAL_001",
    "buyer_username":     "testbuyer",
    "gig_type":           "social_media",
    "package_tier":       "basic",
    "business_name":      "Bloom Bakery",
    "business_type":      "artisan bakery",
    "colors":             "pastel pink and cream with gold accents",
    "style":              "elegant, warm, Instagram-aesthetic",
    "special_instructions": "Posts should feel cozy and inviting",
    "requirements":       "Instagram posts for artisan bakery. Pastel pink and cream. Elegant.",
}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--test",       action="store_true", help="Run with mock thumbnail order")
    parser.add_argument("--test-logo",  action="store_true", help="Run with mock logo order")
    parser.add_argument("--test-social",action="store_true", help="Run with mock social media order")
    parser.add_argument("--test-imap",  action="store_true", help="Test IMAP connection only")
    args = parser.parse_args()

    if args.test_imap:
        print(f"[{MODULE_NAME}] --- Testing IMAP connection ---")
        imap_server = os.getenv("GMAIL_IMAP_SERVER", "imap.gmail.com")
        imap_port   = int(os.getenv("GMAIL_IMAP_PORT", "993"))
        imap_email  = os.getenv("FIVERR_NOTIFICATION_EMAIL", "")
        imap_pass   = os.getenv("GMAIL_APP_PASSWORD", "")

        if not imap_email or not imap_pass or imap_pass == "paste-your-16-char-password-here":
            print(f"[{MODULE_NAME}] ERROR: FIVERR_NOTIFICATION_EMAIL or GMAIL_APP_PASSWORD not set in .env")
        else:
            print(f"[{MODULE_NAME}] Connecting to {imap_server}:{imap_port} as {imap_email} ...")
            try:
                mail = imaplib.IMAP4_SSL(imap_server, imap_port)
                mail.login(imap_email, imap_pass)
                print(f"[{MODULE_NAME}] Login OK")

                mail.select("INBOX")
                _, data = mail.search(None, "ALL")
                total = len(data[0].split()) if data[0] else 0

                _, unseen_data = mail.search(None, "UNSEEN")
                unread = len(unseen_data[0].split()) if unseen_data[0] else 0

                mail.logout()
                print(f"[{MODULE_NAME}] INBOX: {total} total, {unread} unread")
                print(f"[{MODULE_NAME}] IMAP connection test PASSED")
            except imaplib.IMAP4.error as e:
                print(f"[{MODULE_NAME}] IMAP auth error: {e}")
                print(f"[{MODULE_NAME}] Check that you are using a Gmail App Password (not your account password)")
            except Exception as e:
                print(f"[{MODULE_NAME}] Connection error: {e}")

    elif args.test:
        print(f"[{MODULE_NAME}] Running test fulfillment with mock thumbnail order...")
        success = fulfill_order(MOCK_ORDER)
        print(f"[{MODULE_NAME}] Test result: {'SUCCESS' if success else 'FAILED'}")
    elif args.test_logo:
        print(f"[{MODULE_NAME}] Running test fulfillment with mock logo order...")
        success = fulfill_order(MOCK_LOGO_ORDER)
        print(f"[{MODULE_NAME}] Test result: {'SUCCESS' if success else 'FAILED'}")
    elif args.test_social:
        print(f"[{MODULE_NAME}] Running test fulfillment with mock social media order...")
        success = fulfill_order(MOCK_SOCIAL_ORDER)
        print(f"[{MODULE_NAME}] Test result: {'SUCCESS' if success else 'FAILED'}")
    else:
        print(f"[{MODULE_NAME}] Checking for Fiverr orders...")
        orders = check_fiverr_orders()
        if not orders:
            print(f"[{MODULE_NAME}] No new orders.")
        else:
            for o in orders:
                fulfill_order(o)
