import numpy as np

from cutsensei.analysis.video import BoardTracker, analysis_geometry, region_mask, union_rect
from cutsensei.core.media import MediaInfo

W, H = 320, 180


def board():
    img = np.full((H, W), 60, np.uint8)
    return img


def run(frames):
    mask = np.ones((H, W), bool)
    tr = BoardTracker((W, H), mask, 4.0, initial_background=frames[0])
    for f in frames:
        tr.feed(f)
    return tr.finish()


def add_noise(img, rng):
    return np.clip(img.astype(np.int16) + rng.integers(-2, 3, img.shape), 0, 255).astype(np.uint8)


def test_writing_produces_ink_evidence():
    import cv2

    rng = np.random.default_rng(1)
    frames = []
    canvas = board()
    for t in range(80):          # 20 s at 4 fps
        if 20 <= t < 60:         # write for 10 s
            x = 20 + (t - 20) * 6
            cv2.line(canvas, (x, 60), (x + 5, 75), 230, 2)
        frames.append(add_noise(canvas, rng))
    f = run(frames)
    writing = f.ink[20:64].mean()
    quiet = np.concatenate([f.ink[:18], f.ink[68:]]).mean()
    assert writing > 20 * max(quiet, 0.5)


def test_walking_person_is_not_writing():
    import cv2

    rng = np.random.default_rng(2)
    base = board()
    cv2.putText(base, "E = mc2", (40, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 230, 2)
    frames = []
    for t in range(120):          # 30 s
        img = base.copy()
        x = int(20 + (t % 60) * 4.5)
        cv2.rectangle(img, (x, 40), (x + 40, H), 35, -1)       # dark body
        cv2.circle(img, (x + 20, 30), 14, 180, -1)             # head
        frames.append(add_noise(img, rng))
    f = run(frames)
    assert f.motion[5:].mean() > 0.01           # there is movement ...
    assert f.ink[5:].mean() < 15.0              # ... but hardly any ink change


def test_static_scene_has_no_evidence():
    rng = np.random.default_rng(3)
    import cv2

    base = board()
    cv2.putText(base, "x^2 + y^2", (30, 90), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 230, 2)
    frames = [add_noise(base, rng) for _ in range(60)]
    f = run(frames)
    assert f.ink.sum() < 1.0
    assert f.motion.max() < 0.01


def test_region_helpers():
    regions = [[0.1, 0.1, 0.3, 0.3], [0.5, 0.2, 0.4, 0.5]]
    x, y, w, h = union_rect(regions)
    assert (round(x, 3), round(y, 3), round(w, 3), round(h, 3)) == (0.1, 0.1, 0.8, 0.6)
    media = MediaInfo(path="x", duration=10, width=1920, height=1080)
    (cx, cy, cw, ch), (aw, ah) = analysis_geometry(media, regions)
    assert cw % 2 == 0 and ch % 2 == 0 and aw <= 512 and ah <= 320
    mask = region_mask(regions, (aw, ah))
    assert mask.any() and not mask.all()


def test_whiteboard_dark_marker():
    import cv2

    rng = np.random.default_rng(4)
    canvas = np.full((H, W), 225, np.uint8)       # bright whiteboard
    frames = []
    for t in range(80):
        if 20 <= t < 60:
            x = 20 + (t - 20) * 6
            cv2.line(canvas, (x, 80), (x + 5, 95), 40, 2)   # dark marker
        frames.append(add_noise(canvas, rng))
    mask = np.ones((H, W), bool)
    tr = BoardTracker((W, H), mask, 4.0, initial_background=frames[0])
    for f in frames:
        tr.feed(f)
    feats = tr.finish()
    assert feats.polarity == -1
    assert feats.ink[20:64].mean() > 20 * max(np.concatenate([feats.ink[:18],
                                                              feats.ink[68:]]).mean(), 0.5)
