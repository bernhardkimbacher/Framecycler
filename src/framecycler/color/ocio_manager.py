import math
import os
import PyOpenColorIO as OCIO

from ..render.shader_pipeline import build_rhi_shader_bundle, hash_pipeline_state


class OCIOManager:
    def __init__(self, custom_config_path=""):
        self.config = None
        self.config_path = ""
        self.input_colorspace = "ACEScg"
        self.look = None  # None = Bypass
        self.display_output = "sRGB"  # "Raw" | "sRGB" | "Rec709"
        self._custom_lut_path = None
        
        # Grading Tool parameters (Exposure, Gamma, Offset)
        self.grade_exposure = 0.0
        self.grade_gamma = 1.0
        self.grade_offset = 0.0

        self._cached_pipeline_key = ""
        self._cached_ocio_shader = ""
        self._cached_textures_3d = []
        self._cached_textures_1d = []
        self._cached_dynamic_uniforms = []

        self.load_config(custom_config_path)

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

        # 1. OCIO environment variable
        env_path = os.environ.get("OCIO", "").strip()
        if env_path:
            config_loaded = self._try_load_config_from_file(env_path, "OCIO environment variable")

        # 2. Settings path
        if not config_loaded:
            settings_path = (custom_config_path or "").strip()
            if settings_path:
                config_loaded = self._try_load_config_from_file(settings_path, "settings")

        # 3. Bundled config
        if not config_loaded:
            bundled_path = self._bundled_config_path()
            if os.path.exists(bundled_path):
                config_loaded = self._try_load_config_from_file(bundled_path, "bundled default")
            else:
                print(f"Bundled OCIO config not found at: {bundled_path}")
                
        # 3. Post-load initialization
        if config_loaded and self.config:
            try:
                OCIO.SetCurrentConfig(self.config)
                
                # Retrieve default input colorspace
                colorspaces = self.get_colorspaces()
                if colorspaces:
                    if "ACEScg" in colorspaces:
                        self.input_colorspace = "ACEScg"
                    else:
                        self.input_colorspace = colorspaces[0]
                        
                self.look = None
                self.display_output = "sRGB"
                self._custom_lut_path = None
            except Exception as e:
                self.config = None
                print(f"Error initializing loaded OCIO config: {e}")
        else:
            self.config = None
            print("No OCIO Config file loaded, falling back to passthrough mode.")
        self.invalidate_shader_cache()

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

    def get_display_outputs(self):
        return ["Raw", "sRGB", "Rec709"]

    def set_look(self, name):
        if name == "None (Bypass)":
            self.look = None
        else:
            self.look = name

    def set_display_output(self, name):
        if name in self.get_display_outputs():
            self.display_output = name

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

        # Normalized mapping keys to actual config colorspaces
        mapping = {
            'acescg': 'ACEScg',
            'srgb': 'sRGB - Texture',
            'rec709': 'Rec.709 - Texture',
            'bt709': 'Rec.709 - Texture',
            'cineon': 'Cineon (ADX10)',
            'logc3': 'ARRI Alexa LogC3',
            'alexalogc3': 'ARRI Alexa LogC3',
            'logc4': 'ARRI LogC4',
            'slog3': 'Sony S-Log3',
            'vlog': 'Panasonic V-Log',
            'redlog3g10': 'RED Log3G10',
            'raw': 'Raw'
        }

        def normalize(s):
            return str(s).lower().replace('-', '').replace('_', '').replace(' ', '').replace('.', '')

        # 1. Inspect metadata for explicit keys or DPX/video attributes
        if metadata:
            # Check standard colorspace attributes
            for key in ['color_space', 'colorspace', 'ocio_colorspace']:
                if key in metadata and metadata[key]:
                    val = normalize(metadata[key])
                    for k in sorted(mapping.keys(), key=len, reverse=True):
                        if k in val:
                            cs = mapping[k]
                            if cs in available_spaces:
                                return cs

            # DPX specific header tags
            if 'transfer_characteristic' in metadata:
                tc = metadata['transfer_characteristic']
                if tc == 2:  # Printing density (Log)
                    cs = 'Cineon (ADX10)'
                    if cs in available_spaces:
                        return cs
                elif tc == 3:  # Linear
                    cs = 'ACEScg'
                    if cs in available_spaces:
                        return cs
                elif tc == 6:  # Rec.709
                    cs = 'Rec.709 - Texture'
                    if cs in available_spaces:
                        return cs

            # QuickTime/Video stream metadata sub-dictionary
            file_meta = metadata.get('file_metadata', {})
            for k_meta, v_meta in file_meta.items():
                if any(x in k_meta.lower() for x in ['color', 'space', 'transfer', 'primaries', 'trc']):
                    val = normalize(v_meta)
                    for k in sorted(mapping.keys(), key=len, reverse=True):
                        if k in val:
                            cs = mapping[k]
                            if cs in available_spaces:
                                return cs

        # 2. Check filename/filepath hints (e.g. 'shot_srgb.exr')
        filename_norm = normalize(filename)
        for k in sorted(mapping.keys(), key=len, reverse=True):
            if k in filename_norm:
                # Skip generic 'raw' name hints on camera raw formats to allow extension fallbacks (like .r3d)
                if k == 'raw' and ext in ['.r3d', '.ari', '.arri']:
                    continue
                cs = mapping[k]
                if cs in available_spaces:
                    return cs

        # 3. Fallback based on extension
        fallback_cs = 'Raw'
        if ext in ['.exr']:
            fallback_cs = 'ACEScg'
        elif ext in ['.dpx']:
            fallback_cs = 'Cineon (ADX10)'
        elif ext in ['.mov', '.mp4', '.mxf', '.mkv', '.avi', '.m4v']:
            fallback_cs = 'Rec.709 - Texture'
        elif ext in ['.jpg', '.jpeg', '.png', '.tif', '.tiff', '.tga', '.bmp', '.psd']:
            fallback_cs = 'sRGB - Texture'
        elif ext in ['.ari', '.arri']:
            fallback_cs = 'ARRI Alexa LogC3'
        elif ext in ['.r3d']:
            fallback_cs = 'RED Log3G10'

        # Ensure fallback exists in config, otherwise default to Raw/first color space
        if fallback_cs in available_spaces:
            return fallback_cs
            
        return 'ACEScg' if 'ACEScg' in available_spaces else (available_spaces[0] if available_spaces else 'Raw')

    def set_grading_values(
        self,
        exposure: float | None = None,
        gamma: float | None = None,
        offset: float | None = None,
    ) -> None:
        """Update grading parameters without invalidating the OCIO shader text."""
        if exposure is not None:
            self.grade_exposure = float(exposure)
        if gamma is not None:
            self.grade_gamma = float(gamma)
        if offset is not None:
            self.grade_offset = float(offset)

    @staticmethod
    def _gpu_uniform_float(value: float) -> float:
        """Clamp OCIO scalar uniforms to finite float32 range for GPU UBO uploads."""
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
        """Factory defaults for non-interactive GradingPrimary uniforms.

        Vulkan/SPIR-V packs OCIO dynamic uniforms into a UBO that is zero-initialized
        each frame unless explicitly populated. Contrast/pivot at 0 would crush the
        image to black; OpenGL left these unset, so defaults must be supplied here.
        """
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
        """Return OCIO dynamic uniform values for the current grading state."""
        values = self._grading_primary_static_uniform_defaults()
        values.update(
            {
                "ocio_exposure_contrast_exposureVal": self.grade_exposure,
                "ocio_exposure_contrast_gammaVal": self.grade_gamma,
                # Spike A mapping: GradingPrimary offset is driven via brightness uniform.
                "ocio_grading_primary_brightness": (
                    self.grade_offset,
                    self.grade_offset,
                    self.grade_offset,
                ),
            }
        )
        return values

    def get_dynamic_uniform_names(self) -> list[str]:
        self._ensure_shader_cache()
        return list(self._cached_dynamic_uniforms)

    def get_pipeline_key(self) -> str:
        look_key = self.look or ""
        custom_lut = self._custom_lut_path or ""
        return hash_pipeline_state(
            self.input_colorspace,
            look_key,
            self.display_output,
            custom_lut,
            self.config_path,
        )

    @staticmethod
    def _append_dynamic_grading(group: OCIO.GroupTransform, exposure: float, gamma: float, offset: float) -> None:
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

    def _build_transform_group(self) -> OCIO.GroupTransform:
        group = OCIO.GroupTransform()

        if not self.config:
            self._append_dynamic_grading(group, self.grade_exposure, self.grade_gamma, self.grade_offset)
            return group

        working_space = self._resolve_working_space()

        if self.input_colorspace != "Raw" and self.input_colorspace != working_space:
            cs_names = [cs.getName() for cs in self.config.getColorSpaces()]
            if self.input_colorspace in cs_names:
                group.appendTransform(
                    OCIO.ColorSpaceTransform(src=self.input_colorspace, dst=working_space)
                )

        self._append_dynamic_grading(group, self.grade_exposure, self.grade_gamma, self.grade_offset)

        look_applied = False
        if self.look:
            if self._custom_lut_path and self.look == os.path.basename(self._custom_lut_path):
                try:
                    group.appendTransform(
                        OCIO.FileTransform(src=self._custom_lut_path, interpolation=OCIO.INTERP_LINEAR)
                    )
                    look_applied = True
                except Exception as e:
                    print(f"OCIOManager: Failed to apply custom LUT '{self._custom_lut_path}': {e}")
            else:
                try:
                    group.appendTransform(
                        OCIO.LookTransform(src=working_space, dst=working_space, looks=self.look)
                    )
                    look_applied = True
                except Exception as e:
                    print(
                        f"OCIOManager: Failed to apply Look '{self.look}': {e}. "
                        "Ensure LUT file exists in studio_config/luts/."
                    )

        if self.display_output != "Raw" and not look_applied:
            cs_names = [cs.getName() for cs in self.config.getColorSpaces()]
            if working_space == "ACEScg":
                group.appendTransform(
                    OCIO.BuiltinTransform(style="UTILITY - ACES-AP1_to_LINEAR-REC709_BFD")
                )
                if self.display_output == "sRGB":
                    t = OCIO.ExponentWithLinearTransform()
                    t.setGamma([2.4, 2.4, 2.4, 1.0])
                    t.setOffset([0.055, 0.055, 0.055, 0.0])
                    t.setDirection(OCIO.TRANSFORM_DIR_INVERSE)
                    group.appendTransform(t)
                elif self.display_output == "Rec709":
                    t = OCIO.ExponentTransform()
                    t.setValue([2.4, 2.4, 2.4, 1.0])
                    t.setDirection(OCIO.TRANSFORM_DIR_INVERSE)
                    group.appendTransform(t)
            else:
                target_cs = "sRGB - Texture" if self.display_output == "sRGB" else "Rec.709 - Texture"
                if target_cs in cs_names:
                    group.appendTransform(OCIO.ColorSpaceTransform(src=working_space, dst=target_cs))
                else:
                    display_spaces = [
                        cs.getName() for cs in self.config.getColorSpaces() if not cs.isData()
                    ]
                    if display_spaces:
                        group.appendTransform(
                            OCIO.ColorSpaceTransform(src=working_space, dst=display_spaces[-1])
                        )

        return group

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
                        "channel": tex.channel,
                        "data": tex.getValues(),
                    }
                )
        else:
            num_1d_tex = shader_desc.getNumTextures()
            for i in range(num_1d_tex):
                tex_name, sampler_name, width, height, channel, _fmt, _direction, lut_data = shader_desc.getTexture(i)
                textures_1d.append(
                    {
                        "name": tex_name,
                        "sampler": sampler_name,
                        "width": width,
                        "height": height,
                        "channel": channel,
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

    def _compile_gpu_shader(self, group: OCIO.GroupTransform):
        if not self.config:
            raw_cfg = OCIO.Config.CreateRaw()
            processor = raw_cfg.getProcessor(group)
        else:
            processor = self.config.getProcessor(group)

        gpu_proc = processor.getDefaultGPUProcessor()
        shader_desc = OCIO.GpuShaderDesc.CreateShaderDesc()
        shader_desc.setLanguage(OCIO.GPU_LANGUAGE_GLSL_4_0)
        shader_desc.setFunctionName("ocio_color_transform")
        gpu_proc.extractGpuShaderInfo(shader_desc)

        shader_text = shader_desc.getShaderText()
        textures_3d, textures_1d = self._extract_textures(shader_desc)
        dynamic_uniforms = self._extract_dynamic_uniforms(shader_text)
        return shader_text, textures_3d, textures_1d, dynamic_uniforms

    def _ensure_shader_cache(self) -> None:
        pipeline_key = self.get_pipeline_key()
        if pipeline_key == self._cached_pipeline_key and self._cached_ocio_shader:
            return

        try:
            group = self._build_transform_group()
            shader_text, textures_3d, textures_1d, dynamic_uniforms = self._compile_gpu_shader(group)
        except Exception as e:
            print(f"Error compiling OCIO GPU Shader: {e}")
            shader_text = """
            vec4 ocio_color_transform(vec4 color) {
                return color;
            }
            """
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
        """
        Compiles the 3-step pipeline: Input ColorSpace -> Grade -> Look -> Display Output.
        Dynamically adapts to custom configs if the default ACEScg working space is absent.
        Returns (GLSL shader function text, list of 3D Lut textures, list of 1D Lut textures)
        """
        self._ensure_shader_cache()
        return self._cached_ocio_shader, self._cached_textures_3d, self._cached_textures_1d

    def get_rhi_shader_bundle(self):
        """Return a Vulkan-GLSL shader bundle ready for pyside6-qsb / QShaderBaker."""
        self._ensure_shader_cache()
        return build_rhi_shader_bundle(
            self._cached_ocio_shader,
            self._cached_textures_3d,
            self._cached_textures_1d,
            self.get_pipeline_key(),
        )
