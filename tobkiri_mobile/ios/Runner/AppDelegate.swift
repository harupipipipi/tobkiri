import AVFoundation
import Flutter
import Security
import UniformTypeIdentifiers
import UIKit
import UserNotifications
import Vision

@main
@objc class AppDelegate: FlutterAppDelegate {
  private let secureStorage = RumiKeychainStorage()
  private var pendingQrScanResult: FlutterResult?
  private var pendingMediaPickerResult: FlutterResult?
  private var mediaPickerMaxBytes = 4 * 1024 * 1024

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)
    registerSecureStorageChannel()
    registerPreferencesChannel()
    registerUrlLauncherChannel()
    registerNotificationsChannel()
    registerQrScannerChannel()
    registerMediaPickerChannel()
    registerScreenshotChannel()
    registerImageTransformerChannel()
    registerOcrChannel()
    UNUserNotificationCenter.current().delegate = self
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  private func registerSecureStorageChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/secure_storage",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { [secureStorage] call, result in
      guard let args = call.arguments as? [String: Any],
            let key = args["key"] as? String,
            !key.isEmpty else {
        result(FlutterError(code: "invalid_args", message: "Missing key", details: nil))
        return
      }

      switch call.method {
      case "read":
        do {
          result(try secureStorage.read(key: key))
        } catch {
          result(FlutterError(code: "read_failed", message: error.localizedDescription, details: nil))
        }
      case "write":
        guard let value = args["value"] as? String else {
          result(FlutterError(code: "invalid_args", message: "Missing value", details: nil))
          return
        }
        do {
          try secureStorage.write(key: key, value: value)
          result(nil)
        } catch {
          result(FlutterError(code: "write_failed", message: error.localizedDescription, details: nil))
        }
      case "delete":
        do {
          try secureStorage.delete(key: key)
          result(nil)
        } catch {
          result(FlutterError(code: "delete_failed", message: error.localizedDescription, details: nil))
        }
      default:
        result(FlutterMethodNotImplemented)
      }
    }
  }

  private func registerPreferencesChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/preferences",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { call, result in
      guard let args = call.arguments as? [String: Any],
            let key = args["key"] as? String,
            !key.isEmpty else {
        result(FlutterError(code: "invalid_args", message: "Missing key", details: nil))
        return
      }

      switch call.method {
      case "read":
        result(UserDefaults.standard.string(forKey: key))
      case "write":
        guard let value = args["value"] as? String else {
          result(FlutterError(code: "invalid_args", message: "Missing value", details: nil))
          return
        }
        UserDefaults.standard.set(value, forKey: key)
        result(nil)
      case "delete":
        UserDefaults.standard.removeObject(forKey: key)
        result(nil)
      default:
        result(FlutterMethodNotImplemented)
      }
    }
  }

  private func registerUrlLauncherChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/url_launcher",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { call, result in
      guard call.method == "open" else {
        result(FlutterMethodNotImplemented)
        return
      }
      guard let args = call.arguments as? [String: Any],
            let raw = args["url"] as? String,
            let url = URL(string: raw) else {
        result(false)
        return
      }
      UIApplication.shared.open(url, options: [:]) { ok in
        result(ok)
      }
    }
  }

  private func registerNotificationsChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/notifications",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { call, result in
      switch call.method {
      case "requestAuthorization":
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
          DispatchQueue.main.async {
            result(granted)
          }
        }
      case "showPcTaskFinished":
        let args = call.arguments as? [String: Any]
        let title = (args?["title"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        let body = (args?["body"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.showNotification(
          title: title?.isEmpty == false ? title! : "PCタスクが完了しました",
          body: body?.isEmpty == false ? body! : "PCのタスクが完了しました",
          result: result
        )
      default:
        result(FlutterMethodNotImplemented)
      }
    }
  }

  private func showNotification(title: String, body: String, result: @escaping FlutterResult) {
    UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
      guard granted, error == nil else {
        DispatchQueue.main.async {
          result(false)
        }
        return
      }

      let content = UNMutableNotificationContent()
      content.title = title
      content.body = body
      content.sound = .default

      let request = UNNotificationRequest(
        identifier: "rumi-pc-task-finished-\(UUID().uuidString)",
        content: content,
        trigger: UNTimeIntervalNotificationTrigger(timeInterval: 0.1, repeats: false)
      )
      UNUserNotificationCenter.current().add(request) { error in
        DispatchQueue.main.async {
          result(error == nil)
        }
      }
    }
  }

  private func registerQrScannerChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/qr_scanner",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { [weak self] call, result in
      guard let self else {
        result(false)
        return
      }
      guard call.method == "scan" else {
        result(FlutterMethodNotImplemented)
        return
      }
      guard pendingQrScanResult == nil else {
        result(FlutterError(code: "scan_in_progress", message: "QR scanner is already open", details: nil))
        return
      }
      pendingQrScanResult = result
      let scanner = RumiQrScannerViewController { [weak self] value in
        guard let self else { return }
        let pending = pendingQrScanResult
        pendingQrScanResult = nil
        pending?(value)
      }
      scanner.modalPresentationStyle = .fullScreen
      controller.present(scanner, animated: true)
    }
  }

  private func registerMediaPickerChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/media_picker",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { [weak self] call, result in
      guard let self else {
        result([
          "error_code": "not_available",
          "message": "App delegate is not available",
        ])
        return
      }
      guard call.method == "pick" else {
        result(FlutterMethodNotImplemented)
        return
      }
      guard pendingMediaPickerResult == nil else {
        result(mediaPickerError(code: "pick_in_progress", message: "Media picker is already open"))
        return
      }
      let args = call.arguments as? [String: Any]
      let kind = ((args?["kind"] as? String) ?? "file").trimmingCharacters(in: .whitespacesAndNewlines)
      let maxBytes = max(1, min((args?["max_bytes"] as? NSNumber)?.intValue ?? 4 * 1024 * 1024, 8 * 1024 * 1024))
      pendingMediaPickerResult = result
      mediaPickerMaxBytes = maxBytes

      let picker = UIDocumentPickerViewController(
        forOpeningContentTypes: mediaPickerContentTypes(kind: kind),
        asCopy: true
      )
      picker.delegate = self
      picker.allowsMultipleSelection = false
      controller.present(picker, animated: true)
    }
  }

  private func mediaPickerContentTypes(kind: String) -> [UTType] {
    switch kind.lowercased() {
    case "image":
      return [.image]
    case "audio":
      return [.audio]
    default:
      return [.item]
    }
  }

  private func finishMediaPicker(_ value: Any?) {
    let result = pendingMediaPickerResult
    pendingMediaPickerResult = nil
    result?(value)
  }

  private func mediaPickerError(code: String, message: String) -> [String: Any] {
    [
      "error_code": code,
      "message": message,
    ]
  }

  private func selectedMediaPayload(url: URL) -> [String: Any] {
    let access = url.startAccessingSecurityScopedResource()
    defer {
      if access {
        url.stopAccessingSecurityScopedResource()
      }
    }

    do {
      let values = try? url.resourceValues(forKeys: [.fileSizeKey, .nameKey, .contentTypeKey])
      if let knownSize = values?.fileSize, knownSize > mediaPickerMaxBytes {
        return mediaPickerError(
          code: "too_large",
          message: "Selected file is larger than max_bytes"
        )
      }
      let data = try Data(contentsOf: url)
      if data.count > mediaPickerMaxBytes {
        return mediaPickerError(
          code: "too_large",
          message: "Selected file is larger than max_bytes"
        )
      }
      let fileName = values?.name ?? url.lastPathComponent
      let mimeType = values?.contentType?.preferredMIMEType ?? "application/octet-stream"
      return [
        "name": fileName.isEmpty ? "selected-file" : fileName,
        "mime_type": mimeType,
        "size": data.count,
        "base64": data.base64EncodedString(),
      ]
    } catch {
      return mediaPickerError(code: "read_failed", message: error.localizedDescription)
    }
  }

  private func registerScreenshotChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/screenshot",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { [weak self] call, result in
      guard let self else {
        result([
          "error_code": "not_available",
          "message": "App delegate is not available",
        ])
        return
      }
      guard call.method == "capture" else {
        result(FlutterMethodNotImplemented)
        return
      }
      let args = call.arguments as? [String: Any]
      let maxBytes = max(1, min((args?["max_bytes"] as? NSNumber)?.intValue ?? 6 * 1024 * 1024, 12 * 1024 * 1024))
      let maxDimension = max(320, min((args?["max_dimension"] as? NSNumber)?.intValue ?? 1600, 4096))
      result(captureScreenshotPayload(maxBytes: maxBytes, maxDimension: maxDimension))
    }
  }

  private func captureScreenshotPayload(maxBytes: Int, maxDimension: Int) -> [String: Any] {
    guard let targetWindow = window else {
      return mediaPickerError(code: "not_available", message: "No app window is available")
    }
    let bounds = targetWindow.bounds
    guard bounds.width > 0, bounds.height > 0 else {
      return mediaPickerError(code: "not_available", message: "App window has no drawable size")
    }

    let screenScale = max(1, targetWindow.screen.scale)
    let pixelWidth = bounds.width * screenScale
    let pixelHeight = bounds.height * screenScale
    let longest = max(pixelWidth, pixelHeight)
    let resizeScale = min(1, CGFloat(maxDimension) / max(1, longest))
    let outputSize = CGSize(
      width: max(1, bounds.width * resizeScale),
      height: max(1, bounds.height * resizeScale)
    )

    let format = UIGraphicsImageRendererFormat()
    format.scale = screenScale
    let renderer = UIGraphicsImageRenderer(size: outputSize, format: format)
    let image = renderer.image { _ in
      targetWindow.drawHierarchy(
        in: CGRect(origin: .zero, size: outputSize),
        afterScreenUpdates: true
      )
    }
    guard let data = image.pngData() else {
      return mediaPickerError(code: "encode_failed", message: "Could not encode screenshot as PNG")
    }
    if data.count > maxBytes {
      return mediaPickerError(code: "too_large", message: "Captured screenshot is larger than max_bytes")
    }

    return [
      "mime_type": "image/png",
      "size": data.count,
      "width": Int(image.size.width * image.scale),
      "height": Int(image.size.height * image.scale),
      "base64": data.base64EncodedString(),
    ]
  }

  private func registerImageTransformerChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/image_transformer",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { call, result in
      guard call.method == "transform" else {
        result(FlutterMethodNotImplemented)
        return
      }
      let args = call.arguments as? [String: Any]
      result(self.transformImagePayload(args: args))
    }
  }

  private func registerOcrChannel() {
    guard let controller = window?.rootViewController as? FlutterViewController else {
      return
    }
    let channel = FlutterMethodChannel(
      name: "ai.rumi.remote/ocr",
      binaryMessenger: controller.binaryMessenger
    )
    channel.setMethodCallHandler { call, result in
      guard call.method == "recognize" else {
        result(FlutterMethodNotImplemented)
        return
      }
      let args = call.arguments as? [String: Any]
      self.recognizeTextPayload(args: args, result: result)
    }
  }

  private func transformImagePayload(args: [String: Any]?) -> [String: Any] {
    guard let rawBase64 = (args?["base64"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
          !rawBase64.isEmpty,
          let inputData = Data(base64Encoded: rawBase64),
          let image = UIImage(data: inputData) else {
      return mediaPickerError(code: "invalid_image", message: "Image base64 could not be decoded")
    }

    let maxBytes = max(1, min((args?["max_bytes"] as? NSNumber)?.intValue ?? 8 * 1024 * 1024, 16 * 1024 * 1024))
    let format = ((args?["format"] as? String) ?? "png").lowercased()
    let quality = max(1, min((args?["quality"] as? NSNumber)?.intValue ?? 90, 100))
    let targetSize = imageTransformTargetSize(
      image: image,
      maxWidth: (args?["max_width"] as? NSNumber)?.intValue,
      maxHeight: (args?["max_height"] as? NSNumber)?.intValue
    )
    let rendered = renderImage(image, targetSize: targetSize)
    let encoded: Data?
    let mimeType: String
    if format == "jpeg" || format == "jpg" {
      encoded = rendered.jpegData(compressionQuality: CGFloat(quality) / 100.0)
      mimeType = "image/jpeg"
    } else {
      encoded = rendered.pngData()
      mimeType = "image/png"
    }
    guard let data = encoded else {
      return mediaPickerError(code: "encode_failed", message: "Could not encode transformed image")
    }
    if data.count > maxBytes {
      return mediaPickerError(code: "too_large", message: "Transformed image is larger than max_bytes")
    }
    return [
      "mime_type": mimeType,
      "size": data.count,
      "width": Int(rendered.size.width * rendered.scale),
      "height": Int(rendered.size.height * rendered.scale),
      "base64": data.base64EncodedString(),
    ]
  }

  private func imageTransformTargetSize(image: UIImage, maxWidth: Int?, maxHeight: Int?) -> CGSize {
    let sourceWidth = max(1, image.size.width)
    let sourceHeight = max(1, image.size.height)
    let widthLimit = CGFloat(max(1, maxWidth ?? Int(sourceWidth)))
    let heightLimit = CGFloat(max(1, maxHeight ?? Int(sourceHeight)))
    let scale = min(1, widthLimit / sourceWidth, heightLimit / sourceHeight)
    return CGSize(
      width: max(1, floor(sourceWidth * scale)),
      height: max(1, floor(sourceHeight * scale))
    )
  }

  private func renderImage(_ image: UIImage, targetSize: CGSize) -> UIImage {
    if Int(targetSize.width) == Int(image.size.width),
       Int(targetSize.height) == Int(image.size.height) {
      return image
    }
    let format = UIGraphicsImageRendererFormat()
    format.scale = 1
    let renderer = UIGraphicsImageRenderer(size: targetSize, format: format)
    return renderer.image { _ in
      image.draw(in: CGRect(origin: .zero, size: targetSize))
    }
  }

  private func recognizeTextPayload(args: [String: Any]?, result: @escaping FlutterResult) {
    guard #available(iOS 13.0, *) else {
      result(mediaPickerError(code: "not_available", message: "iOS Vision OCR requires iOS 13 or newer"))
      return
    }
    guard let rawBase64 = (args?["base64"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
          !rawBase64.isEmpty,
          let imageData = Data(base64Encoded: rawBase64) else {
      result(mediaPickerError(code: "invalid_image", message: "Image base64 could not be decoded"))
      return
    }
    let maxBytes = max(1, min((args?["max_bytes"] as? NSNumber)?.intValue ?? 8 * 1024 * 1024, 16 * 1024 * 1024))
    guard imageData.count <= maxBytes else {
      result(mediaPickerError(code: "too_large", message: "Image is larger than max_bytes"))
      return
    }
    guard let image = UIImage(data: imageData), let cgImage = image.cgImage else {
      result(mediaPickerError(code: "invalid_image", message: "Image data could not be decoded"))
      return
    }
    let languageHint = (args?["language_hint"] as? String)?
      .trimmingCharacters(in: .whitespacesAndNewlines)

    DispatchQueue.global(qos: .userInitiated).async {
      var requestError: Error?
      var blocks: [[String: Any]] = []
      let request = VNRecognizeTextRequest { request, error in
        requestError = error
        guard error == nil,
              let observations = request.results as? [VNRecognizedTextObservation] else {
          return
        }
        blocks = observations.compactMap { observation in
          guard let candidate = observation.topCandidates(1).first else {
            return nil
          }
          let box = observation.boundingBox
          return [
            "text": candidate.string,
            "confidence": Double(candidate.confidence),
            "bounding_box": [
              "x": Double(box.origin.x),
              "y": Double(box.origin.y),
              "width": Double(box.size.width),
              "height": Double(box.size.height),
              "unit": "normalized",
            ],
          ]
        }
      }
      request.recognitionLevel = .accurate
      request.usesLanguageCorrection = true
      if let languageHint, !languageHint.isEmpty {
        request.recognitionLanguages = [languageHint]
      }

      do {
        try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
        let text = blocks
          .compactMap { $0["text"] as? String }
          .joined(separator: "\n")
        var payload: [String: Any] = [
          "text": text,
          "blocks": blocks,
        ]
        if let languageHint, !languageHint.isEmpty {
          payload["language_code"] = languageHint
        }
        DispatchQueue.main.async {
          result(payload)
        }
      } catch {
        let message = requestError?.localizedDescription ?? error.localizedDescription
        DispatchQueue.main.async {
          result(self.mediaPickerError(code: "ocr_failed", message: message))
        }
      }
    }
  }

  override func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    willPresent notification: UNNotification,
    withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
  ) {
    if #available(iOS 14.0, *) {
      completionHandler([.banner, .list, .sound])
    } else {
      completionHandler([.alert, .sound])
    }
  }
}

extension AppDelegate: UIDocumentPickerDelegate {
  func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
    guard let url = urls.first else {
      finishMediaPicker(nil)
      return
    }
    finishMediaPicker(selectedMediaPayload(url: url))
  }

  func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
    finishMediaPicker(nil)
  }
}

private final class RumiKeychainStorage {
  private let service = "ai.rumi.remote.secure_storage"

  func read(key: String) throws -> String? {
    let current = try readValue(query: baseQuery(key: key))
    if current != nil || skipsLegacyFallback(key: key) {
      return current
    }
    return try readValue(query: legacyQuery(key: key))
  }

  private func readValue(query base: [CFString: Any]) throws -> String? {
    var query = base
    query[kSecReturnData] = true
    query[kSecMatchLimit] = kSecMatchLimitOne

    var item: CFTypeRef?
    let status = SecItemCopyMatching(query as CFDictionary, &item)
    if status == errSecItemNotFound {
      return nil
    }
    guard status == errSecSuccess else {
      throw keychainError(status)
    }
    guard let data = item as? Data else {
      return nil
    }
    return String(data: data, encoding: .utf8)
  }

  func write(key: String, value: String) throws {
    let data = Data(value.utf8)
    let query = baseQuery(key: key)
    let update: [CFString: Any] = [
      kSecValueData: data,
    ]
    let updateStatus = SecItemUpdate(query as CFDictionary, update as CFDictionary)
    if updateStatus == errSecSuccess {
      return
    }
    guard updateStatus == errSecItemNotFound else {
      throw keychainError(updateStatus)
    }

    var addQuery = query
    addQuery[kSecValueData] = data
    addQuery[kSecAttrAccessible] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
    guard addStatus == errSecSuccess else {
      throw keychainError(addStatus)
    }
  }

  func delete(key: String) throws {
    try deleteValue(query: baseQuery(key: key))
    try deleteValue(query: legacyQuery(key: key))
  }

  private func deleteValue(query: [CFString: Any]) throws {
    let status = SecItemDelete(query as CFDictionary)
    if status == errSecSuccess || status == errSecItemNotFound {
      return
    }
    throw keychainError(status)
  }

  private func baseQuery(key: String) -> [CFString: Any] {
    [
      kSecClass: kSecClassGenericPassword,
      kSecAttrService: service,
      kSecAttrAccount: key,
    ]
  }

  private func legacyQuery(key: String) -> [CFString: Any] {
    [
      kSecClass: kSecClassGenericPassword,
      kSecAttrAccount: key,
    ]
  }

  private func skipsLegacyFallback(key: String) -> Bool {
    switch key {
    case "rumi.paired_device.v1",
         "rumi.paired_devices.v1",
         "rumi.pc_connection.v1":
      return true
    default:
      return false
    }
  }

  private func keychainError(_ status: OSStatus) -> NSError {
    let message = SecCopyErrorMessageString(status, nil) as String? ?? "Keychain error \(status)"
    return NSError(
      domain: "ai.rumi.remote.keychain",
      code: Int(status),
      userInfo: [NSLocalizedDescriptionKey: message]
    )
  }
}

private final class RumiQrScannerViewController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
  private let queue = DispatchQueue(label: "ai.rumi.remote.qr_scanner")
  private let completion: (String?) -> Void
  private var session: AVCaptureSession?
  private var previewLayer: AVCaptureVideoPreviewLayer?
  private var position: AVCaptureDevice.Position = .back
  private var lastValue: String?
  private var finished = false

  init(completion: @escaping (String?) -> Void) {
    self.completion = completion
    super.init(nibName: nil, bundle: nil)
  }

  required init?(coder: NSCoder) {
    fatalError("init(coder:) has not been implemented")
  }

  override func viewDidLoad() {
    super.viewDidLoad()
    view.backgroundColor = .black
    buildOverlay()
    start()
  }

  override func viewDidLayoutSubviews() {
    super.viewDidLayoutSubviews()
    previewLayer?.frame = view.bounds
  }

  override func viewWillDisappear(_ animated: Bool) {
    super.viewWillDisappear(animated)
    stop()
  }

  deinit {
    stop()
    if !finished {
      completion(nil)
    }
  }

  private func start() {
    switch AVCaptureDevice.authorizationStatus(for: .video) {
    case .authorized:
      configureAndStart()
    case .notDetermined:
      AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
        if granted {
          self?.configureAndStart()
        } else {
          DispatchQueue.main.async {
            self?.finish(nil)
          }
        }
      }
    default:
      finish(nil)
    }
  }

  private func stop() {
    let currentSession = session
    session = nil
    queue.async {
      currentSession?.stopRunning()
    }
  }

  @objc private func switchCamera() {
    position = position == .back ? .front : .back
    stop()
    configureAndStart()
  }

  @objc private func toggleTorch() {
    guard let device = currentVideoDevice(), device.hasTorch else {
      return
    }
    do {
      try device.lockForConfiguration()
      device.torchMode = device.torchMode == .on ? .off : .on
      device.unlockForConfiguration()
    } catch {
    }
  }

  @objc private func cancel() {
    finish(nil)
  }

  private func configureAndStart() {
    queue.async { [weak self] in
      guard let self else { return }
      let nextSession = AVCaptureSession()
      nextSession.beginConfiguration()
      nextSession.sessionPreset = .high

      guard let device = camera(position: position),
            let input = try? AVCaptureDeviceInput(device: device),
            nextSession.canAddInput(input) else {
        nextSession.commitConfiguration()
        return
      }
      nextSession.addInput(input)

      let output = AVCaptureMetadataOutput()
      guard nextSession.canAddOutput(output) else {
        nextSession.commitConfiguration()
        return
      }
      nextSession.addOutput(output)
      output.setMetadataObjectsDelegate(self, queue: DispatchQueue.main)
      output.metadataObjectTypes = [.qr]
      nextSession.commitConfiguration()

      session = nextSession
      DispatchQueue.main.async { [weak self] in
        guard let self else { return }
        let previewLayer = AVCaptureVideoPreviewLayer(session: nextSession)
        previewLayer.videoGravity = .resizeAspectFill
        previewLayer.frame = view.bounds
        self.previewLayer?.removeFromSuperlayer()
        self.previewLayer = previewLayer
        view.layer.insertSublayer(previewLayer, at: 0)
      }
      nextSession.startRunning()
    }
  }

  private func currentVideoDevice() -> AVCaptureDevice? {
    session?.inputs
      .compactMap { $0 as? AVCaptureDeviceInput }
      .first { $0.device.hasMediaType(.video) }?
      .device
  }

  private func camera(position: AVCaptureDevice.Position) -> AVCaptureDevice? {
    AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: position)
      ?? AVCaptureDevice.default(for: .video)
  }

  func metadataOutput(
    _ output: AVCaptureMetadataOutput,
    didOutput metadataObjects: [AVMetadataObject],
    from connection: AVCaptureConnection
  ) {
    guard let value = metadataObjects
      .compactMap({ $0 as? AVMetadataMachineReadableCodeObject })
      .first(where: { $0.type == .qr })?
      .stringValue?
      .trimmingCharacters(in: .whitespacesAndNewlines),
      !value.isEmpty,
      value != lastValue else {
      return
    }
    lastValue = value
    finish(value)
  }

  private func finish(_ value: String?) {
    if finished {
      return
    }
    finished = true
    stop()
    dismiss(animated: true) { [completion] in
      completion(value)
    }
  }

  private func buildOverlay() {
    let close = UIButton(type: .system)
    close.translatesAutoresizingMaskIntoConstraints = false
    close.setTitle("閉じる", for: .normal)
    close.tintColor = .white
    close.backgroundColor = UIColor.black.withAlphaComponent(0.45)
    close.layer.cornerRadius = 18
    close.contentEdgeInsets = UIEdgeInsets(top: 8, left: 14, bottom: 8, right: 14)
    close.addTarget(self, action: #selector(cancel), for: .touchUpInside)
    view.addSubview(close)

    let guide = UIView()
    guide.translatesAutoresizingMaskIntoConstraints = false
    guide.layer.borderColor = UIColor.white.cgColor
    guide.layer.borderWidth = 3
    guide.layer.cornerRadius = 18
    view.addSubview(guide)

    let hint = UILabel()
    hint.translatesAutoresizingMaskIntoConstraints = false
    hint.text = "QRを枠の中に入れてください"
    hint.textColor = .white
    hint.font = .systemFont(ofSize: 15, weight: .semibold)
    hint.textAlignment = .center
    hint.backgroundColor = UIColor.black.withAlphaComponent(0.45)
    hint.layer.cornerRadius = 16
    hint.clipsToBounds = true
    view.addSubview(hint)

    let torch = UIButton(type: .system)
    torch.translatesAutoresizingMaskIntoConstraints = false
    torch.setTitle("ライト", for: .normal)
    torch.tintColor = .white
    torch.backgroundColor = UIColor.black.withAlphaComponent(0.45)
    torch.layer.cornerRadius = 18
    torch.contentEdgeInsets = UIEdgeInsets(top: 8, left: 14, bottom: 8, right: 14)
    torch.addTarget(self, action: #selector(toggleTorch), for: .touchUpInside)
    view.addSubview(torch)

    let camera = UIButton(type: .system)
    camera.translatesAutoresizingMaskIntoConstraints = false
    camera.setTitle("切替", for: .normal)
    camera.tintColor = .white
    camera.backgroundColor = UIColor.black.withAlphaComponent(0.45)
    camera.layer.cornerRadius = 18
    camera.contentEdgeInsets = UIEdgeInsets(top: 8, left: 14, bottom: 8, right: 14)
    camera.addTarget(self, action: #selector(switchCamera), for: .touchUpInside)
    view.addSubview(camera)

    NSLayoutConstraint.activate([
      close.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor, constant: 16),
      close.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),

      guide.centerXAnchor.constraint(equalTo: view.centerXAnchor),
      guide.centerYAnchor.constraint(equalTo: view.centerYAnchor),
      guide.widthAnchor.constraint(equalToConstant: 250),
      guide.heightAnchor.constraint(equalTo: guide.widthAnchor),

      hint.centerXAnchor.constraint(equalTo: view.centerXAnchor),
      hint.topAnchor.constraint(equalTo: guide.bottomAnchor, constant: 22),
      hint.widthAnchor.constraint(lessThanOrEqualTo: view.widthAnchor, constant: -48),
      hint.heightAnchor.constraint(greaterThanOrEqualToConstant: 40),

      torch.trailingAnchor.constraint(equalTo: view.centerXAnchor, constant: -8),
      torch.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -24),

      camera.leadingAnchor.constraint(equalTo: view.centerXAnchor, constant: 8),
      camera.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -24),
    ])
  }
}
