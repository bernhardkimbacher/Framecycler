import re
import struct
import unittest

from src.framecycler.color.ocio_manager import OCIOManager
from src.framecycler.render.shader_pipeline import (
    annotate_ocio_samplers,
    build_rhi_shader_bundle,
    inject_ocio_into_fragment,
    load_shader_template,
    pack_ocio_ubo_bytes,
    parse_ocio_ubo_layout,
    sort_ubo_members_metal_safe,
    ubo_has_vec3_before_scalar_hazard,
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
        self.assertIn("ocio_to_working", frag)
        self.assertIn("fc_asc_cdl", frag)
        self.assertIn("fc_cdl_enable", frag)

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
        self.assertIn("ocio_to_working", bundle.fragment_source)
        self.assertIn("ocio_to_display", bundle.fragment_source)
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
        self.assertIn("uniform OcioDynamicUbo", fragment)
        self.assertIn("layout(std140, binding = 3) uniform OcioDynamicUbo", fragment)
        self.assertIn("fc_cdl_slope", fragment)
        self.assertIn("fc_cdl_enable", fragment)
        self.assertIn("fc_cdl_enable < 0.5", fragment)
        # GradingPrimary stays dynamic (uploaded via get_grading_uniform_values).
        self.assertIn("ocio_grading_primary_contrast", fragment)
        self.assertIn("ocio_grading_primary_saturation", fragment)
        self.assertNotIn("_fc_gammaVal", fragment)
        self.assertIn("ocio_exposure_contrast_exposureVal", fragment)
        self.assertIn("ocio_grading_primary_brightness", fragment)
        self.assertIn("ocio_exposure_contrast_exposureVal", dynamic)
        self.assertIn("ocio_grading_primary_brightness", dynamic)
        self.assertTrue(dynamic)

    def test_ubo_no_vec3_before_scalar_hazard(self):
        """Metal packs trailing vec3 as packed_float3 when floats follow — RGB=LUM."""
        bundle = self.ocio_mgr.get_rhi_shader_bundle()
        self.assertFalse(ubo_has_vec3_before_scalar_hazard(bundle.fragment_source))

        match = re.search(
            r"uniform\s+OcioDynamicUbo\s*\{([^}]*)\}",
            bundle.fragment_source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group(1)
        # All floats appear before the first vec3.
        first_vec3 = body.find("vec3 ")
        last_float = body.rfind("float ")
        self.assertGreaterEqual(first_vec3, 0)
        self.assertGreaterEqual(last_float, 0)
        self.assertLess(last_float, first_vec3)

    def test_sort_ubo_members_metal_safe(self):
        members = [
            "vec3 fc_cdl_slope;",
            "float fc_cdl_saturation;",
            "vec3 fc_cdl_power;",
            "float fc_cdl_enable;",
        ]
        ordered = sort_ubo_members_metal_safe(members)
        self.assertEqual(
            ordered,
            [
                "float fc_cdl_enable;",
                "float fc_cdl_saturation;",
                "vec3 fc_cdl_power;",
                "vec3 fc_cdl_slope;",
            ],
        )

    def test_ubo_binding_contiguous_with_and_without_luts(self):
        aces = self.ocio_mgr.get_rhi_shader_bundle()
        self.assertEqual(aces.sampler_bindings, [])
        self.assertIn("layout(std140, binding = 3) uniform OcioDynamicUbo", aces.fragment_source)

        self.ocio_mgr.input_colorspace = "Cineon (ADX10)"
        self.ocio_mgr.invalidate_shader_cache()
        cineon = self.ocio_mgr.get_rhi_shader_bundle()
        self.assertTrue(any(dim == "2D" for _, dim, _ in cineon.sampler_bindings))
        self.assertEqual(cineon.sampler_bindings[0][0], 3)
        ubo_binding = 3 + len(cineon.sampler_bindings)
        self.assertIn(
            f"layout(std140, binding = {ubo_binding}) uniform OcioDynamicUbo",
            cineon.fragment_source,
        )
        for binding, _, _ in cineon.sampler_bindings:
            self.assertNotEqual(binding, ubo_binding)
        self.assertFalse(ubo_has_vec3_before_scalar_hazard(cineon.fragment_source))

    def test_pack_ocio_ubo_identity_sat_defaults(self):
        """CDL sat seeds to 1.0; grading primary contrast/sat remain UBO members."""
        bundle = self.ocio_mgr.get_rhi_shader_bundle()
        layout = parse_ocio_ubo_layout(bundle.fragment_source)
        self.assertIsNotNone(layout)
        assert layout is not None

        self.assertIn("ocio_grading_primary_saturation", layout.members)
        self.assertIn("ocio_grading_primary_contrast", layout.members)
        self.assertIn("fc_cdl_saturation", layout.members)
        self.assertIn("ocio_exposure_contrast_exposureVal", layout.members)
        self.assertIn("ocio_grading_primary_brightness", layout.members)

        seeded = pack_ocio_ubo_bytes(layout, seed_identity=True)
        self.assertAlmostEqual(
            struct.unpack_from("<f", seeded, layout.members["fc_cdl_saturation"].offset)[0],
            1.0,
            places=5,
        )
        self.assertAlmostEqual(
            struct.unpack_from("<f", seeded, layout.members["fc_cdl_enable"].offset)[0],
            0.0,
            places=5,
        )

    def test_wrap_ocio_dynamic_uniforms_orders_floats_before_vec3(self):
        ocio_body = (
            "uniform vec3 ocio_grading_primary_brightness;\n"
            "uniform float ocio_exposure_contrast_exposureVal;\n"
        )
        wrapped = wrap_ocio_dynamic_uniforms(
            ocio_body,
            ubo_binding=7,
            extra_members=["vec3 fc_cdl_slope;", "float fc_cdl_enable;"],
        )
        self.assertIn("layout(std140, binding = 7) uniform OcioDynamicUbo", wrapped)
        self.assertNotIn("uniform float ocio_exposure_contrast_exposureVal;", wrapped)
        body = re.search(r"OcioDynamicUbo\s*\{([^}]*)\}", wrapped, re.DOTALL).group(1)
        self.assertLess(body.find("float "), body.find("vec3 "))
        self.assertFalse(
            ubo_has_vec3_before_scalar_hazard(
                "layout(std140, binding = 7) uniform OcioDynamicUbo {" + body + "};"
            )
        )

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
