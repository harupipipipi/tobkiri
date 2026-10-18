MediaPipe hand tracking assets
==============================

Defaultspack ships the following local-only hand tracking assets so the ambient
finger recording window can start from a clean checkout and from packaged
desktop installers without downloading model files at runtime.

Package:
- Source: @mediapipe/tasks-vision
- Version: 0.10.35
- License: Apache-2.0
- package-lock integrity: sha512-HOvadwVRE6JC+45nyYhmnywnr5h/J8KZvOeUNVOG9q/0875pZgItznFB9bRTvLc264YSJqiZ1NsIpCStJw/egg==

Model:
- Source: https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
- File: public/models/hand_landmarker.task
- SHA-256: fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1

WASM files:
- public/mediapipe/wasm/vision_wasm_internal.js: 11fdcbe35b15e222bd60f02c1be7e5f8dd8a73721a0a55cf8adcf38b88977b9e
- public/mediapipe/wasm/vision_wasm_internal.wasm: 6a5c64584c2ab61c763b6e204afbdbc7ce1caf7f5216187322bca8df94f646bc
- public/mediapipe/wasm/vision_wasm_module_internal.js: e23be0c990685926cc0a13a46936015527f36e95adf965250ea08d3b9fd28ef2
- public/mediapipe/wasm/vision_wasm_module_internal.wasm: 617b8e0248dbd27e9d7ece4218004eae4cefb499196d1bb4fa0e3fef21708756
- public/mediapipe/wasm/vision_wasm_nosimd_internal.js: df375e4da93bbc1078481da6e2e519fd55ea125a14a00379a9b7bb395fb56c80
- public/mediapipe/wasm/vision_wasm_nosimd_internal.wasm: 8a3092d34c79d3f57e6ba8592105e8a90f6b07c27891ffecd14cca428bfd3e31
