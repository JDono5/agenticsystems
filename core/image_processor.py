"""
core/image_processor.py — Post-processing utilities for generated designs.

process_design_background():
    Removes the white background from a QA-approved design PNG using a
    flood-fill from all four corners.  Any pixel within 30 RGB units of
    pure white is made fully transparent.  The result is saved back to the
    same path as an RGBA PNG so Printify can place it on any product colour.

    QA always runs on the original white-background version first, so the
    background check in the QA prompt remains valid.
"""

from __future__ import annotations

from pathlib import Path


def process_design_background(
    image_path: str,
    threshold: int = 30,
) -> str:
    """
    Convert white background pixels to transparent via corner flood-fill.

    Args:
        image_path: Path to the PNG file.  Overwritten in-place.
        threshold:  Max Euclidean distance from (255,255,255) a pixel can be
                    while still being treated as background.  Default 30.

    Returns:
        The same image_path that was passed in.

    Raises:
        FileNotFoundError: if image_path does not exist.
        ImportError:       if Pillow is not installed.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise ImportError(
            "Pillow is required for background removal. "
            "Run: pip install Pillow"
        ) from exc

    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    img = Image.open(path).convert("RGBA")
    pixels = img.load()
    width, height = img.size

    # Seeds: all four corners
    seeds = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    visited: set[tuple[int, int]] = set()
    queue: list[tuple[int, int]] = []

    def _is_background(r: int, g: int, b: int) -> bool:
        return (
            (255 - r) ** 2 + (255 - g) ** 2 + (255 - b) ** 2
        ) <= threshold ** 2

    for seed in seeds:
        if seed not in visited:
            queue.append(seed)

    while queue:
        x, y = queue.pop()
        if (x, y) in visited:
            continue
        if x < 0 or x >= width or y < 0 or y >= height:
            continue

        r, g, b, a = pixels[x, y]
        if a == 0 or not _is_background(r, g, b):
            continue

        pixels[x, y] = (255, 255, 255, 0)
        visited.add((x, y))

        queue.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])

    img.save(path, "PNG")
    return image_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python core/image_processor.py <image.png> [threshold]")
        sys.exit(1)

    thr = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    out = process_design_background(sys.argv[1], threshold=thr)
    print(f"Background removed: {out}")
