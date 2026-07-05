import os
import PyOpenColorIO as OCIO

class OCIOManager:
    def __init__(self, custom_config_path=""):
        self.config = None
        self.config_path = ""
        self.input_colorspace = "ACEScg"
        self.display = "sRGB"
        self.view = "sRGB View"
        
        # CDL values (Slope, Offset, Power, Saturation)
        self.cdl_slope = [1.0, 1.0, 1.0]
        self.cdl_offset = [0.0, 0.0, 0.0]
        self.cdl_power = [1.0, 1.0, 1.0]
        self.cdl_saturation = 1.0
        
        self.load_config(custom_config_path)

    def load_config(self, custom_config_path=""):
        config_loaded = False
        
        # 1. Try custom config path first
        if custom_config_path and os.path.exists(custom_config_path):
            try:
                self.config = OCIO.Config.CreateFromFile(custom_config_path)
                self.config_path = custom_config_path
                config_loaded = True
                print(f"Loaded OCIO Config from: {self.config_path}")
            except Exception as e:
                print(f"Failed to load custom OCIO config '{custom_config_path}': {e}. Falling back to bundled config...")
                
        # 2. Try bundled config if custom failed or wasn't specified
        if not config_loaded:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            bundled_path = os.path.join(current_dir, "studio_config", "config.ocio")
            if os.path.exists(bundled_path):
                try:
                    self.config = OCIO.Config.CreateFromFile(bundled_path)
                    self.config_path = bundled_path
                    config_loaded = True
                    print(f"Loaded bundled OCIO Config from: {self.config_path}")
                except Exception as e:
                    print(f"Failed to load bundled OCIO config: {e}")
            else:
                print(f"Bundled OCIO config not found at: {bundled_path}")
                
        # 3. Post-load initialization
        if config_loaded and self.config:
            try:
                OCIO.SetCurrentConfig(self.config)
                
                # Retrieve default display/view/colorspace
                displays = self.get_displays()
                if displays:
                    self.display = displays[0]
                    views = self.get_views(self.display)
                    if views:
                        self.view = views[0]
                
                colorspaces = self.get_colorspaces()
                if colorspaces:
                    if "ACEScg" in colorspaces:
                        self.input_colorspace = "ACEScg"
                    else:
                        self.input_colorspace = colorspaces[0]
            except Exception as e:
                self.config = None
                print(f"Error initializing loaded OCIO config: {e}")
        else:
            self.config = None
            print("No OCIO Config file loaded, falling back to passthrough mode.")

    def get_colorspaces(self):
        if not self.config:
            return ["Raw"]
        try:
            return [cs.getName() for cs in self.config.getColorSpaces()]
        except Exception:
            return ["Raw"]

    def get_displays(self):
        if not self.config:
            return ["sRGB"]
        try:
            return list(self.config.getDisplays())
        except Exception:
            return ["sRGB"]

    def get_views(self, display):
        if not self.config:
            return ["sRGB View"]
        try:
            return list(self.config.getViews(display))
        except Exception:
            return ["sRGB View"]

    def set_cdl(self, slope=None, offset=None, power=None, sat=None):
        if slope is not None:
            self.cdl_slope = list(slope)
        if offset is not None:
            self.cdl_offset = list(offset)
        if power is not None:
            self.cdl_power = list(power)
        if sat is not None:
            self.cdl_saturation = float(sat)

    def get_gpu_shader_glsl(self):
        """
        Compiles the pipeline: Input ColorSpace -> CDL -> Display/View
        Returns (GLSL shader function text, list of 3D Lut texture names/data)
        """
        if not self.config:
            # Identity shader fallback
            return """
            vec4 ocio_color_transform(vec4 color) {
                return color;
            }
            """, []

        try:
            # Build GroupTransform containing:
            # 1. Colorspace Transform from Input to Reference (or compositing scene-linear role)
            # 2. CDL Transform
            # 3. Display View Transform from Reference to Display View
            
            group = OCIO.GroupTransform()
            
            # Step 1: Input to reference/rendering space (e.g. ACEScg / scene-linear)
            # Find scene_linear space or rendering role
            reference_space = None
            try:
                role_name = getattr(OCIO, "ROLE_SCENE_LINEAR", "scene_linear")
                reference_space = self.config.getRoleColorSpace(role_name)
            except Exception:
                pass
                
            if not reference_space:
                reference_space = "ACEScg"
                
            input_transform = OCIO.ColorSpaceTransform(src=self.input_colorspace, dst=reference_space)
            group.appendTransform(input_transform)
            
            # Step 2: CDL Transform (applied in scene-linear reference space)
            cdl = OCIO.CDLTransform()
            cdl.setSlope(self.cdl_slope)
            cdl.setOffset(self.cdl_offset)
            cdl.setPower(self.cdl_power)
            cdl.setSat(self.cdl_saturation)
            group.appendTransform(cdl)
            
            # Step 3: Display View Transform
            display_transform = OCIO.DisplayViewTransform(
                src=reference_space,
                display=self.display,
                view=self.view
            )
            group.appendTransform(display_transform)
            
            # Compile processor
            processor = self.config.getProcessor(group)
            gpu_proc = processor.getDefaultGPUProcessor()
            
            # Create GLSL shader description
            shader_desc = OCIO.GpuShaderDesc.CreateShaderDesc()
            shader_desc.setLanguage(OCIO.GPU_LANGUAGE_GLSL_1_3)
            shader_desc.setFunctionName("ocio_color_transform")
            
            # Extract shader information
            gpu_proc.extractGpuShaderInfo(shader_desc)
            
            # Parse textures
            textures_3d = []
            if hasattr(shader_desc, "get3DTextures"):
                for tex in shader_desc.get3DTextures():
                    textures_3d.append({
                        "name": tex.textureName,
                        "sampler": tex.samplerName,
                        "size": tex.edgeLen,
                        "data": tex.getValues()
                    })
            else:
                num_3d_tex = shader_desc.getNum3DTextures()
                for i in range(num_3d_tex):
                    tex_name, sampler_name, edge_len, lut_data = shader_desc.get3DTexture(i)
                    textures_3d.append({
                        "name": tex_name,
                        "sampler": sampler_name,
                        "size": edge_len,
                        "data": lut_data
                    })
                
            textures_1d = []
            if hasattr(shader_desc, "getTextures"):
                for tex in shader_desc.getTextures():
                    textures_1d.append({
                        "name": tex.textureName,
                        "sampler": tex.samplerName,
                        "width": tex.width,
                        "height": tex.height,
                        "channel": tex.channel,
                        "data": tex.getValues()
                    })
            else:
                num_1d_tex = shader_desc.getNumTextures()
                for i in range(num_1d_tex):
                    tex_name, sampler_name, width, height, channel, format, direction, lut_data = shader_desc.getTexture(i)
                    textures_1d.append({
                        "name": tex_name,
                        "sampler": sampler_name,
                        "width": width,
                        "height": height,
                        "channel": channel,
                        "format": format,
                        "direction": direction,
                        "data": lut_data
                    })
                
            shader_text = shader_desc.getShaderText()
            return shader_text, textures_3d, textures_1d
            
        except Exception as e:
            print(f"Error compiling OCIO GPU Shader: {e}")
            # Fallback to Identity
            return """
            vec4 ocio_color_transform(vec4 color) {
                return color;
            }
            """, [], []
