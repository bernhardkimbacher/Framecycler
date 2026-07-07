import unittest
import threading

from src.framecycler.render.shader_baker import ShaderBaker


class TestShaderBaker(unittest.TestCase):
    def test_bake_sync(self):
        baker = ShaderBaker()
        if not baker.available:
            self.skipTest("pyside6-qsb not available")

        vert = """#version 450
layout(location = 0) in vec2 position;
void main() { gl_Position = vec4(position, 0.0, 1.0); }
"""
        frag = """#version 450
layout(location = 0) out vec4 fragColor;
void main() { fragColor = vec4(1.0); }
"""
        vert_qsb, frag_qsb = baker.bake_sync(vert, frag)
        self.assertGreater(len(vert_qsb), 0)
        self.assertGreater(len(frag_qsb), 0)

    def test_bake_async_debounced(self):
        baker = ShaderBaker(debounce_ms=50)
        if not baker.available:
            self.skipTest("pyside6-qsb not available")

        done = threading.Event()
        results = []

        def callback(vert_qsb, frag_qsb):
            results.append((len(vert_qsb), frag_qsb is not None))
            done.set()

        vert = "#version 450\nlayout(location=0) in vec2 position;\nvoid main(){ gl_Position=vec4(position,0,1);}\n"
        frag = "#version 450\nlayout(location=0) out vec4 fragColor;\nvoid main(){ fragColor=vec4(1);}\n"
        baker.bake_async("ignored", "ignored", callback)
        baker.bake_async(vert, frag, callback)

        self.assertTrue(done.wait(timeout=1.0))
        self.assertGreater(results[-1][0], 0)
        self.assertTrue(results[-1][1])


if __name__ == "__main__":
    unittest.main()
