#include <pybind11/pybind11.h>

#include <rhi/qshader.h>
#include <rhi/qshaderbaker.h>
#include <rhi/qrhi.h>

#include <QList>
#include <QPair>
#include <QString>

#if defined(Q_OS_MACOS)
#include <dlfcn.h>
#endif

#include <cstdint>
#include <string>

namespace py = pybind11;

namespace {

std::string backend_name(QRhi::Implementation backend)
{
    switch (backend) {
    case QRhi::Null:
        return "Null";
    case QRhi::Vulkan:
        return "Vulkan";
    case QRhi::OpenGLES2:
        return "OpenGLES2";
    case QRhi::Metal:
        return "Metal";
    case QRhi::D3D11:
        return "D3D11";
    case QRhi::D3D12:
        return "D3D12";
    default:
        return "Unknown";
    }
}

std::string linked_qt_gui_path()
{
#if defined(Q_OS_MACOS) || defined(Q_OS_LINUX)
    Dl_info info{};
    // Any exported QtGui symbol is sufficient to locate the loaded library.
    void* symbol = reinterpret_cast<void*>(&QShader::fromSerialized);
    if (dladdr(symbol, &info) && info.dli_fname) {
        return info.dli_fname;
    }
#endif
    return {};
}

std::string qt_runtime_version()
{
    return QString::fromLatin1(QT_VERSION_STR).toStdString();
}

std::string probe_shader_baker()
{
    QShaderBaker baker;
    baker.setGeneratedShaderVariants({QShader::StandardShader});
    baker.setGeneratedShaders({
        {QShader::SpirvShader, QShaderVersion(100)},
        {QShader::GlslShader, QShaderVersion(450)},
        {QShader::MslShader, QShaderVersion(20)},
        {QShader::HlslShader, QShaderVersion(50)},
    });
    baker.setSourceString(
        R"(#version 450
layout(location = 0) out vec4 fragColor;
void main() {
    fragColor = vec4(1.0, 0.5, 0.25, 1.0);
})",
        QShader::FragmentStage,
        QStringLiteral("spike.frag"));

    const QShader shader = baker.bake();
    if (!shader.isValid()) {
        const QString err = baker.errorMessage();
        if (err.isEmpty()) {
            return "invalid";
        }
        return std::string("invalid: ") + err.toStdString();
    }
    return std::to_string(shader.availableShaders().size()) + " variants";
}

std::string probe_qrhi_from_address(std::uintptr_t address)
{
    auto* rhi = reinterpret_cast<QRhi*>(address);
    if (rhi == nullptr) {
        return "null";
    }
    return backend_name(rhi->backend());
}

bool qrhi_pointers_match(std::uintptr_t left, std::uintptr_t right)
{
    return left != 0 && left == right;
}

} // namespace

PYBIND11_MODULE(rhi_spike_b, m)
{
    m.doc() = "Phase 0 Spike B: Qt RHI / ShaderTools linkage probe";

    m.def("qt_runtime_version", &qt_runtime_version,
          "Return the Qt version compiled into the extension.");
    m.def("linked_qt_gui_path", &linked_qt_gui_path,
          "Return the filesystem path of the loaded QtGui library (dladdr probe).");
    m.def("probe_shader_baker", &probe_shader_baker,
          "Bake a trivial fragment shader via in-process QShaderBaker.");
    m.def("probe_qrhi_from_address", &probe_qrhi_from_address,
          py::arg("address"),
          "Interpret a QRhi* address (from shiboken6.getCppPointer) and return backend name.");
    m.def("qrhi_pointers_match", &qrhi_pointers_match,
          py::arg("left"), py::arg("right"),
          "Return true when two QRhi pointer addresses refer to the same object.");
}
