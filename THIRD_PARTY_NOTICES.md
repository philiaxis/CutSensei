# Third-party notices

CutSensei itself is released under the MIT License (see `LICENSE`).
It uses the following third-party software. Their licenses apply to the
respective components.

| Component | Used for | License |
|---|---|---|
| [Qt](https://www.qt.io/) / [PySide6](https://doc.qt.io/qtforpython-6/) | user interface, preview playback | LGPL-3.0 (Qt), LGPL-3.0 (PySide6) |
| [FFmpeg](https://ffmpeg.org/) | decoding, filtering, encoding | LGPL-2.1+ / GPL-2.0+ depending on the build |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | provides an FFmpeg binary | BSD-2-Clause (package), FFmpeg binary: GPL build |
| [Silero VAD](https://github.com/snakers4/silero-vad) | speech detection model (`src/cutsensei/models/silero_vad_16k.onnx`) | MIT |
| [ONNX Runtime](https://onnxruntime.ai/) | runs the speech detection model | MIT |
| [OpenCV](https://opencv.org/) (`opencv-python-headless`) | image processing for board writing detection | Apache-2.0 |
| [NumPy](https://numpy.org/) | signal processing | BSD-3-Clause |

## FFmpeg

FFmpeg is executed as a separate program; CutSensei does not link against it.
The FFmpeg binaries distributed with CutSensei builds are "GPL" builds
(they contain libx264 / libx265). Their source code is available from
<https://ffmpeg.org/download.html> and from the respective build projects
(<https://www.gyan.dev/ffmpeg/builds/>, <https://johnvansickle.com/ffmpeg/>,
<https://github.com/imageio/imageio-ffmpeg>). You can replace the bundled
binary with any other FFmpeg build (Preferences → FFmpeg, or the
`CUTSENSEI_FFMPEG` environment variable).

## Qt / PySide6

Qt and PySide6 are used under the terms of the GNU Lesser General Public
License v3. They are dynamically linked; you may replace the Qt/PySide6
libraries in a CutSensei installation with your own modified versions.
Source code: <https://download.qt.io/> and <https://code.qt.io/>.

## Silero VAD

```
MIT License

Copyright (c) 2020-present Silero Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
