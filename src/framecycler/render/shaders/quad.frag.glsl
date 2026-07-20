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
    int falseColorMode;
    float zebraLo;
    float zebraHi;
    float _pad0;
    float _pad1;
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

vec3 fc_heatmap_rgb(float luma) {
    // Display-referred exposure bands (classic review false-color palette).
    if (luma < 0.02) {
        return vec3(0.05, 0.05, 0.35);
    }
    if (luma < 0.10) {
        return mix(vec3(0.05, 0.05, 0.35), vec3(0.15, 0.35, 0.85), (luma - 0.02) / 0.08);
    }
    if (luma < 0.18) {
        return mix(vec3(0.15, 0.35, 0.85), vec3(0.15, 0.75, 0.75), (luma - 0.10) / 0.08);
    }
    if (luma < 0.45) {
        return mix(vec3(0.15, 0.75, 0.75), vec3(0.20, 0.85, 0.25), (luma - 0.18) / 0.27);
    }
    if (luma < 0.70) {
        return mix(vec3(0.20, 0.85, 0.25), vec3(0.95, 0.90, 0.20), (luma - 0.45) / 0.25);
    }
    if (luma < 0.90) {
        return mix(vec3(0.95, 0.90, 0.20), vec3(0.95, 0.45, 0.10), (luma - 0.70) / 0.20);
    }
    if (luma < 1.0) {
        return mix(vec3(0.95, 0.45, 0.10), vec3(0.95, 0.15, 0.15), (luma - 0.90) / 0.10);
    }
    return vec3(1.0, 0.55, 0.95);
}

vec4 fc_false_color(vec4 inColor) {
    if (uFrame.falseColorMode == 0) {
        return inColor;
    }
    float luma = dot(inColor.rgb, vec3(0.2126, 0.7152, 0.0722));
    if (uFrame.falseColorMode == 1) {
        return vec4(fc_heatmap_rgb(luma), inColor.a);
    }
    if (uFrame.falseColorMode == 2) {
        float lo = uFrame.zebraLo;
        float hi = uFrame.zebraHi;
        bool clip = (luma < lo) || (luma > hi);
        if (!clip) {
            return inColor;
        }
        // Static diagonal zebra stripes in clipped regions.
        float stripe = step(0.5, fract((vUV.x + vUV.y) * 28.0));
        vec3 zebra = mix(vec3(0.0), vec3(1.0), stripe);
        return vec4(zebra, inColor.a);
    }
    return inColor;
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

    // Order: input→working+grading (OCIO) → ASC CDL → look+display (OCIO) → false color
    fragColor = fc_false_color(ocio_to_display(fc_asc_cdl(ocio_to_working(finalColor))));
}
