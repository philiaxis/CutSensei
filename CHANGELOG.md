# Changelog

## 0.1.0 — first release

- Automatic editing of lecture videos: speech kept at 1x, silent board writing sped up
  (default 4x), idle waiting removed, ambiguous parts kept and marked for checking.
- Speech detection with the bundled Silero VAD plus a pitch/SNR detector; chalk and pen
  taps are counted separately and never mistaken for speech.
- Board writing detection that tracks strokes staying on the board, ignores a lecturer who
  only walks past and credits writing hidden behind the body; optional board regions.
- Protective rules: speech margins, kept pauses, finished-board hold, merged short parts,
  review items instead of deletions when unsure.
- Desktop editor (PySide6): preview that plays the edited result in real time, timeline
  with thumbnails, waveform, colour + label coded segments, boundary dragging, zoom;
  panels for media statistics, items to check, deleted parts (preview and restore),
  segment properties and automatic-edit settings; undo/redo; keyboard shortcuts;
  English and Japanese.
- Manual edits and protected segments survive re-applying the automatic edit.
- Projects (`.cutsensei`), autosave with recovery.
- MP4 export (H.264/H.265) with resolution, frame rate and quality options, pitch
  preserving audio time-stretch, sample accurate A/V sync, hardware encoders
  (NVENC, Quick Sync, AMF, VideoToolbox) and optional hardware decoding.
- Command line interface (`cutsensei-cli`) and synthetic demo generator.
- Windows, macOS (arm64/x64) and Linux builds.
