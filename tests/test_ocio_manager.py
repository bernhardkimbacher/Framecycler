import unittest
import os
import shutil
import tempfile
from unittest.mock import patch

from src.framecycler.color.ocio_manager import OCIOManager

class TestOCIOManager(unittest.TestCase):
    def setUp(self):
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("OCIO", None)
        # We initialize the manager with default bundled config
        self.ocio_mgr = OCIOManager()

    def tearDown(self):
        self._env_patch.stop()

    def test_initial_state(self):
        self.assertIsNotNone(self.ocio_mgr.config)
        self.assertEqual(self.ocio_mgr.input_colorspace, "ACEScg")
        self.assertIsNone(self.ocio_mgr.look)
        self.assertEqual(self.ocio_mgr.display_output, "sRGB")

    def test_load_config_prefers_ocio_env_over_settings(self):
        bundled_path = OCIOManager._bundled_config_path()
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_config = os.path.join(tmp_dir, "env_config.ocio")
            settings_config = os.path.join(tmp_dir, "settings_config.ocio")
            shutil.copy(bundled_path, env_config)
            shutil.copy(bundled_path, settings_config)

            with patch.dict(os.environ, {"OCIO": env_config}, clear=False):
                mgr = OCIOManager(settings_config)

            self.assertEqual(os.path.abspath(mgr.config_path), os.path.abspath(env_config))

    def test_load_config_uses_settings_when_env_unset(self):
        bundled_path = OCIOManager._bundled_config_path()
        with tempfile.TemporaryDirectory() as tmp_dir:
            settings_config = os.path.join(tmp_dir, "settings_config.ocio")
            shutil.copy(bundled_path, settings_config)

            os.environ.pop("OCIO", None)
            mgr = OCIOManager(settings_config)

            self.assertEqual(os.path.abspath(mgr.config_path), os.path.abspath(settings_config))

    def test_load_config_uses_bundled_when_env_and_settings_unset(self):
        os.environ.pop("OCIO", None)
        mgr = OCIOManager("")
        bundled_path = OCIOManager._bundled_config_path()

        self.assertEqual(os.path.abspath(mgr.config_path), os.path.abspath(bundled_path))

    def test_get_colorspaces(self):
        colorspaces = self.ocio_mgr.get_colorspaces()
        self.assertIn("ACEScg", colorspaces)
        self.assertIn("Raw", colorspaces)
        self.assertIn("ARRI Alexa LogC3", colorspaces)

    def test_get_looks(self):
        looks = self.ocio_mgr.get_looks()
        self.assertIn("None (Bypass)", looks)
        self.assertIn("ARRI LogC3 to Rec709", looks)
        self.assertIn("ARRI LogC4 to Rec709", looks)

    def test_get_display_outputs(self):
        outputs = self.ocio_mgr.get_display_outputs()
        self.assertEqual(outputs, ["Raw", "sRGB", "Rec709"])

    def test_set_look(self):
        self.ocio_mgr.set_look("ARRI LogC3 to Rec709")
        self.assertEqual(self.ocio_mgr.look, "ARRI LogC3 to Rec709")
        self.ocio_mgr.set_look("None (Bypass)")
        self.assertIsNone(self.ocio_mgr.look)

    def test_set_display_output(self):
        self.ocio_mgr.set_display_output("Rec709")
        self.assertEqual(self.ocio_mgr.display_output, "Rec709")
        self.ocio_mgr.set_display_output("Raw")
        self.assertEqual(self.ocio_mgr.display_output, "Raw")
        # Invalid output should be ignored
        self.ocio_mgr.set_display_output("InvalidOutput")
        self.assertEqual(self.ocio_mgr.display_output, "Raw")

    def test_gpu_shader_compilation(self):
        # 1. Test Default Bypass + sRGB
        shader_text, lut_3d, lut_1d = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertIn("ocio_color_transform", shader_text)
        self.assertTrue(len(shader_text) > 0)

        # 2. Test Input Space convert
        self.ocio_mgr.input_colorspace = "ARRI Alexa LogC3"
        shader_text, lut_3d, lut_1d = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertIn("ocio_color_transform", shader_text)

        # 3. Test Display output curves
        self.ocio_mgr.set_display_output("Rec709")
        shader_text, lut_3d, lut_1d = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertIn("ocio_color_transform", shader_text)

    def test_custom_lut_loading(self):
        # Create a temp .cube file to mock custom LUT load
        with tempfile.NamedTemporaryFile(suffix=".cube", delete=False) as f:
            f.write(b"LUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n")
            temp_path = f.name

        try:
            self.ocio_mgr.load_custom_lut(temp_path)
            self.assertEqual(self.ocio_mgr.look, os.path.basename(temp_path))
            self.assertEqual(self.ocio_mgr._custom_lut_path, temp_path)
            self.assertIn(os.path.basename(temp_path), self.ocio_mgr.get_looks())
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_detect_input_colorspace(self):
        # 1. Test filename pattern matching
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("shot_srgb_test.exr"), "sRGB - Texture")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("vfx_acescg_plate.exr"), "ACEScg")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("cam_slog3_clip.mp4"), "Sony S-Log3")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("camera_logc3_render.exr"), "ARRI Alexa LogC3")

        # 2. Test metadata properties
        self.assertEqual(
            self.ocio_mgr.detect_input_colorspace("video.mov", {"color_space": "bt709"}),
            "Rec.709 - Texture"
        )
        self.assertEqual(
            self.ocio_mgr.detect_input_colorspace("dpx_seq.dpx", {"transfer_characteristic": 2}),
            "Cineon (ADX10)"
        )
        self.assertEqual(
            self.ocio_mgr.detect_input_colorspace("dpx_seq.dpx", {"transfer_characteristic": 3}),
            "ACEScg"
        )
        self.assertEqual(
            self.ocio_mgr.detect_input_colorspace("clip.mov", {"file_metadata": {"color_transfer": "s-log3"}}),
            "Sony S-Log3"
        )

        # 3. Test fallback extensions
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("render.exr"), "ACEScg")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("frame.dpx"), "Cineon (ADX10)")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("capture.mov"), "Rec.709 - Texture")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("texture.png"), "sRGB - Texture")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("photo.jpg"), "sRGB - Texture")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("camera.ari"), "ARRI Alexa LogC3")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("raw_footage.r3d"), "RED Log3G10")
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("unknown_file.xyz"), "Raw")

    def test_built_in_grading_tool(self):
        # 1. Default grade parameters should be neutral
        self.assertEqual(self.ocio_mgr.grade_exposure, 0.0)
        self.assertEqual(self.ocio_mgr.grade_gamma, 1.0)
        self.assertEqual(self.ocio_mgr.grade_offset, 0.0)

        # 2. Modify parameters and verify the GPU shader text includes CDL structures
        self.ocio_mgr.grade_exposure = 1.5
        self.ocio_mgr.grade_gamma = 1.2
        self.ocio_mgr.grade_offset = -0.05

        shader_text, lut_3d, lut_1d = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertIn("ocio_exposure_contrast_exposureVal", shader_text)
        self.assertIn("ocio_grading_primary_brightness", shader_text)

    def test_dynamic_grading_does_not_change_shader_text(self):
        text_a, _, _ = self.ocio_mgr.get_gpu_shader_glsl()
        self.ocio_mgr.set_grading_values(exposure=2.0, gamma=0.7, offset=0.2)
        text_b, _, _ = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertEqual(text_a, text_b)

    def test_grading_uniform_values(self):
        self.ocio_mgr.set_grading_values(exposure=1.0, gamma=1.5, offset=-0.1)
        values = self.ocio_mgr.get_grading_uniform_values()
        self.assertEqual(values["ocio_exposure_contrast_exposureVal"], 1.0)
        self.assertEqual(values["ocio_exposure_contrast_gammaVal"], 1.5)
        self.assertEqual(values["ocio_grading_primary_brightness"], (-0.1, -0.1, -0.1))
        self.assertEqual(values["ocio_grading_primary_contrast"], (1.0, 1.0, 1.0))
        self.assertEqual(values["ocio_grading_primary_gamma"], (1.0, 1.0, 1.0))
        self.assertEqual(values["ocio_grading_primary_saturation"], 1.0)
        self.assertEqual(values["ocio_grading_primary_localBypass"], 0.0)

    def test_rhi_shader_bundle(self):
        bundle = self.ocio_mgr.get_rhi_shader_bundle()
        self.assertIn("layout(binding = 1) uniform sampler2D texA", bundle.fragment_source)
        self.assertIn("ocio_color_transform", bundle.fragment_source)
