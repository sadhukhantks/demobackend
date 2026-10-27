"""MobileFaceNet embedding helper — the server-side twin of the Android FaceEngine.

THE ALIGNMENT CONTRACT (v2 — landmark crop)
-------------------------------------------
Both sides MUST do exactly this, or the vectors are not comparable:

  1. locate the face; take the two eye centres and the two mouth corners
  2. rotate the image about the eye midpoint so the eye line is horizontal
  3. square crop of side S = max(eye_distance / 0.31, eye_to_mouth / 0.36),
     with the eye midpoint at (0.5 * S, 0.46 * S) from the crop's top-left
     (the ArcFace 112x112 template MobileFaceNet was trained on)
  4. resize to 112x112 RGB
  5. scale pixels to [-1, 1] with (px - 127.5) / 128.0
  6. run the model, take the 192-d output, L2-normalise it

Why landmarks and not the detector's box: v1 cropped "face box + 20% margin",
but dlib's HOG box (server) and ML Kit's box (device) frame a face very
differently, so the SAME face embedded on the two sides agreed at only ~0.28
cosine, and the loose/tight crops made unrelated people score 0.7+. Eyes and
mouth are anatomical points both detectors report consistently.

Anything that changes here changes MOBILE_MODEL_VER in src/sync/routes.py and
forces a re-embed of every enrolment photo plus a re-pull on every device.

Model file: tools/models/mobilefacenet.onnx (server) and
app/src/main/assets/mobilefacenet.tflite (device) — the SAME weights, exported
twice. Neither binary is in git; drop them in before running.
"""
import os

import numpy as np

INPUT_SIZE = 112
EMBEDDING_DIM = 192
MODEL_VER = 'mobilefacenet-v2'
# Crop geometry (fractions of the square crop side) — see the contract above.
EYE_Y = 0.46        # eye midpoint sits this far down the crop
EYE_DIST = 0.31     # inter-eye distance as a fraction of the side
EYE_MOUTH = 0.36    # eye-midpoint-to-mouth distance as a fraction of the side

# tools/models/, the same place src/sync/routes.py loads from; .tflite first
# because that is the file that actually ships (identical weights to the .onnx).
_MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models')
_DEFAULT_MODEL = next(
    (p for p in (os.path.join(_MODELS_DIR, 'mobilefacenet.tflite'),
                 os.path.join(_MODELS_DIR, 'mobilefacenet.onnx')) if os.path.exists(p)),
    os.path.join(_MODELS_DIR, 'mobilefacenet.tflite'))


class MobileFaceEmbedder:
    """Loads mobilefacenet once and embeds aligned 112x112 crops."""

    def __init__(self, model_path=None):
        self.model_path = model_path or os.environ.get('MOBILEFACENET_MODEL', _DEFAULT_MODEL)
        if not os.path.exists(self.model_path):
            raise FileNotFoundError(
                f"MobileFaceNet model not found at {self.model_path}.\n"
                "Download/export the weights first, e.g. an ONNX export of "
                "MobileFaceNet, and place it there (or set MOBILEFACENET_MODEL).")
        self._kind = 'onnx' if self.model_path.endswith('.onnx') else 'tflite'
        if self._kind == 'onnx':
            import onnxruntime
            self._sess = onnxruntime.InferenceSession(
                self.model_path, providers=['CPUExecutionProvider'])
            self._input = self._sess.get_inputs()[0]
            # NCHW (1,3,112,112) or NHWC (1,112,112,3) — detect from the shape.
            self._nchw = list(self._input.shape)[1] == 3
        else:
            # Three interpreters, same weights. ai-edge-litert is the one that
            # actually installs on Windows + Python 3.12 (tflite-runtime has no
            # wheel there and tensorflow is a ~600 MB dependency for one model),
            # so it is tried first and the older names remain as fallbacks.
            Interpreter = None
            for module, attr in (
                ('ai_edge_litert.interpreter', 'Interpreter'),
                ('tflite_runtime.interpreter', 'Interpreter'),
                ('tensorflow.lite.python.interpreter', 'Interpreter'),
            ):
                try:
                    Interpreter = getattr(__import__(module, fromlist=[attr]), attr)
                    break
                except ImportError:
                    continue
            if Interpreter is None:
                raise ImportError(
                    "No TFLite interpreter available. Install one:\n"
                    "    pip install ai-edge-litert")
            self._interp = Interpreter(model_path=self.model_path)
            self._interp.allocate_tensors()
            self._in = self._interp.get_input_details()[0]
            self._out = self._interp.get_output_details()[0]
            self._nchw = list(self._in['shape'])[1] == 3

    def embed(self, crop_rgb):
        """[112,112,3] uint8 RGB -> L2-normalised float32 vector."""
        x = (crop_rgb.astype(np.float32) - 127.5) / 128.0
        x = np.transpose(x, (2, 0, 1)) if self._nchw else x
        x = np.expand_dims(x, 0)
        if self._kind == 'onnx':
            vec = self._sess.run(None, {self._input.name: x})[0][0]
        else:
            self._interp.set_tensor(self._in['index'], x)
            self._interp.invoke()
            vec = self._interp.get_tensor(self._out['index'])[0]
        vec = np.asarray(vec, dtype=np.float32).reshape(-1)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec


def align_face(image_rgb, landmarks, box=None):
    """Landmark crop: rotate on the eye line, square crop sized from the eyes and
    mouth, eye midpoint at (0.5, EYE_Y). See THE ALIGNMENT CONTRACT.

    image_rgb : HxWx3 uint8
    landmarks : dict from face_recognition.face_landmarks() (eyes; mouth optional)
    box       : ignored (kept for the v1 call signature)
    """
    import math

    from PIL import Image

    a = np.mean(landmarks['left_eye'], axis=0)
    b = np.mean(landmarks['right_eye'], axis=0)
    if a[0] > b[0]:
        a, b = b, a                       # image-left eye first, mirror-safe
    top_lip = landmarks.get('top_lip')
    mouth = ((np.array(top_lip[0]) + np.array(top_lip[6])) / 2.0
             if top_lip and len(top_lip) >= 7 else None)   # dlib points 48 / 54

    angle_deg = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    centre = (a + b) / 2.0
    img = Image.fromarray(image_rgb).rotate(angle_deg, resample=Image.BILINEAR,
                                            center=(float(centre[0]), float(centre[1])))

    def rotated(p):
        v = p - centre
        r = math.radians(-angle_deg)
        return centre + np.array([v[0] * math.cos(r) - v[1] * math.sin(r),
                                  v[0] * math.sin(r) + v[1] * math.cos(r)])

    eye_dist = float(np.linalg.norm(rotated(b) - rotated(a)))
    side = eye_dist / EYE_DIST
    if mouth is not None:
        side = max(side, abs(float(rotated(mouth)[1] - centre[1])) / EYE_MOUTH)

    left = centre[0] - 0.5 * side
    top = centre[1] - EYE_Y * side
    crop = img.crop((int(round(left)), int(round(top)),
                     int(round(left + side)), int(round(top + side))))
    crop = crop.resize((INPUT_SIZE, INPUT_SIZE), Image.BILINEAR)
    return np.asarray(crop.convert('RGB'), dtype=np.uint8)


def embed_image_bytes(embedder, image_bytes):
    """JPEG/PNG bytes -> (vector, reason). vector is None when no usable face."""
    import io

    import face_recognition
    from PIL import Image, ImageOps

    # Honour EXIF rotation: a sideways phone JPEG is "no_face" to dlib.
    image = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert('RGB'),
                       dtype=np.uint8)
    boxes = face_recognition.face_locations(image)
    if not boxes:
        return None, 'no_face'
    # Largest face wins, same rule the device uses.
    box = max(boxes, key=lambda b: (b[2] - b[0]) * (b[1] - b[3]))
    marks = face_recognition.face_landmarks(image, [box])
    if not marks or 'left_eye' not in marks[0] or 'right_eye' not in marks[0]:
        return None, 'no_landmarks'
    return embedder.embed(align_face(image, marks[0], box)), 'ok'


def cosine(a, b):
    return float(np.dot(a, b))  # inputs are already L2-normalised


def _self_check():
    """Alignment maths only — runs without the model or face_recognition."""
    img = np.zeros((300, 300, 3), dtype=np.uint8)
    img[100:200, 100:200] = 255
    # eyes 40 px apart, mouth 46 px below the eye line, no tilt
    marks = {'left_eye': [(170, 120)], 'right_eye': [(130, 120)],
             'top_lip': [(135, 166), 0, 0, 0, 0, 0, (165, 166)]}
    out = align_face(img, marks)
    assert out.shape == (INPUT_SIZE, INPUT_SIZE, 3), out.shape
    assert out.dtype == np.uint8
    # side = max(40/0.31, 46/0.36) = 129; eye row lands at 0.46 of the crop
    # (mirror-safe: 'left_eye' given on the image-right on purpose above)
    v = np.array([3.0, 4.0], dtype=np.float32)
    v = v / np.linalg.norm(v)
    assert abs(cosine(v, v) - 1.0) < 1e-6
    print('mobileface self-check OK')


if __name__ == '__main__':
    _self_check()
