"""Public surface for harn-gibson."""

from harn_gibson.catalog import CatalogEntry, VisualCatalog, default_visual_catalog
from harn_gibson.events import EventPhase, GibsonEvent, phase_for_event, summarize_event, to_jsonable
from harn_gibson.hooks import HookDecision, HookDispatcher, load_hook_module, result_for_harn
from harn_gibson.rendering import (
    DeterministicSceneRenderer,
    RenderPipeline,
    RenderPlan,
    RenderRequest,
    RenderStep,
    RenderSubmitResult,
)
from harn_gibson.routing import EventRouter, RenderInputBatch, RouteDecision, StreamBinding, TimelineWindow
from harn_gibson.scene import (
    SceneAnimation,
    SceneEngine,
    SceneMutation,
    ScenePrimitive,
    SceneState,
    default_mutations_for_event,
    initial_scene,
    scene_update_payload,
)

__all__ = [
    "CatalogEntry",
    "EventPhase",
    "GibsonEvent",
    "HookDecision",
    "HookDispatcher",
    "DeterministicSceneRenderer",
    "EventRouter",
    "RenderPipeline",
    "RenderInputBatch",
    "RenderPlan",
    "RenderRequest",
    "RenderStep",
    "RenderSubmitResult",
    "RouteDecision",
    "SceneAnimation",
    "SceneEngine",
    "SceneMutation",
    "ScenePrimitive",
    "SceneState",
    "StreamBinding",
    "TimelineWindow",
    "VisualCatalog",
    "default_mutations_for_event",
    "default_visual_catalog",
    "initial_scene",
    "load_hook_module",
    "phase_for_event",
    "result_for_harn",
    "scene_update_payload",
    "summarize_event",
    "to_jsonable",
]
