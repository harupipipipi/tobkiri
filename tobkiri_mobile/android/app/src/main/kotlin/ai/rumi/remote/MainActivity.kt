package ai.rumi.remote

import android.Manifest
import android.app.Activity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.net.Uri
import android.os.Build
import android.provider.OpenableColumns
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class MainActivity : FlutterActivity() {
    private var pendingNotificationPermissionResult: ((Boolean) -> Unit)? = null
    private var pendingMediaPickerResult: MethodChannel.Result? = null
    private var mediaPickerMaxBytes: Long = DEFAULT_MEDIA_PICK_MAX_BYTES

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        registerSecureStorageChannel(flutterEngine)
        registerPreferencesChannel(flutterEngine)
        registerUrlLauncherChannel(flutterEngine)
        registerNotificationsChannel(flutterEngine)
        registerMediaPickerChannel(flutterEngine)
        registerScreenshotChannel(flutterEngine)
        registerImageTransformerChannel(flutterEngine)
        registerOcrChannel(flutterEngine)
    }

    private fun registerSecureStorageChannel(flutterEngine: FlutterEngine) {
        val storage = RumiSecureStorage(this)
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "ai.rumi.remote/secure_storage",
        ).setMethodCallHandler { call, result ->
            val args = call.arguments as? Map<*, *>
            val key = args?.get("key") as? String
            if (key.isNullOrBlank()) {
                result.error("invalid_args", "Missing key", null)
                return@setMethodCallHandler
            }

            try {
                when (call.method) {
                    "read" -> result.success(storage.read(key))
                    "write" -> {
                        val value = args["value"] as? String
                        if (value == null) {
                            result.error("invalid_args", "Missing value", null)
                        } else {
                            storage.write(key, value)
                            result.success(null)
                        }
                    }
                    "delete" -> {
                        storage.delete(key)
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            } catch (e: Exception) {
                result.error("secure_storage_failed", e.message, null)
            }
        }
    }

    private fun registerPreferencesChannel(flutterEngine: FlutterEngine) {
        val prefs = getSharedPreferences("rumi_preferences", Context.MODE_PRIVATE)
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "ai.rumi.remote/preferences",
        ).setMethodCallHandler { call, result ->
            val args = call.arguments as? Map<*, *>
            val key = args?.get("key") as? String
            if (key.isNullOrBlank()) {
                result.error("invalid_args", "Missing key", null)
                return@setMethodCallHandler
            }

            when (call.method) {
                "read" -> result.success(prefs.getString(key, null))
                "write" -> {
                    val value = args["value"] as? String
                    if (value == null) {
                        result.error("invalid_args", "Missing value", null)
                    } else {
                        prefs.edit().putString(key, value).apply()
                        result.success(null)
                    }
                }
                "delete" -> {
                    prefs.edit().remove(key).apply()
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        }
    }

    private fun registerUrlLauncherChannel(flutterEngine: FlutterEngine) {
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "ai.rumi.remote/url_launcher",
        ).setMethodCallHandler { call, result ->
            if (call.method != "open") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            val args = call.arguments as? Map<*, *>
            val raw = args?.get("url") as? String
            if (raw.isNullOrBlank()) {
                result.success(false)
                return@setMethodCallHandler
            }
            try {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(raw)))
                result.success(true)
            } catch (e: Exception) {
                result.success(false)
            }
        }
    }

    private fun registerNotificationsChannel(flutterEngine: FlutterEngine) {
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "ai.rumi.remote/notifications",
        ).setMethodCallHandler { call, result ->
            when (call.method) {
                "requestAuthorization" -> {
                    requestNotificationAuthorization { granted ->
                        result.success(granted)
                    }
                }
                "showPcTaskFinished" -> {
                    val args = call.arguments as? Map<*, *>
                    val title = (args?.get("title") as? String)
                        ?.trim()
                        ?.takeIf { it.isNotEmpty() }
                        ?: "PCタスクが完了しました"
                    val body = (args?.get("body") as? String)
                        ?.trim()
                        ?.takeIf { it.isNotEmpty() }
                        ?: "PCのタスクが完了しました"
                    requestNotificationAuthorization { granted ->
                        if (!granted) {
                            result.success(false)
                        } else {
                            showPcTaskFinishedNotification(title, body)
                            result.success(true)
                        }
                    }
                }
                else -> result.notImplemented()
            }
        }
    }

    private fun registerMediaPickerChannel(flutterEngine: FlutterEngine) {
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "ai.rumi.remote/media_picker",
        ).setMethodCallHandler { call, result ->
            if (call.method != "pick") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            if (pendingMediaPickerResult != null) {
                result.success(mediaPickerError("pick_in_progress", "Media picker is already open"))
                return@setMethodCallHandler
            }
            val args = call.arguments as? Map<*, *>
            val kind = (args?.get("kind") as? String)?.trim()?.lowercase() ?: "file"
            mediaPickerMaxBytes = ((args?.get("max_bytes") as? Number)?.toLong()
                ?: DEFAULT_MEDIA_PICK_MAX_BYTES)
                .coerceIn(1L, HARD_MEDIA_PICK_MAX_BYTES)
            pendingMediaPickerResult = result

            val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                addCategory(Intent.CATEGORY_OPENABLE)
                type = when (kind) {
                    "image" -> "image/*"
                    "audio" -> "audio/*"
                    else -> "*/*"
                }
            }
            try {
                startActivityForResult(intent, REQUEST_PICK_MEDIA)
            } catch (e: Exception) {
                pendingMediaPickerResult = null
                result.success(mediaPickerError("not_available", e.message ?: "Media picker is not available"))
            }
        }
    }

    private fun registerScreenshotChannel(flutterEngine: FlutterEngine) {
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "ai.rumi.remote/screenshot",
        ).setMethodCallHandler { call, result ->
            if (call.method != "capture") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            val args = call.arguments as? Map<*, *>
            val maxBytes = ((args?.get("max_bytes") as? Number)?.toLong()
                ?: DEFAULT_SCREENSHOT_MAX_BYTES)
                .coerceIn(1L, HARD_SCREENSHOT_MAX_BYTES)
            val maxDimension = ((args?.get("max_dimension") as? Number)?.toInt()
                ?: DEFAULT_SCREENSHOT_MAX_DIMENSION)
                .coerceIn(320, HARD_SCREENSHOT_MAX_DIMENSION)
            result.success(captureScreenshotPayload(maxBytes, maxDimension))
        }
    }

    private fun registerImageTransformerChannel(flutterEngine: FlutterEngine) {
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "ai.rumi.remote/image_transformer",
        ).setMethodCallHandler { call, result ->
            if (call.method != "transform") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            val args = call.arguments as? Map<*, *>
            result.success(transformImagePayload(args))
        }
    }

    private fun registerOcrChannel(flutterEngine: FlutterEngine) {
        MethodChannel(
            flutterEngine.dartExecutor.binaryMessenger,
            "ai.rumi.remote/ocr",
        ).setMethodCallHandler { call, result ->
            if (call.method != "recognize") {
                result.notImplemented()
                return@setMethodCallHandler
            }
            val args = call.arguments as? Map<*, *>
            recognizeTextPayload(args, result)
        }
    }

    private fun requestNotificationAuthorization(callback: (Boolean) -> Unit) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            callback(true)
            return
        }
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) {
            callback(true)
            return
        }
        if (pendingNotificationPermissionResult != null) {
            callback(false)
            return
        }
        pendingNotificationPermissionResult = callback
        requestPermissions(
            arrayOf(Manifest.permission.POST_NOTIFICATIONS),
            REQUEST_POST_NOTIFICATIONS,
        )
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_POST_NOTIFICATIONS) return
        val granted = grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED
        val callback = pendingNotificationPermissionResult
        pendingNotificationPermissionResult = null
        callback?.invoke(granted)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQUEST_PICK_MEDIA) return
        val result = pendingMediaPickerResult ?: return
        pendingMediaPickerResult = null
        if (resultCode != Activity.RESULT_OK) {
            result.success(null)
            return
        }
        val uri = data?.data
        if (uri == null) {
            result.success(null)
            return
        }
        result.success(readPickedMediaPayload(uri))
    }

    private fun showPcTaskFinishedNotification(title: String, body: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                NOTIFICATION_CHANNEL_ID,
                "Rumi PC Tasks",
                NotificationManager.IMPORTANCE_DEFAULT,
            )
            manager.createNotificationChannel(channel)
        }

        val intent = packageManager.getLaunchIntentForPackage(packageName)
            ?: Intent(this, MainActivity::class.java)
        intent.flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or immutablePendingIntentFlag(),
        )
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
        } else {
            Notification.Builder(this)
        }
        val notification = builder
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .build()
        manager.notify((System.currentTimeMillis() % Int.MAX_VALUE).toInt(), notification)
    }

    private fun readPickedMediaPayload(uri: Uri): Map<String, Any> {
        return try {
            val knownSize = queryOpenableSize(uri)
            if (knownSize != null && knownSize > mediaPickerMaxBytes) {
                return mediaPickerError("too_large", "Selected file is larger than max_bytes")
            }
            val bytes = readUriBytes(uri, mediaPickerMaxBytes)
                ?: return mediaPickerError("read_failed", "Could not read selected file")
            mapOf(
                "name" to (queryOpenableName(uri) ?: uri.lastPathSegment ?: "selected-file"),
                "mime_type" to (contentResolver.getType(uri) ?: "application/octet-stream"),
                "size" to bytes.size,
                "base64" to Base64.encodeToString(bytes, Base64.NO_WRAP),
            )
        } catch (tooLarge: MediaPickerTooLargeException) {
            mediaPickerError("too_large", "Selected file is larger than max_bytes")
        } catch (e: Exception) {
            mediaPickerError("read_failed", e.message ?: "Could not read selected file")
        }
    }

    private fun readUriBytes(uri: Uri, maxBytes: Long): ByteArray? {
        val stream = contentResolver.openInputStream(uri) ?: return null
        stream.use { input ->
            val output = ByteArrayOutputStream()
            val buffer = ByteArray(8192)
            var total = 0L
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                total += read.toLong()
                if (total > maxBytes) throw MediaPickerTooLargeException()
                output.write(buffer, 0, read)
            }
            return output.toByteArray()
        }
    }

    private fun queryOpenableName(uri: Uri): String? {
        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (index >= 0 && cursor.moveToFirst()) {
                return cursor.getString(index)
            }
        }
        return null
    }

    private fun queryOpenableSize(uri: Uri): Long? {
        contentResolver.query(uri, null, null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(OpenableColumns.SIZE)
            if (index >= 0 && cursor.moveToFirst() && !cursor.isNull(index)) {
                return cursor.getLong(index)
            }
        }
        return null
    }

    private fun mediaPickerError(code: String, message: String): Map<String, Any> {
        return mapOf(
            "error_code" to code,
            "message" to message,
        )
    }

    private fun captureScreenshotPayload(maxBytes: Long, maxDimension: Int): Map<String, Any> {
        return try {
            val view = window?.decorView?.rootView
                ?: return mediaPickerError("not_available", "No app view is available")
            val width = view.width
            val height = view.height
            if (width <= 0 || height <= 0) {
                return mediaPickerError("not_available", "App view has no drawable size")
            }
            val longest = maxOf(width, height).coerceAtLeast(1)
            val scale = minOf(1f, maxDimension.toFloat() / longest.toFloat())
            val outputWidth = maxOf(1, (width * scale).toInt())
            val outputHeight = maxOf(1, (height * scale).toInt())
            val bitmap = Bitmap.createBitmap(outputWidth, outputHeight, Bitmap.Config.ARGB_8888)
            val canvas = Canvas(bitmap)
            canvas.scale(scale, scale)
            view.draw(canvas)
            val output = ByteArrayOutputStream()
            bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)
            bitmap.recycle()
            val bytes = output.toByteArray()
            if (bytes.size.toLong() > maxBytes) {
                return mediaPickerError("too_large", "Captured screenshot is larger than max_bytes")
            }
            mapOf(
                "mime_type" to "image/png",
                "size" to bytes.size,
                "width" to outputWidth,
                "height" to outputHeight,
                "base64" to Base64.encodeToString(bytes, Base64.NO_WRAP),
            )
        } catch (e: Exception) {
            mediaPickerError("capture_failed", e.message ?: "Could not capture screenshot")
        }
    }

    private fun transformImagePayload(args: Map<*, *>?): Map<String, Any> {
        return try {
            val rawBase64 = (args?.get("base64") as? String)?.trim()
                ?: return mediaPickerError("invalid_image", "Image base64 is required")
            val input = Base64.decode(rawBase64, Base64.NO_WRAP)
            val bitmap = BitmapFactory.decodeByteArray(input, 0, input.size)
                ?: return mediaPickerError("invalid_image", "Image base64 could not be decoded")
            val maxWidth = (args?.get("max_width") as? Number)?.toInt()?.coerceAtLeast(1)
                ?: bitmap.width
            val maxHeight = (args?.get("max_height") as? Number)?.toInt()?.coerceAtLeast(1)
                ?: bitmap.height
            val maxBytes = ((args?.get("max_bytes") as? Number)?.toLong()
                ?: DEFAULT_IMAGE_TRANSFORM_MAX_BYTES)
                .coerceIn(1L, HARD_IMAGE_TRANSFORM_MAX_BYTES)
            val format = ((args?.get("format") as? String) ?: "png").trim().lowercase()
            val quality = ((args?.get("quality") as? Number)?.toInt() ?: 90).coerceIn(1, 100)
            val scale = minOf(
                1f,
                maxWidth.toFloat() / bitmap.width.coerceAtLeast(1).toFloat(),
                maxHeight.toFloat() / bitmap.height.coerceAtLeast(1).toFloat(),
            )
            val outputWidth = maxOf(1, (bitmap.width * scale).toInt())
            val outputHeight = maxOf(1, (bitmap.height * scale).toInt())
            val resized = if (outputWidth == bitmap.width && outputHeight == bitmap.height) {
                bitmap
            } else {
                Bitmap.createScaledBitmap(bitmap, outputWidth, outputHeight, true)
            }
            val compressFormat = if (format == "jpeg" || format == "jpg") {
                Bitmap.CompressFormat.JPEG
            } else {
                Bitmap.CompressFormat.PNG
            }
            val mimeType = if (compressFormat == Bitmap.CompressFormat.JPEG) {
                "image/jpeg"
            } else {
                "image/png"
            }
            val output = ByteArrayOutputStream()
            resized.compress(compressFormat, quality, output)
            val bytes = output.toByteArray()
            if (resized !== bitmap) resized.recycle()
            bitmap.recycle()
            if (bytes.size.toLong() > maxBytes) {
                return mediaPickerError("too_large", "Transformed image is larger than max_bytes")
            }
            mapOf(
                "mime_type" to mimeType,
                "size" to bytes.size,
                "width" to outputWidth,
                "height" to outputHeight,
                "base64" to Base64.encodeToString(bytes, Base64.NO_WRAP),
            )
        } catch (e: Exception) {
            mediaPickerError("transform_failed", e.message ?: "Could not transform image")
        }
    }

    private fun recognizeTextPayload(args: Map<*, *>?, result: MethodChannel.Result) {
        val rawBase64 = (args?.get("base64") as? String)?.trim()
        if (rawBase64.isNullOrEmpty()) {
            result.success(mediaPickerError("invalid_image", "Image base64 is required"))
            return
        }
        val maxBytes = ((args?.get("max_bytes") as? Number)?.toLong()
            ?: DEFAULT_OCR_MAX_BYTES)
            .coerceIn(1L, HARD_OCR_MAX_BYTES)
        val input = try {
            Base64.decode(rawBase64, Base64.NO_WRAP)
        } catch (e: Exception) {
            result.success(mediaPickerError("invalid_image", "Image base64 could not be decoded"))
            return
        }
        if (input.size.toLong() > maxBytes) {
            result.success(mediaPickerError("too_large", "Image is larger than max_bytes"))
            return
        }
        val bitmap = BitmapFactory.decodeByteArray(input, 0, input.size)
        if (bitmap == null) {
            result.success(mediaPickerError("invalid_image", "Image data could not be decoded"))
            return
        }
        val image = InputImage.fromBitmap(bitmap, 0)
        val recognizer = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
        recognizer.process(image)
            .addOnSuccessListener { visionText ->
                val blocks = visionText.textBlocks.map { block ->
                    val box = block.boundingBox
                    val blockMap = mutableMapOf<String, Any>(
                        "text" to block.text,
                    )
                    if (box != null) {
                        blockMap["bounding_box"] = mapOf(
                            "left" to box.left,
                            "top" to box.top,
                            "right" to box.right,
                            "bottom" to box.bottom,
                            "width" to box.width(),
                            "height" to box.height(),
                            "unit" to "pixels",
                        )
                    }
                    blockMap
                }
                bitmap.recycle()
                recognizer.close()
                val languageHint = (args?.get("language_hint") as? String)
                    ?.trim()
                    ?.takeIf { it.isNotEmpty() }
                val payload = mutableMapOf<String, Any>(
                    "text" to visionText.text,
                    "blocks" to blocks,
                )
                if (languageHint != null) {
                    payload["language_code"] = languageHint
                }
                result.success(payload)
            }
            .addOnFailureListener { error ->
                bitmap.recycle()
                recognizer.close()
                result.success(mediaPickerError("ocr_failed", error.message ?: "OCR failed"))
            }
    }

    private fun immutablePendingIntentFlag(): Int {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PendingIntent.FLAG_IMMUTABLE
        } else {
            0
        }
    }

    private companion object {
        const val REQUEST_POST_NOTIFICATIONS = 7401
        const val REQUEST_PICK_MEDIA = 7402
        const val NOTIFICATION_CHANNEL_ID = "rumi_pc_tasks"
        const val DEFAULT_MEDIA_PICK_MAX_BYTES = 4L * 1024L * 1024L
        const val HARD_MEDIA_PICK_MAX_BYTES = 8L * 1024L * 1024L
        const val DEFAULT_SCREENSHOT_MAX_BYTES = 6L * 1024L * 1024L
        const val HARD_SCREENSHOT_MAX_BYTES = 12L * 1024L * 1024L
        const val DEFAULT_SCREENSHOT_MAX_DIMENSION = 1600
        const val HARD_SCREENSHOT_MAX_DIMENSION = 4096
        const val DEFAULT_IMAGE_TRANSFORM_MAX_BYTES = 8L * 1024L * 1024L
        const val HARD_IMAGE_TRANSFORM_MAX_BYTES = 16L * 1024L * 1024L
        const val DEFAULT_OCR_MAX_BYTES = 8L * 1024L * 1024L
        const val HARD_OCR_MAX_BYTES = 16L * 1024L * 1024L
    }
}

private class MediaPickerTooLargeException : Exception()

private class RumiSecureStorage(context: Context) {
    private val prefs = context.getSharedPreferences("rumi_secure_storage", Context.MODE_PRIVATE)

    fun read(key: String): String? {
        val encoded = prefs.getString(key, null) ?: return null
        val payload = Base64.decode(encoded, Base64.NO_WRAP)
        if (payload.size < IV_SIZE + 1) return null
        val iv = payload.copyOfRange(0, IV_SIZE)
        val ciphertext = payload.copyOfRange(IV_SIZE, payload.size)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(TAG_SIZE_BITS, iv))
        return String(cipher.doFinal(ciphertext), Charsets.UTF_8)
    }

    fun write(key: String, value: String) {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val ciphertext = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
        val payload = ByteBuffer.allocate(cipher.iv.size + ciphertext.size)
            .put(cipher.iv)
            .put(ciphertext)
            .array()
        prefs.edit().putString(key, Base64.encodeToString(payload, Base64.NO_WRAP)).apply()
    }

    fun delete(key: String) {
        prefs.edit().remove(key).apply()
    }

    private fun secretKey(): SecretKey {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val existing = keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry
        if (existing != null) return existing.secretKey

        val keyGenerator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES,
            "AndroidKeyStore",
        )
        keyGenerator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return keyGenerator.generateKey()
    }

    private companion object {
        const val KEY_ALIAS = "rumi_remote_secure_storage"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_SIZE = 12
        const val TAG_SIZE_BITS = 128
    }
}
