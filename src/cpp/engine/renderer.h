#pragma once
#include <string>
#include <vector>
#include <cstdint>
#include "gl_loader.h"

struct TextureUploadState {
    GLuint tex_id = 0;
    GLuint pbo[2] = {0, 0};
    int pbo_index = 0;
    uint64_t last_upload_token = 0;
    int last_w = 0;
    int last_h = 0;
    int last_channels = 0;
};

struct ShaderUniforms {
    GLint texA = -1;
    GLint samplerB = -1;
    GLint compareMode = -1;
    GLint wipePos = -1;
    GLint channelMask = -1;
    GLint scale = -1;
    GLint offset = -1;
    std::vector<GLint> ocio_lut3d;
    bool valid = false;
};

class GLRenderer {
public:
    GLRenderer();
    ~GLRenderer();

    void initialize();
    void set_ocio_shader(const std::string& ocio_shader_code);
    void upload_ocio_lut_3d(int index, int size, const float* data);
    void cleanup();

    void render(
        int width_a, int height_a, int channels_a, const void* data_a, uint64_t upload_token_a,
        int width_b, int height_b, int channels_b, const void* data_b, uint64_t upload_token_b,
        int compare_mode, float wipe_pos, int channel_mask,
        float scale_x, float scale_y, float pan_x, float pan_y
    );

private:
    void _upload_texture(TextureUploadState& state, int w, int h, int channels, const void* data, uint64_t upload_token);
    void _compile_shader_program(const std::string& ocio_code);
    void _cache_uniform_locations();

    TextureUploadState _tex_a_state;
    TextureUploadState _tex_b_state;
    std::vector<GLuint> _ocio_luts_3d;

    GLuint _shader_program;
    GLuint _quad_vao;
    GLuint _quad_vbo;
    ShaderUniforms _uniforms;

    bool _initialized;
};
