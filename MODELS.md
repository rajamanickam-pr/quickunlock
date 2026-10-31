# Bundled models

| File | Purpose | Source | License |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | Face detection (YuNet) | [opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | MIT |
| `face_recognition_sface_2021dec.onnx` | Face embeddings (SFace) | [opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) | Apache-2.0 |
| `speaker_resnet34_trunk.onnx`, `speaker_head.npz` | Speaker embeddings | WeSpeaker ResNet-34 (VoxCeleb), ONNX export from [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models) | Apache-2.0 code; model trained on VoxCeleb (CC BY 4.0) |

The speaker model is split in two by `tools/split_speaker_model.py` because
OpenCV 4.x cannot import its statistics-pooling ops: the convolutional trunk
runs in `cv2.dnn`, pooling + projection run in numpy. Output is identical
to the original model (max abs diff ~1e-6).
