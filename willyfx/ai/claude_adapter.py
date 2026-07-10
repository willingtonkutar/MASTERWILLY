class ClaudeAdapter:
    """Minimal Claude adapter stub. Replace with your Claude integration."""

    def __init__(self, enabled=False):
        self.enabled = bool(enabled)

    def analyze(self, analysis_context):
        """Synchronous placeholder that returns None or a small summary."""
        if not self.enabled:
            return None
        # Here you would call your Claude wrapper and return structured insight.
        # Keep this simple for now.
        return {"recommendation": None, "reasoning": None}
