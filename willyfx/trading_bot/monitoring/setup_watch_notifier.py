"""Telegram watchlist alerts for bias, POIs, and setup progress."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import config
from monitoring.logger import log_event
from monitoring.telegram_notifier import escape_telegram_html, send_telegram_signal, telegram_is_configured


def _mins_to_secs(value, default):
    try:
        return max(float(value), 0.0) * 60.0
    except (TypeError, ValueError):
        return float(default) * 60.0


def _level(value):
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _zone_signature(zone):
    return (
        str(zone.get("timeframe", "")),
        str(zone.get("type", "")),
        str(zone.get("side", "")),
        round(float(zone.get("bottom", 0.0) or 0.0), 2),
        round(float(zone.get("top", 0.0) or 0.0), 2),
        bool(zone.get("tapped")),
        bool(zone.get("has_fvg_overlap")),
        bool(zone.get("has_fvg_in_front")),
        tuple(zone.get("nested_inside") or []),
    )


def _format_zone(zone):
    nested = zone.get("nested_inside") or []
    flags = []
    if zone.get("tapped"):
        flags.append("tapped")
    if zone.get("has_fvg_overlap"):
        flags.append("FVG overlap")
    if zone.get("has_fvg_in_front"):
        flags.append("FVG in front")
    if zone.get("mitigated"):
        flags.append("mitigated")
    if nested:
        flags.append(f"inside {'/'.join(str(item) for item in nested)}")

    flag_text = f" ({', '.join(flags)})" if flags else ""
    return (
        f"- {zone.get('timeframe')} {zone.get('side', '').upper()} {zone.get('type')}: "
        f"{_level(zone.get('bottom'))}-{_level(zone.get('top'))} "
        f"score {float(zone.get('score', 0.0) or 0.0):.1f}{flag_text}"
    )


def _confirmation_signature(signal):
    confirmation = signal.get("entry_confirmation") or {}
    if not confirmation:
        return None
    return (
        confirmation.get("timeframe"),
        confirmation.get("type"),
        round(float(confirmation.get("score", 0.0) or 0.0), 2),
    )


@dataclass
class SetupWatchNotifier:
    """Stateful Telegram throttler for non-trade strategy updates."""

    last_bias: str | None = None
    pending_bias: str | None = None
    pending_bias_count: int = 0
    last_bias_sent_at: float = 0.0
    last_poi_signature: tuple | None = None
    last_poi_sent_at: float = 0.0
    last_stage_signature: tuple | None = None
    last_stage_sent_at: float = 0.0
    sent_messages: list[str] = field(default_factory=list)

    def process(self, signal, candle_key=None, now_ts=None, mode="signal"):
        if not signal or not telegram_is_configured():
            return []

        now_ts = float(now_ts or time.time())
        sent = []
        sent.extend(self._maybe_send_bias_change(signal, candle_key, now_ts, mode))
        sent.extend(self._maybe_send_poi_digest(signal, candle_key, now_ts, mode))
        sent.extend(self._maybe_send_setup_progress(signal, candle_key, now_ts, mode))
        return sent

    def _maybe_send_bias_change(self, signal, candle_key, now_ts, mode):
        if not bool(getattr(config, "TELEGRAM_BIAS_CHANGE_ENABLED", True)):
            return []

        bias = str(signal.get("bias") or "NEUTRAL").upper()
        if bias not in ("BULLISH", "BEARISH"):
            return []

        if self.last_bias is None:
            self.last_bias = bias
            return []

        if bias == self.last_bias:
            self.pending_bias = None
            self.pending_bias_count = 0
            return []

        if bias == self.pending_bias:
            self.pending_bias_count += 1
        else:
            self.pending_bias = bias
            self.pending_bias_count = 1

        required = max(int(getattr(config, "HTF_BIAS_CONFIRM_CANDLES", 2) or 2), 1)
        cooldown = _mins_to_secs(getattr(config, "TELEGRAM_BIAS_CHANGE_COOLDOWN_MINS", 45), 45)
        if self.pending_bias_count < required or (now_ts - self.last_bias_sent_at) < cooldown:
            return []

        old_bias = self.last_bias
        self.last_bias = bias
        self.pending_bias = None
        self.pending_bias_count = 0
        self.last_bias_sent_at = now_ts

        message = (
            f"HTF BIAS CHANGED ({escape_telegram_html(mode.upper())})\n"
            f"Symbol: {escape_telegram_html(config.SYMBOL)}\n"
            f"Candle: {escape_telegram_html(candle_key)}\n"
            f"Bias: {escape_telegram_html(old_bias)} -> {escape_telegram_html(bias)}\n"
            f"Score: {float(signal.get('score', 0.0) or 0.0):.1f}"
        )
        return self._send("HTF_BIAS_CHANGE_ALERT", message)

    def _maybe_send_poi_digest(self, signal, candle_key, now_ts, mode):
        if not bool(getattr(config, "TELEGRAM_POI_WATCH_ENABLED", True)):
            return []

        zones = signal.get("zone_stack") or []
        if not zones:
            return []

        top_zones = sorted(zones, key=lambda item: (not item.get("tapped"), -float(item.get("score", 0.0) or 0.0)))[:5]
        signature = tuple(_zone_signature(zone) for zone in top_zones)
        change_cooldown = _mins_to_secs(getattr(config, "TELEGRAM_POI_CHANGE_COOLDOWN_MINS", 15), 15)
        digest_interval = _mins_to_secs(getattr(config, "TELEGRAM_POI_DIGEST_INTERVAL_MINS", 30), 30)
        changed = signature != self.last_poi_signature
        due_digest = (now_ts - self.last_poi_sent_at) >= digest_interval

        if not ((changed and (now_ts - self.last_poi_sent_at) >= change_cooldown) or due_digest):
            return []

        self.last_poi_signature = signature
        self.last_poi_sent_at = now_ts

        zone_text = "\n".join(_format_zone(zone) for zone in top_zones)
        message = (
            f"POI WATCHLIST ({escape_telegram_html(mode.upper())})\n"
            f"Symbol: {escape_telegram_html(config.SYMBOL)}\n"
            f"Candle: {escape_telegram_html(candle_key)}\n"
            f"Bias: {escape_telegram_html(signal.get('bias') or 'NEUTRAL')}\n"
            f"Decision: {escape_telegram_html(signal.get('direction') or 'WAIT')}\n"
            f"Score: {float(signal.get('score', 0.0) or 0.0):.1f}\n\n"
            f"Zones:\n{escape_telegram_html(zone_text)}"
        )
        return self._send("POI_WATCHLIST_ALERT", message)

    def _maybe_send_setup_progress(self, signal, candle_key, now_ts, mode):
        if not bool(getattr(config, "TELEGRAM_SETUP_PROGRESS_ENABLED", True)):
            return []

        zones = signal.get("zone_stack") or []
        tapped_count = sum(1 for zone in zones if zone.get("tapped"))
        sweep = bool(signal.get("sweep_detected"))
        confirmation_sig = _confirmation_signature(signal)
        direction = signal.get("direction") or signal.get("bias") or "WAIT"

        if signal.get("direction"):
            stage = "VALID_SIGNAL"
        elif confirmation_sig and sweep and tapped_count:
            stage = "TAP_SWEEP_CONFIRMATION"
        elif sweep and tapped_count:
            stage = "TAP_LIQUIDITY_SWEEP"
        elif confirmation_sig and tapped_count:
            stage = "TAP_CONFIRMATION"
        elif tapped_count:
            stage = "POI_TAPPED"
        else:
            return []

        signature = (
            str(direction).upper(),
            stage,
            tapped_count,
            bool(sweep),
            confirmation_sig,
            tuple(_zone_signature(zone) for zone in zones if zone.get("tapped"))[:3],
        )
        cooldown = _mins_to_secs(getattr(config, "TELEGRAM_SETUP_PROGRESS_COOLDOWN_MINS", 10), 10)
        if signature == self.last_stage_signature or (now_ts - self.last_stage_sent_at) < cooldown:
            return []

        self.last_stage_signature = signature
        self.last_stage_sent_at = now_ts

        tapped_zones = [_format_zone(zone) for zone in zones if zone.get("tapped")][:3]
        confirmation = signal.get("entry_confirmation") or {}
        confirmation_text = (
            f"{confirmation.get('timeframe')} {confirmation.get('type')} score {float(confirmation.get('score', 0.0) or 0.0):.1f}"
            if confirmation else "waiting"
        )
        message = (
            f"SETUP PROGRESS ({escape_telegram_html(mode.upper())})\n"
            f"Symbol: {escape_telegram_html(config.SYMBOL)}\n"
            f"Candle: {escape_telegram_html(candle_key)}\n"
            f"Stage: {escape_telegram_html(stage)}\n"
            f"Bias/Direction: {escape_telegram_html(direction)}\n"
            f"Score: {float(signal.get('score', 0.0) or 0.0):.1f}\n"
            f"Liquidity sweep: {'YES' if sweep else 'NO'}\n"
            f"Entry confirmation: {escape_telegram_html(confirmation_text)}\n\n"
            f"Tapped zones:\n{escape_telegram_html(chr(10).join(tapped_zones) or '- N/A')}"
        )
        return self._send("SETUP_PROGRESS_ALERT", message)

    def _send(self, event_name, message):
        ok = send_telegram_signal(message)
        log_event(event_name, {"sent": ok})
        if ok:
            self.sent_messages.append(event_name)
            return [event_name]
        return []
