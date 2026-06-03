"""Example external renderer command for harn-gibson dogfood runs."""

from __future__ import annotations

import json
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    requests = payload.get("requests") or []
    event = requests[-1]["event"] if requests else {"eventType": "idle", "sequence": 0, "timestampMs": 0}
    event_type = str(event.get("eventType") or "event")
    phase = str(event.get("phase") or "lifecycle")
    sequence = int(event.get("sequence") or 0)
    timestamp_ms = int(event.get("timestampMs") or 0)
    tone = {"before": "green", "during": "cyan", "after": "magenta"}.get(phase, "amber")
    text = event_type.upper().replace("_", "::")
    plan = {
        "schema": "harn-gibson.render-plan.v1",
        "metadata": {
            "renderer": "gibson-echo-example",
            "intent": f"external renderer echo for {event_type}",
        },
        "steps": [
            {
                "eventIndex": len(requests) - 1 if requests else 0,
                "mutations": [
                    {
                        "op": "patch",
                        "targetId": "status",
                        "props": {"text": f"external:{event_type}", "phase": phase, "tone": tone},
                    },
                    {
                        "op": "append_log",
                        "entry": {
                            "sequence": sequence,
                            "phase": phase,
                            "eventType": "external_renderer",
                            "title": "External renderer",
                            "summary": f"echo renderer mapped {event_type}",
                        },
                    },
                    {
                        "op": "upsert",
                        "primitive": {
                            "id": "external-vector",
                            "kind": "svg_layer",
                            "region": "stage",
                            "props": {
                                "tone": tone,
                                "viewBox": [0, 0, 100, 100],
                                "position": {"x": 0.50, "y": 0.35},
                                "scale": 0.18,
                                "spin": 0.06,
                                "blend": "screen",
                                "paths": [
                                    {
                                        "d": "M50 8 L86 28 L86 72 L50 92 L14 72 L14 28 Z",
                                        "tone": tone,
                                        "width": 1.6,
                                        "fill": True,
                                        "fillAlpha": 0.12,
                                    },
                                    {
                                        "d": "M24 54 C34 26 66 26 76 54 C64 76 36 76 24 54 Z",
                                        "tone": "cyan",
                                        "width": 1.8,
                                        "dash": [8, 7],
                                        "speed": 0.016,
                                    },
                                ],
                                "symbols": [
                                    {
                                        "kind": "globe",
                                        "x": 50,
                                        "y": 45,
                                        "r": 18,
                                        "tone": tone,
                                        "accentTone": "magenta",
                                        "packets": 6,
                                        "label": "EXT",
                                    },
                                    {
                                        "kind": "data_tunnel",
                                        "x": 50,
                                        "y": 45,
                                        "w": 38,
                                        "h": 24,
                                        "tone": "magenta",
                                        "accentTone": tone,
                                        "rings": 5,
                                        "packets": 8,
                                    }
                                ],
                                "labels": [{"text": text[:12], "x": 50, "y": 54, "tone": "white", "size": 6.8}],
                            },
                        },
                    },
                    {
                        "op": "start_animation",
                        "animation": {
                            "id": f"external-pulse-{sequence}",
                            "targetId": "external-vector",
                            "kind": "packet_burst",
                            "startedAtMs": timestamp_ms,
                            "durationMs": 1800,
                            "props": {"phase": phase, "tone": tone, "sequence": sequence},
                        },
                    },
                ],
            }
        ],
    }
    json.dump(plan, sys.stdout)


if __name__ == "__main__":
    main()
