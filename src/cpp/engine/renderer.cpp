#include "renderer.h"
#include <iostream>

#ifndef GL_HALF_FLOAT
#define GL_HALF_FLOAT 0x140B
#endif
#ifndef GL_RGBA16F
#define GL_RGBA16F 0x881A
#endif
#ifndef GL_RGB16F
#define GL_RGB16F 0x881B
#endif
#ifndef GL_R16F
#define GL_R16F 0x822D
#endif
#ifndef GL_PIXEL_UNPACK_BUFFER
#define GL_PIXEL_UNPACK_BUFFER 0x88EC
#endif
#ifndef GL_STREAM_DRAW
#define GL_STREAM_DRAW 0x88E0
#endif

GLRenderer::GLRenderer()
    : _shader_program(0), _quad_vao(0), _quad_vbo(0), _initialized(false) {}

GLRenderer::~GLRenderer() {
    cleanup();
}

void GLRenderer::initialize() {
    if (_initialized) return;

    load_gl_extensions();

    float quad_vertices[] = {
        -1.0f,  1.0f, 0.0f, 0.0f, 1.0f,
        -1.0f, -1.0f, 0.0f, 0.0f, 0.0f,
         1.0f,  1.0f, 0.0f, 1.0f, 1.0f,
         1.0f, -1.0f, 0.0f, 1.0f, 0.0f,
    };

    glGenVertexArrays(1, &_quad_vao);
    glGenBuffers(1, &_quad_vbo);

    glBindVertexArray(_quad_vao);
    glBindBuffer(GL_ARRAY_BUFFER, _quad_vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad_vertices), quad_vertices, GL_STATIC_DRAW);

    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 5 * sizeof(float), (void*)0);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 5 * sizeof(float), (void*)(3 * sizeof(float)));

    glBindVertexArray(0);

    glDisable(GL_FRAMEBUFFER_SRGB);

    _initialized = true;
}

void GLRenderer::set_ocio_shader(const std::string& ocio_shader_code) {
    if (!_initialized) initialize();
    _compile_shader_program(ocio_shader_code);
}

void GLRenderer::upload_ocio_lut_3d(int index, int size, const float* data) {
    if (!_initialized) initialize();

    if (_ocio_luts_3d.size() <= static_cast<size_t>(index)) {
        _ocio_luts_3d.resize(index + 1, 0);
    }

    GLuint& tex_id = _ocio_luts_3d[index];
    if (tex_id == 0) {
        glGenTextures(1, &tex_id);
    }

    glBindTexture(GL_TEXTURE_3D, tex_id);
    glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_3D, GL_TEXTURE_WRAP_R, GL_CLAMP_TO_EDGE);

    glTexImage3D(GL_TEXTURE_3D, 0, GL_RGB32F, size, size, size, 0, GL_RGB, GL_FLOAT, data);
    glBindTexture(GL_TEXTURE_3D, 0);
}

static void _ensure_pbo_pair(TextureUploadState& state) {
    if (state.pbo[0] == 0) {
        glGenBuffers(2, state.pbo);
    }
}

void GLRenderer::_upload_texture(TextureUploadState& state, int w, int h, int channels, const void* data, uint64_t upload_token) {
    if (data == nullptr) {
        return;
    }

    if (state.last_upload_token == upload_token &&
        state.last_w == w &&
        state.last_h == h &&
        state.last_channels == channels &&
        state.tex_id != 0) {
        return;
    }

    if (state.tex_id == 0) {
        glGenTextures(1, &state.tex_id);
    }

    GLenum gl_format = GL_RGB;
    GLenum internal_format = GL_RGB16F;

    if (channels == 4) {
        gl_format = GL_RGBA;
        internal_format = GL_RGBA16F;
    } else if (channels == 1) {
        gl_format = GL_RED;
        internal_format = GL_R16F;
    }

    const size_t byte_size = static_cast<size_t>(w) * static_cast<size_t>(h) * static_cast<size_t>(channels) * sizeof(uint16_t);

    _ensure_pbo_pair(state);
    state.pbo_index = 1 - state.pbo_index;
    const GLuint pbo = state.pbo[state.pbo_index];

    glBindBuffer(GL_PIXEL_UNPACK_BUFFER, pbo);
    glBufferData(GL_PIXEL_UNPACK_BUFFER, byte_size, data, GL_STREAM_DRAW);

    glBindTexture(GL_TEXTURE_2D, state.tex_id);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);

    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);

    const bool size_changed = (state.last_w != w || state.last_h != h || state.last_channels != channels || state.tex_id == 0);
    if (size_changed) {
        glTexImage2D(GL_TEXTURE_2D, 0, internal_format, w, h, 0, gl_format, GL_HALF_FLOAT, nullptr);
    }
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, gl_format, GL_HALF_FLOAT, nullptr);

    glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
    glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0);
    glBindTexture(GL_TEXTURE_2D, 0);

    state.last_upload_token = upload_token;
    state.last_w = w;
    state.last_h = h;
    state.last_channels = channels;
}

void GLRenderer::render(
    int width_a, int height_a, int channels_a, const void* data_a, uint64_t upload_token_a,
    int width_b, int height_b, int channels_b, const void* data_b, uint64_t upload_token_b,
    int compare_mode, float wipe_pos, int channel_mask,
    float scale_x, float scale_y, float pan_x, float pan_y
) {
    if (!_initialized || _shader_program == 0) return;

    glDisable(GL_FRAMEBUFFER_SRGB);

    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);

    glUseProgram(_shader_program);

    if (data_a != nullptr) {
        _upload_texture(_tex_a_state, width_a, height_a, channels_a, data_a, upload_token_a);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, _tex_a_state.tex_id);
        if (_uniforms.valid && _uniforms.texA != -1) {
            glUniform1i(_uniforms.texA, 0);
        }
    }

    if (data_b != nullptr) {
        _upload_texture(_tex_b_state, width_b, height_b, channels_b, data_b, upload_token_b);
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_2D, _tex_b_state.tex_id);
        if (_uniforms.valid && _uniforms.samplerB != -1) {
            glUniform1i(_uniforms.samplerB, 1);
        }
    } else {
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_2D, 0);
    }

    int tex_unit_start = 2;
    if (_uniforms.valid) {
        for (size_t i = 0; i < _ocio_luts_3d.size() && i < _uniforms.ocio_lut3d.size(); ++i) {
            if (_ocio_luts_3d[i] != 0 && _uniforms.ocio_lut3d[i] != -1) {
                glActiveTexture(GL_TEXTURE0 + tex_unit_start);
                glBindTexture(GL_TEXTURE_3D, _ocio_luts_3d[i]);
                glUniform1i(_uniforms.ocio_lut3d[i], tex_unit_start);
                tex_unit_start++;
            }
        }
    }

    if (_uniforms.valid) {
        if (_uniforms.compareMode != -1) glUniform1i(_uniforms.compareMode, compare_mode);
        if (_uniforms.wipePos != -1) glUniform1f(_uniforms.wipePos, wipe_pos);
        if (_uniforms.channelMask != -1) glUniform1i(_uniforms.channelMask, channel_mask);
        if (_uniforms.scale != -1) glUniform2f(_uniforms.scale, scale_x, scale_y);
        if (_uniforms.offset != -1) glUniform2f(_uniforms.offset, pan_x, pan_y);
    }

    glBindVertexArray(_quad_vao);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glBindVertexArray(0);
    glUseProgram(0);
}

void GLRenderer::_cache_uniform_locations() {
    _uniforms.texA = glGetUniformLocation(_shader_program, "texA");
    _uniforms.samplerB = glGetUniformLocation(_shader_program, "samplerB");
    _uniforms.compareMode = glGetUniformLocation(_shader_program, "compareMode");
    _uniforms.wipePos = glGetUniformLocation(_shader_program, "wipePos");
    _uniforms.channelMask = glGetUniformLocation(_shader_program, "channelMask");
    _uniforms.scale = glGetUniformLocation(_shader_program, "scale");
    _uniforms.offset = glGetUniformLocation(_shader_program, "offset");

    _uniforms.ocio_lut3d.clear();
    for (size_t i = 0; i < _ocio_luts_3d.size(); ++i) {
        std::string lut_sampler_name = "ocio_lut3d_" + std::to_string(i);
        GLint loc = glGetUniformLocation(_shader_program, lut_sampler_name.c_str());
        if (loc == -1) {
            lut_sampler_name += "Sampler";
            loc = glGetUniformLocation(_shader_program, lut_sampler_name.c_str());
        }
        _uniforms.ocio_lut3d.push_back(loc);
    }

    _uniforms.valid = true;
}

void GLRenderer::_compile_shader_program(const std::string& ocio_code) {
    std::string vertex_src = R"(
        #version 150
        in vec3 position;
        in vec2 texCoords;
        out vec2 uv;
        uniform vec2 scale;
        uniform vec2 offset;
        void main() {
            uv = vec2(texCoords.x, 1.0 - texCoords.y);
            gl_Position = vec4(position.xy * scale + offset, 0.0, 1.0);
        }
    )";

    std::string fragment_src = R"(
        #version 150
        in vec2 uv;
        out vec4 fragColor;
        uniform sampler2D texA;
        uniform sampler2D samplerB;
        uniform int compareMode;
        uniform float wipePos;
        uniform int channelMask;

        )" + ocio_code + R"(

        void main() {
            vec4 colorA = texture(texA, uv);
            vec4 colorB = texture(samplerB, uv);

            vec4 finalColor = colorA;

            if (compareMode == 1) {
                if (uv.x > wipePos) {
                    finalColor = colorB;
                }
            } else if (compareMode == 2) {
                finalColor = vec4(abs(colorA.rgb - colorB.rgb), max(colorA.a, colorB.a));
            } else if (compareMode == 3) {
                if (uv.x < 0.5) {
                    finalColor = texture(texA, vec2(uv.x * 2.0, uv.y));
                } else {
                    finalColor = texture(samplerB, vec2((uv.x - 0.5) * 2.0, uv.y));
                }
            }

            if (channelMask == 1) {
                finalColor = vec4(finalColor.r, finalColor.r, finalColor.r, 1.0);
            } else if (channelMask == 2) {
                finalColor = vec4(finalColor.g, finalColor.g, finalColor.g, 1.0);
            } else if (channelMask == 3) {
                finalColor = vec4(finalColor.b, finalColor.b, finalColor.b, 1.0);
            } else if (channelMask == 4) {
                finalColor = vec4(finalColor.a, finalColor.a, finalColor.a, 1.0);
            } else if (channelMask == 5) {
                float lum = dot(finalColor.rgb, vec3(0.2126, 0.7152, 0.0722));
                finalColor = vec4(lum, lum, lum, 1.0);
            }

            fragColor = ocio_color_transform(finalColor);
        }
    )";

    GLuint vs = glCreateShader(GL_VERTEX_SHADER);
    const char* vs_ptr = vertex_src.c_str();
    glShaderSource(vs, 1, &vs_ptr, NULL);
    glCompileShader(vs);

    GLint success;
    glGetShaderiv(vs, GL_COMPILE_STATUS, &success);
    if (!success) {
        char log[512];
        glGetShaderInfoLog(vs, 512, NULL, log);
        std::cerr << "GLRenderer: Vertex shader compilation failed: " << log << std::endl;
    }

    GLuint fs = glCreateShader(GL_FRAGMENT_SHADER);
    const char* fs_ptr = fragment_src.c_str();
    glShaderSource(fs, 1, &fs_ptr, NULL);
    glCompileShader(fs);

    glGetShaderiv(fs, GL_COMPILE_STATUS, &success);
    if (!success) {
        char log[512];
        glGetShaderInfoLog(fs, 512, NULL, log);
        std::cerr << "GLRenderer: Fragment shader compilation failed: " << log << std::endl;
    }

    if (_shader_program != 0) {
        glDeleteProgram(_shader_program);
    }

    _shader_program = glCreateProgram();
    glAttachShader(_shader_program, vs);
    glAttachShader(_shader_program, fs);

    glBindAttribLocation(_shader_program, 0, "position");
    glBindAttribLocation(_shader_program, 1, "texCoords");

    glLinkProgram(_shader_program);

    glDeleteShader(vs);
    glDeleteShader(fs);

    _cache_uniform_locations();
}

void GLRenderer::cleanup() {
    if (_shader_program != 0) {
        glDeleteProgram(_shader_program);
        _shader_program = 0;
    }

    auto cleanup_texture_state = [](TextureUploadState& state) {
        if (state.tex_id != 0) {
            glDeleteTextures(1, &state.tex_id);
            state.tex_id = 0;
        }
        if (state.pbo[0] != 0) {
            glDeleteBuffers(2, state.pbo);
            state.pbo[0] = 0;
            state.pbo[1] = 0;
        }
        state.last_upload_token = 0;
        state.last_w = 0;
        state.last_h = 0;
        state.last_channels = 0;
    };

    cleanup_texture_state(_tex_a_state);
    cleanup_texture_state(_tex_b_state);

    for (auto tex : _ocio_luts_3d) {
        if (tex != 0) {
            glDeleteTextures(1, &tex);
        }
    }
    _ocio_luts_3d.clear();

    if (_quad_vao != 0) {
        glDeleteVertexArrays(1, &_quad_vao);
        _quad_vao = 0;
    }
    if (_quad_vbo != 0) {
        glDeleteBuffers(1, &_quad_vbo);
        _quad_vbo = 0;
    }

    _uniforms = ShaderUniforms{};
}
