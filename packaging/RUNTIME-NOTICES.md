# Windows portable runtime notices

The app, its desktop launcher and its sources use the licence in app/LICENSE. The original component notices and attributions are in app/THIRD_PARTY_NOTICES.md, app/vendor and app/h3studio/web. The licences of the bundled runtime components apply to those components.

## Python and installed packages

CPython 3.11.18 is distributed by Astral's python-build-standalone project. The exact Windows x64 install-only artifact from the 20261124 release has SHA-256 3e4a35e3c238e5090d36bf95c041d9052bc82d6e298cb912ba7f70f6c66cf81a. CPython's licence, historical licences and acknowledgements are included in runtime/LICENSE.txt and its standard library. runtime-libraries.txt reproduces the additional licence files from the matching upstream full build. [Release and source](https://github.com/astral-sh/python-build-standalone/releases/tag/20261124)

windows-dependencies.json gives each of the 19 added Python distributions, its version, upstream project, exact PyPI artifact URL and SHA-256. The original package metadata and shipped licence texts are retained in runtime/Lib/site-packages. NumPy and Pillow include their bundled third-party notices. OpenCV's license and third-party notices accompany cv2. Studio uses OpenCV for analysis and masking. The separate OpenCV FFmpeg video I/O plugin is excluded from this build.

proxy_tools 0.1.0 declares MIT in its PyPI metadata. Its upstream project distributes the BSD licence and copyright notices reproduced in proxy-tools-LICENSE.txt. [Source](https://github.com/jtushman/proxy_tools/tree/db43f1e35d4f90a65c5a4d56d9e9af88212ec6e6)

pywebview supplies the Microsoft WebView2 SDK redistributables. The Microsoft 1.0.3856.49 SDK licence and third-party notices accompany them as WebView2-LICENSE.txt and WebView2-NOTICE.txt. Windows supplies or installs the separate browser runtime. [Microsoft SDK distribution](https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.3856.49)

## Media libraries

PyAV 17.1.0 is BSD-3-Clause. Its Windows wheel uses separate FFmpeg 8.1.1 DLLs and codec libraries under runtime/Lib/site-packages/av.libs. The FFmpeg library identifies its licence as LGPL version 3 or later. The upstream build also supplies separately licensed codec libraries including GPL x264 and x265, LAME, dav1d, Opus, OpenCORE AMR, SVT-AV1, libvpx, libwebp and libvpl. The applicable GPL/LGPL texts are provided here. The codec libraries remain separately replaceable. FFmpeg and ffprobe command-line tools are supplied by the user's selected installation.

The original build instructions, patches, exact upstream source archive URLs and corresponding hashes are identified in codec-sources.txt. The PyAV upstream recipe selected the 8.1.1-2 Windows library bundle. [PyAV build configuration](https://github.com/PyAV-Org/PyAV/blob/v17.1.0/scripts/ffmpeg-latest.json) · [Codec sources and patches](https://github.com/PyAV-Org/pyav-ffmpeg/tree/8.1.1-2)

The user-selected AI engines, models and optional external tools have their own licences. These applications and weights are acquired separately.
