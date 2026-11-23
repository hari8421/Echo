class FakeLLM:
    """Returns queued responses and records calls."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            return {"content": "ok", "tool_calls": []}
        response = self.responses.pop(0)
        if callable(response):
            response = response(messages)
        return response
