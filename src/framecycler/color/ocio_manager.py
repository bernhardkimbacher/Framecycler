import math
import os
import PyOpenColorIO as OCIO

from ..render.shader_pipeline import build_rhi_shader_bundle, hash_pipeline_state


# Legacy display_output strings → (display, view) for settings migration.
_LEGACY_DISPLAY_ALIASES = {
    "Raw": ("", ""),  # bypass DisplayViewTransform
    "sRGB": ("sRGB", "sRGB View"),
    "Rec709": ("Rec.709", "Rec.709 View"),
    "Rec.709": ("Rec.709", "Rec.709 View"),
}


class OCIOManager:
    def __init__(self, custom_config_path=""):
        self.config = None
        self.config_path = ""
        self.input_colorspace = "ACEScg"
        self.look = None  # None = Bypass
        # Config-driven display/view. Empty display ⇒ Raw (bypass DVT).
        self.display_name = "sRGB"
        self.view_name = "sRGB View"
        self._custom_lut_path = None

        # Grading Tool parameters (Exposure, Gamma, Offset) — UBO-driven
        self.grade_exposure = 0.0
        self.grade_gamma = 1.0
        self.grade_offset = 0.0

        # ASC CDL (slope/offset/power/sat) — UBO/GLSL driven (not baked into OCIO)
        self.cdl_slope = (1.0, 1.0, 1.0)
        self.cdl_offset = (0.0, 0.0, 0.0)
        self.cdl_power = (1.0, 1.0, 1.0)
        self.cdl_saturation = 1.0
        self.cdl_style = OCIO.CDL_NO_CLAMP

        self._cached_pipeline_key = ""
        self._cached_ocio_shader = ""
        self._cached_textures_3d = []
        self._cached_textures_1d = []
        self._cached_dynamic_uniforms = []

        self.load_config(custom_config_path)

    @property
    def display_output(self) -> str:
        """Backward-compatible label for UI/status (display / view or Raw)."""
        if not self.display_name:
            return "Raw"
        if self.view_name:
            return f"{self.display_name} / {self.view_name}"
        return self.display_name

    @staticmethod
    def _bundled_config_path() -> str:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, "studio_config", "config.ocio")

    def _try_load_config_from_file(self, path: str, source_label: str) -> bool:
        if not path or not os.path.exists(path):
            return False
        try:
            self.config = OCIO.Config.CreateFromFile(path)
            self.config_path = path
            print(f"Loaded OCIO Config from {source_label}: {self.config_path}")
            return True
        except Exception as e:
            print(f"Failed to load OCIO config from {source_label} '{path}': {e}")
            return False

    def load_config(self, custom_config_path=""):
        config_loaded = False

        env_path = os.environ.get("OCIO", "").strip()
        if env_path:
            config_loaded = self._try_load_config_from_file(env_path, "OCIO environment variable")

        if not config_loaded:
            settings_path = (custom_config_path or "").strip()
            if settings_path:
                config_loaded = self._try_load_config_from_file(settings_path, "settings")

        if not config_loaded:
            bundled_path = self._bundled_config_path()
            if os.path.exists(bundled_path):
                config_loaded = self._try_load_config_from_file(bundled_path, "bundled default")
            else:
                print(f"Bundled OCIO config not found at: {bundled_path}")

        if config_loaded and self.config:
            try:
                OCIO.SetCurrentConfig(self.config)

                colorspaces = self.get_colorspaces()
                if colorspaces:
                    if "ACEScg" in colorspaces:
                        self.input_colorspace = "ACEScg"
                    else:
                        self.input_colorspace = colorspaces[0]

                self.look = None
                self._custom_lut_path = None
                self._set_default_display_view()
            except Exception as e:
                self.config = None
                print(f"Error initializing loaded OCIO config: {e}")
        else:
            self.config = None
            self.display_name = ""
            self.view_name = ""
            print("No OCIO Config file loaded, falling back to passthrough mode.")
        self.invalidate_shader_cache()

    def reload_config(self, custom_config_path: str = "") -> str:
        """Re-read the OCIO config from disk and invalidate the GPU shader cache.

        Preserves input colorspace, look (including custom LUT), and display/view
        when they still exist in the reloaded config. Returns the loaded path
        (or empty string on failure) for status UI.
        """
        prev_input = self.input_colorspace
        prev_look = self.look
        prev_custom = self._custom_lut_path
        prev_display = self.display_name
        prev_view = self.view_name
        prev_output = self.display_output

        self.load_config(custom_config_path)

        if not self.config:
            return ""

        colorspaces = self.get_colorspaces()
        if prev_input in colorspaces:
            self.input_colorspace = prev_input

        if prev_custom and os.path.exists(prev_custom):
            self.load_custom_lut(prev_custom)
        elif prev_look and prev_look in [
            l.getName() for l in self.config.getLooks()
        ]:
            self.look = prev_look

        if prev_output == "Raw":
            self.display_name = ""
            self.view_name = ""
        elif prev_display and prev_view:
            views = self.get_views(prev_display)
            if prev_display in self.get_displays() and prev_view in views:
                self.display_name = prev_display
                self.view_name = prev_view

        self.invalidate_shader_cache()
        return self.config_path or ""

    def _set_default_display_view(self) -> None:
        displays = self.get_displays()
        if not displays:
            self.display_name = ""
            self.view_name = ""
            return
        default_display = ""
        try:
            default_display = self.config.getDefaultDisplay() or ""
        except Exception:
            default_display = ""
        if default_display not in displays:
            default_display = displays[0]
        views = self.get_views(default_display)
        default_view = ""
        try:
            default_view = self.config.getDefaultView(default_display) or ""
        except Exception:
            default_view = ""
        if default_view not in views and views:
            default_view = views[0]
        self.display_name = default_display
        self.view_name = default_view

    def get_colorspaces(self):
        if not self.config:
            return ["Raw"]
        try:
            return [cs.getName() for cs in self.config.getColorSpaces()]
        except Exception:
            return ["Raw"]

    def get_looks(self):
        looks = ["None (Bypass)"]
        if self.config:
            try:
                looks.extend([l.getName() for l in self.config.getLooks()])
            except Exception:
                pass
        if self._custom_lut_path:
            looks.append(os.path.basename(self._custom_lut_path))
        return looks

    def get_displays(self) -> list[str]:
        if not self.config:
            return []
        try:
            names = list(self.config.getDisplays())
            active = []
            try:
                active = list(self.config.getActiveDisplays())
            except Exception:
                active = []
            if active:
                ordered = [d for d in active if d in names]
                ordered.extend([d for d in names if d not in ordered])
                return ordered
            return names
        except Exception:
            return []

    def get_views(self, display: str | None = None) -> list[str]:
        if not self.config:
            return []
        display = display if display is not None else self.display_name
        if not display:
            return []
        try:
            names = list(self.config.getViews(display))
            active = []
            try:
                active = list(self.config.getActiveViews())
            except Exception:
                active = []
            if active:
                ordered = [v for v in active if v in names]
                ordered.extend([v for v in names if v not in ordered])
                return ordered
            return names
        except Exception:
            return []

    def get_display_outputs(self) -> list[str]:
        """Menu entries: Raw plus ``Display / View`` labels."""
        outputs = ["Raw"]
        for display in self.get_displays():
            for view in self.get_views(display):
                outputs.append(f"{display} / {view}")
        return outputs

    def set_look(self, name):
        if name == "None (Bypass)":
            self.look = None
        else:
            self.look = name

    def set_display_output(self, name: str) -> None:
        """Accept legacy aliases or ``Display / View`` labels."""
        if not name:
            return
        if name in _LEGACY_DISPLAY_ALIASES:
            display, view = _LEGACY_DISPLAY_ALIASES[name]
            self.display_name = display
            self.view_name = view
            return
        if name == "Raw":
            self.display_name = ""
            self.view_name = ""
            return
        if " / " in name:
            display, view = name.split(" / ", 1)
            displays = self.get_displays()
            if display in displays and view in self.get_views(display):
                self.display_name = display
                self.view_name = view
            return
        # Bare display name → default view for that display
        if name in self.get_displays():
            views = self.get_views(name)
            self.display_name = name
            self.view_name = views[0] if views else ""

    def set_display_view(self, display: str, view: str) -> None:
        if not display:
            self.display_name = ""
            self.view_name = ""
            return
        if display in self.get_displays() and view in self.get_views(display):
            self.display_name = display
            self.view_name = view

    def load_custom_lut(self, path):
        if path and os.path.exists(path):
            self._custom_lut_path = path
            self.look = os.path.basename(path)
            print(f"Custom LUT loaded: {path}")

    def detect_input_colorspace(self, file_path, metadata=None):
        """
        Detects the standard colorspace from media file path hints, file extension,
        and metadata (DPX headers, QuickTime tags, etc.) and aligns it with config spaces.
        """
        available_spaces = self.get_colorspaces()
        filename = os.path.basename(file_path).lower()
        ext = os.path.splitext(filename)[1]

        mapping = {
            "acescg": "ACEScg",
            "srgb": "sRGB - Texture",
            "rec709": "Rec.709 - Texture",
            "bt709": "Rec.709 - Texture",
            "cineon": "Cineon (ADX10)",
            "logc3": "ARRI Alexa LogC3",
            "alexalogc3": "ARRI Alexa LogC3",
            "logc4": "ARRI LogC4",
            "slog3": "Sony S-Log3",
            "vlog": "Panasonic V-Log",
            "redlog3g10": "RED Log3G10",
            "raw": "Raw",
        }

        def normalize(s):
            return str(s).lower().replace("-", "").replace("_", "").replace(" ", "").replace(".", "")

        if metadata:
            for key in ["color_space", "colorspace", "ocio_colorspace"]:
                if key in metadata and metadata[key]:
                    val = normalize(metadata[key])
                    for k in sorted(mapping.keys(), key=len, reverse=True):
                        if k in val:
                            cs = mapping[k]
                            if cs in available_spaces:
                                return cs

            if "transfer_characteristic" in metadata:
                tc = metadata["transfer_characteristic"]
                if tc == 2:
                    cs = "Cineon (ADX10)"
                    if cs in available_spaces:
                        return cs
                elif tc == 3:
                    cs = "ACEScg"
                    if cs in available_spaces:
                        return cs
                elif tc == 6:
                    cs = "Rec.709 - Texture"
                    if cs in available_spaces:
                        return cs

            file_meta = metadata.get("file_metadata", {})
            for k_meta, v_meta in file_meta.items():
                if any(x in k_meta.lower() for x in ["color", "space", "transfer", "primaries", "trc"]):
                    val = normalize(v_meta)
                    for k in sorted(mapping.keys(), key=len, reverse=True):
                        if k in val:
                            cs = mapping[k]
                            if cs in available_spaces:
                                return cs

        for k in sorted(mapping.keys(), key=len, reverse=True):
            if k in normalize(filename):
                if k == "raw" and ext in [".r3d", ".ari", ".arri"]:
                    continue
                cs = mapping[k]
                if cs in available_spaces:
                    return cs

        fallback_cs = None
        if ext in [".exr"]:
            fallback_cs = "ACEScg"
        elif ext in [".dpx"]:
            fallback_cs = "Cineon (ADX10)"
        elif ext in [".mov", ".mp4", ".mxf", ".mkv", ".avi", ".m4v"]:
            fallback_cs = "Rec.709 - Texture"
        elif ext in [".jpg", ".jpeg", ".png", ".tif", ".tiff", ".tga", ".bmp", ".psd"]:
            fallback_cs = "sRGB - Texture"
        elif ext in [".ari", ".arri"]:
            fallback_cs = "ARRI Alexa LogC3"
        elif ext in [".r3d"]:
            fallback_cs = "RED Log3G10"

        if fallback_cs and fallback_cs in available_spaces:
            return fallback_cs

        return self.input_colorspace if self.input_colorspace in available_spaces else (
            available_spaces[0] if available_spaces else "Raw"
        )

    def set_grading_values(self, exposure=None, gamma=None, offset=None):
        if exposure is not None:
            self.grade_exposure = float(exposure)
        if gamma is not None:
            self.grade_gamma = max(0.01, float(gamma))
        if offset is not None:
            self.grade_offset = float(offset)

    @staticmethod
    def _as_rgb_triple(value) -> tuple[float, float, float]:
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            return (float(value[0]), float(value[1]), float(value[2]))
        scalar = float(value)
        return (scalar, scalar, scalar)

    def _cdl_is_identity(self) -> bool:
        return (
            self.cdl_slope == (1.0, 1.0, 1.0)
            and self.cdl_offset == (0.0, 0.0, 0.0)
            and self.cdl_power == (1.0, 1.0, 1.0)
            and abs(self.cdl_saturation - 1.0) < 1e-9
        )

    def set_cdl_values(
        self,
        slope: tuple[float, float, float] | None = None,
        offset: tuple[float, float, float] | None = None,
        power: tuple[float, float, float] | None = None,
        saturation: float | None = None,
        style=None,
    ) -> None:
        """Update ASC CDL parameters (UBO-driven; does not invalidate shader cache)."""
        if slope is not None:
            self.cdl_slope = self._as_rgb_triple(slope)
        if offset is not None:
            self.cdl_offset = self._as_rgb_triple(offset)
        if power is not None:
            self.cdl_power = self._as_rgb_triple(power)
        if saturation is not None:
            self.cdl_saturation = float(saturation)
        if style is not None:
            self.cdl_style = style

    def reset_cdl_values(self) -> None:
        self.set_cdl_values(
            slope=(1.0, 1.0, 1.0),
            offset=(0.0, 0.0, 0.0),
            power=(1.0, 1.0, 1.0),
            saturation=1.0,
            style=OCIO.CDL_NO_CLAMP,
        )

    def apply_cdl_dict(self, cdl: dict | None) -> None:
        """Apply a Framecycler OTIO CDL dict (slope/offset/power/sat[/style])."""
        if not cdl:
            self.reset_cdl_values()
            return
        style_name = cdl.get("style", "no_clamp")
        style = OCIO.CDL_ASC if style_name == "asc" else OCIO.CDL_NO_CLAMP
        self.set_cdl_values(
            slope=tuple(cdl.get("slope", (1.0, 1.0, 1.0))),
            offset=tuple(cdl.get("offset", (0.0, 0.0, 0.0))),
            power=tuple(cdl.get("power", (1.0, 1.0, 1.0))),
            saturation=float(cdl.get("saturation", 1.0)),
            style=style,
        )

    def load_cdl(self, path: str, cccid: str = "") -> None:
        """Load ASC CDL from a .cdl/.ccc/.cc file via OCIO."""
        cdl = OCIO.CDLTransform.CreateFromFile(path, cccid)
        self.set_cdl_values(
            slope=tuple(cdl.getSlope()),
            offset=tuple(cdl.getOffset()),
            power=tuple(cdl.getPower()),
            saturation=float(cdl.getSat()),
            style=cdl.getStyle(),
        )

    @staticmethod
    def _gpu_uniform_float(value: float) -> float:
        value = float(value)
        if not math.isfinite(value):
            return 0.0
        limit = 3.402823466e38
        if value >= limit:
            return limit
        if value <= -limit:
            return -limit
        return value

    @staticmethod
    def _grading_primary_static_uniform_defaults() -> dict[str, float | tuple[float, float, float]]:
        primary = OCIO.GradingPrimaryTransform().getValue()
        return {
            "ocio_grading_primary_contrast": (
                float(primary.contrast.red),
                float(primary.contrast.green),
                float(primary.contrast.blue),
            ),
            "ocio_grading_primary_gamma": (
                float(primary.gamma.red),
                float(primary.gamma.green),
                float(primary.gamma.blue),
            ),
            "ocio_grading_primary_pivot": OCIOManager._gpu_uniform_float(primary.pivot),
            "ocio_grading_primary_pivotBlack": OCIOManager._gpu_uniform_float(primary.pivotBlack),
            "ocio_grading_primary_pivotWhite": OCIOManager._gpu_uniform_float(primary.pivotWhite),
            "ocio_grading_primary_clampBlack": OCIOManager._gpu_uniform_float(primary.clampBlack),
            "ocio_grading_primary_clampWhite": OCIOManager._gpu_uniform_float(primary.clampWhite),
            "ocio_grading_primary_saturation": OCIOManager._gpu_uniform_float(primary.saturation),
            "ocio_grading_primary_localBypass": 0.0,
        }

    def get_grading_uniform_values(self) -> dict[str, float | tuple[float, float, float]]:
        """Return OCIO dynamic + ASC CDL uniform values for the current grading state.

        Includes GradingPrimary factory defaults so a zero-filled UBO cannot
        crush contrast/saturation (std140 pack must match Metal layout).
        """
        values = self._grading_primary_static_uniform_defaults()
        values.update(
            {
                "ocio_exposure_contrast_exposureVal": self.grade_exposure,
                "ocio_exposure_contrast_gammaVal": self.grade_gamma,
                "ocio_grading_primary_brightness": (
                    self.grade_offset,
                    self.grade_offset,
                    self.grade_offset,
                ),
                "fc_cdl_slope": self.cdl_slope,
                "fc_cdl_offset": self.cdl_offset,
                "fc_cdl_power": self.cdl_power,
                "fc_cdl_saturation": self._gpu_uniform_float(self.cdl_saturation),
                "fc_cdl_enable": 0.0 if self._cdl_is_identity() else 1.0,
            }
        )
        return values

    def get_dynamic_uniform_names(self) -> list[str]:
        self._ensure_shader_cache()
        return list(self._cached_dynamic_uniforms)

    def get_pipeline_key(self) -> str:
        look_key = self.look or ""
        custom_lut = self._custom_lut_path or ""
        display_key = f"{self.display_name}|{self.view_name}"
        # Include config mtime/size so edits to looks/colorspaces invalidate the
        # on-disk QSB cache (keyed only by this string in RhiRenderer::bake_shaders).
        config_stamp = ""
        if self.config_path and os.path.isfile(self.config_path):
            try:
                st = os.stat(self.config_path)
                config_stamp = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                config_stamp = ""
        custom_stamp = ""
        if custom_lut and os.path.isfile(custom_lut):
            try:
                st = os.stat(custom_lut)
                custom_stamp = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                custom_stamp = ""
        # Salt busts stale QSB disk caches when the fragment UBO/template changes.
        return hash_pipeline_state(
            "ocio_frag_v7_config_mtime",
            self.input_colorspace,
            look_key,
            display_key,
            custom_lut,
            custom_stamp,
            self.config_path or "",
            config_stamp,
        )

    @staticmethod
    def _append_dynamic_grading(
        group: OCIO.GroupTransform, exposure: float, gamma: float, offset: float
    ) -> None:
        ect = OCIO.ExposureContrastTransform()
        ect.setExposure(exposure)
        ect.setGamma(gamma)
        ect.makeExposureDynamic()
        ect.makeGammaDynamic()
        group.appendTransform(ect)

        gpt = OCIO.GradingPrimaryTransform()
        primary = gpt.getValue()
        primary.offset = OCIO.GradingRGBM(offset, offset, offset, 0.0)
        gpt.setValue(primary)
        gpt.makeDynamic()
        group.appendTransform(gpt)

    def _resolve_working_space(self) -> str:
        if not self.config:
            return "Raw"
        working_space = "ACEScg"
        if self.config.hasRole("rendering"):
            working_space = self.config.getRoleColorSpace("rendering")
        elif self.config.hasRole("scene_linear"):
            working_space = self.config.getRoleColorSpace("scene_linear")
        else:
            cs_names = [cs.getName() for cs in self.config.getColorSpaces()]
            if "ACEScg" not in cs_names and cs_names:
                working_space = cs_names[0]
        return working_space

    def _append_look(self, group: OCIO.GroupTransform, working_space: str) -> None:
        if not self.look:
            return
        if self._custom_lut_path and self.look == os.path.basename(self._custom_lut_path):
            try:
                group.appendTransform(
                    OCIO.FileTransform(src=self._custom_lut_path, interpolation=OCIO.INTERP_LINEAR)
                )
            except Exception as e:
                print(f"OCIOManager: Failed to apply custom LUT '{self._custom_lut_path}': {e}")
            return
        try:
            group.appendTransform(
                OCIO.LookTransform(src=working_space, dst=working_space, looks=self.look)
            )
        except Exception as e:
            print(
                f"OCIOManager: Failed to apply Look '{self.look}': {e}. "
                "Ensure LUT file exists in studio_config/luts/."
            )

    def _append_display_view(self, group: OCIO.GroupTransform, working_space: str) -> None:
        if not self.config or not self.display_name or not self.view_name:
            return
        try:
            dvt = OCIO.DisplayViewTransform()
            dvt.setSrc(working_space)
            dvt.setDisplay(self.display_name)
            dvt.setView(self.view_name)
            # Looks are applied explicitly via LookTransform; do not re-apply via the view.
            if hasattr(dvt, "setLooksBypass"):
                dvt.setLooksBypass(True)
            group.appendTransform(dvt)
        except Exception as e:
            print(
                f"OCIOManager: Failed DisplayViewTransform "
                f"'{self.display_name}' / '{self.view_name}': {e}"
            )

    def _build_pre_cdl_group(self) -> OCIO.GroupTransform:
        """Input → working + dynamic grading (ASC CDL applied in GLSL after this)."""
        group = OCIO.GroupTransform()
        if not self.config:
            self._append_dynamic_grading(
                group, self.grade_exposure, self.grade_gamma, self.grade_offset
            )
            return group

        working_space = self._resolve_working_space()
        if self.input_colorspace != "Raw" and self.input_colorspace != working_space:
            cs_names = [cs.getName() for cs in self.config.getColorSpaces()]
            if self.input_colorspace in cs_names:
                group.appendTransform(
                    OCIO.ColorSpaceTransform(src=self.input_colorspace, dst=working_space)
                )
        self._append_dynamic_grading(
            group, self.grade_exposure, self.grade_gamma, self.grade_offset
        )
        return group

    def _build_post_cdl_group(self) -> OCIO.GroupTransform:
        """Look + DisplayViewTransform (after ASC CDL in GLSL)."""
        group = OCIO.GroupTransform()
        if not self.config:
            return group
        working_space = self._resolve_working_space()
        self._append_look(group, working_space)
        self._append_display_view(group, working_space)
        return group

    def _build_transform_group(self) -> OCIO.GroupTransform:
        """Full group for introspection/GPU shader bake (CDL omitted — applied in GLSL)."""
        group = OCIO.GroupTransform()
        for t in self._build_pre_cdl_group():
            group.appendTransform(t)
        for t in self._build_post_cdl_group():
            group.appendTransform(t)
        return group

    def _make_cdl_transform(self) -> OCIO.CDLTransform | None:
        """Build an OCIO CDLTransform for the current ASC CDL state, or None if identity."""
        if self._cdl_is_identity():
            return None
        cdl = OCIO.CDLTransform()
        cdl.setSlope(self.cdl_slope)
        cdl.setOffset(self.cdl_offset)
        cdl.setPower(self.cdl_power)
        cdl.setSat(float(self.cdl_saturation))
        try:
            cdl.setStyle(self.cdl_style)
        except Exception:
            pass
        return cdl

    def _build_cpu_transform_group(self) -> OCIO.GroupTransform:
        """CPU path matching viewer order: pre → ASC CDL → post."""
        group = OCIO.GroupTransform()
        for t in self._build_pre_cdl_group():
            group.appendTransform(t)
        cdl = self._make_cdl_transform()
        if cdl is not None:
            group.appendTransform(cdl)
        for t in self._build_post_cdl_group():
            group.appendTransform(t)
        return group

    def get_cpu_processor(self):
        """Return a DefaultCPUProcessor for probe/scopes (includes CDL when active)."""
        if self.config is None:
            return None
        try:
            group = self._build_cpu_transform_group()
            return self.config.getProcessor(group).getDefaultCPUProcessor()
        except Exception:
            return None

    def cpu_processor_signature(self) -> str:
        """Cache key for CPU processor rebuilds (includes CDL + grading)."""
        style = getattr(self.cdl_style, "name", str(self.cdl_style))
        config_stamp = ""
        if self.config_path and os.path.isfile(self.config_path):
            try:
                st = os.stat(self.config_path)
                config_stamp = f"{st.st_mtime_ns}:{st.st_size}"
            except OSError:
                config_stamp = ""
        return (
            f"{self.input_colorspace}|{self.look or ''}|"
            f"{self.display_name}|{self.view_name}|{self.config_path}|{config_stamp}|"
            f"{self.grade_exposure}|{self.grade_gamma}|{self.grade_offset}|"
            f"{self.cdl_slope}|{self.cdl_offset}|{self.cdl_power}|"
            f"{self.cdl_saturation}|{style}"
        )

    @staticmethod
    def _extract_textures(shader_desc) -> tuple[list[dict], list[dict]]:
        textures_3d = []
        if hasattr(shader_desc, "get3DTextures"):
            for tex in shader_desc.get3DTextures():
                textures_3d.append(
                    {
                        "name": tex.textureName,
                        "sampler": tex.samplerName,
                        "size": tex.edgeLen,
                        "data": tex.getValues(),
                    }
                )
        else:
            num_3d_tex = shader_desc.getNum3DTextures()
            for i in range(num_3d_tex):
                tex_name, sampler_name, edge_len, lut_data = shader_desc.get3DTexture(i)
                textures_3d.append(
                    {
                        "name": tex_name,
                        "sampler": sampler_name,
                        "size": edge_len,
                        "data": lut_data,
                    }
                )

        textures_1d = []
        if hasattr(shader_desc, "getTextures"):
            for tex in shader_desc.getTextures():
                textures_1d.append(
                    {
                        "name": tex.textureName,
                        "sampler": tex.samplerName,
                        "width": tex.width,
                        "height": tex.height,
                        "channel": str(tex.channel),
                        "data": tex.getValues(),
                    }
                )
        else:
            num_1d_tex = shader_desc.getNumTextures()
            for i in range(num_1d_tex):
                tex_name, sampler_name, width, height, channel, _fmt, _direction, lut_data = (
                    shader_desc.getTexture(i)
                )
                textures_1d.append(
                    {
                        "name": tex_name,
                        "sampler": sampler_name,
                        "width": width,
                        "height": height,
                        "channel": str(channel),
                        "data": lut_data,
                    }
                )

        return textures_3d, textures_1d

    @staticmethod
    def _extract_dynamic_uniforms(shader_text: str) -> list[str]:
        names = []
        for line in shader_text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("uniform "):
                continue
            if "ocio_exposure_contrast_" in stripped or "ocio_grading_primary_" in stripped:
                parts = stripped.replace(";", "").split()
                if len(parts) >= 3:
                    names.append(parts[2])
        return names

    def _compile_gpu_shader(self, group: OCIO.GroupTransform, function_name: str):
        if not self.config:
            raw_cfg = OCIO.Config.CreateRaw()
            processor = raw_cfg.getProcessor(group)
        else:
            processor = self.config.getProcessor(group)

        gpu_proc = processor.getDefaultGPUProcessor()
        shader_desc = OCIO.GpuShaderDesc.CreateShaderDesc()
        shader_desc.setLanguage(OCIO.GPU_LANGUAGE_GLSL_4_0)
        shader_desc.setFunctionName(function_name)
        gpu_proc.extractGpuShaderInfo(shader_desc)

        shader_text = shader_desc.getShaderText()
        textures_3d, textures_1d = self._extract_textures(shader_desc)
        dynamic_uniforms = self._extract_dynamic_uniforms(shader_text)
        return shader_text, textures_3d, textures_1d, dynamic_uniforms

    @staticmethod
    def _identity_ocio_function(name: str) -> str:
        return f"""
vec4 {name}(vec4 color) {{
    return color;
}}
"""

    def _ensure_shader_cache(self) -> None:
        pipeline_key = self.get_pipeline_key()
        if pipeline_key == self._cached_pipeline_key and self._cached_ocio_shader:
            return

        try:
            pre_text, pre_3d, pre_1d, pre_dyn = self._compile_gpu_shader(
                self._build_pre_cdl_group(), "ocio_to_working"
            )
            post_group = self._build_post_cdl_group()
            post_transforms = list(post_group)
            if not post_transforms:
                post_text = self._identity_ocio_function("ocio_to_display")
                post_3d, post_1d, post_dyn = [], [], []
            else:
                post_text, post_3d, post_1d, post_dyn = self._compile_gpu_shader(
                    post_group, "ocio_to_display"
                )
            shader_text = pre_text + "\n" + post_text
            textures_3d = pre_3d + post_3d
            textures_1d = pre_1d + post_1d
            dynamic_uniforms = list(dict.fromkeys(pre_dyn + post_dyn))
        except Exception as e:
            print(f"Error compiling OCIO GPU Shader: {e}")
            shader_text = (
                self._identity_ocio_function("ocio_to_working")
                + self._identity_ocio_function("ocio_to_display")
            )
            textures_3d, textures_1d, dynamic_uniforms = [], [], []

        self._cached_pipeline_key = pipeline_key
        self._cached_ocio_shader = shader_text
        self._cached_textures_3d = textures_3d
        self._cached_textures_1d = textures_1d
        self._cached_dynamic_uniforms = dynamic_uniforms

    def invalidate_shader_cache(self) -> None:
        self._cached_pipeline_key = ""
        self._cached_ocio_shader = ""
        self._cached_textures_3d = []
        self._cached_textures_1d = []
        self._cached_dynamic_uniforms = []

    def get_gpu_shader_glsl(self):
        self._ensure_shader_cache()
        return self._cached_ocio_shader, self._cached_textures_3d, self._cached_textures_1d

    def get_rhi_shader_bundle(self):
        self._ensure_shader_cache()
        return build_rhi_shader_bundle(
            self._cached_ocio_shader,
            self._cached_textures_3d,
            self._cached_textures_1d,
            self.get_pipeline_key(),
        )

    def transform_group_has_display_view(self) -> bool:
        """Test helper: True when the post-CDL group contains a DisplayViewTransform."""
        for t in self._build_post_cdl_group():
            if t.getTransformType() == OCIO.TRANSFORM_TYPE_DISPLAY_VIEW:
                return True
        return False
