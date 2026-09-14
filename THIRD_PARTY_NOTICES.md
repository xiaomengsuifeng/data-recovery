# Third-party components in ShiHui 0.2.0

Our Python application code in `src/` is provided under the MIT license in `LICENSE`. That license does not replace the licenses of the following components. No third-party library binary has been patched or statically linked into our application.

## Python 3.13.12

The official Windows x64 embeddable runtime is distributed in `runtime/`. Its license and bundled component notices are in `runtime/LICENSE.txt` (Python Software Foundation License and the notices reproduced there).

Source: [CPython 3.13.12](https://www.python.org/downloads/release/python-31312/). Binary provenance and SHA-256 are recorded in `runtime-lock.json`.

## Qt, PySide6 and Shiboken 6.8.3

The application uses the LGPL v3 option for the community Qt/PySide6/Shiboken libraries. Only the Qt Core, Gui, Widgets libraries, their Python bindings and support files, Windows/offscreen platform plugins, the standard Windows style, and GIF/ICO/JPEG/TIFF/WebP image plugins are selected from the PyPI wheel. PNG/BMP handling is part of Qt Gui. Unused QML, designer, web, database, SVG and other modules are not included in the runtime selection.

The libraries are dynamically loaded from `runtime/Lib/site-packages/PySide6` and `shiboken6`. You may replace them with compatible modified versions and debug those modifications under the applicable licenses. The launcher does not require a matching package hash. `Check-Package.cmd` is an optional diagnostic and will report deliberate modifications as changed files.

The corresponding upstream source archives, including build scripts, are supplied in `sources/`:

- `qtbase-everywhere-src-6.8.3.tar.xz` — Qt Core, Gui, Widgets, platform/style and base image plugins.
- `qtimageformats-everywhere-src-6.8.3.tar.xz` — TIFF/WebP plugins and embedded image codec sources.
- `pyside-setup-everywhere-src-6.8.3.tar.xz` — PySide6, Shiboken and their build scripts.

Readable copies of license, copyright, notice and `qt_attribution.json` files from those archives are reproduced under `licenses/`. The full LGPL v3 and GPL v3 texts are included there. Archive-level notices can cover additional source modules that are not loaded by this application. Upstream commercial-license option notices in the wheel are retained; this distribution uses the LGPL option instead of claiming a commercial Qt license.

Qt also incorporates third-party components, including image codecs, Unicode data, compression and text rendering libraries, under their own licenses. Their source, attribution metadata and license texts are retained in the supplied source archives and extracted notices. See [Qt for Python licensing](https://doc.qt.io/qtforpython-6.8/licenses.html) and [Qt source archives](https://download.qt.io/archive/qt/6.8/6.8.3/submodules/).

To rebuild, use the upstream `README.md`, `CMakeLists.txt`, configure/build scripts and [Qt for Python build guide](https://doc.qt.io/qtforpython-6.8/building_from_source/index.html). Keep the x64 architecture and a compatible Python ABI. Replace the corresponding DLL/PYD files together; our uncompiled Python application is supplied in full and does not require relinking to replace a compatible library. Original binary/source download URLs and exact digests are in `runtime-lock.json`.

## The Sleuth Kit 4.15.0

The official Windows x86 CLI tools and their supplied DLLs/notices are preserved under `vendor/tsk/`. Only `fls.exe` and `icat.exe` are invoked, as separate processes. Windows x64 runs these x86 tools through its compatibility support; their DLLs are kept separate from the x64 Python/Qt libraries.

The corresponding source release `sleuthkit-4.15.0.tar.gz` is supplied under `sources/`, with license/notice files also copied under `licenses/`. TSK includes code under the Common Public License 1.0, IBM Public License 1.0 and other component licenses. This application does not relicense those tools under MIT. See [TSK licenses](https://www.sleuthkit.org/sleuthkit/licenses.php) and [the source release](https://github.com/sleuthkit/sleuthkit/releases/tag/sleuthkit-4.15.0).

## Microsoft runtime files

Python, PySide6/Shiboken and the TSK Windows distributions supply Microsoft Visual C++ runtime DLLs. These are retained from the respective upstream binary archives. They are Microsoft components, not MIT or LGPL application code. The x64 and x86 copies remain in their respective process directories. Windows system libraries are supplied by Windows and are not included by this project.

## Development fixtures

The NIST DFR test image is used only in development. No disk images, recovered sample contents, user scans or development virtual environments are included in the application bundle. Fixture source and interpretation are documented in the project at `tests/integration/fixture-source.md`.
