import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:http/http.dart' as http;

const mobileAuthorityScopes = <String>{
  'authority.request.list',
  'authority.request.read',
  'authority.request.approve',
  'authority.request.deny',
};

abstract class AuthoritySecretStore {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> delete(String key);
}

class FlutterAuthoritySecretStore implements AuthoritySecretStore {
  FlutterAuthoritySecretStore({FlutterSecureStorage? storage})
      : _storage = storage ??
            const FlutterSecureStorage(
              aOptions: AndroidOptions(encryptedSharedPreferences: true),
            );

  final FlutterSecureStorage _storage;

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value);

  @override
  Future<void> delete(String key) => _storage.delete(key: key);
}

class MobileAuthorityConnection {
  const MobileAuthorityConnection({
    required this.baseUrl,
    required this.deviceId,
    required this.approvalToken,
    required this.approvalScopes,
  });

  final String baseUrl;
  final String deviceId;
  final String approvalToken;
  final Set<String> approvalScopes;

  bool get isValid {
    final uri = Uri.tryParse(baseUrl);
    return uri != null &&
        uri.hasScheme &&
        uri.host.isNotEmpty &&
        deviceId.isNotEmpty &&
        approvalToken.isNotEmpty &&
        approvalScopes.containsAll(mobileAuthorityScopes) &&
        approvalScopes.every(mobileAuthorityScopes.contains);
  }

  Map<String, Object> toJson() => {
        'base_url': baseUrl,
        'device_id': deviceId,
        'approval_token': approvalToken,
        'approval_scopes': approvalScopes.toList()..sort(),
      };

  factory MobileAuthorityConnection.fromJson(Map<String, dynamic> json) =>
      MobileAuthorityConnection(
        baseUrl: json['base_url'] as String? ?? '',
        deviceId: json['device_id'] as String? ?? '',
        approvalToken: json['approval_token'] as String? ?? '',
        approvalScopes: (json['approval_scopes'] as List? ?? const [])
            .map((value) => value.toString())
            .toSet(),
      );
}

class MobileAuthorityConnectionStore {
  MobileAuthorityConnectionStore({AuthoritySecretStore? storage})
      : _storage = storage ?? FlutterAuthoritySecretStore();

  static const storageKey = 'rumi.mobile.authority_connection.v1';
  final AuthoritySecretStore _storage;

  Future<void> saveVerified(MobileAuthorityConnection connection) async {
    if (!connection.isValid) {
      throw StateError('authority connection is invalid');
    }
    final encoded = jsonEncode(connection.toJson());
    await _storage.write(storageKey, encoded);
    if (await _storage.read(storageKey) != encoded) {
      await _storage.delete(storageKey);
      throw StateError(
        'authority connection persistence could not be verified',
      );
    }
  }

  Future<MobileAuthorityConnection?> load() async {
    final raw = await _storage.read(storageKey);
    if (raw == null || raw.isEmpty) return null;
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! Map) throw const FormatException();
      final connection = MobileAuthorityConnection.fromJson(
        Map<String, dynamic>.from(decoded),
      );
      if (!connection.isValid) throw const FormatException();
      return connection;
    } catch (_) {
      throw StateError('stored authority connection is invalid');
    }
  }

  Future<void> clear() => _storage.delete(storageKey);
}

abstract class AuthorityPayloadSigner {
  Future<String> signPayloadHash(String payloadHash);
}

class SharedMobileIdentitySigner implements AuthorityPayloadSigner {
  SharedMobileIdentitySigner({AuthoritySecretStore? storage})
      : _storage = storage ?? FlutterAuthoritySecretStore();

  static const sharedIdentityKey = 'rumi.mobile.credential_identity.v1';
  final AuthoritySecretStore _storage;

  @override
  Future<String> signPayloadHash(String payloadHash) async {
    final raw = await _storage.read(sharedIdentityKey);
    if (raw == null || raw.isEmpty) {
      throw StateError('paired device signing identity is unavailable');
    }
    final decoded = jsonDecode(raw);
    if (decoded is! Map) throw StateError('paired device identity is invalid');
    final identity = Map<String, dynamic>.from(decoded);
    final publicValue = identity['signing_public_key'] as String? ?? '';
    final privateValue = identity['signing_private_key'] as String? ?? '';
    if (!publicValue.startsWith('ed25519:')) {
      throw StateError('paired device signing identity is invalid');
    }
    final publicBytes = _decode(publicValue.substring('ed25519:'.length));
    final privateBytes = _decode(privateValue);
    if (publicBytes.length != 32 || privateBytes.length != 32) {
      throw StateError('paired device signing identity is invalid');
    }
    final digest = _hexBytes(payloadHash);
    if (digest.length != 32) {
      throw StateError('approval payload hash is invalid');
    }
    final pair = SimpleKeyPairData(
      privateBytes,
      publicKey: SimplePublicKey(publicBytes, type: KeyPairType.ed25519),
      type: KeyPairType.ed25519,
    );
    final signature = await Ed25519().sign(digest, keyPair: pair);
    return base64Url.encode(signature.bytes).replaceAll('=', '');
  }
}

class AuthorityRequestItem {
  const AuthorityRequestItem({
    required this.requestId,
    required this.status,
    required this.principalId,
    required this.permissionId,
    required this.reason,
    required this.riskLevel,
    required this.resource,
    this.createdAt = '',
    this.expiresAt = '',
    this.conversationId = '',
    this.profileId = '',
    this.nodeId = '',
    this.graphId = '',
    this.displayMetadata = const {},
    this.allowedScopes = const [],
    this.settlementReason = '',
    this.settledAt = '',
  });

  final String requestId;
  final String status;
  final String principalId;
  final String permissionId;
  final String reason;
  final String riskLevel;
  final Map<String, dynamic> resource;
  final String createdAt;
  final String expiresAt;
  final String conversationId;
  final String profileId;
  final String nodeId;
  final String graphId;
  final Map<String, dynamic> displayMetadata;
  final List<String> allowedScopes;
  final String settlementReason;
  final String settledAt;

  bool get isPending => status == 'pending';
  bool get isExpired {
    if (expiresAt.isEmpty) return false;
    final value = DateTime.tryParse(expiresAt);
    return value != null && !value.isAfter(DateTime.now().toUtc());
  }

  String get normalizedRiskLevel => riskLevel.trim().toLowerCase();

  bool get isHighImpact =>
      !const {'low', 'medium'}.contains(normalizedRiskLevel);

  bool get typedConfirmationRequired =>
      displayMetadata['typed_confirmation_required'] == true;

  String get confirmationPhrase =>
      _text(displayMetadata['confirmation_phrase']);

  bool get hasRequiredDecisionContext {
    final expiry = DateTime.tryParse(expiresAt);
    return requestId.isNotEmpty &&
        status.isNotEmpty &&
        principalId.isNotEmpty &&
        permissionId.isNotEmpty &&
        profileId.isNotEmpty &&
        resource.isNotEmpty &&
        _presentedConsequence.isNotEmpty &&
        _presentedTarget.isNotEmpty &&
        _presentedAffectedData.isNotEmpty &&
        const {'low', 'medium', 'high', 'critical'}
            .contains(normalizedRiskLevel) &&
        expiry != null &&
        allowedScopes.contains('once') &&
        (!typedConfirmationRequired || confirmationPhrase.isNotEmpty);
  }

  String get title => _firstText([
        displayMetadata['title'],
        displayMetadata['permission_label'],
      ], fallback: permissionId);

  String get _presentedConsequence => _firstText([
        displayMetadata['summary'],
        resource['consequence'],
        reason,
      ], fallback: '');

  String get consequence => _presentedConsequence.isEmpty
      ? 'The PC did not provide a consequence. Refresh before deciding.'
      : _presentedConsequence;

  String get _presentedTarget => _firstText([
        displayMetadata['endpoint_url'],
        displayMetadata['access_summary'],
        resource['target'],
        resource['path'],
        resource['command'],
        resource['endpoint_url'],
        resource['domain'],
        resource['operation'],
      ], fallback: '');

  String get target => _presentedTarget.isEmpty
      ? 'The PC did not provide an exact target. Refresh before deciding.'
      : _presentedTarget;

  String get _presentedAffectedData => _firstText([
        resource['affected_data'],
        resource['data'],
        resource['target_paths'],
        resource['target_urls'],
        displayMetadata['access_summary'],
      ], fallback: '');

  String get affectedData => _presentedAffectedData.isEmpty
      ? 'The PC did not identify the affected data or resource.'
      : _presentedAffectedData;

  String get riskExplanation {
    switch (normalizedRiskLevel) {
      case 'critical':
        return 'This can execute or change security-sensitive resources. '
            'Verify every target before continuing.';
      case 'high':
        return 'This may execute a command, change data, or send data outside '
            'the device. Review the target and consequence carefully.';
      case 'medium':
        return 'This may access an external service or change local data.';
      default:
        return 'This action still requires your explicit decision before it runs.';
    }
  }

  String get scopeLabel => 'This request only (one execution)';

  String get persistenceLabel =>
      'Not remembered. A future request needs a new approval.';

  String get auditText => _firstText([
        displayMetadata['audit_text'],
      ],
          fallback:
              'The signed decision and exact request are recorded locally.');

  AuthorityRequestItem copyWith({
    String? status,
    String? settlementReason,
    String? settledAt,
  }) =>
      AuthorityRequestItem(
        requestId: requestId,
        status: status ?? this.status,
        principalId: principalId,
        permissionId: permissionId,
        reason: reason,
        riskLevel: riskLevel,
        resource: resource,
        createdAt: createdAt,
        expiresAt: expiresAt,
        conversationId: conversationId,
        profileId: profileId,
        nodeId: nodeId,
        graphId: graphId,
        displayMetadata: displayMetadata,
        allowedScopes: allowedScopes,
        settlementReason: settlementReason ?? this.settlementReason,
        settledAt: settledAt ?? this.settledAt,
      );

  factory AuthorityRequestItem.fromJson(Map<String, dynamic> json) =>
      AuthorityRequestItem(
        requestId: _text(json['request_id']),
        status: _text(json['status']).toLowerCase(),
        principalId: _text(json['principal_id']),
        permissionId: _text(json['permission_id']),
        reason: _text(json['reason']),
        riskLevel: _text(json['risk_level']),
        resource: json['resource'] is Map
            ? Map<String, dynamic>.from(json['resource'] as Map)
            : <String, dynamic>{},
        createdAt: _text(json['created_at']),
        expiresAt: _text(json['expires_at']),
        conversationId: _text(json['conversation_id']),
        profileId: _text(json['profile_id']),
        nodeId: _text(json['node_id']),
        graphId: _text(json['graph_id']),
        displayMetadata: json['display_metadata'] is Map
            ? Map<String, dynamic>.from(json['display_metadata'] as Map)
            : <String, dynamic>{},
        allowedScopes: (json['allowed_scopes'] is List
                ? json['allowed_scopes'] as List
                : const [])
            .whereType<String>()
            .map((value) => value.trim().toLowerCase())
            .where((value) => value.isNotEmpty)
            .toList(growable: false),
        settlementReason: _text(json['settlement_reason']),
        settledAt: _text(json['settled_at']),
      );
}

class AuthorityRequestListResult {
  const AuthorityRequestListResult({
    required this.requests,
    this.invalidItemCount = 0,
  });

  final List<AuthorityRequestItem> requests;
  final int invalidItemCount;

  bool get isPartial => invalidItemCount > 0;
}

enum AuthorityFailureKind {
  offline,
  timeout,
  malformedResponse,
  expired,
  alreadySettled,
  stale,
  unauthorized,
  server,
}

class AuthorityClientException implements Exception {
  const AuthorityClientException(
    this.kind, {
    this.statusCode,
    this.settledStatus = '',
  });

  final AuthorityFailureKind kind;
  final int? statusCode;
  final String settledStatus;

  bool get retryable => const {
        AuthorityFailureKind.offline,
        AuthorityFailureKind.timeout,
        AuthorityFailureKind.server,
      }.contains(kind);

  String get userMessage {
    switch (kind) {
      case AuthorityFailureKind.offline:
        return 'The PC is offline or unreachable. Check the connection and retry.';
      case AuthorityFailureKind.timeout:
        return 'The PC did not respond in time. The request was not assumed settled; retry after checking its status.';
      case AuthorityFailureKind.malformedResponse:
        return 'The PC returned an incomplete response. Refresh before deciding.';
      case AuthorityFailureKind.expired:
        return 'This request expired before the decision could be recorded.';
      case AuthorityFailureKind.alreadySettled:
        return settledStatus.isEmpty
            ? 'This request was already settled on another surface.'
            : 'This request was already $settledStatus on another surface.';
      case AuthorityFailureKind.stale:
        return 'This request is stale or no longer exists. Refresh the list.';
      case AuthorityFailureKind.unauthorized:
        return 'This device no longer has permission to review this request. Pair it again.';
      case AuthorityFailureKind.server:
        return 'The PC could not complete the authority operation${statusCode == null ? '' : ' ($statusCode)'}. Retry after checking its status.';
    }
  }

  @override
  String toString() => userMessage;
}

class MobileAuthorityClient {
  MobileAuthorityClient({
    required this.connection,
    required this.signer,
    http.Client? client,
    this.timeout = const Duration(seconds: 15),
  }) : _client = client ?? http.Client();

  final MobileAuthorityConnection connection;
  final AuthorityPayloadSigner signer;
  final http.Client _client;
  final Duration timeout;
  bool _closed = false;

  Future<List<AuthorityRequestItem>> listPending() async {
    return (await listPendingWithDiagnostics())
        .requests
        .where((request) => request.isPending)
        .toList(growable: false);
  }

  Future<AuthorityRequestListResult> listPendingWithDiagnostics() async {
    final result = await listRequestsWithDiagnostics(status: 'pending');
    return AuthorityRequestListResult(
      requests: result.requests
          .where((request) => request.isPending)
          .toList(growable: false),
      invalidItemCount: result.invalidItemCount,
    );
  }

  Future<AuthorityRequestListResult> listRequestsWithDiagnostics({
    String status = 'all',
  }) async {
    final data = await _request(
      'GET',
      '/api/authority/requests',
      query: {'status': status},
    );
    final raw = data['pending'] ?? data['requests'];
    if (raw is! List) {
      throw const AuthorityClientException(
        AuthorityFailureKind.malformedResponse,
      );
    }
    final requests = <AuthorityRequestItem>[];
    final requestIds = <String>{};
    var invalidItemCount = 0;
    for (final rawItem in raw) {
      if (rawItem is! Map) {
        invalidItemCount++;
        continue;
      }
      late final AuthorityRequestItem item;
      try {
        item = AuthorityRequestItem.fromJson(
          Map<String, dynamic>.from(rawItem),
        );
      } on Object {
        invalidItemCount++;
        continue;
      }
      if (item.requestId.isEmpty ||
          item.status.isEmpty ||
          item.permissionId.isEmpty ||
          !const {'pending', 'approved', 'denied', 'expired'}
              .contains(item.status) ||
          !requestIds.add(item.requestId)) {
        invalidItemCount++;
        continue;
      }
      requests.add(item);
    }
    return AuthorityRequestListResult(
      requests: List.unmodifiable(requests),
      invalidItemCount: invalidItemCount,
    );
  }

  Future<AuthorityRequestItem> getRequest(String requestId) async {
    final data = await _request(
      'GET',
      '/api/authority/requests/${Uri.encodeComponent(requestId)}',
    );
    final raw = data['request'] is Map
        ? Map<String, dynamic>.from(data['request'] as Map)
        : data;
    final request = AuthorityRequestItem.fromJson(raw);
    if (request.requestId != requestId ||
        request.status.isEmpty ||
        request.permissionId.isEmpty ||
        !const {'pending', 'approved', 'denied', 'expired'}
            .contains(request.status)) {
      throw const AuthorityClientException(
        AuthorityFailureKind.malformedResponse,
      );
    }
    return request;
  }

  Future<AuthorityRequestItem> approve(
    AuthorityRequestItem request, {
    String confirmationText = '',
  }) =>
      _settle(request, decision: 'approve', confirmationText: confirmationText);

  Future<AuthorityRequestItem> deny(
    AuthorityRequestItem request, {
    String reason = '',
  }) =>
      _settle(request, decision: 'deny', reason: reason);

  Future<AuthorityRequestItem> _settle(
    AuthorityRequestItem request, {
    required String decision,
    String reason = '',
    String confirmationText = '',
  }) async {
    if (!request.isPending || request.isExpired) {
      throw AuthorityClientException(
        request.isExpired
            ? AuthorityFailureKind.expired
            : AuthorityFailureKind.alreadySettled,
        settledStatus: request.status,
      );
    }
    final encodedId = Uri.encodeComponent(request.requestId);
    final current = await getRequest(request.requestId);
    if (!current.isPending || current.isExpired) {
      throw AuthorityClientException(
        current.isExpired
            ? AuthorityFailureKind.expired
            : AuthorityFailureKind.alreadySettled,
        settledStatus: current.status,
      );
    }
    if (!_sameReviewedRequest(request, current) ||
        !current.hasRequiredDecisionContext) {
      throw const AuthorityClientException(AuthorityFailureKind.stale);
    }
    final challenge = await _request(
      'POST',
      '/api/authority/requests/$encodedId/challenge',
      body: {'decision': decision, 'scope': 'once'},
    );
    final payloadHash = challenge['payload_hash'] as String? ?? '';
    final challengeData = challenge['challenge'] is Map
        ? Map<String, dynamic>.from(challenge['challenge'] as Map)
        : const <String, dynamic>{};
    final challengeId = challengeData['challenge_id'] as String? ?? '';
    final challengeExpiry =
        DateTime.tryParse(_text(challengeData['expires_at']));
    final issuedAt = DateTime.tryParse(_text(challengeData['issued_at']));
    final approvalTtl = challengeData['approval_expires_in_seconds'];
    final expectedResourceHash = await _resourceHash(current.resource);
    final expectedPayloadHash = await _sha256Hex(
      utf8.encode(_canonicalJson(challengeData)),
    );
    final now = DateTime.now().toUtc();
    if (!RegExp(r'^[a-f0-9]{64}$').hasMatch(payloadHash) ||
        challengeId.isEmpty ||
        challenge['request_id'] != request.requestId ||
        challengeData['request_id'] != request.requestId ||
        challengeData['profile_id'] != current.profileId ||
        challengeData['device_id'] != connection.deviceId ||
        _text(challengeData['token_id']).isEmpty ||
        challengeData['permission_id'] != current.permissionId ||
        challengeData['resource_hash'] != expectedResourceHash ||
        challengeData['decision'] != decision ||
        challengeData['scope'] != 'once' ||
        approvalTtl is! int ||
        approvalTtl <= 0 ||
        approvalTtl > 300 ||
        _text(challengeData['nonce']).isEmpty ||
        issuedAt == null ||
        challengeExpiry == null ||
        issuedAt.isAfter(now.add(const Duration(minutes: 1))) ||
        !challengeExpiry.isAfter(now) ||
        !challengeExpiry.isAfter(issuedAt) ||
        challengeExpiry.difference(issuedAt) > const Duration(minutes: 5) ||
        payloadHash != expectedPayloadHash) {
      throw const AuthorityClientException(
        AuthorityFailureKind.malformedResponse,
      );
    }
    final signature = await signer.signPayloadHash(payloadHash);
    final attestation = {
      'challenge_id': challengeId,
      'payload_hash': payloadHash,
      'signature': signature,
      'signature_algorithm': 'ed25519',
    };
    final settlement = await _request(
      'POST',
      '/api/authority/requests/$encodedId/${decision == 'approve' ? 'approve' : 'deny'}',
      body: {
        if (decision == 'approve') 'scope': 'once',
        if (decision == 'approve' && confirmationText.isNotEmpty)
          'config': {'confirmation_text': confirmationText},
        if (decision == 'deny' && reason.isNotEmpty) 'reason': reason,
        'attestation': attestation,
      },
    );
    final decisionRecorded = decision == 'approve'
        ? settlement['approved'] == true
        : settlement['denied'] == true;
    final approvalExpiry = DateTime.tryParse(_text(settlement['expires_at']));
    if (settlement['request_id'] != request.requestId ||
        !decisionRecorded ||
        (decision == 'approve' &&
            (settlement['scope'] != 'once' ||
                settlement['permission_id'] != current.permissionId ||
                approvalExpiry == null ||
                !approvalExpiry.isAfter(DateTime.now().toUtc())))) {
      throw const AuthorityClientException(
        AuthorityFailureKind.malformedResponse,
      );
    }
    final authoritative = await getRequest(request.requestId);
    final expectedStatus = decision == 'approve' ? 'approved' : 'denied';
    if (!_sameReviewedRequest(current, authoritative) ||
        authoritative.status != expectedStatus) {
      throw const AuthorityClientException(
        AuthorityFailureKind.malformedResponse,
      );
    }
    return authoritative;
  }

  Future<Map<String, dynamic>> _request(
    String method,
    String path, {
    Map<String, String>? query,
    Map<String, dynamic>? body,
  }) async {
    if (_closed) throw StateError('authority client is closed');
    if (!connection.isValid) {
      throw StateError('authority connection is invalid');
    }
    final base = Uri.parse(connection.baseUrl);
    var uri = base.replace(
      path: '${_trimTrailingSlash(base.path)}$path',
      query: null,
      fragment: null,
    );
    if (query != null) uri = uri.replace(queryParameters: query);
    final request = http.Request(method, uri)
      ..headers.addAll({
        'Accept': 'application/json',
        'Authorization': 'Bearer ${connection.approvalToken}',
        'X-Rumi-Client': 'rumi-mobile',
        if (body != null) 'Content-Type': 'application/json; charset=utf-8',
      });
    if (body != null) request.body = jsonEncode(body);
    late final http.Response response;
    try {
      response = await http.Response.fromStream(
        await _client.send(request).timeout(timeout),
      );
    } on TimeoutException {
      throw const AuthorityClientException(AuthorityFailureKind.timeout);
    } on SocketException {
      throw const AuthorityClientException(AuthorityFailureKind.offline);
    } on http.ClientException {
      throw const AuthorityClientException(AuthorityFailureKind.offline);
    }
    dynamic decoded;
    try {
      decoded = response.body.trim().isEmpty
          ? <String, dynamic>{}
          : jsonDecode(utf8.decode(response.bodyBytes));
    } on FormatException {
      throw AuthorityClientException(
        AuthorityFailureKind.malformedResponse,
        statusCode: response.statusCode,
      );
    }
    if (response.statusCode < 200 ||
        response.statusCode >= 300 ||
        decoded is! Map ||
        decoded['status'] == 'error' ||
        decoded['success'] == false) {
      throw _responseFailure(response.statusCode, decoded);
    }
    final data = decoded['data'];
    if (data is! Map) {
      throw AuthorityClientException(
        AuthorityFailureKind.malformedResponse,
        statusCode: response.statusCode,
      );
    }
    return Map<String, dynamic>.from(data);
  }

  void close() {
    _closed = true;
    _client.close();
  }
}

AuthorityClientException _responseFailure(int statusCode, dynamic decoded) {
  final rawError = decoded is Map ? decoded['error'] : null;
  final serverError = (rawError is Map
          ? _firstText([
              rawError['code'],
              rawError['message'],
            ], fallback: '')
          : _text(rawError))
      .toLowerCase();
  final statusMatch = RegExp(
    r'authority request is (approved|denied)',
  ).firstMatch(serverError);
  if (serverError.contains('expired')) {
    return AuthorityClientException(
      AuthorityFailureKind.expired,
      statusCode: statusCode,
    );
  }
  if (statusMatch != null) {
    return AuthorityClientException(
      AuthorityFailureKind.alreadySettled,
      statusCode: statusCode,
      settledStatus: statusMatch.group(1) ?? '',
    );
  }
  if (statusCode == 404) {
    return AuthorityClientException(
      AuthorityFailureKind.stale,
      statusCode: statusCode,
    );
  }
  if (statusCode == 401 || statusCode == 403) {
    return AuthorityClientException(
      AuthorityFailureKind.unauthorized,
      statusCode: statusCode,
    );
  }
  if (statusCode == 409) {
    return AuthorityClientException(
      AuthorityFailureKind.stale,
      statusCode: statusCode,
    );
  }
  return AuthorityClientException(
    AuthorityFailureKind.server,
    statusCode: statusCode,
  );
}

String _text(Object? value) => value is String ? value.trim() : '';

bool _sameReviewedRequest(
  AuthorityRequestItem reviewed,
  AuthorityRequestItem current,
) =>
    reviewed.requestId == current.requestId &&
    reviewed.principalId == current.principalId &&
    reviewed.permissionId == current.permissionId &&
    reviewed.profileId == current.profileId &&
    reviewed.conversationId == current.conversationId &&
    reviewed.nodeId == current.nodeId &&
    reviewed.graphId == current.graphId &&
    reviewed.reason == current.reason &&
    reviewed.normalizedRiskLevel == current.normalizedRiskLevel &&
    _canonicalJson(reviewed.resource) == _canonicalJson(current.resource) &&
    _canonicalJson(reviewed.displayMetadata) ==
        _canonicalJson(current.displayMetadata) &&
    _canonicalJson(reviewed.allowedScopes) ==
        _canonicalJson(current.allowedScopes) &&
    reviewed.expiresAt == current.expiresAt;

Future<String> _resourceHash(Map<String, dynamic> resource) async {
  final sanitized = _sanitizeResource(resource);
  sanitized.remove('stream');
  return _sha256Hex(utf8.encode(_canonicalJson(sanitized)));
}

Future<String> _sha256Hex(List<int> bytes) async {
  final hash = await Sha256().hash(bytes);
  return hash.bytes
      .map((value) => value.toRadixString(16).padLeft(2, '0'))
      .join();
}

String _canonicalJson(Object? value) => jsonEncode(_canonicalValue(value));

Object? _canonicalValue(Object? value) {
  if (value is Map) {
    final keys = value.keys.map((key) => key.toString()).toList()..sort();
    return {
      for (final key in keys) key: _canonicalValue(value[key]),
    };
  }
  if (value is Iterable) {
    return value.map(_canonicalValue).toList(growable: false);
  }
  return value;
}

Map<String, dynamic> _sanitizeResource(Map<String, dynamic> resource) {
  final output = <String, dynamic>{};
  for (final entry in resource.entries) {
    if (_resourceKeyIsSecretLike(entry.key)) continue;
    final value = entry.value;
    if (value == null || value is String || value is num || value is bool) {
      output[entry.key] = value;
    } else if (value is Map) {
      output[entry.key] = _sanitizeResource(Map<String, dynamic>.from(value));
    } else if (value is List) {
      output[entry.key] = [
        for (final item in value)
          if (item == null || item is String || item is num || item is bool)
            item
          else if (item is Map)
            _sanitizeResource(Map<String, dynamic>.from(item)),
      ];
    }
  }
  return output;
}

bool _resourceKeyIsSecretLike(String key) {
  final normalized = key.toLowerCase().replaceAll(RegExp('[^a-z0-9]'), '');
  const exact = {
    'apikey',
    'xapikey',
    'authorization',
    'proxyauthorization',
    'bearer',
    'token',
    'accesstoken',
    'refreshtoken',
    'idtoken',
    'secret',
    'password',
    'passwd',
    'cookie',
    'setcookie',
    'credential',
    'credentials',
    'clientsecret',
    'privatekey',
    'secretkey',
    'accesskey',
    'secretaccesskey',
  };
  return exact.contains(normalized) ||
      normalized.contains('apikey') ||
      normalized.contains('privatekey') ||
      normalized.contains('secretkey') ||
      const {
        'token',
        'secret',
        'password',
        'passwd',
        'cookie',
        'credential',
        'credentials',
      }.any(normalized.endsWith);
}

String _firstText(List<Object?> values, {required String fallback}) {
  for (final value in values) {
    final text = value is Iterable
        ? value.map((item) => item.toString()).join(', ').trim()
        : value?.toString().trim() ?? '';
    if (text.isNotEmpty) return text;
  }
  return fallback;
}

String _trimTrailingSlash(String path) {
  if (path.isEmpty || path == '/') return '';
  return path.endsWith('/') ? path.substring(0, path.length - 1) : path;
}

Uint8List _decode(String value) =>
    Uint8List.fromList(base64Url.decode(base64Url.normalize(value)));

Uint8List _hexBytes(String value) {
  final normalized = value.trim();
  if (normalized.length.isOdd ||
      !RegExp(r'^[0-9a-fA-F]+$').hasMatch(normalized)) {
    return Uint8List(0);
  }
  return Uint8List.fromList(
    List<int>.generate(
      normalized.length ~/ 2,
      (index) =>
          int.parse(normalized.substring(index * 2, index * 2 + 2), radix: 16),
    ),
  );
}
