"""Provider-independent entry point; no storage, identifiers or publishing."""

from app.shared.models import BusinessProfile, GeneratedImage, TextDraft
from app.shared.ports import ImageProvider

from .errors import MediaConfigurationError
from .limits import MediaLimits
from .validation import DEFAULT_LIMITS, build_image_prompt, validate_draft, validate_limits


async def generate_image(
    profile: BusinessProfile,
    text: TextDraft,
    feedback: str | None = None,
    *,
    provider: ImageProvider,
    limits: MediaLimits = DEFAULT_LIMITS,
) -> GeneratedImage:
    """Generate one draft through an explicitly injected provider; never retry."""
    validate_limits(limits)
    build_image_prompt(profile, text, feedback, limits)
    if not callable(getattr(provider, "generate", None)):
        raise MediaConfigurationError("An image provider with generate() is required.")
    draft = await provider.generate(profile, text, feedback)
    validate_draft(draft, limits)
    return draft
