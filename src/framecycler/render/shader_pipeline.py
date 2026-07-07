from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

SAMPLER_DECL_RE = re.compile(
    r"^\s*uniform\s+sampler(?P<dim>2D|3D)\s+(?P<name>[A-Za-z0-9_]+)\s*;",
    re.MULTILINE,
)

DYNAMIC_UNIFORM_RE = re.compile(
    r"^\s*uniform\s+(?:bool|float|vec[234])\s+(?P<name>[A-Za-z0-9_]+)\s*;",
    re.MULTILINE,
)

SHADER_DIR = Path(__file__).resolve().parent / "shaders"
OCIO_SAMPLER_START_BINDING = 3


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
    return DYNAMIC_UNIFORM_RE.findall(ocio_glsl)


def wrap_ocio_dynamic_uniforms(
    ocio_glsl: str,
    ubo_binding: int,
) -> str:
    """Pack loose OCIO scalar/vector uniforms into a UBO for Vulkan/SPIR-V baking."""
    matches = list(DYNAMIC_UNIFORM_RE.finditer(ocio_glsl))
    if not matches:
        return ocio_glsl

    members: list[str] = []
    for match in matches:
        decl = match.group(0).strip()
        members.append(decl[len("uniform ") :])

    block = "\n".join(
        [
            f"layout(std140, binding = {ubo_binding}) uniform OcioDynamicUbo {{",
            *(f"    {member}" for member in members),
            "};",
        ]
    )

    stripped = DYNAMIC_UNIFORM_RE.sub("", ocio_glsl)
    marker = "// Declaration of all variables"
    if marker in stripped:
        return stripped.replace(marker, f"{marker}\n\n{block}", 1)

    return f"{block}\n\n{stripped}"


def inject_ocio_into_fragment(ocio_glsl: str, start_binding: int = OCIO_SAMPLER_START_BINDING) -> tuple[str, list[tuple[int, str, str]], list[str]]:
    annotated, bindings = annotate_ocio_samplers(ocio_glsl, start_binding=start_binding)
    dynamic_uniforms = extract_dynamic_uniforms(annotated)
    next_binding = max((binding for binding, _, _ in bindings), default=start_binding - 1) + 1
    annotated = wrap_ocio_dynamic_uniforms(annotated, ubo_binding=next_binding)
    template = load_shader_template("quad.frag.glsl")
    marker = "// OCIO-generated declarations and ocio_color_transform() are injected here."
    if marker not in template:
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
