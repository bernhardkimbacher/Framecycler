"""Subprocess smoke for present-timing ring (Null RHI).

Bare QWindow + RhiRenderer can SIGBUS on macOS; callers should run this as a
subprocess and treat a crash as skip.
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FRAMECYCLER_FORCE_NULL_RHI", "1")

import numpy as np
from PySide6.QtGui import QGuiApplication, QWindow

from src.framecycler import framecycler_engine as eng

SIMPLE_VERT = """#version 440
layout(location = 0) in vec2 position;
layout(location = 1) in vec2 texCoord;
layout(location = 0) out vec2 v_uv;
layout(std140, binding = 0) uniform buf {
    float scale_x; float scale_y; float pan_x; float pan_y;
    int compare_mode; float wipe_pos; int channel_mask; int padding;
};
void main() {
    vec2 p = position * vec2(scale_x, scale_y) + vec2(pan_x, pan_y);
    gl_Position = vec4(p, 0.0, 1.0);
    v_uv = texCoord;
}
"""

SIMPLE_FRAG = """#version 440
layout(location = 0) in vec2 v_uv;
layout(location = 0) out vec4 fragColor;
layout(binding = 1) uniform sampler2D texA;
layout(binding = 2) uniform sampler2D texB;
layout(std140, binding = 0) uniform buf {
    float scale_x; float scale_y; float pan_x; float pan_y;
    int compare_mode; float wipe_pos; int channel_mask; int padding;
};
void main() { fragColor = texture(texA, v_uv); }
"""


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])
    window = QWindow()
    window.resize(64, 64)
    window.setSurfaceType(QWindow.SurfaceType.OpenGLSurface)
    window.create()
    window.show()
    app.processEvents()

    renderer = eng.RhiRenderer()
    renderer.set_force_null_backend(True)
    if not renderer.initialize(int(window.winId())):
        print("SKIP:init_failed")
        return 0

    renderer.set_exposed(True)
    renderer.set_pending_size(64, 64)
    renderer.set_present_timing_enabled(True)
    renderer.clear_present_timings()

    cache = eng.CacheManager(0.25)
    pixels = np.zeros((8, 8, 4), dtype=np.float16)
    cache.write_frame(0, 8, 8, 4, pixels)
    renderer.register_cache(0, cache)
    renderer.set_shader_sources("present_timing_test", SIMPLE_VERT, SIMPLE_FRAG)

    for i in range(8):
        params = eng.RenderParams()
        params.compare_mode = 0
        params.sequence_index = 0
        params.scale_x = 1.0
        params.scale_y = 1.0
        slot = eng.FrameSlotSpec()
        slot.source_index = 0
        slot.frame_index = 0
        slot.width = 8
        slot.height = 8
        slot.channels = 4
        slot.upload_token = i
        params.slots = [slot]
        renderer.update_render_params(params)
        renderer.request_redraw()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.005)
            if renderer.get_debug_stats().get("frames_drawn", 0) > i:
                break

    samples = list(renderer.drain_present_timings())
    renderer.set_present_timing_enabled(False)
    renderer.shutdown()
    if not samples:
        print("SKIP:no_samples")
        return 0

    prev = -1
    for sample in samples:
        if sample["steady_ns"] <= prev:
            print("FAIL:non_monotonic")
            return 1
        prev = sample["steady_ns"]
    print(f"OK:{len(samples)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
