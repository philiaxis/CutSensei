# `.cutsensei` project format

A project is a UTF-8 JSON document (written atomically). Unknown keys are ignored when loading,
so newer versions can add fields without breaking older files.

```jsonc
{
  "app": "CutSensei",
  "format_version": 1,
  "app_version": "0.1.0",
  "name": "lecture",
  "media": {
    "path": "/abs/path/lecture.mp4",      // absolute path when saved
    "relpath": "../videos/lecture.mp4",   // relative to the project file (may be null)
    "duration": 5400.0, "width": 1920, "height": 1080, "fps": 29.97,
    "fps_fraction": "30000/1001", "rotation": 0, "has_audio": true,
    "audio_rate": 48000, "audio_channels": 2, "size": 123456789, "mtime": 1790000000.0
  },
  "board_regions": [[0.05, 0.08, 0.90, 0.62]],   // x, y, w, h as fractions of the frame
  "settings": { "writing_speed": 4.0, "aggressiveness": 50, "pad_before": 0.25,
                "source_type": "auto", ... },   // auto | camera | screen
  "export":   { "height": 0, "fps": 0.0, "quality": 2, "encoder": "auto", ... },
  "auto_applied": true,
  "segments": [
    {
      "start": 0.0, "end": 60.5,          // source seconds, contiguous, cover the video
      "kind": "speech",                   // speech | writing | idle | uncertain
      "action": "keep",                   // keep | speed | cut
      "speed": null,                      // explicit speed for "speed" (null = settings)
      "volume": null,                     // explicit gain (null = default for the action)
      "protected": false,                 // "always keep"
      "manual": false,                    // edited by the user (kept on regeneration)
      "review": false, "reviewed": false, // item to check / checked
      "auto_action": "keep",              // what the automatic edit decided
      "reason": "speech", "confidence": 0.97,
      "id": "a1b2c3d4e5"
    }
  ],
  "analysis": "<base64 of a compressed .npz>"   // per-0.1 s features, may be null
}
```

The `analysis` blob contains the arrays `speech`, `voicing`, `loudness`, `clicks`, `ink`,
`motion`, `hand`, `global_change`, `nav` (float16, one value per 0.1 s; `nav` is the scrolling /
page turn activity of screen recordings and may be missing in older files) and `wave_peaks`
(uint8, 100 values per second) plus a JSON `__meta__` entry (duration, board regions used, speech
detector, version and in `meta`: `source` = how the video was analysed (`camera` / `screen`,
missing = camera), `source_detected`, `source_confidence` and `live_rects`, the normalized
rectangles of a webcam picture that were ignored). With it, a project can be re-edited with different settings without
analysing the video again. Thumbnails are not stored in the project; they are regenerated
into the user cache directory when needed.

Source resolution when opening: the absolute path, then `relpath` relative to the project,
then a file with the same name next to the project; otherwise the user is asked to locate it
(the duration must match).
