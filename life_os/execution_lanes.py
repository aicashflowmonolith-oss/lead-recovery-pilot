"""Shared queue classification for work that can block lightweight control."""

HEAVY_JOB_KINDS = (
    "engineering.build",
    "engineering.ci_repair",
    "capability.build",
    "request.execute",
    "maintainer.audit",
    "maintainer.backup",
)
