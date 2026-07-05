#pragma once
#include <string>
#include <vector>
#include "gl_loader.h"

class GLRenderer {
public:
    GLRenderer();
    ~GLRenderer();

    void initialize();
    void set_ocio_shader(const std::string& ocio_shader_code);
    void upload_ocio_lut_3d(int index, int size, const float* data);
    void cleanup();

    void render(
        int width_a, int height_a, int channels_a, const float* data_a,
        int width_b, int height_b, int channels_b, const float* data_b,
        int compare_mode, float wipe_pos, int channel_mask,
        float scale_x, float scale_y, float pan_x, float pan_y
    );

private:
    void _upload_texture(GLuint& tex_id, int w, int h, int channels, const float* data);
    void _compile_shader_program(const std::string& ocio_code);

    GLuint _tex_a;
    GLuint _tex_b;
    std::vector<GLuint> _ocio_luts_3d;

    GLuint _shader_program;
    GLuint _quad_vao;
    GLuint _quad_vbo;

    bool _initialized;
};
