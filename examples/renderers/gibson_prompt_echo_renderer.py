"""Example prompt-command renderer for harn-gibson model-renderer dogfood runs."""

from __future__ import annotations

import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    messages = payload.get("messages", [])
    user_text = messages[-1].get("content", "") if messages and isinstance(messages[-1], dict) else ""
    event_type = "renderer_batch"
    for candidate in ("tool_call", "tool_result", "browser_input", "runtime_error", "input"):
        if candidate in user_text:
            event_type = candidate
            break
    json.dump(
        {
            "schema": "harn-gibson.render-plan.v1",
            "metadata": {
                "renderer": "prompt-echo",
                "intent": f"prompt-command visualization for {event_type}",
            },
            "steps": [
                {
                    "mutations": [
                        {
                            "op": "patch",
                            "targetId": "status",
                            "props": {
                                "text": f"model:{event_type}",
                                "phase": "lifecycle",
                                "tone": "cyan",
                            },
                        },
                        {
                            "op": "upsert",
                            "primitive": {
                                "id": "model-rain",
                                "kind": "data_rain",
                                "region": "stage",
                                "props": {
                                    "glyphs": "MODEL01<>[]{}",
                                    "columns": 28,
                                    "density": 0.62,
                                    "speed": 0.48,
                                    "tone": "green",
                                    "accentTone": "white",
                                    "opacity": 0.42,
                                    "bands": 2,
                                    "seed": 42,
                                },
                            },
                        },
                    ]
                }
            ],
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
