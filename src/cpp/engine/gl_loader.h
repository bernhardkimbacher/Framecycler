#pragma once

#ifdef _WIN32
    #include <windows.h>
    #include <GL/gl.h>
    #include <stddef.h>
    
    // Define modern OpenGL types not present in legacy Windows gl.h
    typedef char GLchar;
    typedef ptrdiff_t GLsizeiptr;
    typedef ptrdiff_t GLintptr;
    
    // Modern OpenGL constants
    #define GL_ARRAY_BUFFER 0x8892
    #define GL_STATIC_DRAW 0x88E4
    #define GL_TEXTURE_3D 0x806F
    #define GL_CLAMP_TO_EDGE 0x812F
    #define GL_TEXTURE_WRAP_R 0x8072
    #define GL_RGB32F 0x8815
    #define GL_RGBA32F 0x8814
    #define GL_R32F 0x822E
    #define GL_R16F 0x822D
    #define GL_RGB16F 0x881B
    #define GL_RGBA16F 0x881A
    #define GL_HALF_FLOAT 0x140B
    #define GL_PIXEL_UNPACK_BUFFER 0x88EC
    #define GL_STREAM_DRAW 0x88E0
    #define GL_TEXTURE0 0x84C0
    #define GL_TEXTURE1 0x84C1
    #define GL_VERTEX_SHADER 0x8B31
    #define GL_FRAGMENT_SHADER 0x8B30
    #define GL_COMPILE_STATUS 0x8B81
    #define GL_FRAMEBUFFER_SRGB 0x8DB9
    
    // OpenGL extension function typedefs
    typedef void (APIENTRY * PFNGLGENVERTEXARRAYSPROC) (GLsizei n, GLuint *arrays);
    typedef void (APIENTRY * PFNGLBINDVERTEXARRAYPROC) (GLuint array);
    typedef void (APIENTRY * PFNGLGENBUFFERSPROC) (GLsizei n, GLuint *buffers);
    typedef void (APIENTRY * PFNGLBINDBUFFERPROC) (GLenum target, GLuint buffer);
    typedef void (APIENTRY * PFNGLBUFFERDATAPROC) (GLenum target, GLsizeiptr size, const void *data, GLenum usage);
    typedef void (APIENTRY * PFNGLENABLEVERTEXATTRIBARRAYPROC) (GLuint index);
    typedef void (APIENTRY * PFNGLBINDATTRIBLOCATIONPROC) (GLuint program, GLuint index, const GLchar *name);
    typedef void (APIENTRY * PFNGLVERTEXATTRIBPOINTERPROC) (GLuint index, GLint size, GLenum type, GLboolean normalized, GLsizei stride, const void *pointer);
    typedef void (APIENTRY * PFNGLDELETEVERTEXARRAYSPROC) (GLsizei n, const GLuint *arrays);
    typedef void (APIENTRY * PFNGLDELETEBUFFERSPROC) (GLsizei n, const GLuint *buffers);
    
    typedef GLuint (APIENTRY * PFNGLCREATESHADERPROC) (GLenum type);
    typedef void (APIENTRY * PFNGLSHADERSOURCEPROC) (GLuint shader, GLsizei count, const GLchar *const*string, const GLint *length);
    typedef void (APIENTRY * PFNGLCOMPILESHADERPROC) (GLuint shader);
    typedef void (APIENTRY * PFNGLGETSHADERIVPROC) (GLuint shader, GLenum pname, GLint *params);
    typedef void (APIENTRY * PFNGLGETSHADERINFOLOGPROC) (GLuint shader, GLsizei bufSize, GLsizei *length, GLchar *infoLog);
    typedef GLuint (APIENTRY * PFNGLCREATEPROGRAMPROC) (void);
    typedef void (APIENTRY * PFNGLATTACHSHADERPROC) (GLuint program, GLuint shader);
    typedef void (APIENTRY * PFNGLLINKPROGRAMPROC) (GLuint program);
    typedef void (APIENTRY * PFNGLGETPROGRAMIVPROC) (GLuint program, GLenum pname, GLint *params);
    typedef void (APIENTRY * PFNGLGETPROGRAMINFOLOGPROC) (GLuint program, GLsizei bufSize, GLsizei *length, GLchar *infoLog);
    typedef void (APIENTRY * PFNGLUSEPROGRAMPROC) (GLuint program);
    typedef void (APIENTRY * PFNGLDELETESHADERPROC) (GLuint shader);
    typedef void (APIENTRY * PFNGLDELETEPROGRAMPROC) (GLuint program);
    
    typedef GLint (APIENTRY * PFNGLGETUNIFORMLOCATIONPROC) (GLuint program, const GLchar *name);
    typedef void (APIENTRY * PFNGLUNIFORM1IPROC) (GLint location, GLint v0);
    typedef void (APIENTRY * PFNGLUNIFORM1FPROC) (GLint location, GLfloat v0);
    typedef void (APIENTRY * PFNGLUNIFORM2FPROC) (GLint location, GLfloat v0, GLfloat v1);
    
    typedef void (APIENTRY * PFNGLACTIVETEXTUREPROC) (GLenum texture);
    typedef void (APIENTRY * PFNGLTEXIMAGE3DPROC) (GLenum target, GLint level, GLint internalformat, GLsizei width, GLsizei height, GLsizei depth, GLint border, GLenum format, GLenum type, const void *pixels);

    // Global function pointers
    inline PFNGLGENVERTEXARRAYSPROC glGenVertexArrays = nullptr;
    inline PFNGLBINDVERTEXARRAYPROC glBindVertexArray = nullptr;
    inline PFNGLGENBUFFERSPROC glGenBuffers = nullptr;
    inline PFNGLBINDBUFFERPROC glBindBuffer = nullptr;
    inline PFNGLBUFFERDATAPROC glBufferData = nullptr;
    inline PFNGLENABLEVERTEXATTRIBARRAYPROC glEnableVertexAttribArray = nullptr;
    inline PFNGLBINDATTRIBLOCATIONPROC glBindAttribLocation = nullptr;
    inline PFNGLVERTEXATTRIBPOINTERPROC glVertexAttribPointer = nullptr;
    inline PFNGLDELETEVERTEXARRAYSPROC glDeleteVertexArrays = nullptr;
    inline PFNGLDELETEBUFFERSPROC glDeleteBuffers = nullptr;
    
    inline PFNGLCREATESHADERPROC glCreateShader = nullptr;
    inline PFNGLSHADERSOURCEPROC glShaderSource = nullptr;
    inline PFNGLCOMPILESHADERPROC glCompileShader = nullptr;
    inline PFNGLGETSHADERIVPROC glGetShaderiv = nullptr;
    inline PFNGLGETSHADERINFOLOGPROC glGetShaderInfoLog = nullptr;
    inline PFNGLCREATEPROGRAMPROC glCreateProgram = nullptr;
    inline PFNGLATTACHSHADERPROC glAttachShader = nullptr;
    inline PFNGLLINKPROGRAMPROC glLinkProgram = nullptr;
    inline PFNGLGETPROGRAMIVPROC glGetProgramiv = nullptr;
    inline PFNGLGETPROGRAMINFOLOGPROC glGetProgramInfoLog = nullptr;
    inline PFNGLUSEPROGRAMPROC glUseProgram = nullptr;
    inline PFNGLDELETESHADERPROC glDeleteShader = nullptr;
    inline PFNGLDELETEPROGRAMPROC glDeleteProgram = nullptr;
    
    inline PFNGLGETUNIFORMLOCATIONPROC glGetUniformLocation = nullptr;
    inline PFNGLUNIFORM1IPROC glUniform1i = nullptr;
    inline PFNGLUNIFORM1FPROC glUniform1f = nullptr;
    inline PFNGLUNIFORM2FPROC glUniform2f = nullptr;
    
    inline PFNGLACTIVETEXTUREPROC glActiveTexture = nullptr;
    inline PFNGLTEXIMAGE3DPROC glTexImage3D = nullptr;

    inline void load_gl_extensions() {
        if (glGenVertexArrays != nullptr) return; // Already loaded
        
        glGenVertexArrays = (PFNGLGENVERTEXARRAYSPROC)wglGetProcAddress("glGenVertexArrays");
        glBindVertexArray = (PFNGLBINDVERTEXARRAYPROC)wglGetProcAddress("glBindVertexArray");
        glGenBuffers = (PFNGLGENBUFFERSPROC)wglGetProcAddress("glGenBuffers");
        glBindBuffer = (PFNGLBINDBUFFERPROC)wglGetProcAddress("glBindBuffer");
        glBufferData = (PFNGLBUFFERDATAPROC)wglGetProcAddress("glBufferData");
        glEnableVertexAttribArray = (PFNGLENABLEVERTEXATTRIBARRAYPROC)wglGetProcAddress("glEnableVertexAttribArray");
        glBindAttribLocation = (PFNGLBINDATTRIBLOCATIONPROC)wglGetProcAddress("glBindAttribLocation");
        glVertexAttribPointer = (PFNGLVERTEXATTRIBPOINTERPROC)wglGetProcAddress("glVertexAttribPointer");
        glDeleteVertexArrays = (PFNGLDELETEVERTEXARRAYSPROC)wglGetProcAddress("glDeleteVertexArrays");
        glDeleteBuffers = (PFNGLDELETEBUFFERSPROC)wglGetProcAddress("glDeleteBuffers");
        
        glCreateShader = (PFNGLCREATESHADERPROC)wglGetProcAddress("glCreateShader");
        glShaderSource = (PFNGLSHADERSOURCEPROC)wglGetProcAddress("glShaderSource");
        glCompileShader = (PFNGLCOMPILESHADERPROC)wglGetProcAddress("glCompileShader");
        glGetShaderiv = (PFNGLGETSHADERIVPROC)wglGetProcAddress("glGetShaderiv");
        glGetShaderInfoLog = (PFNGLGETSHADERINFOLOGPROC)wglGetProcAddress("glGetShaderInfoLog");
        glCreateProgram = (PFNGLCREATEPROGRAMPROC)wglGetProcAddress("glCreateProgram");
        glAttachShader = (PFNGLATTACHSHADERPROC)wglGetProcAddress("glAttachShader");
        glLinkProgram = (PFNGLLINKPROGRAMPROC)wglGetProcAddress("glLinkProgram");
        glGetProgramiv = (PFNGLGETPROGRAMIVPROC)wglGetProcAddress("glGetProgramiv");
        glGetProgramInfoLog = (PFNGLGETPROGRAMINFOLOGPROC)wglGetProcAddress("glGetProgramInfoLog");
        glUseProgram = (PFNGLUSEPROGRAMPROC)wglGetProcAddress("glUseProgram");
        glDeleteShader = (PFNGLDELETESHADERPROC)wglGetProcAddress("glDeleteShader");
        glDeleteProgram = (PFNGLDELETEPROGRAMPROC)wglGetProcAddress("glDeleteProgram");
        
        glGetUniformLocation = (PFNGLGETUNIFORMLOCATIONPROC)wglGetProcAddress("glGetUniformLocation");
        glUniform1i = (PFNGLUNIFORM1IPROC)wglGetProcAddress("glUniform1i");
        glUniform1f = (PFNGLUNIFORM1FPROC)wglGetProcAddress("glUniform1f");
        glUniform2f = (PFNGLUNIFORM2FPROC)wglGetProcAddress("glUniform2f");
        
        glActiveTexture = (PFNGLACTIVETEXTUREPROC)wglGetProcAddress("glActiveTexture");
        glTexImage3D = (PFNGLTEXIMAGE3DPROC)wglGetProcAddress("glTexImage3D");
        
        if (!glGetProgramiv) glGetProgramiv = (PFNGLGETPROGRAMIVPROC)wglGetProcAddress("glGetProgramiv");
    }
#else
    #define GL_GLEXT_PROTOTYPES
    #ifdef __APPLE__
        #include <OpenGL/gl3.h>
    #else
        #include <GL/gl.h>
        #include <GL/glext.h>
    #endif
    inline void load_gl_extensions() {
        // POSIX compilers link extensions statically or fetch them automatically
    }
#endif
