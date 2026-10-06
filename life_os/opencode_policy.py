"""OpenCode routes permitted for arbitrary internal engineering input.

The Zen pricing/privacy table was checked on 2026-10-02. It advertises these
routes as free with zero retention and no model training. NVIDIA trial routes
prohibit personal/confidential input; contributor and feedback routes collect
input for model improvement. Those routes are not general internal fallbacks.

This policy neither grants provider authority nor proves execution readiness.
Existing capability enablement, authentication and circuit gates still apply.
"""
from __future__ import annotations

OPENCODE_POLICY_SOURCE = "https://opencode.ai/docs/en/zen/"
OPENCODE_POLICY_CHECKED_ON = "2026-10-02"
OPENCODE_INTERNAL_MODELS = (
    "opencode/longcat-2.5-preview-free",
    "opencode/space-bunny-free",
)


def permits_internal_model(model: str) -> bool:
    # A route advertising a zero price does not establish its input policy.
    return model in OPENCODE_INTERNAL_MODELS
