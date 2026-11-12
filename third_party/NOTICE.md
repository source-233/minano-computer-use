# CPython spawn adaptation

`src/minano_computer_use/isolated_spawn.py` adapts the Windows spawn bootstrap
from CPython 3.13 `Lib/multiprocessing/popen_spawn_win32.py`.
Copyright (c) 2001-2024 Python Software Foundation; All Rights Reserved.

Changes: use a local process/Popen subclass with explicit NUL standard handles
and a minimal inherited-handle whitelist; preserve multiprocessing bootstrap
pipe duplication and process lifecycle; reap incomplete bootstrap failures;
limit the adapter to the tested CPython 3.13 protocol. No global stdlib patch
or process-wide standard-handle mutation is performed.

The full bundled upstream license is retained in `CPython-LICENSE.txt`.
The remainder of this project's original implementation is under MIT.

# Python video dependency

Video encoding depends on PyAV (`av`), distributed separately through the Python
package installer. PyAV 19.0.2 declares BSD-3-Clause and includes its copyright,
license and author notices in its wheel. This repository does not vendor PyAV
or its bundled libraries.

PyAV wheels include FFmpeg libraries; the selected Windows wheel also exposes
the libx264 encoder. Those binary components have their own licensing terms.
Redistributors must obtain and preserve the notices and satisfy the obligations
for the actual binaries they distribute; PyAV's BSD metadata does not establish
that every bundled component is BSD or LGPL-only. This dependency does not
relicense the project's original MIT/PSF source code by itself.

Upstream references:
- https://github.com/PyAV-Org/PyAV/blob/v19.0.2/LICENSE.txt
- https://pyav.org/docs/stable/overview/installation.html
- https://ffmpeg.org/legal.html
- https://www.videolan.org/developers/x264.html
