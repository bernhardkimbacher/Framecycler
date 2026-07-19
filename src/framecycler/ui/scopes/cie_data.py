"""Compact CIE 1931 spectral locus + gamut triangles for scope overlay."""

from __future__ import annotations

# Approximate CIE 1931 xy spectral locus (selected wavelengths), closed via purple line.
# Compact polyline — sufficient for review overlay, not a full CIE table.
CIE_LOCUS_XY: list[tuple[float, float]] = [
    (0.1756, 0.0053),  # 380
    (0.1440, 0.0297),
    (0.1241, 0.0578),
    (0.1096, 0.0868),
    (0.0913, 0.1327),
    (0.0454, 0.2950),
    (0.0082, 0.5384),
    (0.0139, 0.7502),
    (0.0389, 0.8120),
    (0.0743, 0.8338),
    (0.1142, 0.8262),
    (0.1547, 0.8059),
    (0.2296, 0.7543),
    (0.3004, 0.6923),  # 560
    (0.3731, 0.6245),
    (0.4441, 0.5547),
    (0.5125, 0.4866),
    (0.5752, 0.4242),
    (0.6270, 0.3725),
    (0.6915, 0.3083),  # 640
    (0.7230, 0.2769),
    (0.7347, 0.2653),
    (0.7341, 0.2650),  # 700+
    (0.1756, 0.0053),  # close purple line
]

# Display-referred gamut triangles (D65 white).
REC709_PRIMARIES_XY: list[tuple[float, float]] = [
    (0.6400, 0.3300),  # R
    (0.3000, 0.6000),  # G
    (0.1500, 0.0600),  # B
]
P3_D65_PRIMARIES_XY: list[tuple[float, float]] = [
    (0.6800, 0.3200),
    (0.2650, 0.6900),
    (0.1500, 0.0600),
]
D65_WHITE_XY: tuple[float, float] = (0.3127, 0.3290)

# Rec.709 RGB → XYZ (D65, linear).
REC709_TO_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
