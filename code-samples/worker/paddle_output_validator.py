"""
Paddle Output Validator — Schema Enforcement & Normalization for ML Inference

This module acts as a contract boundary between the GPU-accelerated OCR/structure
engine (PaddleOCR/PaddleX) and the frontend rendering layer. It validates,
normalizes, and sanitizes every region produced by the ML pipeline before it
ever reaches the user interface.

Original context: Axima document processing SaaS (GCP, Python 3.10).
Extracted and anonymized for engineering showcase.

Key patterns demonstrated:
- Schema validation and type normalization for ML outputs
- Defensive clamping of bounding boxes to image dimensions
- Fallback strategies for incomplete or malformed table structures
- Binary error signaling for critical validation failures (metrics separation)
- Render strategy hints for a canvas-based frontend
"""

from typing import List, Dict, Any, Tuple, Optional


class PaddleOutputValidator:
    """
    Validates and normalizes structured output from PaddleOCR/PaddleX.

    The ML pipeline produces regions of type: text, table, figure, seal, etc.
    Each region has a bounding box (bbox) and type-specific content.
    This validator ensures:
    - All bboxes are clamped to image dimensions.
    - All types are recognized and normalized.
    - Table content is enriched with a render strategy (HTML, canvas, or fallback).
    - Non-standard types are safely downgraded to 'text'.
    - Degenerate regions (zero area) are discarded.
    """

    # Whitelist of known region types from the ML model
    VALID_TYPES = {"text", "table", "figure", "title", "equation", "seal", "header", "footer"}

    @staticmethod
    def validate_page(
        blocks: List[Dict[str, Any]],
        img_w: int,
        img_h: int,
        child_id: int = 0
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Validates and normalizes all regions on a single page.

        Args:
            blocks: Raw regions from PaddleOCR/PaddleX structure analysis.
            img_w: Image width in pixels (for bbox clamping).
            img_h: Image height in pixels.
            child_id: Worker identifier for log correlation.

        Returns:
            (validated_blocks, has_critical_error)
            - validated_blocks: Clean, frontend-ready region list.
            - has_critical_error: True if any block was too malformed to recover.
        """
        validated_blocks: List[Dict[str, Any]] = []
        warnings: List[str] = []

        for idx, block in enumerate(blocks):
            try:
                # -------------------------------------------------------------
                # Step 1: Validate and normalize the region type
                # -------------------------------------------------------------
                region_type = str(block.get("type", "")).lower()
                if region_type not in PaddleOutputValidator.VALID_TYPES:
                    warnings.append(
                        f"Block {idx}: unknown type '{region_type}' → forced to 'text'"
                    )
                    region_type = "text"

                # -------------------------------------------------------------
                # Step 2: Validate and clamp bounding box
                # -------------------------------------------------------------
                bbox = block.get("bbox", [])
                if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
                    warnings.append(f"Block {idx}: invalid bbox → discarded")
                    continue

                x1, y1, x2, y2 = [
                    max(0, min(int(c), img_w if j % 2 == 0 else img_h))
                    for j, c in enumerate(bbox)
                ]

                # Discard degenerate boxes (zero or negative area)
                if x1 >= x2 or y1 >= y2:
                    warnings.append(
                        f"Block {idx}: degenerate bbox [{x1},{y1},{x2},{y2}] → discarded"
                    )
                    continue

                # -------------------------------------------------------------
                # Step 3: Normalize content by type
                # -------------------------------------------------------------
                content = block.get("content", {})
                normalized: Dict[str, Any] = {
                    "type": region_type,
                    "bbox": [x1, y1, x2, y2],
                    "confidence": float(
                        content.get("confidence", content.get("score", 0.0))
                    ),
                }

                if region_type == "table":
                    # Tables can arrive in two formats:
                    # a) Full HTML (ready to render in an iframe)
                    # b) Cell structure (needs canvas-based rendering)
                    # c) Neither → OCR fallback (text only)
                    html = content.get("html", "")
                    if "<table" in html:
                        normalized["render_strategy"] = "html_iframe"
                        normalized["content"] = {"html": html}
                    elif "structure" in content and "cells" in content["structure"]:
                        normalized["render_strategy"] = "canvas_native"
                        cells = content["structure"]["cells"]
                        normalized["content"] = {
                            "cells": [
                                {
                                    "bbox": c.get("bbox", []),
                                    "text": str(c.get("text", "")),
                                    "rowspan": c.get("rowspan", 1),
                                    "colspan": c.get("colspan", 1),
                                }
                                for c in cells
                            ],
                            "has_merged": any(
                                c.get("rowspan", 1) > 1 or c.get("colspan", 1) > 1
                                for c in cells
                            ),
                        }
                    else:
                        normalized["render_strategy"] = "ocr_fallback"
                        normalized["content"] = {
                            "text": "",
                            "note": "table_structure_missing",
                        }

                elif region_type in ("seal", "figure"):
                    # Seals and figures are rendered as overlay images
                    normalized["render_strategy"] = "overlay_image"
                    normalized["content"] = {
                        "is_seal": region_type == "seal",
                        "confidence": normalized["confidence"],
                    }

                else:
                    # Text, title, header, footer → canvas text rendering
                    normalized["render_strategy"] = "canvas_text"
                    normalized["content"] = {
                        "text": str(content.get("text", "")).strip(),
                        "polygon": content.get("polygon", content.get("pts", [])),
                    }
                    # Hint for frontend: apply perspective correction if polygon
                    normalized["render_hints"] = {
                        "needs_perspective_correction": len(
                            normalized["content"]["polygon"]
                        )
                        == 4,
                        "is_seal_region": False,
                    }

                validated_blocks.append(normalized)

            except Exception as e:
                warnings.append(f"Block {idx}: normalization error → {str(e)[:80]}")
                continue

        # -----------------------------------------------------------------
        # Step 4: Determine if any critical failure occurred
        # Critical = block was so malformed it couldn't be included at all.
        # This feeds a separate metric for alerting.
        # -----------------------------------------------------------------
        has_critical_error = any(
            "invalid" in w.lower()
            or "degenerado" in w.lower()
            or "normalization error" in w.lower()
            for w in warnings
        )

        return validated_blocks, has_critical_error