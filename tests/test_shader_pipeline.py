import unittest

from src.framecycler.color.ocio_manager import OCIOManager
from src.framecycler.render.shader_pipeline import (
    annotate_ocio_samplers,
    build_rhi_shader_bundle,
    inject_ocio_into_fragment,
    load_shader_template,
    parse_ocio_ubo_layout,
    wrap_ocio_dynamic_uniforms,
)


class TestShaderPipeline(unittest.TestCase):
    def setUp(self):
        self.ocio_mgr = OCIOManager()

    def test_templates_load(self):
        vert = load_shader_template("quad.vert.glsl")
        frag = load_shader_template("quad.frag.glsl")
        self.assertIn("#version 450", vert)
        self.assertIn("layout(binding = 1) uniform sampler2D texA", frag)
        self.assertIn("ocio_color_transform", frag)

    def test_annotate_ocio_samplers(self):
        ocio_body = "uniform sampler3D ocio_lut3d_0Sampler;\n"
        annotated, bindings = annotate_ocio_samplers(ocio_body, start_binding=3)
        self.assertIn("layout(binding = 3) uniform sampler3D ocio_lut3d_0Sampler", annotated)
        self.assertEqual(bindings, [(3, "3D", "ocio_lut3d_0Sampler")])

    def test_build_rhi_shader_bundle_with_look(self):
        self.ocio_mgr.set_look("ARRI LogC3 to Rec709")
        ocio_text, tex3d, tex1d = self.ocio_mgr.get_gpu_shader_glsl()
        bundle = build_rhi_shader_bundle(ocio_text, tex3d, tex1d, self.ocio_mgr.get_pipeline_key())
        self.assertIn("layout(binding = 3) uniform sampler3D", bundle.fragment_source)
        self.assertIn("ocio_color_transform", bundle.fragment_source)
        self.assertTrue(bundle.sampler_bindings)
        self.assertEqual(bundle.pipeline_key, self.ocio_mgr.get_pipeline_key())

    def test_grading_shader_text_stable(self):
        text_a, _, _ = self.ocio_mgr.get_gpu_shader_glsl()
        self.ocio_mgr.set_grading_values(exposure=1.0, gamma=0.8, offset=0.1)
        text_b, _, _ = self.ocio_mgr.get_gpu_shader_glsl()
        self.assertEqual(text_a, text_b)
        self.assertIn("ocio_exposure_contrast_exposureVal", text_a)

    def test_get_rhi_shader_bundle(self):
        bundle = self.ocio_mgr.get_rhi_shader_bundle()
        self.assertIn("#version 450", bundle.vertex_source)
        fragment, bindings, dynamic = inject_ocio_into_fragment(bundle.ocio_function_source)
        self.assertIn("layout(binding = 1) uniform sampler2D texA", fragment)
        self.assertIn("layout(std140, binding = 3) uniform OcioDynamicUbo", fragment)
        self.assertTrue(dynamic)

    def test_wrap_ocio_dynamic_uniforms(self):
        ocio_body = (
            "uniform float ocio_exposure_contrast_exposureVal;\n"
            "uniform vec3 ocio_grading_primary_brightness;\n"
        )
        wrapped = wrap_ocio_dynamic_uniforms(ocio_body, ubo_binding=7)
        self.assertIn("layout(std140, binding = 7) uniform OcioDynamicUbo", wrapped)
        self.assertNotIn("uniform float ocio_exposure_contrast_exposureVal;", wrapped)
        self.assertIn("float ocio_exposure_contrast_exposureVal;", wrapped)

    def test_parse_ocio_ubo_layout(self):
        fragment = (
            "layout(std140, binding = 4) uniform OcioDynamicUbo {\n"
            "    float ocio_exposure_contrast_exposureVal;\n"
            "    vec3 ocio_grading_primary_brightness;\n"
            "};\n"
        )
        layout = parse_ocio_ubo_layout(fragment)
        self.assertIsNotNone(layout)
        self.assertEqual(layout.binding, 4)
        self.assertGreaterEqual(layout.size, 32)
        self.assertIn("ocio_exposure_contrast_exposureVal", layout.members)
        self.assertIn("ocio_grading_primary_brightness", layout.members)


if __name__ == "__main__":
    unittest.main()
