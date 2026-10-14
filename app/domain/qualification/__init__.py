"""Phase 5 qualification gate - pure, deterministic domain logic (no I/O, no vendors).

Order (never reordered; hard rules can never be overridden by Laya or an LLM):
  1. identity/domain eligibility      -> rules.evaluate_hard_rules
  2. hard ICP rules (funding / intent) -> rules.evaluate_hard_rules
  3. signal normalization + clustering -> clustering.cluster_signals
  4. evidence bundle                   -> bundle.build_bundle
  5. Laya typed decisions              -> (provider, via bundle.LAYA_QUESTIONS)
  6. optional LLM review (REVIEW only) -> (provider)
  7. final decision                    -> policy.decide
"""

from app.domain.qualification.icp import POLICY_VERSION, IcpConfig

__all__ = ["POLICY_VERSION", "IcpConfig"]
