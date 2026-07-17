"""Session controller: OTIO timeline + media pool + playback plan."""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import opentimelineio as otio

from . import otio_model
from .media_pool import MediaPool
from .media_source import MediaSource
from .playback_plan import PlaybackPlan, VersionSlot, build as build_plan
from .settings import Settings

TimelineChangedCallback = Callable[[], None]


class Session:
    """Owns the live OTIO timeline and derived playback state."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.timeline: otio.schema.Timeline = otio_model.new_timeline()
        self.media_pool = MediaPool(settings)
        self.plan = PlaybackPlan()
        self._on_changed: Optional[TimelineChangedCallback] = None
        # Selected shot / version for metadata UI (independent of playhead sequence shot)
        self.selected_shot_index: int = 0
        self.selected_version_index: int = 0

    def set_changed_callback(self, callback: Optional[TimelineChangedCallback]) -> None:
        self._on_changed = callback

    def set_frame_ready_callback(self, callback) -> None:
        self.media_pool.set_frame_ready_callback(callback)

    def _notify(self) -> None:
        self.plan = build_plan(self.timeline, self.media_pool)
        if self.plan.segments:
            self.selected_shot_index = max(
                0, min(self.selected_shot_index, len(self.plan.segments) - 1)
            )
            versions = self.plan.segments[self.selected_shot_index].versions
            if versions:
                self.selected_version_index = max(
                    0, min(self.selected_version_index, len(versions) - 1)
                )
            else:
                self.selected_version_index = 0
        else:
            self.selected_shot_index = 0
            self.selected_version_index = 0
        if self._on_changed is not None:
            self._on_changed()

    def clear(self) -> None:
        self.media_pool.clear()
        self.timeline = otio_model.new_timeline()
        self.plan = PlaybackPlan()
        self.selected_shot_index = 0
        self.selected_version_index = 0
        if self._on_changed is not None:
            self._on_changed()

    @property
    def empty(self) -> bool:
        return self.plan.empty

    @property
    def shot_count(self) -> int:
        return len(self.plan.segments)

    def selected_version(self) -> Optional[VersionSlot]:
        if self.plan.empty:
            return None
        segment = self.plan.segments[self.selected_shot_index]
        if not segment.versions:
            return None
        idx = max(0, min(self.selected_version_index, len(segment.versions) - 1))
        return segment.versions[idx]

    def selected_source(self) -> Optional[MediaSource]:
        version = self.selected_version()
        if version is None or version.offline:
            return None
        return version.source

    def acquire_clip_media(self, clip: otio.schema.Clip) -> Optional[MediaSource]:
        path = otio_model.media_path_from_clip(clip)
        if not path:
            otio_model.mark_clip_offline(clip, True)
            return None
        if not os.path.exists(path):
            otio_model.mark_clip_offline(clip, True)
            return None
        try:
            source = self.media_pool.acquire(path)
            otio_model.mark_clip_offline(clip, False)
            # Keep OTIO metadata in sync with decoder
            fc = otio_model._fc(clip)
            fc["media_path"] = source.path
            fc["fps"] = source.fps
            fc["start_frame"] = source.decoder_start_frame
            fc["frame_count"] = source.frame_count
            fc["width"] = source.width
            fc["height"] = source.height
            fc["pixel_aspect_ratio"] = source.pixel_aspect_ratio
            otio_model._set_fc(clip, fc)
            return source
        except Exception:
            otio_model.mark_clip_offline(clip, True)
            return None

    def add_media(
        self,
        paths: Sequence[str],
        *,
        mode: str = "sequence",
        playhead_frame: Optional[int] = None,
    ) -> int:
        """
        Add media files to the session.

        mode:
          - \"replace\": clear timeline, then append as sequence
          - \"sequence\": append each file as a new shot
          - \"stack\": add each file as a version of the shot under playhead
        """
        valid_paths = [p for p in paths if p and os.path.exists(p)]
        if not valid_paths:
            return 0

        if mode == "replace":
            self.media_pool.clear()
            self.timeline = otio_model.new_timeline()

        loaded = 0
        if mode == "stack" and not otio_model.shot_stacks(self.timeline):
            mode = "sequence"

        target_stack = None
        if mode == "stack":
            target_stack = self._stack_at_playhead(playhead_frame)

        errors: list[str] = []
        for path in valid_paths:
            acquired = False
            try:
                source = self.media_pool.acquire(path)
                acquired = True
                clip = otio_model.clip_from_media(path, source.metadata)
                otio_model.mark_clip_offline(clip, False)

                if mode == "stack" and target_stack is not None:
                    otio_model.add_version(target_stack, clip, make_active=True)
                else:
                    otio_model.append_shot(self.timeline, clip)
                loaded += 1
            except Exception as exc:
                if acquired:
                    self.media_pool.release(path)
                errors.append(f"{os.path.basename(path)}: {exc}")

        self._prune_unused_media()
        self._notify()
        if errors and loaded == 0:
            raise RuntimeError("; ".join(errors))
        self._last_add_errors = errors
        return loaded

    def _stack_at_playhead(self, playhead_frame: Optional[int]):
        if self.plan.empty:
            stacks = otio_model.shot_stacks(self.timeline)
            return stacks[-1] if stacks else None
        frame = playhead_frame if playhead_frame is not None else self.plan.global_start
        segment = self.plan.segment_at(frame)
        return segment.stack if segment is not None else None

    def remove_shot(self, shot_index: int) -> None:
        stacks = otio_model.shot_stacks(self.timeline)
        if shot_index < 0 or shot_index >= len(stacks):
            return
        stack = stacks[shot_index]
        paths = []
        for clip in otio_model.version_clips(stack):
            path = otio_model.media_path_from_clip(clip)
            if path:
                paths.append(path)
        otio_model.remove_shot(self.timeline, stack)
        for path in paths:
            self.media_pool.release(path)
        self._notify()

    def remove_version(self, shot_index: int, version_index: int) -> None:
        stacks = otio_model.shot_stacks(self.timeline)
        if shot_index < 0 or shot_index >= len(stacks):
            return
        stack = stacks[shot_index]
        clips = otio_model.version_clips(stack)
        if version_index < 0 or version_index >= len(clips):
            return
        if len(clips) <= 1:
            # Removing the last version removes the shot
            self.remove_shot(shot_index)
            return
        clip = clips[version_index]
        path = otio_model.media_path_from_clip(clip)
        if otio_model.remove_version(stack, version_index):
            if path:
                self.media_pool.release(path)
            self._notify()

    def reorder_shots(self, shot_indices: Sequence[int]) -> None:
        stacks = otio_model.shot_stacks(self.timeline)
        if len(shot_indices) != len(stacks):
            return
        try:
            reordered = [stacks[i] for i in shot_indices]
        except IndexError:
            return
        otio_model.reorder_shots(self.timeline, reordered)
        self._notify()

    def set_active_version(self, shot_index: int, version_index: int) -> None:
        stacks = otio_model.shot_stacks(self.timeline)
        if shot_index < 0 or shot_index >= len(stacks):
            return
        otio_model.set_active_version(stacks[shot_index], version_index)
        self.selected_shot_index = shot_index
        self.selected_version_index = version_index
        self._notify()

    def trim_active_version(
        self,
        shot_index: int,
        source_start_frame: int,
        duration_frames: int,
    ) -> None:
        stacks = otio_model.shot_stacks(self.timeline)
        if shot_index < 0 or shot_index >= len(stacks):
            return
        if otio_model.trim_active_version(stacks[shot_index], source_start_frame, duration_frames):
            self.selected_shot_index = shot_index
            self._notify()

    def set_compare_version(self, shot_index: int, version_index: int) -> None:
        stacks = otio_model.shot_stacks(self.timeline)
        if shot_index < 0 or shot_index >= len(stacks):
            return
        otio_model.set_compare_version(stacks[shot_index], version_index)
        self._notify()

    def set_selection(self, shot_index: int, version_index: int = 0) -> None:
        if self.plan.empty:
            return
        self.selected_shot_index = max(0, min(shot_index, len(self.plan.segments) - 1))
        versions = self.plan.segments[self.selected_shot_index].versions
        if versions:
            self.selected_version_index = max(0, min(version_index, len(versions) - 1))
        else:
            self.selected_version_index = 0

    def effective_fps(self, playhead_frame: Optional[int] = None) -> float:
        override = otio_model.playback_rate_override(self.timeline)
        if override is not None:
            return override
        if self.plan.empty:
            return float(self.settings.default_fps)
        frame = playhead_frame if playhead_frame is not None else self.plan.global_start
        segment = self.plan.segment_at(frame)
        if segment is None:
            return float(self.settings.default_fps)
        return float(segment.rate)

    def set_playback_rate_override(self, rate: Optional[float]) -> None:
        otio_model.set_playback_rate_override(self.timeline, rate)
        # No structural change; callers update timer/UI themselves.

    def playback_rate_override(self) -> Optional[float]:
        return otio_model.playback_rate_override(self.timeline)

    def _stack_at(self, shot_index: int) -> Optional[otio.schema.Stack]:
        stacks = otio_model.shot_stacks(self.timeline)
        if shot_index < 0 or shot_index >= len(stacks):
            return None
        return stacks[shot_index]

    def _clip_at(self, shot_index: int, version_index: int) -> Optional[otio.schema.Clip]:
        stack = self._stack_at(shot_index)
        if stack is None:
            return None
        clips = otio_model.version_clips(stack)
        if version_index < 0 or version_index >= len(clips):
            return None
        return clips[version_index]

    def playhead_shot_version(
        self, playhead_frame: Optional[int] = None
    ) -> Optional[Tuple[int, int]]:
        """Return (shot_index, active_version_index) for the playhead shot."""
        stacks = otio_model.shot_stacks(self.timeline)
        if not stacks:
            return None
        if self.plan.empty:
            return (0, otio_model.active_index(stacks[0]))
        frame = playhead_frame if playhead_frame is not None else self.plan.global_start
        segment = self.plan.segment_at(frame)
        if segment is None:
            return (0, otio_model.active_index(stacks[0]))
        shot_index = max(0, min(segment.index, len(stacks) - 1))
        return (shot_index, otio_model.active_index(stacks[shot_index]))

    def set_clip_cdl(
        self,
        shot_index: int,
        version_index: int,
        *,
        slope: Any = None,
        offset: Any = None,
        power: Any = None,
        saturation: Optional[float] = None,
        style: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        clip = self._clip_at(shot_index, version_index)
        if clip is None:
            return None
        return otio_model.set_cdl(
            clip,
            slope=slope,
            offset=offset,
            power=power,
            saturation=saturation,
            style=style,
        )

    def set_stack_cdl(
        self,
        shot_index: int,
        *,
        slope: Any = None,
        offset: Any = None,
        power: Any = None,
        saturation: Optional[float] = None,
        style: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        stack = self._stack_at(shot_index)
        if stack is None:
            return None
        return otio_model.set_cdl(
            stack,
            slope=slope,
            offset=offset,
            power=power,
            saturation=saturation,
            style=style,
        )

    def set_timeline_cdl(
        self,
        *,
        slope: Any = None,
        offset: Any = None,
        power: Any = None,
        saturation: Optional[float] = None,
        style: Optional[str] = None,
    ) -> Dict[str, Any]:
        return otio_model.set_cdl(
            self.timeline,
            slope=slope,
            offset=offset,
            power=power,
            saturation=saturation,
            style=style,
        )

    def clear_clip_cdl(self, shot_index: int, version_index: int) -> bool:
        clip = self._clip_at(shot_index, version_index)
        if clip is None:
            return False
        otio_model.clear_cdl(clip)
        return True

    def clear_stack_cdl(self, shot_index: int) -> bool:
        stack = self._stack_at(shot_index)
        if stack is None:
            return False
        otio_model.clear_cdl(stack)
        return True

    def clear_timeline_cdl(self) -> None:
        otio_model.clear_cdl(self.timeline)

    def resolved_cdl_for_active(
        self, playhead_frame: Optional[int] = None
    ) -> Dict[str, Any]:
        """Resolve Clip > Stack > Timeline CDL for the playhead active version."""
        stacks = otio_model.shot_stacks(self.timeline)
        if not stacks:
            return otio_model.identity_cdl()
        loc = self.playhead_shot_version(playhead_frame)
        if loc is None:
            return otio_model.resolve_cdl(None, None, self.timeline)
        shot_index, version_index = loc
        stack = stacks[shot_index]
        clips = otio_model.version_clips(stack)
        clip = clips[version_index] if 0 <= version_index < len(clips) else None
        return otio_model.resolve_cdl(clip, stack, self.timeline)

    def export_timeline(self, path: str) -> None:
        otio_model.save_timeline(self.timeline, path)

    def import_timeline(self, path: str) -> None:
        timeline = otio_model.load_timeline(path)
        otio_model.resolve_media_urls(timeline, os.path.dirname(os.path.abspath(path)))
        self.media_pool.clear()
        self.timeline = timeline
        # Acquire media for every clip
        for clip in self.timeline.find_clips():
            self.acquire_clip_media(clip)
        self._notify()

    def _prune_unused_media(self) -> None:
        used = set()
        for clip in self.timeline.find_clips():
            path = otio_model.media_path_from_clip(clip)
            if path:
                used.add(os.path.abspath(path))
        for path in list(self.media_pool.paths()):
            if path not in used:
                # Force-release until gone
                while self.media_pool.get(path) is not None:
                    self.media_pool.release(path)

    def all_online_sources(self) -> List[MediaSource]:
        return [s for s in self.media_pool.sources() if not s.offline]
