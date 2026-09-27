"""compose(category, merchant, trigger, customer) -> message dict (template path).

The deterministic core used by /tick, the offline submission and the LLM pipeline (as the
always-valid fallback candidate).
"""
from __future__ import annotations

from datetime import datetime

from . import facts as F
from . import validator
from .templates import Ctx, Draft, render


def build_ctx(category: dict, merchant: dict, trigger: dict, customer: dict | None, now: datetime | None,
              used_insights: set | None = None) -> Ctx:
    fs = F.build(category, merchant, trigger, customer, now)
    insights = F.diagnose(fs, category, merchant, now)
    return Ctx(fs=fs, category=category, merchant=merchant, trigger=trigger, customer=customer,
               insights=insights, used_insights=set(used_insights or ()), now=now)


def compose_template(category: dict, merchant: dict, trigger: dict, customer: dict | None = None,
                     now: datetime | None = None, used_insights: set | None = None,
                     prior_bodies: list[str] | None = None) -> tuple[Draft, Ctx, list[str]]:
    ctx = build_ctx(category, merchant, trigger, customer, now, used_insights)
    draft = render(ctx)
    violations = validator.validate(draft.body, ctx.fs, category=category, prior_bodies=prior_bodies,
                                    require_hinglish=ctx.fs.lang == "hinglish")
    return draft, ctx, violations


def to_message(draft: Draft, trigger: dict, customer: dict | None) -> dict:
    return {
        "body": draft.body,
        "cta": draft.cta,
        "send_as": "merchant_on_behalf" if customer or trigger.get("scope") == "customer" else "vera",
        "suppression_key": trigger.get("suppression_key") or f"{trigger.get('kind')}:{trigger.get('id')}",
        "template_name": draft.template_name,
        "template_params": [str(p) for p in draft.template_params],
        "rationale": draft.rationale,
    }


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """Challenge-brief section 7.1 contract (deterministic, template path)."""
    draft, _ctx, _v = compose_template(category, merchant, trigger, customer, None)
    return to_message(draft, trigger, customer)
