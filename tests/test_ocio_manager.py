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
        self.assertEqual(self.ocio_mgr.display_name, "sRGB")
        self.assertEqual(self.ocio_mgr.view_name, "sRGB View")
        self.assertEqual(self.ocio_mgr.display_output, "sRGB / sRGB View")

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
        self.assertIn("Raw", outputs)
        self.assertIn("sRGB / sRGB View", outputs)
        self.assertIn("Rec.709 / Rec.709 View", outputs)

    def test_set_display_output(self):
        self.ocio_mgr.set_display_output("Rec709")
        self.assertEqual(self.ocio_mgr.display_name, "Rec.709")
        self.assertEqual(self.ocio_mgr.view_name, "Rec.709 View")
        self.assertEqual(self.ocio_mgr.display_output, "Rec.709 / Rec.709 View")
        self.ocio_mgr.set_display_output("Raw")
        self.assertEqual(self.ocio_mgr.display_output, "Raw")
        # Invalid output should be ignored
        self.ocio_mgr.set_display_output("InvalidOutput")
        self.assertEqual(self.ocio_mgr.display_output, "Raw")

    def test_display_view_transform_in_group(self):
        self.assertTrue(self.ocio_mgr.transform_group_has_display_view())
        self.ocio_mgr.set_display_output("Raw")
        self.assertFalse(self.ocio_mgr.transform_group_has_display_view())
        self.ocio_mgr.set_look("ARRI LogC3 to Rec709")
        self.ocio_mgr.set_display_output("sRGB")
        self.assertTrue(self.ocio_mgr.transform_group_has_display_view())
        post = list(self.ocio_mgr._build_post_cdl_group())
        self.assertGreaterEqual(len(post), 2)  # look + DVT

    def test_gpu_shader_compilation(self):
        # 1. Test Default Bypass + sRGB view
        shader_text, lut_3d, lut_1d = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertIn("ocio_to_working", shader_text)
        self.assertIn("ocio_to_display", shader_text)
        self.assertTrue(len(shader_text) > 0)

        # 2. Test Input Space convert
        self.ocio_mgr.input_colorspace = "ARRI Alexa LogC3"
        self.ocio_mgr.invalidate_shader_cache()
        shader_text, lut_3d, lut_1d = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertIn("ocio_to_working", shader_text)

        # 3. Test Display view change
        self.ocio_mgr.set_display_output("Rec709")
        self.ocio_mgr.invalidate_shader_cache()
        shader_text, lut_3d, lut_1d = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertIn("ocio_to_display", shader_text)

    def test_cineon_emits_1d_lut(self):
        self.ocio_mgr.input_colorspace = "Cineon (ADX10)"
        self.ocio_mgr.invalidate_shader_cache()
        bundle = self.ocio_mgr.get_rhi_shader_bundle()
        self.assertTrue(
            bundle.textures_1d or any(dim == "2D" for _, dim, _ in bundle.sampler_bindings),
            "Expected 1D/2D LUT samplers for Cineon→ACEScg",
        )
        self.assertTrue(
            any(dim == "2D" for _, dim, _ in bundle.sampler_bindings)
            or "sampler2D" in bundle.fragment_source
        )

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
        self.assertEqual(self.ocio_mgr.detect_input_colorspace("unknown_file.xyz"), "ACEScg")

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

    def test_cdl_defaults_identity(self):
        self.assertEqual(self.ocio_mgr.cdl_slope, (1.0, 1.0, 1.0))
        self.assertEqual(self.ocio_mgr.cdl_offset, (0.0, 0.0, 0.0))
        self.assertEqual(self.ocio_mgr.cdl_power, (1.0, 1.0, 1.0))
        self.assertEqual(self.ocio_mgr.cdl_saturation, 1.0)

    def test_cdl_does_not_change_pipeline_key_or_shader(self):
        key_a = self.ocio_mgr.get_pipeline_key()
        text_a, _, _ = self.ocio_mgr.get_gpu_shader_glsl()

        self.ocio_mgr.set_cdl_values(
            slope=(1.2, 1.0, 0.8),
            offset=(0.01, 0.0, -0.01),
            power=(1.0, 1.0, 1.0),
            saturation=1.15,
        )
        key_b = self.ocio_mgr.get_pipeline_key()
        text_b, _, _ = self.ocio_mgr.get_gpu_shader_glsl()

        self.assertEqual(key_a, key_b)
        self.assertEqual(text_a, text_b)
        values = self.ocio_mgr.get_grading_uniform_values()
        self.assertEqual(values["fc_cdl_slope"], (1.2, 1.0, 0.8))
        self.assertEqual(values["fc_cdl_saturation"], 1.15)
        self.assertEqual(values["fc_cdl_enable"], 1.0)

    def test_cdl_enable_identity_vs_active(self):
        values = self.ocio_mgr.get_grading_uniform_values()
        self.assertEqual(values["fc_cdl_enable"], 0.0)
        self.ocio_mgr.set_cdl_values(saturation=0.8)
        self.assertEqual(self.ocio_mgr.get_grading_uniform_values()["fc_cdl_enable"], 1.0)
        self.ocio_mgr.reset_cdl_values()
        self.assertEqual(self.ocio_mgr.get_grading_uniform_values()["fc_cdl_enable"], 0.0)

    def test_rhi_bundle_has_cdl_enable_early_out(self):
        bundle = self.ocio_mgr.get_rhi_shader_bundle()
        self.assertIn("fc_cdl_enable", bundle.fragment_source)
        self.assertIn("fc_cdl_enable < 0.5", bundle.fragment_source)

    def test_reset_cdl_values(self):
        self.ocio_mgr.set_cdl_values(slope=(1.5, 1.5, 1.5), saturation=0.5)
        self.ocio_mgr.reset_cdl_values()
        self.assertEqual(self.ocio_mgr.cdl_slope, (1.0, 1.0, 1.0))
        self.assertEqual(self.ocio_mgr.cdl_saturation, 1.0)

    def test_grading_uniform_values(self):
        self.ocio_mgr.set_grading_values(exposure=1.0, gamma=1.5, offset=-0.1)
        values = self.ocio_mgr.get_grading_uniform_values()
        self.assertEqual(values["ocio_exposure_contrast_exposureVal"], 1.0)
        self.assertEqual(values["ocio_exposure_contrast_gammaVal"], 1.5)
        self.assertEqual(values["ocio_grading_primary_brightness"], (-0.1, -0.1, -0.1))
        # GradingPrimary factory defaults must be uploaded (zero UBO → gray).
        self.assertEqual(values["ocio_grading_primary_contrast"], (1.0, 1.0, 1.0))
        self.assertEqual(values["ocio_grading_primary_saturation"], 1.0)
        self.assertIn("fc_cdl_slope", values)
        self.assertIn("fc_cdl_saturation", values)
        self.assertEqual(values["fc_cdl_enable"], 0.0)

    def test_rhi_shader_bundle(self):
        from src.framecycler.render.shader_pipeline import ubo_has_vec3_before_scalar_hazard

        bundle = self.ocio_mgr.get_rhi_shader_bundle()
        self.assertIn("layout(binding = 1) uniform sampler2D texA", bundle.fragment_source)
        self.assertIn("ocio_to_working", bundle.fragment_source)
        self.assertIn("ocio_to_display", bundle.fragment_source)
        self.assertIn("fc_asc_cdl", bundle.fragment_source)
        self.assertIn("fc_cdl_slope", bundle.fragment_source)
        self.assertIn("layout(std140, binding = 3) uniform OcioDynamicUbo", bundle.fragment_source)
        self.assertIn("ocio_grading_primary_contrast", bundle.fragment_source)
        self.assertNotIn("_fc_gammaVal", bundle.fragment_source)
        self.assertFalse(ubo_has_vec3_before_scalar_hazard(bundle.fragment_source))
        self.assertIn("ocio_exposure_contrast_exposureVal", bundle.fragment_source)
        self.assertIn("ocio_grading_primary_brightness", bundle.fragment_source)

    def test_reload_config_preserves_look_and_display(self):
        self.ocio_mgr.set_look("ARRI LogC3 to Rec709")
        self.ocio_mgr.set_display_output("sRGB / sRGB View")
        self.ocio_mgr.input_colorspace = "ARRI Alexa LogC3"
        path = self.ocio_mgr.reload_config("")
        self.assertTrue(path)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.ocio_mgr.look, "ARRI LogC3 to Rec709")
        self.assertEqual(self.ocio_mgr.display_output, "sRGB / sRGB View")
        self.assertEqual(self.ocio_mgr.input_colorspace, "ARRI Alexa LogC3")

    def test_pipeline_key_changes_when_config_mtime_changes(self):
        """QSB disk cache is keyed by pipeline_key; config edits must change it."""
        self.ocio_mgr.set_look("ARRI LogC3 to Rec709")
        self.ocio_mgr.set_display_output("sRGB / sRGB View")
        key_before = self.ocio_mgr.get_pipeline_key()
        path = self.ocio_mgr.config_path
        self.assertTrue(path and os.path.isfile(path))
        os.utime(path, None)  # bump mtime
        key_after = self.ocio_mgr.get_pipeline_key()
        self.assertNotEqual(key_before, key_after)

    def test_arri_look_roundtrips_to_acescg_before_display(self):
        """Look must exit true ACEScg so sRGB View encodes once (not double display)."""
        import numpy as np
        import PyOpenColorIO as OCIO

        def apply_group(group, rgb):
            cpu = self.ocio_mgr.config.getProcessor(group).getDefaultCPUProcessor()
            pixel = np.array(rgb, dtype=np.float32)
            cpu.applyRGB(pixel)
            return pixel

        # Mid-grey ACEScg sample (working space; input convert is identity).
        sample = [0.18, 0.18, 0.18]

        self.ocio_mgr.input_colorspace = "ACEScg"
        self.ocio_mgr.set_look("ARRI LogC3 to Rec709")
        self.ocio_mgr.set_display_output("sRGB / sRGB View")
        with_display = apply_group(self.ocio_mgr._build_post_cdl_group(), sample)

        self.ocio_mgr.set_display_output("Raw")
        look_only = apply_group(self.ocio_mgr._build_post_cdl_group(), sample)

        # Explicit ACEScg → sRGB after look-only path (proves look exited scene-linear).
        display_only = OCIO.GroupTransform()
        display_only.appendTransform(
            OCIO.ColorSpaceTransform(src="ACEScg", dst="sRGB - Texture")
        )
        look_then_display = apply_group(display_only, look_only.tolist())

        np.testing.assert_allclose(
            with_display,
            look_then_display,
            rtol=1e-4,
            atol=1e-4,
            err_msg="Look+sRGB View must match Look+Raw then ACEScg→sRGB",
        )
        # Regression: without Rec.709→ACEScg return, Look+sRGB ≈ Look+Raw (show LUT).
        self.assertFalse(
            np.allclose(with_display, look_only, rtol=1e-3, atol=1e-3),
            "Look+sRGB View must differ from Look+Raw (display encode must run once)",
        )

        # LogC4 look must also round-trip (same structural check on mid sample).
        self.ocio_mgr.set_look("ARRI LogC4 to Rec709")
        self.ocio_mgr.set_display_output("sRGB / sRGB View")
        with_display_c4 = apply_group(self.ocio_mgr._build_post_cdl_group(), sample)
        self.ocio_mgr.set_display_output("Raw")
        look_only_c4 = apply_group(self.ocio_mgr._build_post_cdl_group(), sample)
        look_then_display_c4 = apply_group(display_only, look_only_c4.tolist())
        np.testing.assert_allclose(
            with_display_c4,
            look_then_display_c4,
            rtol=1e-4,
            atol=1e-4,
            err_msg="LogC4 Look+sRGB View must match Look+Raw then ACEScg→sRGB",
        )


if __name__ == "__main__":
    unittest.main()
