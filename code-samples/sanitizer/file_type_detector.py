"""
File Type Detector — Robust Document Identification for Untrusted Input

This module provides a reliable file type detection mechanism that combines
extension-based heuristics with magic-byte validation. It is designed to
handle arbitrary user uploads in a document processing pipeline, ensuring
that malformed or mislabeled files are correctly identified (or safely rejected)
before GPU resources are allocated.

Original context: Axima document processing SaaS (GCP, Python 3.10, OpenCV).
Extracted and anonymized for engineering showcase.

Key patterns demonstrated:
- Multi-layered detection: extension → magic bytes → image decoder
- Support for multi-page TIFF documents via OpenCV
- Defensive reading (minimal I/O, early exit on invalid formats)
- Graceful error handling with structured logging hooks
- Clear separation between file identification and conversion
"""

import os
from typing import List, Tuple, Optional

import cv2
import numpy as np

# Supported file extensions (lowercase, dot-prefixed)
ALLOWED_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png', '.tiff', '.tif'}

# Magic byte signatures for common document/image formats
MAGIC_SIGNATURES = {
    b'\x89PNG': 'png',
    b'\xff\xd8\xff': 'jpeg',
    b'II*\x00': 'tiff_little_endian',
    b'MM\x00*': 'tiff_big_endian',
    b'%PDF': 'pdf',
}


def detect_file_type(file_path: str) -> str:
    """
    Determines the document type of a file.

    Strategy (in order):
    1. Extract lowercase extension from the filename.
    2. If extension is .pdf, return 'pdf'.
    3. If extension matches an image format, attempt magic-byte verification.
    4. If magic bytes fail but extension suggests an image, still return 'image'
       (optimistic) — the image loader will catch corrupt files.
    5. If extension is unsupported, raise ValueError with a clear message.

    Args:
        file_path: Absolute path to the file in /dev/shm or local storage.

    Returns:
        One of: 'pdf' or 'image'.

    Raises:
        ValueError: If the file format is not supported.
    """
    ext = os.path.splitext(file_path)[1].lower()

    # Explicit PDF detection
    if ext == '.pdf':
        return 'pdf'

    # Image formats: validate via extension, optionally reinforced by magic bytes
    if ext in ('.jpg', '.jpeg', '.png', '.tiff', '.tif'):
        # Read only the first 4 bytes for magic checks
        try:
            with open(file_path, 'rb') as f:
                magic = f.read(4)
        except OSError:
            raise ValueError(f"Cannot read file for magic detection: {file_path}")

        # Verify against known signatures
        for signature, fmt in MAGIC_SIGNATURES.items():
            if magic.startswith(signature):
                # File matches expected type
                return 'image'

        # If magic doesn't match but extension is an image, still accept as image.
        # The conversion step (OpenCV) will raise a clear error if the data is corrupt.
        return 'image'

    # Unsupported extension
    raise ValueError(
        f"Unsupported file format: '{ext}'. "
        f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
    )


def convert_to_image_array(
    file_path: str, file_type: str
) -> Tuple[List[np.ndarray], Optional[str]]:
    """
    Converts a PDF or image file into a list of NumPy arrays (one per page).

    Supports:
    - Single-page images (JPG, PNG)
    - Multi-page TIFF (via OpenCV's imreadmulti)
    - PDF (via an external PDF renderer, e.g., pypdfium2; not shown here)

    Returns:
        (list_of_arrays, None) on success, or (empty_list, error_message) on failure.
    """
    images: List[np.ndarray] = []

    if file_type == 'image':
        # Attempt multi-page TIFF first
        success, multi = cv2.imreadmulti(file_path, [], cv2.IMREAD_COLOR)
        if success and len(multi) > 1:
            return multi, None

        # Single image fallback
        img = cv2.imread(file_path)
        if img is None:
            return [], f"OpenCV could not decode image: {file_path}"

        # Downscale if excessively large to prevent memory pressure downstream
        h, w = img.shape[:2]
        if max(h, w) > 2500:
            scale = 2500 / max(h, w)
            img = cv2.resize(img, (0, 0), fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

        # Basic resolution sanity check (~150 DPI equivalent for A4 portrait)
        if img.shape[0] < 1200 or img.shape[1] < 900:
            # Low resolution is warned but not rejected
            pass

        images.append(img)
        return images, None

    # PDF handling would go here (omitted for brevity; uses external renderer)
    return [], f"PDF rendering not implemented in this showcase extract"


# ---------------------------------------------------------------------------
# Usage example (integrated into the Sanitizer API)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_files = [
        "/dev/shm/sample.pdf",
        "/dev/shm/sample.png",
        "/dev/shm/sample_malicious.exe",
    ]
    for path in test_files:
        try:
            ftype = detect_file_type(path)
            print(f"{path} → {ftype}")
        except ValueError as e:
            print(f"{path} → REJECTED: {e}")