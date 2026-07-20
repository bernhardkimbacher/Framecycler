"""OpenTimelineIO timeline construction and mutation for Framecycler.

The live source of truth is an ``otio.schema.Timeline`` whose video track is a
sequence of per-shot ``Stack``s. Each stack holds one or more version ``Clip``s;
active/compare selection lives in ``stack.metadata["framecycler"]``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import unquote, urlparse

import opentimelineio as otio

FC_META = "framecycler"
VIDEO_TRACK_NAME = "V1"
CDL_KEY = "cdl"
INPUT_COLORSPACE_KEY = "input_colorspace"

_IDENTITY_CDL: Dict[str, Any] = {
    "slope": [1.0, 1.0, 1.0],
    "offset": [0.0, 0.0, 0.0],
    "power": [1.0, 1.0, 1.0],
    "saturation": 1.0,
}


def _fc(item) -> Dict[str, Any]:
    """Return a mutable copy of Framecycler metadata; caller must write back via _set_fc."""
    raw = item.metadata.get(FC_META)
    if isinstance(raw, dict):
        return dict(raw)
    # OTIO AnyDictionary proxy — convert safely
    try:
        return dict(raw) if raw is not None else {}
    except Exception:
        return {}


def _set_fc(item, meta: Dict[str, Any]) -> None:
    item.metadata[FC_META] = dict(meta)


def get_fc_meta(item) -> Dict[str, Any]:
    """Public read of ``metadata['framecycler']`` (copy)."""
    return _fc(item)


def update_fc_meta(item, **fields: Any) -> Dict[str, Any]:
    """Merge fields into ``metadata['framecycler']`` and write back."""
    meta = _fc(item)
    meta.update(fields)
    _set_fc(item, meta)
    return meta


def identity_cdl() -> Dict[str, Any]:
    return {
        "slope": list(_IDENTITY_CDL["slope"]),
        "offset": list(_IDENTITY_CDL["offset"]),
        "power": list(_IDENTITY_CDL["power"]),
        "saturation": float(_IDENTITY_CDL["saturation"]),
    }


def _as_rgb_list(value: Any) -> List[float]:
    # OTIO stores vectors as AnyVector (iterable but not list/tuple).
    if isinstance(value, (str, bytes)):
        scalar = float(value)
        return [scalar, scalar, scalar]
    try:
        items = list(value)
        if len(items) >= 3:
            return [float(items[0]), float(items[1]), float(items[2])]
    except TypeError:
        pass
    scalar = float(value)
    return [scalar, scalar, scalar]


def _as_plain_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Convert OTIO AnyDictionary / Mapping to a plain dict."""
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return dict(value)
    except Exception:
        return None


def normalize_cdl(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a complete CDL dict with defaults filled in."""
    result = identity_cdl()
    plain = _as_plain_dict(data)
    if plain is None:
        return result
    if "slope" in plain:
        result["slope"] = _as_rgb_list(plain["slope"])
    if "offset" in plain:
        result["offset"] = _as_rgb_list(plain["offset"])
    if "power" in plain:
        result["power"] = _as_rgb_list(plain["power"])
    if "saturation" in plain:
        result["saturation"] = float(plain["saturation"])
    style = plain.get("style")
    if style in ("no_clamp", "asc"):
        result["style"] = style
    elif style is not None:
        # Preserve unknown string styles; ignore invalid types
        result["style"] = str(style)
    return result


def cdl_is_identity(data: Optional[Dict[str, Any]]) -> bool:
    cdl = normalize_cdl(data)
    return (
        cdl["slope"] == [1.0, 1.0, 1.0]
        and cdl["offset"] == [0.0, 0.0, 0.0]
        and cdl["power"] == [1.0, 1.0, 1.0]
        and abs(float(cdl["saturation"]) - 1.0) < 1e-9
    )


def get_cdl(item) -> Optional[Dict[str, Any]]:
    """Return normalized CDL if ``framecycler.cdl`` is present, else None (inherit)."""
    meta = _fc(item)
    if CDL_KEY not in meta:
        return None
    raw = meta.get(CDL_KEY)
    plain = _as_plain_dict(raw)
    if plain is None:
        return identity_cdl()
    return normalize_cdl(plain)


def set_cdl(
    item,
    *,
    slope: Any = None,
    offset: Any = None,
    power: Any = None,
    saturation: Optional[float] = None,
    style: Optional[str] = None,
) -> Dict[str, Any]:
    """Write/merge ASC CDL onto a Clip, Stack, or Timeline under ``framecycler.cdl``."""
    current = get_cdl(item) or identity_cdl()
    if slope is not None:
        current["slope"] = _as_rgb_list(slope)
    if offset is not None:
        current["offset"] = _as_rgb_list(offset)
    if power is not None:
        current["power"] = _as_rgb_list(power)
    if saturation is not None:
        current["saturation"] = float(saturation)
    if style is not None:
        if style not in ("no_clamp", "asc"):
            raise ValueError(f"Unsupported CDL style: {style!r}")
        current["style"] = style
    update_fc_meta(item, **{CDL_KEY: current})
    return current


def clear_cdl(item) -> None:
    """Remove stored CDL so inheritance from parent levels applies again."""
    meta = _fc(item)
    if CDL_KEY in meta:
        meta.pop(CDL_KEY, None)
        _set_fc(item, meta)


def resolve_cdl(
    clip=None,
    stack=None,
    timeline=None,
) -> Dict[str, Any]:
    """Resolve effective CDL: Clip > Stack > Timeline > identity."""
    for item in (clip, stack, timeline):
        if item is None:
            continue
        cdl = get_cdl(item)
        if cdl is not None:
            return cdl
    return identity_cdl()


def cdl_cache_key(cdl: Dict[str, Any]) -> str:
    """Stable string key for viewer apply caching."""
    norm = normalize_cdl(cdl)
    style = norm.get("style", "no_clamp")
    return (
        f"{norm['slope']}|{norm['offset']}|{norm['power']}|"
        f"{norm['saturation']}|{style}"
    )


def get_input_colorspace(clip) -> Optional[str]:
    """Return stored input colorspace on a Clip, or None if unset."""
    if clip is None:
        return None
    meta = _fc(clip)
    raw = meta.get(INPUT_COLORSPACE_KEY)
    if raw is None:
        return None
    name = str(raw).strip()
    return name or None


def set_input_colorspace(clip, name: str) -> str:
    """Persist input colorspace on a Clip under ``framecycler.input_colorspace``."""
    value = str(name).strip()
    if not value:
        raise ValueError("input_colorspace must be a non-empty string")
    update_fc_meta(clip, **{INPUT_COLORSPACE_KEY: value})
    return value


def clear_input_colorspace(clip) -> None:
    """Remove stored input colorspace from a Clip."""
    meta = _fc(clip)
    if INPUT_COLORSPACE_KEY in meta:
        meta.pop(INPUT_COLORSPACE_KEY, None)
        _set_fc(clip, meta)


def new_timeline(name: str = "Framecycler Session") -> otio.schema.Timeline:
    timeline = otio.schema.Timeline(name=name)
    track = otio.schema.Track(name=VIDEO_TRACK_NAME, kind=otio.schema.TrackKind.Video)
    timeline.tracks.append(track)
    meta = _fc(timeline)
    meta.setdefault("playback_rate", None)
    _set_fc(timeline, meta)
    return timeline


def video_track(timeline: otio.schema.Timeline) -> otio.schema.Track:
    for track in timeline.tracks:
        if getattr(track, "kind", None) == otio.schema.TrackKind.Video:
            return track
    if timeline.tracks:
        return timeline.tracks[0]
    track = otio.schema.Track(name=VIDEO_TRACK_NAME, kind=otio.schema.TrackKind.Video)
    timeline.tracks.append(track)
    return track


def shot_stacks(timeline: otio.schema.Timeline) -> List[otio.schema.Stack]:
    track = video_track(timeline)
    return [child for child in track if isinstance(child, otio.schema.Stack)]


def version_clips(stack: otio.schema.Stack) -> List[otio.schema.Clip]:
    return [child for child in stack if isinstance(child, otio.schema.Clip)]


def _clamp_index(index: int, count: int) -> int:
    if count <= 0:
        return 0
    return max(0, min(count - 1, index))


def active_index(stack: otio.schema.Stack) -> int:
    clips = version_clips(stack)
    return _clamp_index(int(_fc(stack).get("active", 0)), len(clips))


def compare_index(stack: otio.schema.Stack) -> int:
    clips = version_clips(stack)
    if len(clips) <= 1:
        return active_index(stack)
    raw = _fc(stack).get("compare", 0)
    idx = _clamp_index(int(raw), len(clips))
    if idx == active_index(stack) and len(clips) > 1:
        return 0 if active_index(stack) != 0 else 1
    return idx


def set_active_version(stack: otio.schema.Stack, index: int) -> None:
    clips = version_clips(stack)
    index = _clamp_index(index, len(clips))
    meta = _fc(stack)
    meta["active"] = index
    if "compare" not in meta:
        meta["compare"] = 0 if index != 0 else (1 if len(clips) > 1 else 0)
    _set_fc(stack, meta)
    _sync_stack_source_range(stack)


def set_compare_version(stack: otio.schema.Stack, index: int) -> None:
    clips = version_clips(stack)
    meta = _fc(stack)
    meta["compare"] = _clamp_index(index, len(clips))
    _set_fc(stack, meta)


def active_clip(stack: otio.schema.Stack) -> Optional[otio.schema.Clip]:
    clips = version_clips(stack)
    if not clips:
        return None
    return clips[active_index(stack)]


def compare_clip(stack: otio.schema.Stack) -> Optional[otio.schema.Clip]:
    clips = version_clips(stack)
    if not clips:
        return None
    return clips[compare_index(stack)]


def clip_available_range_frames(clip: otio.schema.Clip) -> tuple[int, int]:
    """Return (available_start, available_duration) in media timebase frames."""
    ref = clip.media_reference
    if ref is not None and ref.available_range is not None:
        start = int(round(ref.available_range.start_time.value))
        duration = max(0, int(round(ref.available_range.duration.value)))
        return start, duration
    meta = _fc(clip)
    frame_count = int(meta.get("frame_count", 0) or 0)
    return 0, max(0, frame_count)


def clip_source_start_frames(clip: otio.schema.Clip) -> int:
    """Start of the clip's source_range in media timebase frames."""
    try:
        if clip.source_range is not None:
            return int(round(clip.source_range.start_time.value))
    except Exception:
        pass
    avail_start, _ = clip_available_range_frames(clip)
    return avail_start


def clip_duration_frames(clip: otio.schema.Clip) -> int:
    try:
        if clip.source_range is not None:
            return max(0, int(round(clip.source_range.duration.value)))
    except Exception:
        pass
    _, avail_duration = clip_available_range_frames(clip)
    return avail_duration


def trim_active_version(
    stack: otio.schema.Stack,
    source_start_frame: int,
    duration_frames: int,
) -> bool:
    """
    Trim the active version's source_range, clamped to available media.
    Returns True if the clip was updated.
    """
    clip = active_clip(stack)
    if clip is None:
        return False
    avail_start, avail_duration = clip_available_range_frames(clip)
    if avail_duration <= 0:
        return False
    avail_end = avail_start + avail_duration
    rate = clip_rate(clip)
    start = max(avail_start, min(avail_end - 1, int(source_start_frame)))
    max_duration = avail_end - start
    duration = max(1, min(max_duration, int(duration_frames)))
    clip.source_range = otio.opentime.TimeRange(
        start_time=otio.opentime.RationalTime(start, rate),
        duration=otio.opentime.RationalTime(duration, rate),
    )
    _sync_stack_source_range(stack)
    return True


def clip_rate(clip: otio.schema.Clip, default: float = 24.0) -> float:
    ref = clip.media_reference
    if isinstance(ref, otio.schema.ImageSequenceReference):
        rate = float(ref.rate or 0.0)
        if rate > 0:
            return rate
    if ref is not None and ref.available_range is not None:
        rate = float(ref.available_range.start_time.rate or 0.0)
        if rate > 0:
            return rate
    meta = _fc(clip)
    rate = float(meta.get("fps", 0.0) or 0.0)
    return rate if rate > 0 else default


def clip_start_frame(clip: otio.schema.Clip) -> int:
    meta = _fc(clip)
    if "start_frame" in meta:
        return int(meta["start_frame"])
    ref = clip.media_reference
    if isinstance(ref, otio.schema.ImageSequenceReference):
        return int(ref.start_frame)
    if ref is not None and ref.available_range is not None:
        return int(round(ref.available_range.start_time.value))
    return 0


def _sync_stack_source_range(stack: otio.schema.Stack) -> None:
    """Trim the stack's contribution on the parent track to the active version length."""
    clip = active_clip(stack)
    if clip is None:
        stack.source_range = None
        return
    rate = clip_rate(clip)
    frames = clip_duration_frames(clip)
    stack.source_range = otio.opentime.TimeRange(
        start_time=otio.opentime.RationalTime(0, rate),
        duration=otio.opentime.RationalTime(frames, rate),
    )


def _shot_name_from_path(path: str) -> str:
    base = os.path.basename(path)
    name, _ = os.path.splitext(base)
    # Strip trailing frame number for sequences: shot.1001 -> shot
    stripped = re.sub(r"[._-]?\d+$", "", name)
    return stripped or name or base


def _sequence_parts_from_path(path: str) -> Optional[Dict[str, Any]]:
    """Derive ImageSequenceReference fields from a representative file path."""
    from ..decoders.base import _frame_token_match

    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        return None
    dir_name = os.path.dirname(abs_path)
    base_name = os.path.basename(abs_path)
    name_part, ext = os.path.splitext(base_name)
    match = _frame_token_match(name_part)
    if not match:
        return None
    digit_string = match.group(1)
    start_pos = match.start(1)
    end_pos = match.end(1)
    return {
        "target_url_base": otio.url_utils.url_from_filepath(dir_name.rstrip(os.sep) + os.sep),
        "name_prefix": name_part[:start_pos],
        "name_suffix": name_part[end_pos:] + ext,
        "start_frame": int(digit_string),
        "frame_zero_padding": len(digit_string),
    }


def clip_from_media(
    path: str,
    metadata: Optional[Dict[str, Any]] = None,
    *,
    name: Optional[str] = None,
) -> otio.schema.Clip:
    """Build an OTIO Clip + media reference from a filesystem path and decoder metadata."""
    metadata = dict(metadata or {})
    abs_path = os.path.abspath(path)
    fps = float(metadata.get("fps", 24.0) or 24.0)
    if fps <= 0:
        fps = 24.0
    frame_count = int(metadata.get("frame_count", 0) or 0)
    start_frame = int(metadata.get("start_frame", 0) or 0)
    available_range = otio.opentime.TimeRange(
        start_time=otio.opentime.RationalTime(0, fps),
        duration=otio.opentime.RationalTime(max(0, frame_count), fps),
    )

    ext = os.path.splitext(abs_path)[1].lower()
    is_sequence = ext in {".exr", ".dpx"} and frame_count > 1
    media_ref: otio.core.MediaReference
    if is_sequence:
        parts = _sequence_parts_from_path(abs_path)
        if parts is not None:
            media_ref = otio.schema.ImageSequenceReference(
                target_url_base=parts["target_url_base"],
                name_prefix=parts["name_prefix"],
                name_suffix=parts["name_suffix"],
                start_frame=start_frame if start_frame else parts["start_frame"],
                frame_step=1,
                rate=fps,
                frame_zero_padding=parts["frame_zero_padding"],
                available_range=available_range,
            )
        else:
            media_ref = otio.schema.ExternalReference(
                target_url=otio.url_utils.url_from_filepath(abs_path),
                available_range=available_range,
            )
    else:
        media_ref = otio.schema.ExternalReference(
            target_url=otio.url_utils.url_from_filepath(abs_path),
            available_range=available_range,
        )

    clip_name = name or os.path.basename(abs_path)
    clip = otio.schema.Clip(name=clip_name, media_reference=media_ref)
    clip.source_range = available_range
    fc = {
        "media_path": abs_path,
        "fps": fps,
        "start_frame": start_frame,
        "frame_count": frame_count,
        "width": int(metadata.get("width", 0) or 0),
        "height": int(metadata.get("height", 0) or 0),
        "pixel_aspect_ratio": float(metadata.get("pixel_aspect_ratio", 1.0) or 1.0),
    }
    if "layers" in metadata:
        fc["layers"] = list(metadata.get("layers") or [])
    _set_fc(clip, fc)
    return clip


def wrap_shot_stack(clip: otio.schema.Clip, name: Optional[str] = None) -> otio.schema.Stack:
    path = media_path_from_clip(clip) or clip.name or "shot"
    stack = otio.schema.Stack(name=name or _shot_name_from_path(path))
    stack.append(clip)
    # Merge-safe: preserve any pre-set stack FC keys (e.g. cdl) when initializing indices.
    meta = _fc(stack)
    meta.setdefault("active", 0)
    meta.setdefault("compare", 0)
    _set_fc(stack, meta)
    _sync_stack_source_range(stack)
    return stack


def append_shot(timeline: otio.schema.Timeline, clip: otio.schema.Clip) -> otio.schema.Stack:
    stack = wrap_shot_stack(clip)
    video_track(timeline).append(stack)
    _update_global_start(timeline)
    return stack


def add_version(stack: otio.schema.Stack, clip: otio.schema.Clip, *, make_active: bool = True) -> int:
    stack.append(clip)
    index = len(version_clips(stack)) - 1
    if make_active:
        set_active_version(stack, index)
        # Default compare to previous active (previous last index)
        if index > 0:
            set_compare_version(stack, index - 1)
    else:
        _sync_stack_source_range(stack)
    return index


def remove_shot(timeline: otio.schema.Timeline, stack: otio.schema.Stack) -> None:
    track = video_track(timeline)
    try:
        track.remove(stack)
    except ValueError:
        # Fall back to index-based removal
        for i, child in enumerate(list(track)):
            if child is stack:
                del track[i]
                break
    _update_global_start(timeline)


def remove_version(stack: otio.schema.Stack, index: int) -> bool:
    clips = version_clips(stack)
    if index < 0 or index >= len(clips):
        return False
    if len(clips) <= 1:
        return False
    clip = clips[index]
    try:
        stack.remove(clip)
    except ValueError:
        for i, child in enumerate(list(stack)):
            if child is clip:
                del stack[i]
                break
    meta = _fc(stack)
    active = int(meta.get("active", 0))
    compare = int(meta.get("compare", 0))
    if active >= index:
        active = max(0, active - 1)
    if compare >= index:
        compare = max(0, compare - 1)
    meta["active"] = active
    meta["compare"] = compare
    _set_fc(stack, meta)
    _sync_stack_source_range(stack)
    return True


def reorder_shots(timeline: otio.schema.Timeline, stacks: Sequence[otio.schema.Stack]) -> None:
    track = video_track(timeline)
    current = shot_stacks(timeline)
    if len(stacks) != len(current):
        return
    current_ids = {id(s) for s in current}
    if {id(s) for s in stacks} != current_ids:
        return
    # Clear track children that are stacks, then re-append in order.
    # Preserve non-stack children (gaps, etc.) by rebuilding.
    others = [c for c in list(track) if not isinstance(c, otio.schema.Stack)]
    while len(track):
        del track[0]
    for stack in stacks:
        track.append(stack)
    for other in others:
        track.append(other)
    _update_global_start(timeline)


def _update_global_start(timeline: otio.schema.Timeline) -> None:
    stacks = shot_stacks(timeline)
    if not stacks:
        timeline.global_start_time = None
        return
    clip = active_clip(stacks[0])
    if clip is None:
        timeline.global_start_time = None
        return
    rate = clip_rate(clip)
    start = clip_start_frame(clip)
    timeline.global_start_time = otio.opentime.RationalTime(start, rate)


def playback_rate_override(timeline: otio.schema.Timeline) -> Optional[float]:
    value = _fc(timeline).get("playback_rate")
    if value is None:
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def set_playback_rate_override(timeline: otio.schema.Timeline, rate: Optional[float]) -> None:
    meta = _fc(timeline)
    meta["playback_rate"] = None if rate is None else float(rate)
    _set_fc(timeline, meta)


def media_path_from_clip(clip: otio.schema.Clip) -> Optional[str]:
    meta = _fc(clip)
    stored = meta.get("media_path")
    if stored and isinstance(stored, str):
        return os.path.abspath(stored)

    ref = clip.media_reference
    if ref is None or isinstance(ref, otio.schema.MissingReference):
        return None
    if isinstance(ref, otio.schema.ExternalReference) and ref.target_url:
        return _filepath_from_url(ref.target_url)
    if isinstance(ref, otio.schema.ImageSequenceReference):
        try:
            return _filepath_from_url(ref.target_url_for_image_number(0))
        except Exception:
            return None
    return None


def _filepath_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    try:
        return otio.url_utils.filepath_from_url(url)
    except Exception:
        parsed = urlparse(url)
        if parsed.scheme in ("", "file"):
            return unquote(parsed.path)
        return url


def is_offline_clip(clip: otio.schema.Clip) -> bool:
    return bool(_fc(clip).get("offline", False))


def mark_clip_offline(clip: otio.schema.Clip, offline: bool = True) -> None:
    meta = _fc(clip)
    meta["offline"] = bool(offline)
    _set_fc(clip, meta)


def save_timeline(timeline: otio.schema.Timeline, path: str) -> None:
    otio.adapters.write_to_file(timeline, path)


def load_timeline(path: str) -> otio.schema.Timeline:
    timeline = otio.adapters.read_from_file(path)
    if not isinstance(timeline, otio.schema.Timeline):
        # Some adapters return a SerializableCollection
        raise ValueError(f"Expected an OTIO Timeline, got {type(timeline).__name__}")
    return coerce_to_shot_stacks(timeline)


def coerce_to_shot_stacks(timeline: otio.schema.Timeline) -> otio.schema.Timeline:
    """Normalize an imported timeline into Framecycler's Track-of-Stacks shape."""
    track = video_track(timeline)
    children = list(track)
    # Detach all children so we can re-parent freely
    while len(track):
        del track[0]

    for child in children:
        if isinstance(child, otio.schema.Stack):
            clips = [c for c in child if isinstance(c, otio.schema.Clip)]
            if not clips:
                # Pull clips out of nested compositions
                nested_clips = []
                for nested in list(child):
                    if isinstance(nested, otio.core.Composition):
                        nested_clips.extend(c.clone() for c in nested.find_clips())
                while len(child):
                    del child[0]
                for clip in nested_clips:
                    child.append(clip)
                clips = nested_clips
            if not clips:
                continue
            meta = _fc(child)
            meta.setdefault("active", 0)
            meta.setdefault("compare", 0)
            _set_fc(child, meta)
            _sync_stack_source_range(child)
            track.append(child)
        elif isinstance(child, otio.schema.Clip):
            track.append(wrap_shot_stack(child))
        elif isinstance(child, otio.core.Composition):
            for clip in child.find_clips():
                track.append(wrap_shot_stack(clip.clone()))
        # Gaps / transitions ignored for now

    _update_global_start(timeline)
    meta = _fc(timeline)
    meta.setdefault("playback_rate", None)
    _set_fc(timeline, meta)
    return timeline


def resolve_media_urls(timeline: otio.schema.Timeline, base_dir: str) -> None:
    """Resolve relative media paths against the .otio file directory."""
    base_dir = os.path.abspath(base_dir)
    for clip in timeline.find_clips():
        path = media_path_from_clip(clip)
        if path and not os.path.isabs(path):
            abs_path = os.path.abspath(os.path.join(base_dir, path))
            meta = _fc(clip)
            meta["media_path"] = abs_path
            _set_fc(clip, meta)
            ref = clip.media_reference
            if isinstance(ref, otio.schema.ExternalReference):
                ref.target_url = otio.url_utils.url_from_filepath(abs_path)
