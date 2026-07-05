#include "renderer.h"
#include <iostream>

GLRenderer::GLRenderer()
    : _tex_a(0), _tex_b(0), _shader_program(0), _quad_vao(0), _quad_vbo(0), _initialized(false) {}

GLRenderer::~GLRenderer() {
    cleanup();
}

void GLRenderer::initialize() {
    if (_initialized) return;
    
    load_gl_extensions();
    
    // Geometry
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
    _initialized = true;
}

void GLRenderer::set_ocio_shader(const std::string& ocio_shader_code) {
    if (!_initialized) initialize();
    _compile_shader_program(ocio_shader_code);
}

void GLRenderer::upload_ocio_lut_3d(int index, int size, const float* data) {
    if (!_initialized) initialize();
    
    // Create new slot if needed
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

void GLRenderer::_upload_texture(GLuint& tex_id, int w, int h, int channels, const float* data) {
    if (tex_id == 0) {
        glGenTextures(1, &tex_id);
    }
    
    glBindTexture(GL_TEXTURE_2D, tex_id);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    
    GLenum gl_format = GL_RGB;
    GLenum internal_format = GL_RGB32F;
    
    if (channels == 4) {
        gl_format = GL_RGBA;
        internal_format = GL_RGBA32F;
    } else if (channels == 1) {
        gl_format = GL_RED;
        internal_format = GL_R32F;
    }
    
    glTexImage2D(GL_TEXTURE_2D, 0, internal_format, w, h, 0, gl_format, GL_FLOAT, data);
    glBindTexture(GL_TEXTURE_2D, 0);
}

void GLRenderer::render(
    int width_a, int height_a, int channels_a, const float* data_a,
    int width_b, int height_b, int channels_b, const float* data_b,
    int compare_mode, float wipe_pos, int channel_mask,
    float scale_x, float scale_y, float pan_x, float pan_y
) {
    if (!_initialized || _shader_program == 0) return;
    
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
    
    glUseProgram(_shader_program);
    
    // Upload Input A
    if (data_a != nullptr) {
        _upload_texture(_tex_a, width_a, height_a, channels_a, data_a);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, _tex_a);
        glUniform1i(glGetUniformLocation(_shader_program, "texA"), 0);
    }
    
    // Upload Input B
    if (data_b != nullptr) {
        _upload_texture(_tex_b, width_b, height_b, channels_b, data_b);
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_2D, _tex_b);
        glUniform1i(glGetUniformLocation(_shader_program, "samplerB"), 1);
    } else {
        glActiveTexture(GL_TEXTURE1);
        glBindTexture(GL_TEXTURE_2D, 0);
    }
    
    // Bind OCIO LUTs
    int tex_unit_start = 2;
    for (size_t i = 0; i < _ocio_luts_3d.size(); ++i) {
        if (_ocio_luts_3d[i] != 0) {
            glActiveTexture(GL_TEXTURE0 + tex_unit_start);
            glBindTexture(GL_TEXTURE_3D, _ocio_luts_3d[i]);
            
            // Map uniform to slot
            std::string lut_sampler_name = "ocio_lut3d_" + std::to_string(i);
            GLint loc = glGetUniformLocation(_shader_program, lut_sampler_name.c_str());
            if (loc != -1) {
                glUniform1i(loc, tex_unit_start);
            }
            tex_unit_start++;
        }
    }
    
    // Uniforms
    glUniform1i(glGetUniformLocation(_shader_program, "compareMode"), compare_mode);
    glUniform1f(glGetUniformLocation(_shader_program, "wipePos"), wipe_pos);
    glUniform1i(glGetUniformLocation(_shader_program, "channelMask"), channel_mask);
    glUniform2f(glGetUniformLocation(_shader_program, "scale"), scale_x, scale_y);
    glUniform2f(glGetUniformLocation(_shader_program, "offset"), pan_x, pan_y);
    
    // Render quad
    glBindVertexArray(_quad_vao);
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glBindVertexArray(0);
    glUseProgram(0);
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
        
        // OCIO compiled transformations injected here
        )" + ocio_code + R"(
        
        void main() {
            vec4 colorA = texture(texA, uv);
            vec4 colorB = texture(samplerB, uv);
            
            vec4 finalColor = colorA;
            
            if (compareMode == 1) { // Split vertical wipe slider
                if (uv.x > wipePos) {
                    finalColor = colorB;
                }
            } else if (compareMode == 2) { // Difference
                finalColor = vec4(abs(colorA.rgb - colorB.rgb), max(colorA.a, colorB.a));
            } else if (compareMode == 3) { // Tiling side-by-side
                if (uv.x < 0.5) {
                    finalColor = texture(texA, vec2(uv.x * 2.0, uv.y));
                } else {
                    finalColor = texture(samplerB, vec2((uv.x - 0.5) * 2.0, uv.y));
                }
            }
            
            // Mask channels
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
    
    // Shader compile logic
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
    glLinkProgram(_shader_program);
    
    glDeleteShader(vs);
    glDeleteShader(fs);
}

void GLRenderer::cleanup() {
    if (_shader_program != 0) {
        glDeleteProgram(_shader_program);
        _shader_program = 0;
    }
    if (_tex_a != 0) {
        glDeleteTextures(1, &_tex_a);
        _tex_a = 0;
    }
    if (_tex_b != 0) {
        glDeleteTextures(1, &_tex_b);
        _tex_b = 0;
    }
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
}
