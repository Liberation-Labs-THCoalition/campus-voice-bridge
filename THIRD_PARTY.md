# Third-party software

This repository holds only its own code. None of the software below is included here: `pip install -r
requirements.txt` fetches it from PyPI, and each package keeps its own license. The licenses were read from each
package's installed metadata (Python 3.12.3, the pinned versions), on 2026-11-15.

## In `requirements.txt`

| Package | Version | License | Where the license was read |
|---|---|---|---|
| livekit | 1.2.1 | Apache-2.0 | `License-Expression` |
| kokoro-onnx | 0.6.1 | MIT | its `LICENSE` file (the `License` field is empty) |
| onnxruntime | 1.31.1 | MIT | `License` and classifier |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | `License-Expression` |
| soundfile | 0.14.0 | BSD-3-Clause | `License` and classifier |

## In `requirements-dev.txt` (the tests only)

| Package | Version | License | Where the license was read |
|---|---|---|---|
| PyJWT | 2.15.1 | MIT | `License-Expression` |

## Loaded at run time by kokoro-onnx, not listed above

kokoro-onnx turns text into phonemes with these, and pip installs them as its dependencies:

| Package | Version | License | Note |
|---|---|---|---|
| phonemizer | 3.6.0 | GPL-3.0-or-later | classifier "GNU General Public License v3 or later (GPLv3+)" |
| espeakng-loader | 0.2.4 | **check**: its metadata names no license | its wheel carries the espeak-ng library and data |

**espeak-ng** (GPL-3.0-or-later) is loaded at run time through these packages, from the copy that espeakng-loader's
wheel installs. It is not distributed here. Anyone who ships this tool together with its dependencies (an image, an
installer, a frozen binary) ships espeak-ng and phonemizer too, under their GPL terms, and should check how those terms
sit beside this repository's license before doing so.

## Downloaded at setup, not distributed here

The Kokoro model and voices (`kokoro-v1.0.onnx`, `voices-v1.0.bin`), from kokoro-onnx's release page; `models.sha256`
pins their hashes. Their license was not read from package metadata: **check** it at the model's source.
