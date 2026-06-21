# ============================================================
#  CLAUDE AI - CONTEXT ANALYZER ONLY (Not Trader)
# ============================================================
#
# IMPORTANT: Claude is NOT the trading engine.
# Claude's ONLY role is to provide:
# - Market narrative/context
# - Sentiment overlay
# - Key level observations
# - Confidence scoring
#
# The STRATEGY ENGINE (not Claude) makes entry/exit decisions.
# ============================================================

import requests
import time
from datetime import datetime
import config
from monitoring.logger import log_event


# Runtime guards to prevent API spam and keep trading loop stable.
_last_call_ts = 0.0
_last_error_log_ts = 0.0
_disabled_until_ts = 0.0
_consecutive_failures = 0
_active_model = config.CLAUDE_MODEL
_model_candidates = []
for _candidate in [
    config.CLAUDE_MODEL,
    config.CLAUDE_CHEAP_MODEL,
    config.CLAUDE_COMPLEX_MODEL,
    *config.CLAUDE_FALLBACK_MODELS,
]:
    if _candidate and _candidate not in _model_candidates:
        _model_candidates.append(_candidate)

if not _model_candidates:
    _model_candidates = [config.CLAUDE_MODEL]
_model_stats = {
    model: {"failures": 0, "lockout_until": 0.0}
    for model in _model_candidates
}
_last_model_failure_log_ts = {
    model: 0.0 for model in _model_candidates
}
_warmup_done = False
_cached_context = {
    "bias": "NEUTRAL",
    "momentum": "UNKNOWN",
    "observations": [],
    "confidence": 0,
    "warning": "No Claude context available yet"
}

_static_context_instruction = (
    "You are a forex context analyzer. Do not provide trading entries, exits, or position sizing. "
    "Respond with concise context only."
)

# Session-level usage counters so each bot run can print Claude consumption.
_session_usage = {
    "total_api_calls": 0,
    "successful_calls": 0,
    "failed_calls": 0,
    "warmup_calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "by_model": {},
    "by_feature": {},
    "by_day": {},
    "estimated_cost_usd": 0.0,
    "budget_tier": "GREEN"
}


def _ensure_model_bucket(model):
    if model not in _session_usage["by_model"]:
        _session_usage["by_model"][model] = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0
        }


def _ensure_feature_bucket(feature):
    if feature not in _session_usage["by_feature"]:
        _session_usage["by_feature"][feature] = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0
        }


def _ensure_day_bucket(day_key):
    if day_key not in _session_usage["by_day"]:
        _session_usage["by_day"][day_key] = {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0
        }


def _estimate_cost_usd(model, input_tokens, output_tokens):
    model_name = (model or "").lower()

    if "opus" in model_name:
        in_rate = config.CLAUDE_COST_PER_MILLION_INPUT_OPUS
        out_rate = config.CLAUDE_COST_PER_MILLION_OUTPUT_OPUS
    elif "haiku" in model_name:
        in_rate = config.CLAUDE_COST_PER_MILLION_INPUT_HAIKU
        out_rate = config.CLAUDE_COST_PER_MILLION_OUTPUT_HAIKU
    else:
        in_rate = config.CLAUDE_COST_PER_MILLION_INPUT_SONNET
        out_rate = config.CLAUDE_COST_PER_MILLION_OUTPUT_SONNET

    return ((input_tokens / 1_000_000.0) * in_rate) + ((output_tokens / 1_000_000.0) * out_rate)


def _current_budget_tier():
    budget = int(getattr(config, "CLAUDE_SESSION_TOKEN_BUDGET", 0) or 0)
    if budget <= 0:
        return "GREEN"

    used = _session_usage["total_tokens"]
    ratio = used / float(budget)

    if ratio >= float(config.CLAUDE_RED_BUDGET_RATIO):
        return "RED"
    if ratio >= float(config.CLAUDE_AMBER_BUDGET_RATIO):
        return "AMBER"
    return "GREEN"


def _log_budget_tier_change(new_tier):
    prev_tier = _session_usage.get("budget_tier", "GREEN")
    if new_tier == prev_tier:
        return

    _session_usage["budget_tier"] = new_tier
    log_event("Claude Budget Tier Changed", {
        "previous": prev_tier,
        "current": new_tier,
        "tokens_used": _session_usage.get("total_tokens", 0),
        "token_budget": int(getattr(config, "CLAUDE_SESSION_TOKEN_BUDGET", 0) or 0)
    })


def _pick_model_for_call(feature, complexity_hint=0):
    if not config.CLAUDE_ENABLE_SMART_ROUTING:
        return _active_model

    tier = _current_budget_tier()
    _log_budget_tier_change(tier)

    if tier in ["AMBER", "RED"]:
        return config.CLAUDE_CHEAP_MODEL

    if int(complexity_hint or 0) >= int(config.CLAUDE_COMPLEXITY_THRESHOLD):
        return config.CLAUDE_COMPLEX_MODEL

    if feature == "refine":
        return config.CLAUDE_CHEAP_MODEL

    return config.CLAUDE_MODEL


def _resolve_routed_model(preferred_model):
    stat = _model_stats.get(preferred_model)
    if stat and time.time() >= stat.get("lockout_until", 0.0):
        return preferred_model

    # If preferred model is locked, pick first available configured model.
    for model in _model_candidates:
        stat = _model_stats.get(model, {"lockout_until": 0.0})
        if time.time() >= stat.get("lockout_until", 0.0):
            return model

    return _active_model


def _should_skip_feature_in_tier(feature):
    tier = _current_budget_tier()
    if tier == "RED" and feature == "refine" and config.CLAUDE_RED_DISABLE_REFINE:
        return True
    return False


def _build_messages_payload(prompt):
    if not config.CLAUDE_ENABLE_PROMPT_CACHING:
        return [{"role": "user", "content": prompt}], None

    system_content = [
        {
            "type": "text",
            "text": _static_context_instruction,
            "cache_control": {"type": "ephemeral"}
        }
    ]
    user_content = [{"type": "text", "text": prompt}]
    messages = [{"role": "user", "content": user_content}]
    return messages, system_content


def _record_usage(model, feature, input_tokens, output_tokens):
    total_tokens = input_tokens + output_tokens
    day_key = datetime.utcnow().strftime("%Y-%m-%d")
    estimated_cost = _estimate_cost_usd(model, input_tokens, output_tokens)

    _session_usage["successful_calls"] += 1
    _session_usage["input_tokens"] += input_tokens
    _session_usage["output_tokens"] += output_tokens
    _session_usage["total_tokens"] += total_tokens
    _session_usage["estimated_cost_usd"] += estimated_cost

    _ensure_model_bucket(model)
    _session_usage["by_model"][model]["calls"] += 1
    _session_usage["by_model"][model]["input_tokens"] += input_tokens
    _session_usage["by_model"][model]["output_tokens"] += output_tokens
    _session_usage["by_model"][model]["total_tokens"] += total_tokens
    _session_usage["by_model"][model]["estimated_cost_usd"] += estimated_cost

    _ensure_feature_bucket(feature)
    _session_usage["by_feature"][feature]["calls"] += 1
    _session_usage["by_feature"][feature]["input_tokens"] += input_tokens
    _session_usage["by_feature"][feature]["output_tokens"] += output_tokens
    _session_usage["by_feature"][feature]["total_tokens"] += total_tokens
    _session_usage["by_feature"][feature]["estimated_cost_usd"] += estimated_cost

    _ensure_day_bucket(day_key)
    _session_usage["by_day"][day_key]["calls"] += 1
    _session_usage["by_day"][day_key]["input_tokens"] += input_tokens
    _session_usage["by_day"][day_key]["output_tokens"] += output_tokens
    _session_usage["by_day"][day_key]["total_tokens"] += total_tokens
    _session_usage["by_day"][day_key]["estimated_cost_usd"] += estimated_cost

    _log_budget_tier_change(_current_budget_tier())


def _maybe_log_usage_checkpoint():
    freq = int(getattr(config, "CLAUDE_USAGE_LOG_EVERY_N_CALLS", 0) or 0)
    if freq <= 0:
        return

    success_calls = _session_usage.get("successful_calls", 0)
    if success_calls <= 0 or (success_calls % freq) != 0:
        return

    log_event("Claude Usage Checkpoint", {
        "successful_calls": success_calls,
        "total_tokens": _session_usage.get("total_tokens", 0),
        "estimated_cost_usd": round(_session_usage.get("estimated_cost_usd", 0.0), 6),
        "budget_tier": _session_usage.get("budget_tier", "GREEN")
    })


def _build_context_prompt(market_data):
    return f"""
    Analyze this forex market context (price action + structure + flow):

    Current Price: {market_data.get('price')}
    EMA9 vs EMA50: {market_data.get('trend')}
    Volatility: {market_data.get('volatility')}
    Recent Structure: {market_data.get('structure')}
    Momentum: {market_data.get('momentum')}
    Session: {market_data.get('session')}
    DXY Trend: {market_data.get('dxy_trend')}
    Yields Trend: {market_data.get('yields_trend')}
    Silver Trend: {market_data.get('silver_trend')}
    Retail Long Ratio: {market_data.get('retail_long_ratio')}

    RESPOND IN THIS EXACT COMPACT FORMAT:
    BIAS: BULLISH|BEARISH|NEUTRAL
    MOMENTUM: EXPANDING|CONTRACTING|STABLE
    OBSERVATION: one concise sentence explaining why the trade works, what could invalidate it, and what institutions may be doing
    CONFIDENCE: 0-100
    """


def _build_refine_prompt(signal, market_context):
    return f"""
    Given this market context:
    - Market Bias: {market_context.get('bias')}
    - Momentum: {market_context.get('momentum')}
    - Observations: {market_context.get('observations')}

    And this quantitative signal:
    - Direction: {signal.get('direction')}
    - Score: {signal.get('score')}
    - Reason: {signal.get('breakdown')}

    RESPOND IN THIS EXACT COMPACT FORMAT:
    ALIGNED: YES|NO
    WHY: short reason the setup works or fails
    INVALIDATION: short condition that breaks the setup
    INSTITUTIONAL: short note on likely institutional flow
    WARNING: none|short warning text
    """


def _build_news_prompt(symbol, structure_items, news_items, calendar_items):
    headline_lines = []
    for item in (news_items or [])[: int(getattr(config, "NEWS_ITEMS", 6) or 6)]:
        title = item.get("title") or ""
        source = item.get("source") or ""
        headline_lines.append(f"- {title} ({source})")

    calendar_lines = []
    for event in (calendar_items or [])[: int(getattr(config, "NEWS_CALENDAR_ITEMS", 4) or 4)]:
        label = event.get("event") or ""
        impact = event.get("impact") or ""
        country = event.get("country") or ""
        date = event.get("date") or ""
        meta = f"{country} {impact}".strip()
        if meta:
            calendar_lines.append(f"- {label} ({meta}) @ {date}")
        else:
            calendar_lines.append(f"- {label} @ {date}")

    structure_lines = []
    for item in structure_items or []:
        tf = item.get("timeframe") or "?"
        structure = item.get("structure") or "UNKNOWN"
        swing_high = item.get("swing_high")
        swing_low = item.get("swing_low")
        structure_lines.append(
            f"- {tf}: {structure}, high={swing_high}, low={swing_low}"
        )

    headlines_text = "\n".join(headline_lines) if headline_lines else "- No headlines available"
    calendar_text = "\n".join(calendar_lines) if calendar_lines else "- No calendar events available"
    structure_text = "\n".join(structure_lines) if structure_lines else "- No structure data available"

    return f"""
    You are a macro news and market impact analyst for {symbol}.

    STRUCTURE SNAPSHOT:
    {structure_text}

    HEADLINES (latest):
    {headlines_text}

    ECONOMIC CALENDAR (upcoming):
    {calendar_text}

    RESPOND IN THIS EXACT FORMAT:
    SUMMARY: short narrative of the macro/news backdrop
    IMPACT: near-term trade impact on {symbol} (bullish/bearish/volatile/neutral + why)
    RISKS: key risk factors to watch (including geopolitical or policy shocks)
    WATCHLIST: specific topics to monitor in the next 1-3 days
    """


def _context_complexity_hint(market_data):
    score = 0
    volatility = str(market_data.get("volatility", "")).upper()
    structure = str(market_data.get("structure", "")).upper()
    momentum = str(market_data.get("momentum", "")).upper()

    if "TRANSITION" in volatility or "VOLATILE" in volatility:
        score += 1
    if "CHOCH" in structure or "CONFLICT" in structure:
        score += 1
    if "RSI=" in momentum:
        try:
            rsi_value = float(momentum.split("RSI=")[-1].strip())
            if rsi_value >= 70 or rsi_value <= 30:
                score += 1
        except ValueError:
            pass

    return score


def _refine_complexity_hint(signal, market_context):
    score = 0
    confidence = str(signal.get("confidence", "")).upper()
    signal_score = float(signal.get("score", 0) or 0)
    bias = str(market_context.get("bias", "")).upper()
    direction = str(signal.get("direction", "")).upper()

    if confidence in ["LOW", "MEDIUM"]:
        score += 1
    if signal_score < float(config.HIGH_POTENTIAL_MIN_SCORE):
        score += 1
    if bias == "BULLISH" and direction == "SELL":
        score += 1
    if bias == "BEARISH" and direction == "BUY":
        score += 1

    return score


def analyze_news_context(symbol, structure_items, news_items, calendar_items):
    """
    Claude analyzes macro/news context and expected impact.

    Returns:
        dict: summary, impact, risks, watchlist, raw
    """

    if not config.ENABLE_CLAUDE or not config.CLAUDE_API_KEY:
        return {
            "summary": "Claude disabled or API key missing",
            "impact": "UNKNOWN",
            "risks": "UNKNOWN",
            "watchlist": "UNKNOWN",
            "raw": "",
        }

    if not bool(getattr(config, "ENABLE_CLAUDE_NEWS_CONTEXT", True)):
        return {
            "summary": "Claude news context disabled",
            "impact": "UNKNOWN",
            "risks": "UNKNOWN",
            "watchlist": "UNKNOWN",
            "raw": "",
        }

    prompt = _build_news_prompt(symbol, structure_items, news_items, calendar_items)
    response = _call_claude_api(
        prompt,
        max_tokens=config.CLAUDE_NEWS_MAX_TOKENS,
        feature="news",
        complexity_hint=1,
    )

    if not response:
        return {
            "summary": "No Claude news response",
            "impact": "UNKNOWN",
            "risks": "UNKNOWN",
            "watchlist": "UNKNOWN",
            "raw": "",
        }

    return _parse_news_response(response)


def analyze_market_context(market_data):
    """
    Claude analyzes market CONTEXT ONLY (not trading decisions)
    
    Claude outputs:
    - Market bias (bullish/bearish/neutral)
    - Momentum (expanding/contracting)
    - Key observations
    - Context confidence (0-100)
    
    Claude DOES NOT output:
    - Entry/exit prices
    - Position sizing
    - Trading decisions
    - SL/TP levels
    
    Args:
        market_data (dict): price, structure, momentum data
        
    Returns:
        dict: context_analysis with bias, momentum, observations
    """
    
    global _cached_context

    if not config.ENABLE_CLAUDE:
        return {
            "bias": "NEUTRAL",
            "momentum": "UNKNOWN",
            "observations": [],
            "confidence": 0,
            "warning": "Claude disabled by config"
        }

    if not config.CLAUDE_API_KEY:
        return {
            "bias": "NEUTRAL",
            "momentum": "UNKNOWN",
            "observations": [],
            "confidence": 0,
            "warning": "Claude API key not configured"
        }
    
    prompt = _build_context_prompt(market_data)
    complexity_hint = _context_complexity_hint(market_data)
    response = _call_claude_api(
        prompt,
        max_tokens=config.CLAUDE_CONTEXT_MAX_TOKENS,
        feature="context",
        complexity_hint=complexity_hint
    )
    
    if not response:
        # Keep loop stable by returning the last known good context.
        return _cached_context
    
    # Parse Claude's response
    parsed = _parse_context_response(response)

    _cached_context = parsed
    return parsed


def refine_signal(signal, market_context):
    """
    Claude validates that signal makes sense in current context
    (Optional secondary check, not primary decision)
    
    Returns:
        dict: refined_signal with claude's notes
    """
    
    if not config.ENABLE_CLAUDE or not config.CLAUDE_API_KEY:
        return signal
    
    prompt = _build_refine_prompt(signal, market_context)
    complexity_hint = _refine_complexity_hint(signal, market_context)
    response = _call_claude_api(
        prompt,
        max_tokens=config.CLAUDE_REFINE_MAX_TOKENS,
        feature="refine",
        complexity_hint=complexity_hint
    )
    
    if response:
        signal["claude_validation"] = response
    
    return signal


def _warmup_active_model():
    """Probe configured models once at startup and pick the first healthy model."""

    global _warmup_done, _active_model

    if _warmup_done:
        return

    _warmup_done = True

    for model in _model_candidates:
        stat = _model_stats.get(model, {"failures": 0, "lockout_until": 0.0})
        if time.time() < stat["lockout_until"]:
            continue

        ok = _probe_model(model)
        if ok:
            _active_model = model
            log_event("Claude Warmup Selected Model", {"model": _active_model})
            return

    log_event("Claude Warmup Failed", {
        "reason": "No configured model responded during startup probe"
    })


def _probe_model(model):
    """Small test call to verify model availability."""

    headers = {
        "x-api-key": config.CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    payload = {
        "model": model,
        "max_tokens": 10,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "ping"}]}]
    }

    try:
        _session_usage["total_api_calls"] += 1
        _session_usage["warmup_calls"] += 1
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=min(config.CLAUDE_TIMEOUT_SECS, 10)
        )

        if response.status_code == 200:
            _session_usage["successful_calls"] += 1
            _model_stats[model]["failures"] = 0
            return True

        _session_usage["failed_calls"] += 1
        _mark_model_failure(model, f"Warmup HTTP {response.status_code}: {response.text[:300]}")
        return False
    except requests.exceptions.RequestException as e:
        _session_usage["failed_calls"] += 1
        _mark_model_failure(model, f"Warmup exception: {str(e)}")
        return False


def _mark_model_failure(model, error_text):
    """Track model failures and lock it out when threshold is reached."""

    stat = _model_stats.setdefault(model, {"failures": 0, "lockout_until": 0.0})
    stat["failures"] += 1

    if stat["failures"] >= config.CLAUDE_MAX_CONSECUTIVE_FAILURES:
        stat["lockout_until"] = time.time() + config.CLAUDE_MODEL_LOCKOUT_SECS
        stat["failures"] = 0
        log_event("Claude Model Locked", {
            "model": model,
            "lockout_secs": config.CLAUDE_MODEL_LOCKOUT_SECS,
            "reason": "Repeated failures"
        })

    last_log = _last_model_failure_log_ts.get(model, 0.0)
    now = time.time()
    if now - last_log >= 30:
        log_event("Claude Model Failure", {
            "model": model,
            "error": error_text,
            "failures": stat["failures"]
        })
        _last_model_failure_log_ts[model] = now


def _select_next_available_model():
    """Pick next model that is not currently locked out."""

    global _active_model

    now = time.time()
    for model in _model_candidates:
        stat = _model_stats.get(model, {"failures": 0, "lockout_until": 0.0})
        if now >= stat["lockout_until"]:
            if model != _active_model:
                _active_model = model
                log_event("Claude Model Switched", {"model": _active_model})
            return True

    return False


def _call_claude_api(prompt, max_tokens=300, feature="context", complexity_hint=0):
    """Internal API call to Claude"""

    global _last_call_ts, _last_error_log_ts, _disabled_until_ts, _consecutive_failures, _active_model

    now = time.time()

    _warmup_active_model()

    if _should_skip_feature_in_tier(feature):
        return None

    # Respect cooldown between calls to avoid API spam every bot loop.
    if now - _last_call_ts < config.CLAUDE_CALL_COOLDOWN_SECS:
        return None

    # Temporary disable window after repeated failures.
    if now < _disabled_until_ts:
        return None
    
    headers = {
        "x-api-key": config.CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    routed_model = _pick_model_for_call(feature, complexity_hint)
    chosen_model = _resolve_routed_model(routed_model)
    if chosen_model:
        _active_model = chosen_model

    messages, system_content = _build_messages_payload(prompt)
    if config.CLAUDE_ENABLE_PROMPT_CACHING:
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"
    
    data = {
        "model": _active_model,
        "max_tokens": max_tokens,
        "messages": messages
    }

    if system_content:
        data["system"] = system_content
    
    try:
        current_model = _active_model
        _last_call_ts = now
        _session_usage["total_api_calls"] += 1
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=data,
            timeout=config.CLAUDE_TIMEOUT_SECS
        )

        if response.status_code >= 400:
            err_detail = response.text[:500]
            raise requests.exceptions.HTTPError(
                f"HTTP {response.status_code} for Claude API: {err_detail}",
                response=response
            )

        result = response.json()
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        total_tokens = input_tokens + output_tokens

        _record_usage(current_model, feature, input_tokens, output_tokens)

        if "content" in result and len(result["content"]) > 0:
            block = result["content"][0]
            text = block.get("text", "") if isinstance(block, dict) else ""
            log_event("Claude Context Analysis", {
                "feature": feature,
                "model": current_model,
                "tokens": output_tokens,
                "budget_tier": _session_usage.get("budget_tier", "GREEN")
            })
            _maybe_log_usage_checkpoint()
            _consecutive_failures = 0
            _model_stats[current_model]["failures"] = 0
            return text

    except requests.exceptions.RequestException as e:
        _session_usage["failed_calls"] += 1
        _consecutive_failures += 1
        error_text = str(e)

        current_model = _active_model
        _mark_model_failure(current_model, error_text)
        switched = _select_next_available_model()
        if switched and _active_model != current_model:
            log_event("Claude Model Fallback", {
                "from_model": current_model,
                "new_model": _active_model,
                "reason": "Model failure"
            })

        # Throttle error logging to once every 30s.
        if now - _last_error_log_ts >= 30:
            log_event("Claude API Error", {
                "error": error_text,
                "model": current_model,
                "consecutive_failures": _consecutive_failures
            })
            _last_error_log_ts = now

        if _consecutive_failures >= config.CLAUDE_MAX_CONSECUTIVE_FAILURES and not switched:
            _disabled_until_ts = now + config.CLAUDE_FAILURE_DISABLE_SECS
            log_event("Claude Disabled Temporarily", {
                "disable_seconds": config.CLAUDE_FAILURE_DISABLE_SECS,
                "reason": "No available Claude model after repeated failures"
            })
    
    return None


def get_session_usage_summary():
    """Return Claude API usage stats for the current bot session."""

    summary = {
        "enabled": config.ENABLE_CLAUDE,
        "api_key_configured": bool(config.CLAUDE_API_KEY),
        "total_api_calls": _session_usage["total_api_calls"],
        "successful_calls": _session_usage["successful_calls"],
        "failed_calls": _session_usage["failed_calls"],
        "warmup_calls": _session_usage["warmup_calls"],
        "input_tokens": _session_usage["input_tokens"],
        "output_tokens": _session_usage["output_tokens"],
        "total_tokens": _session_usage["total_tokens"],
        "estimated_cost_usd": round(_session_usage["estimated_cost_usd"], 6),
        "budget_tier": _session_usage.get("budget_tier", "GREEN"),
        "by_model": _session_usage["by_model"],
        "by_feature": _session_usage["by_feature"],
        "by_day": _session_usage["by_day"]
    }

    budget = int(getattr(config, "CLAUDE_SESSION_TOKEN_BUDGET", 0) or 0)
    if budget > 0:
        summary["token_budget"] = budget
        summary["tokens_remaining"] = max(0, budget - summary["total_tokens"])

    return summary


def _parse_context_response(response):
    """Parse Claude's context response"""
    
    # Simple parsing - Claude should follow format strictly
    lines = response.split('\n')
    
    result = {
        "bias": "NEUTRAL",
        "momentum": "UNKNOWN",
        "observations": [],
        "confidence": 50,
        "raw": response
    }
    
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.upper().startswith("OBSERVATION:"):
            observation = line.split(":", 1)[1].strip()
            if observation:
                result["observations"].append(observation)

        line_upper = line.upper()
        if "BULLISH" in line_upper:
            result["bias"] = "BULLISH"
        elif "BEARISH" in line_upper:
            result["bias"] = "BEARISH"
        
        if "EXPAND" in line_upper:
            result["momentum"] = "EXPANDING"
        elif "CONTRACT" in line_upper:
            result["momentum"] = "CONTRACTING"
        
        if any(c.isdigit() for c in line):
            # Try to extract confidence score
            import re
            numbers = re.findall(r'\d+', line)
            if numbers:
                result["confidence"] = min(int(numbers[0]), 100)
    
    return result


def _parse_news_response(response):
    """Parse Claude's news response format."""

    result = {
        "summary": "",
        "impact": "",
        "risks": "",
        "watchlist": "",
        "raw": response,
    }

    for line in response.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        upper = stripped.upper()
        if upper.startswith("SUMMARY:"):
            result["summary"] = stripped.split(":", 1)[1].strip()
        elif upper.startswith("IMPACT:"):
            result["impact"] = stripped.split(":", 1)[1].strip()
        elif upper.startswith("RISKS:"):
            result["risks"] = stripped.split(":", 1)[1].strip()
        elif upper.startswith("WATCHLIST:"):
            result["watchlist"] = stripped.split(":", 1)[1].strip()

    if not result["summary"]:
        result["summary"] = "No summary returned"
    if not result["impact"]:
        result["impact"] = "No impact returned"
    if not result["risks"]:
        result["risks"] = "No risks returned"
    if not result["watchlist"]:
        result["watchlist"] = "No watchlist returned"

    return result


class ClaudeAI:
    """Wrapper class for Claude AI functionality."""
    
    def __init__(self):
        """Initialize ClaudeAI wrapper."""
        pass
    
    def get_market_summary(self, prompt):
        """Get a market summary from Claude."""
        try:
            # Use the internal _call_claude_api function to get a response
            response = _call_claude_api(
                prompt,
                max_tokens=500,
                feature="market_summary",
                complexity_hint=1
            )
            return response if response else "Unable to generate market summary"
        except Exception as e:
            return f"Error generating market summary: {str(e)}"
