"""Local configuration bounds, not a second set of backend wire models."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MediaLimits:
    max_prompt_chars: int = 16_000
    max_facts: int = 20
    max_fact_chars: int = 2_000
    max_visual_idea_chars: int = 4_000
    max_feedback_chars: int = 4_000
    max_style_chars: int = 2_000
    max_constraints: int = 20
    max_constraint_chars: int = 1_000
    max_image_bytes: int = 5 * 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    max_dimension: int = 4_096
    max_pixels: int = 16_777_216
    max_png_chunks: int = 8_192
