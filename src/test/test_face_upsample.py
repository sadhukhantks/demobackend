"""_encode_live_face detection-scale + fallback tests (no DB, no real face).

The speed win is entirely in *not* scanning at upsample=1. These assert on which
scales actually ran, because a fallback that fires when it shouldn't quietly
gives back the 4.7x it was added to save, and one that never fires turns a slow
punch into "No face detected".
"""

import pytest

from src.mobileapp.src.attendance import attendance as att


class FakeFaceRecognition:
    """Stands in for the face_recognition module, recording every detection
    scale it was asked for."""

    def __init__(self, found_at):
        self.found_at = found_at  # upsample values at which a face "appears"
        self.scans = []

    def face_locations(self, img, number_of_times_to_upsample=1):
        self.scans.append(number_of_times_to_upsample)
        if number_of_times_to_upsample in self.found_at:
            return [(0, 10, 10, 0)]
        return []

    def face_encodings(self, img, known_face_locations=None):
        return [f"enc{i}" for i, _ in enumerate(known_face_locations or [])]


@pytest.fixture
def fake_fr(monkeypatch):
    def _install(found_at, upsample=0):
        fake = FakeFaceRecognition(found_at)
        monkeypatch.setattr(att, "face_recognition", fake)
        monkeypatch.setattr(att, "FACE_UPSAMPLE", upsample)
        return fake

    return _install


class TestEncodeLiveFace:
    def test_fast_scale_hit_does_not_scan_twice(self, fake_fr):
        """The common case — face found at upsample=0, no second scan."""
        fake = fake_fr(found_at={0})
        assert att._encode_live_face(object()) == ["enc0"]
        assert fake.scans == [0]

    def test_falls_back_when_fast_scale_finds_nothing(self, fake_fr):
        """A face only the slow scan can see must still get in, not 400."""
        fake = fake_fr(found_at={1})
        assert att._encode_live_face(object()) == ["enc0"]
        assert fake.scans == [0, 1]

    def test_no_face_at_either_scale_returns_empty(self, fake_fr):
        fake = fake_fr(found_at=set())
        assert att._encode_live_face(object()) == []
        assert fake.scans == [0, 1]

    def test_revert_flag_scans_once_at_the_old_scale(self, fake_fr):
        """FACE_UPSAMPLE=1 is the rollback switch — it must reproduce the old
        behaviour exactly, not scan at upsample=1 twice."""
        fake = fake_fr(found_at={1}, upsample=1)
        assert att._encode_live_face(object()) == ["enc0"]
        assert fake.scans == [1]

    def test_only_the_first_detection_is_encoded(self, fake_fr):
        """Callers only ever read live_encodings[0]; encoding a bystander's face
        too is pure cost."""
        fake = fake_fr(found_at={0})
        fake.face_locations = lambda img, number_of_times_to_upsample=1: [
            (0, 10, 10, 0), (0, 20, 20, 0)]
        assert att._encode_live_face(object()) == ["enc0"]
