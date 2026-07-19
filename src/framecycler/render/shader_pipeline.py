from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path

SAMPLER_DECL_RE = re.compile(
    r"^\s*uniform\s+sampler(?P<dim>2D|3D)\s+(?P<name>[A-Za-z0-9_]+)\s*;",
    re.MULTILINE,
)

DYNAMIC_UNIFORM_RE = re.compile(
    r"^\s*uniform\s+(?P<type>bool|float|vec[234])\s+(?P<name>[A-Za-z0-9_]+)\s*;",
    re.MULTILINE,
)

SHADER_DIR = Path(__file__).resolve().parent / "shaders"
OCIO_SAMPLER_START_BINDING = 3

# Scalar types before vectors — avoids Metal packed_float3 vs std140 mismatch
# when a vec3 is followed by floats (SPIRV-Cross packs the trailing vec3).
_SCALAR_UBO_TYPES = frozenset({"bool", "float", "int"})
_VECTOR_UBO_TYPES = frozenset({"vec2", "vec3", "vec4"})


@dataclass(frozen=True)
class OcioUniformMember:
    offset: int
    size: int
    is_vec3: bool


@dataclass(frozen=True)
class OcioUniformLayout:
    binding: int
    size: int
    members: dict[str, OcioUniformMember]


@dataclass(frozen=True)
class RhiShaderBundle:
    pipeline_key: str
    vertex_source: str
    fragment_source: str
    ocio_function_source: str
    sampler_bindings: list[tuple[int, str, str]]
    dynamic_uniforms: list[str]
    textures_3d: list[dict]
    textures_1d: list[dict]


OCIO_UBO_BLOCK_RE = re.compile(
    r"layout\s*\(\s*std140\s*,\s*binding\s*=\s*(?P<binding>\d+)\s*\)\s*uniform\s+OcioDynamicUbo\s*\{(?P<body>[^}]*)\}",
    re.DOTALL,
)
UBO_MEMBER_RE = re.compile(
    r"^\s*(?P<type>float|vec2|vec3|vec4|int|bool)\s+(?P<name>[A-Za-z0-9_]+)\s*;",
    re.MULTILINE,
)


def load_shader_template(name: str) -> str:
    path = SHADER_DIR / name
    return path.read_text(encoding="utf-8")


def annotate_ocio_samplers(
    ocio_glsl: str,
    start_binding: int = OCIO_SAMPLER_START_BINDING,
) -> tuple[str, list[tuple[int, str, str]]]:
    """Add layout(binding=N) to OCIO sampler declarations for Vulkan-GLSL."""
    bindings: list[tuple[int, str, str]] = []
    next_binding = start_binding

    def repl(match: re.Match[str]) -> str:
        nonlocal next_binding
        dim = match.group("dim")
        name = match.group("name")
        binding = next_binding
        next_binding += 1
        bindings.append((binding, dim, name))
        return f"layout(binding = {binding}) uniform sampler{dim} {name};"

    processed = SAMPLER_DECL_RE.sub(repl, ocio_glsl)
    return processed, bindings


def extract_dynamic_uniforms(ocio_glsl: str) -> list[str]:
    return [m.group("name") for m in DYNAMIC_UNIFORM_RE.finditer(ocio_glsl)]


def _member_sort_key(member_decl: str) -> tuple[int, str]:
    """Sort key: scalars first, then vectors (Metal packed_float3 safety)."""
    type_name = member_decl.split()[0]
    if type_name in _SCALAR_UBO_TYPES:
        return (0, member_decl)
    if type_name in _VECTOR_UBO_TYPES:
        return (1, member_decl)
    return (2, member_decl)


def sort_ubo_members_metal_safe(members: list[str]) -> list[str]:
    """Reorder UBO decls so no vec3 is followed by a scalar (packed_float3 hazard)."""
    return sorted(members, key=_member_sort_key)


def ubo_has_vec3_before_scalar_hazard(fragment_source: str) -> bool:
    """True if OcioDynamicUbo has a vec3/vec4 followed later by float/int/bool."""
    match = OCIO_UBO_BLOCK_RE.search(fragment_source)
    if not match:
        return False
    members = [
        (m.group("type"), m.group("name"))
        for m in UBO_MEMBER_RE.finditer(match.group("body"))
    ]
    saw_vector = False
    for type_name, _name in members:
        if type_name in _VECTOR_UBO_TYPES:
            saw_vector = True
        elif saw_vector and type_name in _SCALAR_UBO_TYPES:
            return True
    return False


def wrap_ocio_dynamic_uniforms(
    ocio_glsl: str,
    ubo_binding: int,
    extra_members: list[str] | None = None,
) -> str:
    """Pack loose OCIO scalar/vector uniforms into a UBO for Vulkan/SPIR-V baking.

    Members are ordered scalars-then-vectors so Metal/SPIRV-Cross does not emit
    ``packed_float3`` before trailing floats (breaks std140 CPU packing).
    """
    matches = list(DYNAMIC_UNIFORM_RE.finditer(ocio_glsl))
    members: list[str] = []
    for match in matches:
        decl = match.group(0).strip()
        members.append(decl[len("uniform ") :])
    if extra_members:
        members.extend(extra_members)

    if not members:
        return ocio_glsl

    # Deduplicate by member name, preserving first declaration's type.
    by_name: dict[str, str] = {}
    for member in members:
        name = member.rstrip(";").split()[-1]
        by_name.setdefault(name, member if member.endswith(";") else f"{member};")

    ordered = sort_ubo_members_metal_safe(list(by_name.values()))

    block = "\n".join(
        [
            f"layout(std140, binding = {ubo_binding}) uniform OcioDynamicUbo {{",
            *(f"    {member}" for member in ordered),
            "};",
        ]
    )

    stripped = DYNAMIC_UNIFORM_RE.sub("", ocio_glsl)
    marker = "// Declaration of all variables"
    if marker in stripped:
        return stripped.replace(marker, f"{marker}\n\n{block}", 1)

    return f"{block}\n\n{stripped}"


# ASC CDL uniforms always present so quad.frag.glsl can reference them.
# Floats listed before vec3s (wrap also re-sorts for Metal safety).
ASC_CDL_UBO_MEMBERS = [
    "float fc_cdl_saturation;",
    "float fc_cdl_enable;",
    "vec3 fc_cdl_slope;",
    "vec3 fc_cdl_offset;",
    "vec3 fc_cdl_power;",
]


def inject_ocio_into_fragment(
    ocio_glsl: str, start_binding: int = OCIO_SAMPLER_START_BINDING
) -> tuple[str, list[tuple[int, str, str]], list[str]]:
    annotated, bindings = annotate_ocio_samplers(ocio_glsl, start_binding=start_binding)
    dynamic_uniforms = list(
        dict.fromkeys(
            extract_dynamic_uniforms(annotated)
            + [
                "fc_cdl_saturation",
                "fc_cdl_enable",
                "fc_cdl_slope",
                "fc_cdl_offset",
                "fc_cdl_power",
            ]
        )
    )
    # Contiguous after LUT samplers (3 + N). Exact lut_count shrink avoids collisions.
    next_binding = start_binding + len(bindings)
    annotated = wrap_ocio_dynamic_uniforms(
        annotated, ubo_binding=next_binding, extra_members=ASC_CDL_UBO_MEMBERS
    )
    template = load_shader_template("quad.frag.glsl")
    marker = (
        "// OCIO-generated declarations and ocio_to_working()/ocio_to_display() "
        "are injected here."
    )
    if marker not in template:
        # Backward-compatible marker from older templates
        legacy = "// OCIO-generated declarations and ocio_color_transform() are injected here."
        if legacy in template:
            marker = legacy
        else:
            raise ValueError("quad.frag.glsl is missing OCIO injection marker")
    fragment_source = template.replace(marker, annotated.strip())
    return fragment_source, bindings, dynamic_uniforms


def build_rhi_shader_bundle(
    ocio_function_source: str,
    textures_3d: list[dict],
    textures_1d: list[dict],
    pipeline_key: str,
) -> RhiShaderBundle:
    fragment_source, sampler_bindings, dynamic_uniforms = inject_ocio_into_fragment(
        ocio_function_source
    )
    return RhiShaderBundle(
        pipeline_key=pipeline_key,
        vertex_source=load_shader_template("quad.vert.glsl"),
        fragment_source=fragment_source,
        ocio_function_source=ocio_function_source,
        sampler_bindings=sampler_bindings,
        dynamic_uniforms=dynamic_uniforms,
        textures_3d=textures_3d,
        textures_1d=textures_1d,
    )


def hash_pipeline_state(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def compute_std140_member_layout(
    members: list[tuple[str, str]],
) -> tuple[int, dict[str, OcioUniformMember]]:
    """Compute std140 offsets for UBO members in declaration order."""
    offset = 0
    layout: dict[str, OcioUniformMember] = {}
    for type_name, name in members:
        if type_name in {"float", "int", "bool"}:
            align, size, is_vec3 = 4, 4, False
        elif type_name == "vec2":
            align, size, is_vec3 = 8, 8, False
        elif type_name == "vec3":
            align, size, is_vec3 = 16, 16, True
        elif type_name == "vec4":
            align, size, is_vec3 = 16, 16, False
        else:
            continue

        if offset % align:
            offset += align - (offset % align)
        layout[name] = OcioUniformMember(offset=offset, size=size, is_vec3=is_vec3)
        offset += size

    if offset <= 0:
        return 0, layout
    total_size = (offset + 15) // 16 * 16
    return total_size, layout


def parse_ocio_ubo_layout(fragment_source: str) -> OcioUniformLayout | None:
    match = OCIO_UBO_BLOCK_RE.search(fragment_source)
    if not match:
        return None

    binding = int(match.group("binding"))
    body = match.group("body")
    members = [
        (member_match.group("type"), member_match.group("name"))
        for member_match in UBO_MEMBER_RE.finditer(body)
    ]
    size, member_layout = compute_std140_member_layout(members)
    if size <= 0:
        return None
    return OcioUniformLayout(binding=binding, size=size, members=member_layout)


# Identity seeds matching C++ pack_ocio_ubo / Python _fill_ocio_ubo.
OCIO_UBO_IDENTITY_FLOATS: dict[str, float] = {
    "ocio_exposure_contrast_exposureVal": 0.0,
    "ocio_exposure_contrast_gammaVal": 1.0,
    "ocio_grading_primary_saturation": 1.0,
    "ocio_grading_primary_localBypass": 0.0,
    "fc_cdl_saturation": 1.0,
    "fc_cdl_enable": 0.0,
}
OCIO_UBO_IDENTITY_VEC3S: dict[str, tuple[float, float, float]] = {
    "ocio_grading_primary_brightness": (0.0, 0.0, 0.0),
    "ocio_grading_primary_contrast": (1.0, 1.0, 1.0),
    "ocio_grading_primary_gamma": (1.0, 1.0, 1.0),
    "fc_cdl_slope": (1.0, 1.0, 1.0),
    "fc_cdl_offset": (0.0, 0.0, 0.0),
    "fc_cdl_power": (1.0, 1.0, 1.0),
}


def pack_ocio_ubo_bytes(
    layout: OcioUniformLayout,
    floats: dict[str, float] | None = None,
    vec3s: dict[str, tuple[float, float, float]] | None = None,
    *,
    seed_identity: bool = True,
) -> bytes:
    """Pack an OCIO dynamic UBO; optionally seed identity defaults first."""
    buf = bytearray(layout.size)

    def write_float(name: str, value: float) -> None:
        member = layout.members.get(name)
        if member is None or member.is_vec3:
            return
        struct.pack_into("<f", buf, member.offset, float(value))

    def write_vec3(name: str, xyz: tuple[float, float, float]) -> None:
        member = layout.members.get(name)
        if member is None or not member.is_vec3:
            return
        struct.pack_into("<fff", buf, member.offset, xyz[0], xyz[1], xyz[2])

    if seed_identity:
        for name, value in OCIO_UBO_IDENTITY_FLOATS.items():
            write_float(name, value)
        for name, value in OCIO_UBO_IDENTITY_VEC3S.items():
            write_vec3(name, value)

    if floats:
        for name, value in floats.items():
            write_float(name, value)
    if vec3s:
        for name, value in vec3s.items():
            write_vec3(name, value)
    return bytes(buf)
