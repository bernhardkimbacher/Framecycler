#version 450
layout(location = 0) in vec2 position;
layout(location = 1) in vec2 texCoord;
layout(location = 0) out vec2 vUV;

layout(std140, binding = 0) uniform PerFrameUbo {
    vec2 scale;
    vec2 offset;
    int compareMode;
    float wipePos;
    int channelMask;
    int falseColorMode;
    float zebraLo;
    float zebraHi;
    float _pad0;
    float _pad1;
} uFrame;

void main() {
    vUV = vec2(texCoord.x, 1.0 - texCoord.y);
    gl_Position = vec4(position * uFrame.scale + uFrame.offset, 0.0, 1.0);
}
