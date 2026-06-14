"""ChaosArena runtime configuration.

All tuneable constants live here. Import from this module everywhere else
so there is a single place to adjust values.
"""

# ── Agent runtime ──────────────────────────────────────────────────────────
DEFAULT_MAX_TURNS = 35          # default turn budget per evaluation run
MAX_TOKENS = 8192               # max output tokens per Bedrock/API call
TEMPERATURE = 0.0               # lock sampling for reproducibility
TOP_P_RECORDED = 1.0            # not sent to Bedrock; recorded for provenance
TOP_K_RECORDED = 1              # not sent to Bedrock; recorded for provenance
ENABLE_R_CONTEXT_TRIMMING = True  # archive completed R history outside the active prompt

# ── Model IDs ──────────────────────────────────────────────────────────────
# Bedrock requires a cross-region inference profile, not the base model ID.
# Find yours with:
#   aws bedrock list-inference-profiles --region us-west-2 \
#     --query "inferenceProfileSummaries[?contains(inferenceProfileId,'sonnet-4-6')].inferenceProfileId" \
#     --output table
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-6"
DEFAULT_DIRECT_MODEL = "claude-sonnet-4-6"

# ── Bedrock Sonnet 4.6 pricing (2026-Q2) ──────────────────────────────────
# Source: https://aws.amazon.com/bedrock/pricing/
# Update BEDROCK_PRICING_VERSION whenever these change.
BEDROCK_PRICING_VERSION = "2026-Q2"
INPUT_COST_PER_MTOK = 3.00          # $/1M input tokens
OUTPUT_COST_PER_MTOK = 15.00        # $/1M output tokens
CACHE_CREATION_PER_MTOK = 3.75      # $/1M cache-write tokens
CACHE_READ_PER_MTOK = 0.30          # $/1M cache-read tokens

# ── Spec drafter ───────────────────────────────────────────────────────────
DRAFTER_TEMPERATURE = 0.0
DRAFTER_MAX_TOKENS = 16384   # thinking + JSON; Sonnet 4.6 thinking can use 8K+ tokens alone

# ── Interactive trim (spec_drafter) ───────────────────────────────────────
# Rough turn cost per R-category used in the budget table shown to the TA.
CATEGORY_TURN_COST: dict[str, int] = {
    "race_conditions": 3,
    "async_invariants": 4,
    "auth_boundaries": 2,
    "edge_cases": 1,
}

PRIORITY_ORDER: dict[str, int] = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
