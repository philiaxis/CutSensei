# Contributing to CutSensei

Thank you for helping! Bug reports with a short sample video (or a description of the lecture
setting: blackboard/whiteboard, camera position, microphone) are especially valuable, because the
detection is tuned on real lectures.

## Setup

```bash
git clone https://github.com/philiaxis/CutSensei.git
cd CutSensei
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m cutsensei          # GUI
python -m cutsensei.cli info # environment check
```

## Tests

```bash
python -m pytest -q                  # everything (~1 minute, generates and encodes videos)
python -m pytest -q -m "not slow"    # fast unit tests
```

GUI tests run with `QT_QPA_PLATFORM=offscreen` (set automatically in `tests/conftest.py`).
`tests/test_acceptance.py` checks the reference scenario of the design document
(60 s speech + 40 s silent writing + 20 s idle at 4x ≈ 70 s + margins) end to end.

## Code layout

| Path | Contents |
|---|---|
| `src/cutsensei/core/` | segments and edit map (`timeline.py`), settings, project files, undo, FFmpeg helpers, media probing |
| `src/cutsensei/analysis/` | audio features and VAD, camera / screen recording detection (`source.py`), board writing tracker (`video.py`), digital notes tracker (`screen.py`), classifier, pipeline, thumbnails |
| `src/cutsensei/render/` | export: filter graph, WSOLA audio rendering, encoder selection |
| `src/cutsensei/ui/` | PySide6 user interface (no business logic; edits go through `controller.py`) |
| `src/cutsensei/cli.py` | command line interface |
| `src/cutsensei/demo.py` | synthetic lecture generator used by tests and `cutsensei-cli demo` |
| `packaging/` | PyInstaller spec and build script |

The `core`, `analysis` and `render` packages do not import Qt and can be used as a library.

## Translations

UI strings are written in English and wrapped in `tr("...")`. Translations are dictionaries in
`src/cutsensei/ui/translations_<lang>.py`. To add a language:

1. copy `translations_ja.py` to `translations_<code>.py` and translate the values,
2. add the language to `SUPPORTED` in `src/cutsensei/ui/i18n.py`,
3. run `python tools/extract_strings.py` to list all strings; `tests/test_gui.py` checks that the
   Japanese table is complete and that `{placeholders}` match.

## Style

- Keep the UI free of analysis logic, keep `core`/`analysis`/`render` free of Qt.
- Long running work must accept a `CancelToken` and report progress.
- Never modify the source video; all edits are decisions in the timeline.
- Please add or update tests for behaviour changes.

## Releases

Push a tag `vX.Y.Z` — the `Release builds` workflow builds Windows, macOS (arm64 + x64) and Linux
bundles and attaches them to a GitHub release.
