from __future__ import annotations

import argparse
import json
import math
import os
import socket
from typing import Sequence


def _source_probe(timeout_seconds: float, max_requests: int) -> dict:
    if max_requests < 2:
        raise ValueError("source probe requires two requests so robots.txt remains enforced")
    per_request_timeout = max(0.1, timeout_seconds / max_requests)
    socket.setdefaulttimeout(per_request_timeout)
    from infrastructure.external.sources.fetch_policy import SourceFetchPolicy as InfraSourceFetchPolicy
    from interfaces.services.source_runtime import build_source_runtime_composition

    fetch_policy = InfraSourceFetchPolicy(
        timeout_seconds=per_request_timeout,
        max_bytes=128_000,
        max_redirects=0,
        respect_robots=True,
        retry_times=0,
    )
    result = build_source_runtime_composition(fetch_policy=fetch_policy).source_service.check_source_health(limit=1, force=True)
    payload = result.to_dict()
    return {
        "reachable": payload.get("failed_count", 1) == 0 and payload.get("succeeded_count", 0) > 0,
        "checked_count": payload.get("checked_count", 0),
        "failed_count": payload.get("failed_count", 0),
        "request_budget": max_requests,
    }


def _llm_probe(timeout_seconds: float, max_input_tokens: int, max_output_tokens: int, max_cost_usd: float) -> dict:
    from framework.llm import LLMRequest, load_openai_compatible_deployment
    from interfaces.composition.research_settings import ResearchRuntimeSettings

    prompt = "Reply with OK."
    messages = [{"role": "user", "content": prompt}]
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    estimated_input = max(8, (len(serialized) + 3) // 4 + 8)
    if estimated_input > max_input_tokens or max_output_tokens <= 0:
        raise ValueError("probe token budget is insufficient")
    price_text = str(os.environ.get("NEWSROOM_DESKTOP_PROBE_MAX_COST_PER_1K_TOKENS_USD") or "").strip()
    price = float(price_text)
    estimated_cost = (estimated_input + min(8, max_output_tokens)) / 1000 * price
    if not math.isfinite(price) or price < 0 or not math.isfinite(estimated_cost) or estimated_cost > max_cost_usd:
        raise ValueError("probe cost budget is unverifiable or exceeded")
    settings = ResearchRuntimeSettings.from_env(os.environ, cwd=os.getcwd())
    deployment = load_openai_compatible_deployment(settings.llm.models_config_path, route_id=settings.llm.route_id, apply_environment_overrides=False)
    if deployment.config.model != settings.llm.model or deployment.config.base_url.rstrip("/") != settings.llm.base_url.rstrip("/"):
        raise ValueError("Research LLM deployment does not match resolved settings")
    client = deployment.build_client()
    response = client.complete(
        LLMRequest(
            messages=messages,
            temperature=0,
            max_tokens=min(8, max_output_tokens),
            timeout_seconds=timeout_seconds,
            max_transport_attempts=1,
        )
    )
    return {"reachable": bool(response.content.strip()), "model": response.model, "estimated_input_tokens": estimated_input, "estimated_cost_usd": estimated_cost}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("source", "llm"))
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--max-requests", type=int, default=1)
    parser.add_argument("--max-input-tokens", type=int, default=0)
    parser.add_argument("--max-output-tokens", type=int, default=0)
    parser.add_argument("--max-cost-usd", type=float, default=0)
    args = parser.parse_args(argv)
    try:
        if args.kind == "source":
            payload = _source_probe(args.timeout, args.max_requests)
        else:
            payload = _llm_probe(args.timeout, args.max_input_tokens, args.max_output_tokens, args.max_cost_usd)
    except Exception as exc:
        payload = {"reachable": False, "error_type": type(exc).__name__}
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload.get("reachable") else 1


if __name__ == "__main__":
    raise SystemExit(main())
