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

// OCIO-generated declarations and ocio_to_working()/ocio_to_display() are injected here.
// ASC CDL uniforms (fc_cdl_*) live in OcioDynamicUbo when present.

vec4 fc_asc_cdl(vec4 inColor) {
    // Identity CDL: skip entirely so a zeroed sat uniform cannot desaturate RGB.
    if (fc_cdl_enable < 0.5) {
        return inColor;
    }
    // ASC CDL: out = sat(power(slope * in + offset))
    vec3 slope = fc_cdl_slope;
    vec3 cdlOffset = fc_cdl_offset;
    vec3 power = fc_cdl_power;
    float sat = fc_cdl_saturation;

    vec3 c = inColor.rgb * slope + cdlOffset;
    c = sign(c) * pow(abs(c) + 1e-10, power);
    float luma = dot(c, vec3(0.2126, 0.7152, 0.0722));
    c = mix(vec3(luma), c, sat);
    return vec4(c, inColor.a);
}

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
    } else if (uFrame.compareMode == 4) {
        // Blend: active/compare mix; wipePos is the blend amount (0=A, 1=B).
        finalColor = mix(colorA, colorB, clamp(uFrame.wipePos, 0.0, 1.0));
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

    // Order: input→working+grading (OCIO) → ASC CDL → look+display (OCIO)
    fragColor = ocio_to_display(fc_asc_cdl(ocio_to_working(finalColor)));
}
