#version 450
layout(location = 0) in vec2 vUV;
layout(location = 0) out vec4 fragColor;

layout(binding = 1) uniform sampler2D texA;
layout(binding = 2) uniform sampler2D texB;

layout(std140, binding = 0) uniform PerFrameUbo {
    vec2 scale;
    vec2 offset;
    int compareMode;
    float wipePos;
    int channelMask;
} uFrame;

// OCIO-generated declarations and ocio_color_transform() are injected here.

void main() {
    vec4 colorA = texture(texA, vUV);
    vec4 colorB = texture(texB, vUV);
    vec4 finalColor = colorA;

    if (uFrame.compareMode == 1) {
        if (vUV.x > uFrame.wipePos) {
            finalColor = colorB;
        }
    } else if (uFrame.compareMode == 2) {
        finalColor = vec4(abs(colorA.rgb - colorB.rgb), max(colorA.a, colorB.a));
    } else if (uFrame.compareMode == 3) {
        if (vUV.x < 0.5) {
            finalColor = texture(texA, vec2(vUV.x * 2.0, vUV.y));
        } else {
            finalColor = texture(texB, vec2((vUV.x - 0.5) * 2.0, vUV.y));
        }
    }

    if (uFrame.channelMask == 1) {
        finalColor = vec4(finalColor.r, finalColor.r, finalColor.r, 1.0);
    } else if (uFrame.channelMask == 2) {
        finalColor = vec4(finalColor.g, finalColor.g, finalColor.g, 1.0);
    } else if (uFrame.channelMask == 3) {
        finalColor = vec4(finalColor.b, finalColor.b, finalColor.b, 1.0);
    } else if (uFrame.channelMask == 4) {
        finalColor = vec4(finalColor.a, finalColor.a, finalColor.a, 1.0);
    } else if (uFrame.channelMask == 5) {
        float lum = dot(finalColor.rgb, vec3(0.2126, 0.7152, 0.0722));
        finalColor = vec4(lum, lum, lum, 1.0);
    }

    fragColor = ocio_color_transform(finalColor);
}
