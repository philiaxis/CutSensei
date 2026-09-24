"""Screen recordings of digital notes (GoodNotes, OneNote, ...): the writing
tracker on synthetic frames, the camera / screen detection and the design
document scenario recorded as a tablet screen recording."""

import numpy as np
import pytest

from cutsensei.analysis.screen import ScreenTracker

W, H = 480, 360


def page():
    img = np.full((H, W, 3), 255, np.uint8)
    img[::24] = (238, 222, 196)          # ruling
    return img


def run(frames, mask=None):
    tr = ScreenTracker((W, H), np.ones((H, W), bool) if mask is None else mask, 4.0)
    for f in frames:
        tr.feed(f)
    return tr.finish()


def write(img, t, color=(110, 45, 25)):
    import cv2

    x = 20 + (t * 9) % 420
    y = 60 + 24 * ((t * 9) // 420)
    cv2.line(img, (x, y), (x + 6, y - 10), color, 2, cv2.LINE_AA)
    cv2.line(img, (x + 6, y - 10), (x + 9, y), color, 2, cv2.LINE_AA)


def test_writing_is_ink_and_static_page_is_not():
    frames, canvas = [], page()
    for t in range(120):                 # 30 s at 4 fps
        if 40 <= t < 80:                 # write for 10 s
            write(canvas, t)
        frames.append(canvas.copy())
    f = run(frames)
    writing = f.ink[40:84].mean()
    quiet = np.concatenate([f.ink[:38], f.ink[90:]])
    assert writing > 40
    assert quiet.max() == 0
    assert f.nav.max() == 0


def test_laser_pointer_and_cursor_are_not_ink():
    import cv2

    frames, base = [], page()
    cv2.putText(base, "f(x) = x^2", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (30, 30, 30), 2)
    trail = []
    for t in range(160):
        img = base.copy()
        if 20 <= t < 100:                # a laser pointer moving around with a trail
            trail.append((int(240 + 150 * np.sin(t * 0.3)), int(180 + 80 * np.sin(t * 0.5))))
            trail = trail[-3:]
            for k in range(1, len(trail)):
                cv2.line(img, trail[k - 1], trail[k], (40, 40, 255), 4 - k, cv2.LINE_AA)
            cv2.circle(img, trail[-1], 6, (40, 40, 255), -1)
        if 100 <= t < 124:               # the laser dot rests on a word for 6 s, then goes
            cv2.circle(img, (200, 110), 6, (40, 40, 255), -1)
        cv2.circle(img, (400, 300), 3, (0, 0, 0), -1) if t % 2 else None   # blinking caret
        frames.append(img)
    f = run(frames)
    assert f.ink.sum() == 0
    assert f.motion[25:95].mean() > 0.0005


def test_scroll_and_page_turn_are_navigation_not_ink():
    import cv2

    rng = np.random.default_rng(3)
    tall = np.full((H * 3, W, 3), 255, np.uint8)
    tall[::24] = (238, 222, 196)
    for i in range(40):                  # existing notes on the page
        x, y = int(rng.integers(20, W - 80)), int(rng.integers(20, H * 3 - 20))
        cv2.putText(tall, "abc", (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (110, 45, 25), 2)
    frames = []
    offset = 0
    for t in range(100):
        if 20 <= t < 28:                 # scroll down for 2 s
            offset += 30
        if t == 60:                      # page turn to a blank page
            tall = np.full_like(tall, 255)
            tall[::24] = (238, 222, 196)
            offset = 0
        frames.append(tall[offset:offset + H].copy())
    f = run(frames)
    assert f.ink.sum() == 0
    assert f.nav[20:29].max() == 1.0 and f.nav[59:62].max() == 1.0
    assert f.nav[35:55].max() == 0.0


def test_status_bar_clock_is_ignored():
    import cv2

    frames, canvas = [], page()
    for t in range(4 * 90):              # 90 s; the "clock" changes every 15 s
        img = canvas.copy()
        cv2.putText(img, f"10:{41 + t // 60:02d}", (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                    (20, 20, 20), 1, cv2.LINE_AA)
        frames.append(img)
    f = run(frames)
    assert f.ink.sum() == 0


def test_webcam_picture_in_picture_is_ignored():
    import cv2

    rng = np.random.default_rng(5)
    frames, canvas = [], page()
    for t in range(4 * 40):
        img = canvas.copy()
        cam = np.full((90, 120, 3), 140, np.int16)
        cx = int(60 + 10 * np.sin(t * 0.4))
        cv2.circle(cam, (cx, 40), 22, (140, 165, 205), -1)
        cam += rng.normal(0, 4, cam.shape).astype(np.int16)
        img[H - 100:H - 10, W - 130:W - 10] = np.clip(cam, 0, 255).astype(np.uint8)
        if 80 <= t < 120:
            write(canvas, t)
        frames.append(img)
    f = run(frames)
    assert f.ink[:76].sum() == 0 and f.ink[126:].sum() == 0
    assert f.ink[80:122].mean() > 40


def test_compression_flicker_on_a_photo_is_ignored():
    rng = np.random.default_rng(7)
    frames, canvas = [], page()
    photo = rng.integers(40, 220, (100, 140, 3)).astype(np.int16)
    for t in range(4 * 30):
        img = canvas.copy()
        # every 2 s a "key frame" re-encodes the photo slightly differently
        jitter = rng.integers(-18, 19, photo.shape) if t % 8 == 0 else 0
        photo_t = np.clip(photo + jitter, 0, 255).astype(np.uint8)
        img[50:150, 300:440] = photo_t
        frames.append(img)
    f = run(frames)
    assert f.ink.sum() < 5


def test_board_region_mask_excludes_rectangles():
    from cutsensei.analysis.video import region_mask

    m = region_mask([], (100, 50), exclude=[[0.5, 0.5, 0.5, 0.5]])
    assert m[:25].all() and not m[30:, 60:].any() and m[30:, :40].all()


# ---------------------------------------------------------------------------
# detection and the complete pipeline (ffmpeg)
# ---------------------------------------------------------------------------

def _iou(a, b):
    ax1, ay1, bx1, by1 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax1, bx1) - max(a[0], b[0]))
    ih = max(0.0, min(ay1, by1) - max(a[1], b[1]))
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def test_detects_camera_recording(short_video):
    from cutsensei.analysis.source import detect_source
    from cutsensei.core.media import probe

    det = detect_source(probe(short_video[0]))
    assert det.kind == "camera"
    assert det.live_rects == []


def test_detects_screen_recording_and_webcam(screen_webcam_video):
    from cutsensei.analysis.source import detect_source
    from cutsensei.core.media import probe
    from cutsensei.demo import webcam_rect

    path, spec = screen_webcam_video
    det = detect_source(probe(path))
    assert det.kind == "screen"
    assert len(det.live_rects) == 1
    assert _iou(det.live_rects[0], webcam_rect(spec)) > 0.5


@pytest.mark.slow
def test_mixed_screen_recording(screen_webcam_video):
    """Laser pointer, scrolling, a page turn and a webcam picture."""
    from cutsensei.analysis.classifier import regenerate
    from cutsensei.analysis.pipeline import analyze
    from cutsensei.core.media import probe
    from cutsensei.core.project import Project
    from cutsensei.core.settings import AutoEditSettings
    from cutsensei.core.timeline import Action, Kind

    path, spec = screen_webcam_video
    media = probe(path)
    res = analyze(media, [])
    assert res.source == "screen" and res.source_detected == "screen"
    st = AutoEditSettings()
    tl = regenerate(Project(media).timeline, res, st)
    for kind, a, b in spec.scene_ranges():
        mid = (a + b) / 2
        seg = tl.segment_at(mid)
        if kind.startswith("speech"):
            assert seg.action == Action.KEEP and seg.kind == Kind.SPEECH, (kind, a)
        elif kind == "writing":
            assert seg.action == Action.SPEED, (kind, a)
        elif kind in ("scroll", "page"):
            # working on the notes: sped up with the writing around it, never cut
            assert seg.action == Action.SPEED, (kind, a)
        elif kind == "idle":
            assert seg.action == Action.CUT, (kind, a)
        elif kind == "laser":
            # pointing silently may matter: kept and marked for checking
            assert seg.action == Action.KEEP and seg.review, (kind, a)


@pytest.mark.slow
@pytest.mark.parametrize("aggressiveness", [0, 50, 100])
def test_design_doc_scenario_as_screen_recording(screen_demo_video, aggressiveness):
    """60 s explanation + 40 s silent writing + 20 s waiting, written in a note
    app on a tablet: 60 + 40/4 = 70 s plus the protective margins."""
    from cutsensei.analysis.classifier import regenerate
    from cutsensei.core.project import Project
    from cutsensei.core.settings import AutoEditSettings
    from cutsensei.core.timeline import Action, Kind

    media, res = screen_demo_video
    assert res.source == "screen"
    st = AutoEditSettings(writing_speed=4.0, aggressiveness=aggressiveness)
    tl = regenerate(Project(media).timeline, res, st)
    for t in (0.05, 10, 30, 59.8):
        seg = tl.segment_at(t)
        assert seg.action == Action.KEEP and seg.speed_value(st) == 1.0, t
    for t in (62, 70, 80, 90, 99):
        seg = tl.segment_at(t)
        assert seg.action == Action.SPEED and seg.kind == Kind.WRITING, t
    cut = sum(s.duration for s in tl if s.action == Action.CUT and s.start >= 99.0)
    assert cut >= 14.0
    out = tl.build_map(st).out_duration
    assert 70.0 <= out <= 75.0, out


def test_forced_source_type_and_reanalysis_rules(screen_demo_video):
    from cutsensei.analysis.result import AnalysisResult, resolve_source, video_analysis_current

    _media, res = screen_demo_video
    assert video_analysis_current(res, [], "auto")
    assert video_analysis_current(res, [], "screen")
    assert not video_analysis_current(res, [], "camera")
    assert not video_analysis_current(res, [[0.1, 0.1, 0.5, 0.5]], "auto")
    # the analysis survives a project round trip
    again = AnalysisResult.from_b64(res.to_b64())
    assert again.source == "screen" and len(again.nav) == len(res.nav)
    # analyses made before screen recordings were supported count as camera
    old = AnalysisResult(duration=10.0)
    old.ensure_lengths()
    assert old.source == "camera" and resolve_source("auto", old) == "camera"
    assert video_analysis_current(old, [], "auto")
    assert not video_analysis_current(old, [], "screen")
