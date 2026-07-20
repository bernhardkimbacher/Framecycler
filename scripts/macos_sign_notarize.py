#!/usr/bin/env python3
"""Deep-sign and notarize a Framecycler .app for Gatekeeper.

Skips (exit 0) when MACOS_CODESIGN_IDENTITY is unset so CI still produces
unsigned artifacts before Apple Developer credentials are configured.

Env:
  MACOS_CODESIGN_IDENTITY  Developer ID Application: … (required to sign)
  MACOS_ENTITLEMENTS       path to entitlements.plist (optional)
  APPLE_API_KEY            path to AuthKey_*.p8 (notarize)
  APPLE_API_KEY_ID         key id
  APPLE_API_ISSUER         issuer UUID
  APPLE_NOTARY_PROFILE     alternate: keychain profile for notarytool
  MACOS_SKIP_NOTARIZE      set to 1 to sign only
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check, text=True, capture_output=False)


def _sign_app(app: Path, identity: str, entitlements: Path | None) -> None:
    cmd = [
        "codesign",
        "--deep",
        "--force",
        "--options",
        "runtime",
        "--timestamp",
        "--sign",
        identity,
    ]
    if entitlements is not None and entitlements.is_file():
        cmd.extend(["--entitlements", str(entitlements)])
    cmd.append(str(app))
    _run(cmd)
    _run(["codesign", "--verify", "--deep", "--strict", str(app)])


def _notarize(app: Path) -> None:
    if os.environ.get("MACOS_SKIP_NOTARIZE") == "1":
        print("Skipping notarize (MACOS_SKIP_NOTARIZE=1)")
        return

    profile = os.environ.get("APPLE_NOTARY_PROFILE", "").strip()
    key = os.environ.get("APPLE_API_KEY", "").strip()
    key_id = os.environ.get("APPLE_API_KEY_ID", "").strip()
    issuer = os.environ.get("APPLE_API_ISSUER", "").strip()

    if not profile and not (key and key_id and issuer):
        print(
            "Skipping notarize: set APPLE_NOTARY_PROFILE or "
            "APPLE_API_KEY + APPLE_API_KEY_ID + APPLE_API_ISSUER"
        )
        return

    key_path: Path | None = None
    tmp_key: Path | None = None
    if key and not profile:
        candidate = Path(key)
        if candidate.is_file():
            key_path = candidate
        else:
            # Secret may be raw PEM / base64 contents rather than a path.
            tmp_dir = Path(tempfile.mkdtemp(prefix="fc_api_key_"))
            tmp_key = tmp_dir / "AuthKey.p8"
            raw = key
            if "BEGIN PRIVATE KEY" not in raw:
                import base64

                try:
                    raw = base64.b64decode(raw).decode("utf-8")
                except Exception:
                    pass
            tmp_key.write_text(raw, encoding="utf-8")
            key_path = tmp_key

    with tempfile.TemporaryDirectory(prefix="fc_notarize_") as tmp:
        zip_path = Path(tmp) / f"{app.stem}.zip"
        _run(
            [
                "ditto",
                "-c",
                "-k",
                "--keepParent",
                str(app),
                str(zip_path),
            ]
        )
        submit = [
            "xcrun",
            "notarytool",
            "submit",
            str(zip_path),
            "--wait",
        ]
        if profile:
            submit.extend(["--keychain-profile", profile])
        else:
            assert key_path is not None
            submit.extend(
                [
                    "--key",
                    str(key_path),
                    "--key-id",
                    key_id,
                    "--issuer",
                    issuer,
                ]
            )
        _run(submit)

    _run(["xcrun", "stapler", "staple", str(app)])
    _run(["xcrun", "stapler", "validate", str(app)])

    if tmp_key is not None:
        try:
            tmp_key.unlink(missing_ok=True)
            tmp_key.parent.rmdir()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "app",
        type=Path,
        help="Path to Framecycler-Reboot.app (or similar)",
    )
    args = parser.parse_args()
    app: Path = args.app.resolve()

    if not app.is_dir() or app.suffix != ".app":
        print(f"ERROR: not an .app bundle: {app}", file=sys.stderr)
        return 1

    identity = os.environ.get("MACOS_CODESIGN_IDENTITY", "").strip()
    if not identity:
        print(
            "skipped: missing secrets — set MACOS_CODESIGN_IDENTITY to enable "
            "Developer ID signing and notarization"
        )
        return 0

    entitlements_env = os.environ.get("MACOS_ENTITLEMENTS", "").strip()
    if entitlements_env:
        entitlements = Path(entitlements_env)
    else:
        entitlements = (
            Path(__file__).resolve().parent.parent
            / "packaging"
            / "macos"
            / "entitlements.plist"
        )

    if shutil.which("codesign") is None:
        print("ERROR: codesign not found", file=sys.stderr)
        return 1

    print(f"Signing {app} with identity {identity!r}")
    _sign_app(app, identity, entitlements if entitlements.is_file() else None)
    _notarize(app)
    print("macOS sign/notarize complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
