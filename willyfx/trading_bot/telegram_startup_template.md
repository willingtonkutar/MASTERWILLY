# Telegram Startup / Point-Of-Interest (POI) Message Template

Use this as the formatted message the bot sends on startup or when reporting a POI.

BOT STARTED

- Symbol: {{SYMBOL}}
- Mode: {{MODE}}  # Alert | Live
- Start Time: {{START_TIME}}

HTF Bias Summary:
- D1: {{D1_TREND}}
- H4: {{H4_TREND}}
- H1: {{H1_TREND}}
- M15: {{M15_STATE}}

Current Point of Interest (POI):
- Type: {{POI_TYPE}}  # e.g., Bullish OB + FVG overlap
- Timeframe: {{POI_TF}}
- Zone: {{POI_LOW}} - {{POI_HIGH}}
- Liquidity: {{POI_LIQUIDITY}}  # e.g., sell-side sweep pending
- Waiting for: {{POI_CONFIRMATION}}  # e.g., M5 CHOCH or iFVG confirmation

Signal Preview:
- Current Score: {{SCORE}}
- Confidence: {{CONFIDENCE}}
- Bias: {{BIAS}}

Notes / Warnings:
- {{NOTES}}

Example usage when sending via `monitoring.telegram_notifier`:

1. Build a brief summary dict with the values above.
2. Format as HTML (or Markdown if configured) and escape content.
3. Send using your existing `send_telegram_signal()` helper.
