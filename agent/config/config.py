"""ChaosArena runtime configuration.

All tuneable constants live here. Import from this module everywhere else
so there is a single place to adjust values.
"""

# ── Agent runtime ──────────────────────────────────────────────────────────
DEFAULT_MAX_TURNS = 60          # default turn budget per evaluation run
MAX_TOKENS = 8192               # max output tokens per LLM call
TEMPERATURE = 0.0               # lock sampling for reproducibility
TOP_P_RECORDED = 1.0            # not sent to LLM; recorded for provenance
TOP_K_RECORDED = 1              # not sent to LLM; recorded for provenance
DEFAULT_R_ESTIMATED_TURNS = 3     # fallback when a hand-written spec omits per-R budget metadata

# ── Model IDs ──────────────────────────────────────────────────────────────
# Override with MODEL_ID env var at runtime.
# LLM_BACKEND=bedrock  → uses DEFAULT_BEDROCK_MODEL (cross-region inference profile)
# LLM_BACKEND=direct   → uses DEFAULT_DIRECT_MODEL  (direct API model name)
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-6"
DEFAULT_DIRECT_MODEL = "claude-sonnet-4-6"

# ── LLM pricing (2026-Q2, Sonnet 4.6) ─────────────────────────────────────
# Update PRICING_VERSION whenever these change.
PRICING_VERSION = "2026-Q2"
INPUT_COST_PER_MTOK = 3.00          # $/1M input tokens
OUTPUT_COST_PER_MTOK = 15.00        # $/1M output tokens
CACHE_CREATION_PER_MTOK = 3.75      # $/1M cache-write tokens
CACHE_READ_PER_MTOK = 0.30          # $/1M cache-read tokens

# ── Spec drafter ───────────────────────────────────────────────────────────
DRAFTER_TEMPERATURE = 0.0
DRAFTER_MAX_TOKENS = 16384   # thinking + JSON; extended thinking can use 8K+ tokens alone

# ── Interactive trim (spec_drafter) ───────────────────────────────────────
# Rough turn cost per R-category used in the budget table shown to the TA.
CATEGORY_TURN_COST: dict[str, int] = {
    "race_conditions": 3,
    "async_invariants": 4,
    "auth_boundaries": 2,
    "edge_cases": 1,
}

PRIORITY_ORDER: dict[str, int] = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
