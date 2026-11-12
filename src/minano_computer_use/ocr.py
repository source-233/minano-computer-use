"""Isolated Windows OCR through a fixed WinRT script, never user shell code."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .contracts import result
from .jobs import JobRunner, ServiceError, checkpoint, make_deadline

# Arguments are base64 UTF-8 data, not interpolated PowerShell source. WinRT async
# objects remain confined to this disposable process and its owned Job Object.
SCRIPT = r"""param([string]$Image64, [string]$Language64)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
  $null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
  $null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
  function Await($Operation, [Type]$Type) {
    $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
      $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    $task = $method.MakeGenericMethod($Type).Invoke($null, @($Operation))
    return $task.GetAwaiter().GetResult()
  }
  $path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Image64))
  $lang = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Language64))
  if ($lang) {
    $language = New-Object Windows.Globalization.Language($lang)
    if (-not [Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($language)) {
      @{error='OCR_LANGUAGE_UNAVAILABLE'} | ConvertTo-Json -Compress
      exit 0
    }
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
  } else {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  }
  if ($null -eq $engine) {
    @{error='OCR_LANGUAGE_UNAVAILABLE'} | ConvertTo-Json -Compress
    exit 0
  }
  $operation = [Windows.Storage.StorageFile]::GetFileFromPathAsync($path)
  $file = Await $operation ([Windows.Storage.StorageFile])
  $operation = $file.OpenAsync([Windows.Storage.FileAccessMode]::Read)
  $stream = Await $operation ([Windows.Storage.Streams.IRandomAccessStream])
  try {
    $operation = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)
    $decoder = Await $operation ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    try {
      if ($bitmap.PixelWidth -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension -or
          $bitmap.PixelHeight -gt [Windows.Media.Ocr.OcrEngine]::MaxImageDimension) {
        @{error='OCR_IMAGE_TOO_LARGE'} | ConvertTo-Json -Compress
        exit 0
      }
      $recognized = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
      @{text=$recognized.Text;language=$engine.RecognizerLanguage.LanguageTag} |
        ConvertTo-Json -Compress
    } finally { $bitmap.Dispose() }
  } finally { $stream.Dispose() }
} catch {
  @{error='OCR_UNAVAILABLE';hresult=$_.Exception.HResult} | ConvertTo-Json -Compress
  exit 0
}
"""


def recognize(
    target="desktop",
    *,
    language=None,
    image_bytes=None,
    timeout=10,
    cancel_event=None,
    deadline=None,
    runner=None,
    capture=None,
):
    operation = "window.ocr"
    own_runner = runner is None
    runner = runner or JobRunner()
    try:
        deadline = make_deadline(timeout, deadline)
        checkpoint(cancel_event, deadline)
        if language is not None and (not isinstance(language, str) or len(language) > 128):
            raise ValueError("language must be a language tag")
        if os.name != "nt":
            raise ServiceError(
                "OCR_UNAVAILABLE", "Windows WinRT OCR is unavailable on this platform"
            )
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise ServiceError("OCR_UNAVAILABLE", "Windows PowerShell is unavailable")
        metadata = None
        if image_bytes is None:
            if capture is None:
                from .capture import CaptureService

                capture = CaptureService()
                own_capture = True
            else:
                own_capture = False
            try:
                frame = capture.screenshot(
                    target=target,
                    max_dim=0,
                    format="png",
                    timeout=max(0.001, deadline - time.monotonic()),
                    cancel_event=cancel_event,
                )
                image_bytes, metadata = frame.image_bytes, frame.metadata
            finally:
                if own_capture:
                    capture.close()
        checkpoint(cancel_event, deadline)
        if not isinstance(image_bytes, bytes) or len(image_bytes) > 64 * 1024 * 1024:
            raise ValueError("OCR input must be bounded image bytes")
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.width * image.height > 40_000_000:
                raise ValueError("OCR image is too large")
            output = io.BytesIO()
            image.convert("RGB").save(output, format="PNG")
            png = output.getvalue()
        from .trace import ArtifactStore

        # Pin the private directory hierarchy, then exclusively create the script.
        # Neither image data nor a user-controlled language/path becomes source.
        with tempfile.TemporaryDirectory(prefix="minano-ocr-") as directory:
            store = ArtifactStore(Path(directory))
            try:
                image_path = store.allocate(".png")
                store.write_bytes(image_path, png)
                script_path = store.root / "ocr.ps1"
                descriptor = store._open_owned_file(script_path)
                with os.fdopen(descriptor, "wb") as script:
                    script.write(SCRIPT.encode("utf-8-sig"))

                def encode(value):
                    return base64.b64encode(value.encode("utf-8")).decode("ascii")

                data = runner.run(
                    [
                        powershell,
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(script_path),
                        encode(str(image_path)),
                        encode(language or ""),
                    ],
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
            finally:
                store.close()
        checkpoint(cancel_event, deadline)
        if data["exit_code"] or data.get("stdout_truncated"):
            raise ServiceError("OCR_UNAVAILABLE", "WinRT OCR script did not return complete output")
        try:
            payload = json.loads(data["stdout"])
        except (ValueError, TypeError) as exc:
            raise ServiceError("OCR_UNAVAILABLE", "WinRT OCR returned invalid output") from exc
        if not isinstance(payload, dict):
            raise ServiceError("OCR_UNAVAILABLE", "WinRT OCR returned invalid output")
        if "error" in payload:
            code = payload["error"]
            if code not in {"OCR_UNAVAILABLE", "OCR_LANGUAGE_UNAVAILABLE", "OCR_IMAGE_TOO_LARGE"}:
                code = "OCR_UNAVAILABLE"
            raise ServiceError(code, "WinRT OCR or requested language package is unavailable")
        if not isinstance(payload.get("text"), str):
            raise ServiceError("OCR_UNAVAILABLE", "WinRT OCR returned no text result")
        return result(
            "ok",
            operation,
            method="windows_winrt_ocr",
            data={
                "text": payload["text"],
                "language": payload.get("language"),
                "capture": metadata,
                "source": "ocr",
                "empty": not bool(payload["text"]),
            },
        )
    except Exception as exc:
        code = getattr(exc, "code", "OCR_ERROR")
        return result(
            "error" if code in {"TIMEOUT", "CANCELLED"} else "refused",
            operation,
            code=code,
            message="OCR did not complete"
            if code in {"TIMEOUT", "CANCELLED"}
            else "OCR backend, image, or requested language is unavailable",
            **({"cancelled": True} if code == "CANCELLED" else {}),
        )
    finally:
        if own_runner:
            runner.close()
