import 'dart:async';

import 'package:flutter/material.dart';

import '../data/pc/device_store.dart';
import '../data/pc/pc_catalog.dart';
import '../data/pc/pc_catalog_client.dart';
import '../data/pc/pc_pairing_client.dart';
import '../platform/platform_services.dart';
import '../qr/qr_payload.dart';
import '../qr/qr_scanner_screen.dart';
import 'api_config_store.dart';
import 'defaultspack_mobile_providers.g.dart';

class SettingsScreen extends StatefulWidget {
  const SettingsScreen({
    super.key,
    required this.configStore,
    required this.deviceStore,
    required this.onApiChanged,
    this.onDevicePaired,
  });

  final ApiConfigStore configStore;
  final MobileDeviceStore deviceStore;
  final ValueChanged<ApiConfig> onApiChanged;
  final ValueChanged<PairedDevice?>? onDevicePaired;

  @override
  State<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsFeedback {
  const _SettingsFeedback({
    required this.message,
    required this.isError,
    this.onRetry,
  });

  final String message;
  final bool isError;
  final Future<void> Function()? onRetry;
}

class _SettingsScreenState extends State<SettingsScreen> {
  late ApiConfig _config;
  List<MobileProviderConfig> _providerConfigs = [];
  List<ModelFavoriteConfig> _modelFavorites = [];
  late PcConnection? _pc;
  MobileNotificationSettings _notificationSettings =
      MobileNotificationSettings.defaults;
  List<PairedDevice> _pairedDevices = [];
  DeviceIdentity? _deviceIdentity;
  bool _loading = true;
  bool _saving = false;
  bool _notificationSaving = false;
  bool _providerSaving = false;
  bool _favoriteSaving = false;
  _SettingsFeedback? _apiFeedback;
  _SettingsFeedback? _notificationFeedback;
  _SettingsFeedback? _modelFeedback;

  PcBootstrap? _pcBootstrap;
  PcCatalog? _pcCatalog;
  bool _fetchingCatalog = false;
  String? _catalogError;

  bool _pairingInProgress = false;
  String? _pairingError;
  String _pairingVerificationCode = '';

  final _baseUrl = TextEditingController();
  final _apiKey = TextEditingController();
  final _model = TextEditingController();
  final _label = TextEditingController();
  final _systemPrompt = TextEditingController();
  final _pcUrl = TextEditingController();
  final _pcToken = TextEditingController();

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    for (final c in [
      _baseUrl,
      _apiKey,
      _model,
      _label,
      _systemPrompt,
      _pcUrl,
      _pcToken,
    ]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final api = await widget.configStore.loadApi();
      final savedProviderConfigs = await widget.configStore
          .loadProviderConfigs();
      final providerConfigs = _mergeDefaultProviderConfigs(
        savedProviderConfigs,
      );
      final modelFavorites = await widget.configStore.loadModelFavorites();
      final pc = await widget.configStore.loadPc();
      final notificationSettings = await widget.configStore
          .loadNotificationSettings();
      final paired = await widget.deviceStore.loadPairedDevice();
      final pairedDevices = await widget.deviceStore.loadPairedDevices();
      final identity = await widget.deviceStore.loadOrCreateIdentity();
      if (!mounted) return;
      setState(() {
        _config = api;
        _providerConfigs = providerConfigs;
        _modelFavorites = modelFavorites;
        _pc = pc;
        _notificationSettings = notificationSettings;
        _pairedDevices = pairedDevices;
        if (_pc == null && paired != null) {
          _pc = paired.toPcConnection();
        }
        _deviceIdentity = identity;
        _syncControllers();
        _loading = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _config = ApiConfig.defaults;
        _providerConfigs = _mergeDefaultProviderConfigs(
          const <MobileProviderConfig>[],
        );
        _modelFavorites = const [];
        _pc = null;
        _notificationSettings = MobileNotificationSettings.defaults;
        _pairedDevices = const [];
        _deviceIdentity = null;
        _syncControllers();
        _loading = false;
      });
    }
  }

  void _syncControllers() {
    _baseUrl.text = _config.baseUrl;
    _apiKey.text = _config.apiKey;
    _model.text = _config.model;
    _label.text = _config.label;
    _systemPrompt.text = _config.systemPrompt;
    _pcUrl.text = _pc?.baseUrl ?? '';
    _pcToken.text = _pc?.token ?? '';
  }

  ApiConfig _buildConfig() => ApiConfig(
    providerId: 'openai-compatible',
    baseUrl: _baseUrl.text.trim(),
    apiKey: _apiKey.text.trim(),
    model: _model.text.trim().isEmpty
        ? ApiConfig.defaults.model
        : _model.text.trim(),
    label: _label.text.trim(),
    systemPrompt: _systemPrompt.text.trim(),
    temperature: _config.temperature,
    apiCompatibility: 'openai',
  );

  Future<bool> _save() async {
    if (_saving) return false;
    final config = _buildConfig();
    final pcUrl = _pcUrl.text.trim();
    final pcToken = _pcToken.text.trim();
    final validationError = _validateSettingsDraft(
      config: config,
      pcUrl: pcUrl,
      pcToken: pcToken,
    );
    if (validationError != null) {
      if (mounted) {
        setState(() {
          _apiFeedback = _SettingsFeedback(
            message: validationError,
            isError: true,
            onRetry: () async {
              await _save();
            },
          );
        });
      }
      return false;
    }
    final pc = pcUrl.isEmpty
        ? null
        : PcConnection(baseUrl: pcUrl, token: pcToken);

    setState(() {
      _saving = true;
      _apiFeedback = null;
    });
    try {
      await widget.configStore.saveApiAndPcOrRollback(config, pc);
      if (!mounted) return false;
      setState(() {
        _config = config;
        _pc = pc;
        _apiFeedback = const _SettingsFeedback(
          message: 'API設定とPC接続を保存しました',
          isError: false,
        );
      });
      widget.onApiChanged(config);
      return true;
    } on SettingsPersistenceException catch (error) {
      await _reconcileApiAndPcAfterFailure();
      if (!mounted) return false;
      setState(() {
        _apiFeedback = _SettingsFeedback(
          message: error.reconciled
              ? '保存できませんでした。以前の設定へ戻しました。再試行してください。'
              : '一部の保存状態を確認できません。保存済み設定を再読込したため、内容を確認して再試行してください。',
          isError: true,
          onRetry: () async {
            await _save();
          },
        );
      });
      return false;
    } catch (_) {
      await _reconcileApiAndPcAfterFailure();
      if (!mounted) return false;
      setState(() {
        _apiFeedback = _SettingsFeedback(
          message: '保存できませんでした。保存済み設定を再読込しました。再試行してください。',
          isError: true,
          onRetry: () async {
            await _save();
          },
        );
      });
      return false;
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  String? _validateSettingsDraft({
    required ApiConfig config,
    required String pcUrl,
    required String pcToken,
  }) {
    final apiUri = Uri.tryParse(config.baseUrl);
    if (apiUri == null ||
        !apiUri.hasScheme ||
        !apiUri.hasAuthority ||
        (apiUri.scheme != 'https' && apiUri.scheme != 'http')) {
      return 'API Base URLを有効なHTTP(S) URLで入力してください。保存は行われていません。';
    }
    if ((pcUrl.isEmpty && pcToken.isNotEmpty) ||
        (pcUrl.isNotEmpty && pcToken.isEmpty)) {
      return 'PC接続はURLとBearer tokenを両方入力してください。保存は行われていません。';
    }
    if (pcUrl.isNotEmpty && !pcConnectionUrlAllowed(pcUrl)) {
      return 'release版ではPC接続にHTTPS URLが必要です。保存は行われていません。';
    }
    return null;
  }

  Future<void> _reconcileApiAndPcAfterFailure() async {
    final api = await widget.configStore.loadApi();
    final pc = await widget.configStore.loadPc();
    if (!mounted) return;
    setState(() {
      _config = api;
      _pc = pc;
      _syncControllers();
    });
  }

  Future<void> _scanApi({BuildContext? navigationContext}) async {
    final result = await Navigator.of(navigationContext ?? context)
        .push<(QrPayload, bool)>(
          MaterialPageRoute(
            builder: (_) => const QrScannerScreen(
              purpose: QrScanPurpose.apiImport,
              hint: 'PCの「アプリ」欄に表示されたAPI/モデルQRをスキャン',
            ),
          ),
        );
    if (result == null) return;
    final (payload, mismatch) = result;
    if (mismatch) {
      _toast('このQRはAPI形式ではありません');
      return;
    }
    if (payload is QrApiImport) {
      final providerId = payload.providerId?.trim() ?? '';
      if (providerId.isNotEmpty) {
        await _importProviderApi(payload);
        return;
      }
      setState(() {
        _baseUrl.text = payload.baseUrl;
        _apiKey.text = payload.apiKey;
        if (payload.model != null && payload.model!.isNotEmpty) {
          _model.text = payload.model!;
        }
        if (payload.label != null && payload.label!.isNotEmpty) {
          _label.text = payload.label!;
        }
      });
      _toast('API/モデルを取り込みました。保存してください。');
    }
  }

  Future<void> _importProviderApi(QrApiImport payload) async {
    final providerId = payload.providerId!.trim();
    final existing = _providerConfigById(_providerConfigs, providerId);
    final fallback = _providerConfigById(
      defaultspackMobileProviderConfigs,
      providerId,
    );
    final source = existing ?? fallback;
    if (source == null) {
      setState(() {
        _baseUrl.text = payload.baseUrl;
        _apiKey.text = payload.apiKey;
        if (payload.model?.isNotEmpty == true) _model.text = payload.model!;
        if (payload.label?.isNotEmpty == true) _label.text = payload.label!;
      });
      _toast('未登録providerのAPIを取り込みました。高度な設定から保存してください。');
      return;
    }
    final next = source.copyWith(
      apiKey: payload.apiKey,
      label: payload.label?.trim().isNotEmpty == true
          ? payload.label!.trim()
          : source.label,
      baseUrl: payload.baseUrl.trim().isNotEmpty
          ? payload.baseUrl.trim()
          : source.baseUrl,
      model: payload.model?.trim().isNotEmpty == true
          ? payload.model!.trim()
          : source.model,
      apiCompatibility: payload.apiCompatibility?.trim().isNotEmpty == true
          ? payload.apiCompatibility!.trim()
          : source.apiCompatibility,
    );
    final nextProviders = [
      for (final provider in _providerConfigs)
        if (provider.providerId != providerId) provider,
      next,
    ]..sort((a, b) => a.effectiveLabel.compareTo(b.effectiveLabel));
    await _persistProviderEdit(next, nextProviders);
  }

  Future<void> _scanPc() async {
    final result = await Navigator.of(context).push<(QrPayload, bool)>(
      MaterialPageRoute(
        builder: (_) => const QrScannerScreen(
          purpose: QrScanPurpose.general,
          hint: 'PC接続QRまたはペアリングQRをスキャン',
        ),
      ),
    );
    if (result == null) return;
    final (payload, mismatch) = result;
    if (payload is QrPcConnection) {
      if (!pcConnectionUrlAllowed(payload.baseUrl)) {
        _toast('release版ではPC接続にHTTPS URLが必要です');
        return;
      }
      setState(() {
        _pcUrl.text = payload.baseUrl;
        _pcToken.text = payload.token;
      });
      _toast('PC接続情報を取り込みました。保存してください。');
    } else if (payload is QrPairingV2) {
      await _startPairingV2(payload.payload);
    } else {
      _toast('このQRはPC接続形式ではありません');
    }
  }

  Future<void> _startPairingV2(PairingV2Payload payload) async {
    if (payload.isExpired) {
      _toast('QRコードの有効期限が切れています');
      return;
    }
    final identity = _deviceIdentity;
    if (identity == null) {
      _toast('デバイスIDが利用できません');
      return;
    }

    const requestedScopes = ['chat.read', 'chat.write', 'tools.observe'];
    final verificationCode = await claimVerificationCode(
      pairingId: payload.pairingId,
      device: identity,
      requestedCapabilities: requestedScopes,
    );

    setState(() {
      _pairingInProgress = true;
      _pairingError = null;
      _pairingVerificationCode = verificationCode;
    });

    final client = PcPairingClient();
    try {
      final selectedUrl = preferredPairingBaseUrl(payload.baseUrls);
      if (selectedUrl.isEmpty) {
        throw const PcPairingException('接続URLが見つかりません');
      }

      final tempPc = PcConnection(baseUrl: selectedUrl, token: '');
      final claimResp = await client.claim(
        tempPc,
        pairingId: payload.pairingId,
        code: payload.code,
        device: identity,
        requestedCapabilities: requestedScopes,
      );

      if (!mounted) return;
      _toast('ペアリング要求を送信しました。PC側で承認してください。');

      await _pollPairingUntilAccepted(
        client: client,
        pc: tempPc,
        pairingId: claimResp.pairingId,
        pcLabel: friendlyPcLabel(null, selectedUrl),
        payload: payload,
      );
    } on PcPairingException catch (e) {
      if (!mounted) return;
      setState(() {
        _pairingError = e.toString();
        _pairingInProgress = false;
        _pairingVerificationCode = '';
      });
      _toast('ペアリングエラー: ${e.message}');
    } on SettingsPersistenceException {
      if (!mounted) return;
      setState(() {
        _pairingError = 'ペアリング情報を安全に保存できませんでした';
        _pairingInProgress = false;
        _pairingVerificationCode = '';
      });
      _toast('ペアリング情報を保存できませんでした。もう一度ペアリングしてください。');
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _pairingError = '予期しないペアリングエラーが発生しました';
        _pairingInProgress = false;
        _pairingVerificationCode = '';
      });
      _toast('ペアリングを完了できませんでした');
    } finally {
      client.close();
    }
  }

  Future<void> _pollPairingUntilAccepted({
    required PcPairingClient client,
    required PcConnection pc,
    required String pairingId,
    required String pcLabel,
    required PairingV2Payload payload,
  }) async {
    const maxAttempts = 60;
    const interval = Duration(seconds: 2);

    for (var i = 0; i < maxAttempts; i++) {
      if (!mounted) return;
      await Future<void>.delayed(interval);

      try {
        final statusResp = await client.pollStatus(pc, pairingId: pairingId);
        if (!statusResp.isAccepted) continue;

        final tokenResp = await client.pickupTokenDelivery(
          pc,
          pairingId: pairingId,
          pickupSecret: payload.pickupSecret,
          deviceId: _deviceIdentity!.deviceId,
        );
        if (!tokenResp.hasDeviceToken && !tokenResp.hasTokenDeliveryEnvelope) {
          if (!mounted) return;
          setState(() {
            _pairingInProgress = false;
            _pairingError = 'PCから端末トークンが返りませんでした';
            _pairingVerificationCode = '';
          });
          _toast('ペアリングエラー: 端末トークンが空です');
          return;
        }
        if (tokenResp.isReady) {
          final deliveryPayload = tokenResp.hasTokenDeliveryEnvelope
              ? await widget.deviceStore.decryptTokenDeliveryEnvelope(
                  tokenResp.tokenDeliveryEnvelope!,
                  pairingId: pairingId,
                  deviceId: _deviceIdentity!.deviceId,
                )
              : <String, dynamic>{};
          final token =
              (deliveryPayload['client_access_token'] as String? ??
                      deliveryPayload['device_token'] as String? ??
                      tokenResp.deviceToken ??
                      '')
                  .trim();
          final approvalToken =
              (deliveryPayload['approver_access_token'] as String? ??
                      deliveryPayload['approval_token'] as String? ??
                      tokenResp.approvalToken ??
                      '')
                  .trim();
          if (token.isEmpty) {
            if (!mounted) return;
            setState(() {
              _pairingInProgress = false;
              _pairingError = '端末トークンの復号に失敗しました';
              _pairingVerificationCode = '';
            });
            _toast('ペアリングエラー: 端末トークンを復号できませんでした');
            return;
          }
          final scopes = _stringList(
            deliveryPayload['scopes'],
            fallback: tokenResp.scopes,
          );
          final approvalScopes = _stringList(
            deliveryPayload['approval_scopes'],
            fallback: tokenResp.approvalScopes,
          );
          final device = PairedDevice(
            deviceId: _deviceIdentity!.deviceId,
            deviceToken: token,
            approvalToken: approvalToken,
            label: _deviceIdentity!.deviceLabel,
            scopes: scopes,
            approvalScopes: approvalScopes,
            pcBaseUrl: pc.baseUrl,
            pcLabel: friendlyPcLabel(
              deliveryPayload['pc_label'] as String? ?? tokenResp.pcLabel,
              pc.baseUrl,
            ),
            pairingId: pairingId,
          );
          await widget.deviceStore.savePairedDevice(device);
          final newPc = PcConnection(
            baseUrl: pc.baseUrl,
            token: token,
            approvalToken: approvalToken,
          );
          await widget.configStore.savePcOrThrow(newPc);
          if (tokenResp.hasTokenDeliveryEnvelope) {
            try {
              await client.ackTokenDelivery(
                pc,
                pairingId: pairingId,
                pickupSecret: payload.pickupSecret,
                deviceId: _deviceIdentity!.deviceId,
                deliveryId: tokenResp.deliveryId,
              );
            } catch (_) {
              // The encrypted payload is already saved locally; ack can retry on
              // a later status poll without rotating the server-side token.
            }
          }
          final pairedDevices = await widget.deviceStore.loadPairedDevices();
          if (!mounted) return;
          setState(() {
            _pairedDevices = pairedDevices;
            _pc = newPc;
            _pcUrl.text = pc.baseUrl;
            _pcToken.text = token;
            _pairingInProgress = false;
            _pairingVerificationCode = '';
          });
          widget.onDevicePaired?.call(device);
          _toast('PCとのペアリングが完了しました');
          return;
        }
      } on PcPairingException {
        // continue polling
      }
    }

    if (!mounted) return;
    setState(() {
      _pairingInProgress = false;
      _pairingError = 'タイムアウト: PC側で承認されませんでした';
      _pairingVerificationCode = '';
    });
    _toast('ペアリングがタイムアウトしました');
  }

  Future<void> _selectPairedDevice(PairedDevice device) async {
    if (_saving) return;
    final pc = device.toPcConnection();
    setState(() {
      _saving = true;
      _apiFeedback = null;
    });
    try {
      await widget.deviceStore.savePairedDevice(device);
      await widget.configStore.savePcOrThrow(pc);
      if (!mounted) return;
      setState(() {
        _pc = pc;
        _pcUrl.text = pc.baseUrl;
        _pcToken.text = pc.token;
        _apiFeedback = _SettingsFeedback(
          message: '${device.displayPcLabel} へ接続を切り替えました',
          isError: false,
        );
      });
      widget.onDevicePaired?.call(device);
    } catch (_) {
      await _reconcileApiAndPcAfterFailure();
      if (!mounted) return;
      setState(() {
        _apiFeedback = _SettingsFeedback(
          message: 'PC接続を切り替えられませんでした。保存済み接続を再読込しました。',
          isError: true,
          onRetry: () => _selectPairedDevice(device),
        );
      });
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _unpair(PairedDevice device) async {
    if (_saving) return;
    setState(() {
      _saving = true;
      _apiFeedback = null;
    });
    try {
      await widget.deviceStore.removePairedDevice(device.connectionId);
      final devices = await widget.deviceStore.loadPairedDevices();
      final removingActive =
          _pc?.baseUrl == device.pcBaseUrl && _pc?.token == device.deviceToken;
      PcConnection? nextPc = _pc;
      PairedDevice? nextDevice;
      if (removingActive) {
        nextDevice = devices.isNotEmpty ? devices.first : null;
        nextPc = nextDevice?.toPcConnection();
        await widget.configStore.savePcOrThrow(nextPc);
        if (nextDevice != null) {
          await widget.deviceStore.savePairedDevice(nextDevice);
        }
      }
      if (!mounted) return;
      setState(() {
        _pairedDevices = devices;
        _pc = nextPc;
        _pcUrl.text = nextPc?.baseUrl ?? '';
        _pcToken.text = nextPc?.token ?? '';
        _pcBootstrap = null;
        _pcCatalog = null;
        _apiFeedback = _SettingsFeedback(
          message: '${device.displayPcLabel} との接続を解除しました',
          isError: false,
        );
      });
      if (removingActive) widget.onDevicePaired?.call(nextDevice);
    } catch (_) {
      final devices = await widget.deviceStore.loadPairedDevices();
      await _reconcileApiAndPcAfterFailure();
      if (!mounted) return;
      setState(() {
        _pairedDevices = devices;
        _apiFeedback = _SettingsFeedback(
          message: 'PC接続の解除を完了できませんでした。保存済み状態を再読込しました。',
          isError: true,
          onRetry: () => _unpair(device),
        );
      });
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(message)));
  }

  MobileProviderConfig? _providerConfigById(
    Iterable<MobileProviderConfig> configs,
    String providerId,
  ) {
    for (final config in configs) {
      if (config.providerId == providerId) return config;
    }
    return null;
  }

  List<MobileProviderConfig> _mergeDefaultProviderConfigs(
    List<MobileProviderConfig> savedConfigs,
  ) {
    final byProvider = {
      for (final config in defaultspackMobileProviderConfigs)
        if (_shouldShowMobileProviderConfig(config)) config.providerId: config,
    };
    for (final saved in savedConfigs) {
      final fallback = byProvider[saved.providerId];
      byProvider[saved.providerId] = fallback == null
          ? saved
          : fallback.copyWith(
              label: saved.label,
              apiKey: saved.apiKey,
              baseUrl: saved.baseUrl.trim().isNotEmpty
                  ? saved.baseUrl
                  : fallback.baseUrl,
              model: saved.model.trim().isNotEmpty
                  ? saved.model
                  : fallback.model,
              openaiCompatible: saved.openaiCompatible,
              local: saved.local,
              catalogOnly: saved.catalogOnly,
              apiCompatibility: saved.apiCompatibility,
            );
    }
    final merged = byProvider.values.toList()
      ..sort((a, b) => a.effectiveLabel.compareTo(b.effectiveLabel));
    return merged;
  }

  bool _shouldShowMobileProviderConfig(MobileProviderConfig provider) {
    final providerId = provider.providerId.trim();
    if (providerId.isEmpty) return false;
    if (providerId == 'human-operator' || providerId == 'xiaomi-mimo') {
      return false;
    }
    if (provider.local && provider.baseUrl.startsWith('local://')) {
      return false;
    }
    return true;
  }

  Future<void> _fetchPcCatalog() async {
    final pc = _pc;
    if (pc == null || !pc.isConfigured) {
      _toast('PC接続情報を保存してください。');
      return;
    }
    setState(() {
      _fetchingCatalog = true;
      _catalogError = null;
    });
    final client = PcCatalogClient();
    try {
      final bootstrap = await client.fetchBootstrap(pc);
      final catalog = await client.fetchCapabilities(pc);
      final providerConfigs = _mergeProviderCatalog(catalog);
      final modelFavorites = _mergeModelFavoritesFromCatalog(
        catalog,
        pcLabel: bootstrap.label,
      );
      await widget.configStore.saveProviderConfigsOrThrow(providerConfigs);
      await widget.configStore.saveModelFavoritesOrThrow(modelFavorites);
      if (!mounted) return;
      setState(() {
        _pcBootstrap = bootstrap;
        _pcCatalog = catalog;
        _providerConfigs = providerConfigs;
        _modelFavorites = modelFavorites;
        _fetchingCatalog = false;
      });
      _toast(
        '${catalog.providers.length}プロバイダー / ${catalog.models.length}モデルを取得しました',
      );
    } catch (_) {
      final persistedProviders = await widget.configStore.loadProviderConfigs();
      final persistedFavorites = await widget.configStore.loadModelFavorites();
      if (!mounted) return;
      setState(() {
        _providerConfigs = _mergeDefaultProviderConfigs(persistedProviders);
        _modelFavorites = persistedFavorites;
        _catalogError = 'PCカタログの設定を保存できませんでした。保存済み状態を再読込しました。再試行してください。';
        _fetchingCatalog = false;
      });
    } finally {
      client.close();
    }
  }

  List<MobileProviderConfig> _mergeProviderCatalog(PcCatalog catalog) {
    final existingByProvider = {
      for (final config in _providerConfigs) config.providerId: config,
    };
    final merged = <MobileProviderConfig>[];
    for (final provider in catalog.providers) {
      if (!_shouldShowMobileProvider(provider)) continue;
      final existing = existingByProvider.remove(provider.providerId);
      final model = existing?.model.trim().isNotEmpty == true
          ? existing!.model
          : _defaultModelForProvider(provider, catalog);
      final baseUrl = existing?.baseUrl.trim().isNotEmpty == true
          ? existing!.baseUrl
          : _defaultBaseUrlForProvider(provider);
      final openaiCompatible =
          provider.openaiCompatible ||
          _usesOpenAiCompatibleFallback(provider) ||
          _baseUrlLooksOpenAiCompatible(baseUrl);
      final apiCompatibility = _apiCompatibilityForProvider(
        provider,
        baseUrl: baseUrl,
      );
      merged.add(
        MobileProviderConfig(
          providerId: provider.providerId,
          displayName: provider.displayName.isNotEmpty
              ? provider.displayName
              : provider.providerId,
          label: existing?.label ?? '',
          apiKey: existing?.apiKey ?? '',
          baseUrl: baseUrl,
          model: model,
          openaiCompatible: openaiCompatible,
          local: provider.local,
          catalogOnly: provider.catalogOnly,
          apiCompatibility: apiCompatibility,
        ),
      );
    }
    merged.addAll(existingByProvider.values);
    merged.sort((a, b) => a.effectiveLabel.compareTo(b.effectiveLabel));
    return merged;
  }

  bool _shouldShowMobileProvider(ProviderEntry provider) {
    final providerId = provider.providerId.trim();
    if (providerId.isEmpty) return false;
    if (providerId == 'stub' || providerId == 'rumi') return false;
    if (provider.local && provider.defaultBaseUrl.startsWith('local://')) {
      return false;
    }
    return true;
  }

  String _defaultModelForProvider(ProviderEntry provider, PcCatalog catalog) {
    final chatDefault = provider.defaultModelFor['chat']?.trim() ?? '';
    if (chatDefault.isNotEmpty) return chatDefault;
    final providerDefault = provider.defaultModel.trim();
    if (providerDefault.isNotEmpty) return providerDefault;
    final profiles = catalog.profiles
        .where((profile) => profile.providerId == provider.providerId)
        .toList();
    if (profiles.isNotEmpty) return profiles.first.modelId;
    final models = catalog.modelsForProvider(provider.providerId);
    if (models.isNotEmpty) return models.first.modelId;
    return ApiConfig.defaults.model;
  }

  String _defaultBaseUrlForProvider(ProviderEntry provider) {
    final catalogUrl = provider.defaultBaseUrl.trim();
    if (catalogUrl.isNotEmpty) return catalogUrl;
    switch (provider.providerId) {
      case 'openai':
        return 'https://api.openai.com/v1';
      case 'google':
      case 'gemini':
        return 'https://generativelanguage.googleapis.com/v1beta/openai';
      case 'anthropic':
        return 'https://api.anthropic.com';
      case 'openrouter':
        return 'https://openrouter.ai/api/v1';
      default:
        return '';
    }
  }

  bool _usesOpenAiCompatibleFallback(ProviderEntry provider) {
    return provider.providerId == 'deepseek' ||
        provider.providerId == 'google' ||
        provider.providerId == 'gemini' ||
        provider.providerId == 'openai' ||
        provider.providerId == 'openrouter';
  }

  bool _baseUrlLooksOpenAiCompatible(String baseUrl) {
    final lower = baseUrl.trim().toLowerCase();
    if (lower.isEmpty || lower.startsWith('local://')) return false;
    return lower.contains('/openai') ||
        lower.endsWith('/v1') ||
        lower.contains('/v1/');
  }

  String _apiCompatibilityForProvider(
    ProviderEntry provider, {
    required String baseUrl,
  }) {
    final caps = provider.capabilities.map((cap) => cap.toLowerCase()).toSet();
    if (provider.providerId == 'anthropic' ||
        provider.providerId == 'opencode-zen' ||
        provider.providerId == 'opencode-go' ||
        caps.contains('anthropic_compatible')) {
      return 'anthropic_messages';
    }
    if (provider.openaiCompatible ||
        _usesOpenAiCompatibleFallback(provider) ||
        caps.contains('openai_compatible') ||
        caps.contains('openai_compatible_if_confirmed') ||
        _baseUrlLooksOpenAiCompatible(baseUrl)) {
      return 'openai';
    }
    return 'unsupported';
  }

  bool _providerRunsOnMobile(MobileProviderConfig provider) {
    final compatibility = provider.apiCompatibility.trim();
    final uri = Uri.tryParse(provider.baseUrl.trim());
    return uri != null &&
        uri.hasAuthority &&
        (uri.scheme == 'https' || uri.scheme == 'http') &&
        provider.model.trim().isNotEmpty &&
        (compatibility == 'openai' ||
            compatibility == 'anthropic_messages' ||
            provider.providerId == 'anthropic');
  }

  List<ModelFavoriteConfig> _mergeModelFavoritesFromCatalog(
    PcCatalog catalog, {
    required String pcLabel,
  }) {
    final byKey = {
      for (final favorite in _modelFavorites) favorite.key: favorite,
    };
    final favoriteProfiles = catalog.runtime.favoriteProfiles
        .map((id) => id.trim())
        .toSet();
    for (final profile in catalog.selectableProfiles) {
      final isFavorite =
          profile.favorite ||
          favoriteProfiles.any((id) => _pcProfileMatchesId(profile, id));
      if (!isFavorite) continue;
      final favorite = _favoriteFromPcProfile(profile, pcLabel: pcLabel);
      byKey[favorite.key] = favorite;
    }
    return _sortModelFavorites(byKey.values);
  }

  bool _pcProfileMatchesId(ProfileEntry profile, String id) {
    if (id.isEmpty) return false;
    return profile.effectiveProfileId == id ||
        profile.profileId == id ||
        profile.qualifiedModelId == id ||
        '${profile.providerId}/${profile.modelId}' == id;
  }

  ModelFavoriteConfig _favoriteFromPcProfile(
    ProfileEntry profile, {
    String pcLabel = '',
  }) {
    return ModelFavoriteConfig(
      source: ModelFavoriteConfig.sourcePc,
      providerId: profile.providerId,
      modelId: profile.modelId,
      profileId: profile.effectiveProfileId,
      label: profile.displayLabel,
      pcLabel: pcLabel,
    );
  }

  Future<void> _setModelFavorite(
    ModelFavoriteConfig favorite,
    bool enabled,
  ) async {
    if (_favoriteSaving) return;
    final previous = _modelFavorites;
    final next = enabled
        ? _sortModelFavorites([
            for (final existing in _modelFavorites)
              if (existing.key != favorite.key) existing,
            favorite,
          ])
        : _modelFavorites
              .where((existing) => existing.key != favorite.key)
              .toList();
    setState(() {
      _favoriteSaving = true;
      _modelFavorites = next;
      _modelFeedback = null;
    });
    try {
      await widget.configStore.saveModelFavoritesOrThrow(next);
      if (!mounted) return;
      setState(() {
        _modelFeedback = _SettingsFeedback(
          message: enabled
              ? '${favorite.effectiveLabel} をStar付きモデルへ保存しました'
              : '${favorite.effectiveLabel} をStar付きモデルから外しました',
          isError: false,
        );
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _modelFavorites = previous;
        _modelFeedback = _SettingsFeedback(
          message: 'モデルのStarを保存できなかったため、表示を元に戻しました。',
          isError: true,
          onRetry: () => _setModelFavorite(favorite, enabled),
        );
      });
    } finally {
      if (mounted) setState(() => _favoriteSaving = false);
    }
  }

  bool _isModelFavorite(ModelFavoriteConfig favorite) {
    return _modelFavorites.any((existing) => existing.key == favorite.key);
  }

  bool _isActiveMobileProvider(MobileProviderConfig provider) {
    return _config.providerId == provider.providerId &&
        _config.baseUrl == provider.baseUrl &&
        _config.model == provider.model;
  }

  Future<void> _activateMobileProvider(MobileProviderConfig provider) async {
    if (_providerSaving) return;
    if (!provider.isConfigured || !_providerRunsOnMobile(provider)) {
      await _editMobileProvider(provider);
      return;
    }
    final next = provider.toApiConfig(
      systemPrompt: _systemPrompt.text.trim(),
      temperature: _config.temperature,
    );
    setState(() {
      _providerSaving = true;
      _apiFeedback = null;
    });
    try {
      await widget.configStore.saveApiOrThrow(next);
      if (!mounted) return;
      setState(() {
        _config = next;
        _syncControllers();
        _apiFeedback = _SettingsFeedback(
          message: '${provider.effectiveLabel} をこのスマホのAPIとして保存しました',
          isError: false,
        );
      });
      widget.onApiChanged(next);
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _apiFeedback = _SettingsFeedback(
          message: '${provider.effectiveLabel} を有効化できませんでした。現在のAPI設定は変更していません。',
          isError: true,
          onRetry: () => _activateMobileProvider(provider),
        );
      });
    } finally {
      if (mounted) setState(() => _providerSaving = false);
    }
  }

  Future<void> _editMobileProvider(
    MobileProviderConfig provider, {
    BuildContext? sheetContext,
  }) async {
    final saved = await showModalBottomSheet<MobileProviderConfig>(
      context: sheetContext ?? context,
      isScrollControlled: true,
      showDragHandle: true,
      useSafeArea: true,
      builder: (_) => _ProviderEditSheet(provider: provider),
    );
    if (saved == null) return;
    final nextProviders = [
      for (final existing in _providerConfigs)
        if (existing.providerId != saved.providerId) existing,
      saved,
    ]..sort((a, b) => a.effectiveLabel.compareTo(b.effectiveLabel));
    await _persistProviderEdit(saved, nextProviders);
  }

  Future<void> _persistProviderEdit(
    MobileProviderConfig provider,
    List<MobileProviderConfig> nextProviders,
  ) async {
    if (_providerSaving) return;
    final validationError = _validateProviderDraft(provider);
    if (validationError != null) {
      setState(() {
        _apiFeedback = _SettingsFeedback(
          message: validationError,
          isError: true,
        );
      });
      return;
    }
    var providerSaved = false;
    setState(() {
      _providerSaving = true;
      _apiFeedback = null;
    });
    try {
      await widget.configStore.saveProviderConfigsOrThrow(nextProviders);
      providerSaved = true;
      ApiConfig? nextApi;
      if (provider.isConfigured && _providerRunsOnMobile(provider)) {
        nextApi = provider.toApiConfig(
          systemPrompt: _systemPrompt.text.trim(),
          temperature: _config.temperature,
        );
        await widget.configStore.saveApiOrThrow(nextApi);
      }
      if (!mounted) return;
      setState(() {
        _providerConfigs = nextProviders;
        if (nextApi != null) {
          _config = nextApi;
          _syncControllers();
        }
        _apiFeedback = _SettingsFeedback(
          message: nextApi == null
              ? '${provider.effectiveLabel} のprovider設定を保存しました'
              : '${provider.effectiveLabel} のprovider設定を保存し、このスマホで有効化しました',
          isError: false,
        );
      });
      if (nextApi != null) widget.onApiChanged(nextApi);
    } catch (_) {
      final persistedProviders = await widget.configStore.loadProviderConfigs();
      final persistedApi = await widget.configStore.loadApi();
      if (!mounted) return;
      setState(() {
        _providerConfigs = _mergeDefaultProviderConfigs(persistedProviders);
        _config = persistedApi;
        _syncControllers();
        _apiFeedback = _SettingsFeedback(
          message: providerSaved
              ? 'provider設定は保存されましたが、APIとして有効化できませんでした。保存済み状態を再読込しました。'
              : 'provider設定を保存できませんでした。以前の表示へ戻しました。',
          isError: true,
          onRetry: () => _persistProviderEdit(provider, nextProviders),
        );
      });
    } finally {
      if (mounted) setState(() => _providerSaving = false);
    }
  }

  String? _validateProviderDraft(MobileProviderConfig provider) {
    if (provider.providerId.trim().isEmpty) {
      return 'provider IDがないため保存できません。保存は行われていません。';
    }
    final uri = Uri.tryParse(provider.baseUrl.trim());
    if (uri == null ||
        !uri.hasAuthority ||
        (uri.scheme != 'https' && uri.scheme != 'http')) {
      return 'providerのAPI Base URLを有効なHTTP(S) URLで入力してください。保存は行われていません。';
    }
    if (provider.model.trim().isEmpty) {
      return 'providerのモデルを入力してください。保存は行われていません。';
    }
    return null;
  }

  Future<void> _setPcTaskFinishedNotifications(bool enabled) async {
    final next = _notificationSettings.copyWith(pcTaskFinishedEnabled: enabled);
    final saved = await _persistNotificationSettings(
      next,
      successMessage: enabled ? 'PCタスク完了通知をONで保存しました' : 'PCタスク完了通知をOFFで保存しました',
      retry: () => _setPcTaskFinishedNotifications(enabled),
    );
    if (saved && enabled) {
      unawaited(const PlatformNotifications().requestAuthorization());
    }
  }

  Future<void> _setPcToolDelegation(bool enabled) async {
    final next = _notificationSettings.copyWith(
      delegatePhoneToolsToPcWhenAvailable: enabled,
    );
    await _persistNotificationSettings(
      next,
      successMessage: enabled
          ? 'PC環境のtool委譲をONで保存しました'
          : 'PC環境のtool委譲をOFFで保存しました',
      retry: () => _setPcToolDelegation(enabled),
    );
  }

  Future<bool> _persistNotificationSettings(
    MobileNotificationSettings next, {
    required String successMessage,
    required Future<void> Function() retry,
  }) async {
    if (_notificationSaving) return false;
    final previous = _notificationSettings;
    setState(() {
      _notificationSaving = true;
      _notificationSettings = next;
      _notificationFeedback = null;
    });
    try {
      await widget.configStore.saveNotificationSettingsOrThrow(next);
      if (!mounted) return false;
      setState(() {
        _notificationFeedback = _SettingsFeedback(
          message: successMessage,
          isError: false,
        );
      });
      return true;
    } catch (_) {
      if (!mounted) return false;
      setState(() {
        _notificationSettings = previous;
        _notificationFeedback = _SettingsFeedback(
          message: '通知とtool委譲の設定を保存できなかったため、表示を元に戻しました。',
          isError: true,
          onRetry: retry,
        );
      });
      return false;
    } finally {
      if (mounted) setState(() => _notificationSaving = false);
    }
  }

  Future<void> _pickModelFromCatalog() async {
    final catalog = _pcCatalog;
    if (catalog == null) {
      _toast('まず「PCから取得」してください。');
      return;
    }
    final selected = await showModalBottomSheet<ModelEntry>(
      context: context,
      isScrollControlled: true,
      builder: (context) => _PcModelPicker(catalog: catalog),
    );
    if (selected == null || !mounted) return;
    setState(() {
      _model.text = selected.modelId;
      _label.text = selected.displayName;
    });
    _toast('${selected.displayName} を選択しました。保存してください。');
  }

  bool _isActivePairedDevice(PairedDevice device) {
    final pc = _pc;
    return pc != null &&
        pc.baseUrl == device.pcBaseUrl &&
        pc.token == device.deviceToken;
  }

  Future<void> _openMobileApiSettings() async {
    await Navigator.of(context).push<void>(
      MaterialPageRoute(
        builder: (routeContext) => StatefulBuilder(
          builder: (context, setRouteState) {
            Future<void> refresh(Future<void> Function() action) async {
              await action();
              if (routeContext.mounted) {
                setRouteState(() {});
              }
            }

            final feedback = _apiFeedback;
            final routedFeedback = feedback == null
                ? null
                : _SettingsFeedback(
                    message: feedback.message,
                    isError: feedback.isError,
                    onRetry: feedback.onRetry == null
                        ? null
                        : () => refresh(feedback.onRetry!),
                  );

            return _MobileApiSettingsPage(
              providerConfigs: _providerConfigs,
              config: _config,
              fetchingCatalog: _fetchingCatalog,
              catalogError: _catalogError,
              saving: _saving || _providerSaving,
              feedback: routedFeedback,
              baseUrl: _baseUrl,
              apiKey: _apiKey,
              model: _model,
              label: _label,
              systemPrompt: _systemPrompt,
              isActiveProvider: _isActiveMobileProvider,
              providerRunsOnMobile: _providerRunsOnMobile,
              onScanApi: () =>
                  refresh(() => _scanApi(navigationContext: context)),
              onFetchPcCatalog: () => refresh(_fetchPcCatalog),
              onSaveDirectConfig: () async {
                final saved = await _save();
                if (routeContext.mounted) setRouteState(() {});
                return saved;
              },
              onUseProvider: (provider) =>
                  refresh(() => _activateMobileProvider(provider)),
              onEditProvider: (provider) => refresh(
                () => _editMobileProvider(provider, sheetContext: context),
              ),
            );
          },
        ),
      ),
    );
    if (mounted) {
      setState(() {});
    }
  }

  Future<void> _openModelSettings() async {
    await Navigator.of(context).push<void>(
      MaterialPageRoute(
        builder: (routeContext) => StatefulBuilder(
          builder: (context, setRouteState) {
            Future<void> refresh(Future<void> Function() action) async {
              await action();
              if (routeContext.mounted) {
                setRouteState(() {});
              }
            }

            final feedback = _modelFeedback;
            final routedFeedback = feedback == null
                ? null
                : _SettingsFeedback(
                    message: feedback.message,
                    isError: feedback.isError,
                    onRetry: feedback.onRetry == null
                        ? null
                        : () => refresh(feedback.onRetry!),
                  );

            return _ModelSettingsPage(
              providerConfigs: _providerConfigs,
              config: _config,
              modelFavorites: _modelFavorites,
              pcCatalog: _pcCatalog,
              fetchingCatalog: _fetchingCatalog,
              catalogError: _catalogError,
              saving: _favoriteSaving,
              feedback: routedFeedback,
              isFavorite: _isModelFavorite,
              providerRunsOnMobile: _providerRunsOnMobile,
              onFetchPcCatalog: () => refresh(_fetchPcCatalog),
              onToggleFavorite: (favorite, enabled) =>
                  refresh(() => _setModelFavorite(favorite, enabled)),
              onUseProvider: (provider) =>
                  refresh(() => _activateMobileProvider(provider)),
              favoriteFromPcProfile: _favoriteFromPcProfile,
            );
          },
        ),
      ),
    );
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    return _UnfocusOnTapOutside(
      child: Scaffold(
        appBar: AppBar(title: const Text('設定')),
        body: SafeArea(
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              _SectionTitle(
                icon: Icons.psychology_outlined,
                title: 'このスマホのAI API',
                subtitle: 'API Keyとプロバイダーは別ページで管理します。',
              ),
              const SizedBox(height: 12),
              _SettingsNavCard(
                icon: Icons.key_outlined,
                title: 'API / プロバイダー設定',
                subtitle:
                    '${_configuredProviderCount(_providerConfigs)} / ${_providerConfigs.length} 件のKey保存済み · 現在 ${_mobileApiLabel(_config, _providerConfigs)}',
                onTap: () => unawaited(_openMobileApiSettings()),
              ),
              const SizedBox(height: 12),
              _SettingsNavCard(
                icon: Icons.star_outline,
                title: 'モデル設定',
                subtitle:
                    'Star付き ${_modelFavorites.length} 件 · PCから取り込み / このスマホで設定',
                onTap: () => unawaited(_openModelSettings()),
              ),
              const SizedBox(height: 28),
              _SectionTitle(
                icon: Icons.desktop_windows_outlined,
                title: 'PC接続',
                subtitle: 'PCのdefaultspack Kernel APIへ接続する情報。',
              ),
              const SizedBox(height: 12),
              _MutationStatusBanner(
                feedback: _notificationFeedback,
                pending: _notificationSaving,
                pendingMessage: '通知とtool委譲の設定を保存しています…',
              ),
              if (_notificationFeedback != null || _notificationSaving)
                const SizedBox(height: 12),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                secondary: const Icon(Icons.notifications_active_outlined),
                title: const Text('PCタスク完了通知'),
                subtitle: const Text('PCで実行したチャット/タスクが終わったら通知します。'),
                value: _notificationSettings.pcTaskFinishedEnabled,
                onChanged: _notificationSaving
                    ? null
                    : (value) {
                        unawaited(_setPcTaskFinishedNotifications(value));
                      },
              ),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                secondary: const Icon(Icons.construction_outlined),
                title: const Text('PC環境のtoolを使う'),
                subtitle: const Text('このスマホでチャット中でも、接続中のPCで実行できるtoolへ委譲します。'),
                value:
                    _notificationSettings.delegatePhoneToolsToPcWhenAvailable,
                onChanged: _notificationSaving
                    ? null
                    : (value) {
                        unawaited(_setPcToolDelegation(value));
                      },
              ),
              const SizedBox(height: 12),
              if (_pairedDevices.isNotEmpty) ...[
                for (final device in _pairedDevices) ...[
                  _PairedDeviceCard(
                    device: device,
                    active: _isActivePairedDevice(device),
                    onUse: () => unawaited(_selectPairedDevice(device)),
                    onUnpair: () => unawaited(_unpair(device)),
                  ),
                  const SizedBox(height: 12),
                ],
              ],
              if (_pairingInProgress) ...[
                Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: Theme.of(context).cardTheme.color,
                    borderRadius: BorderRadius.circular(14),
                    border: Border.all(
                      color:
                          Theme.of(context).dividerTheme.color ??
                          Colors.transparent,
                    ),
                  ),
                  child: Column(
                    children: [
                      const CircularProgressIndicator(),
                      const SizedBox(height: 12),
                      const Text('PC側の承認を待っています...'),
                      if (_pairingVerificationCode.isNotEmpty) ...[
                        const SizedBox(height: 12),
                        Text(
                          '確認コード',
                          style: Theme.of(context).textTheme.labelSmall,
                        ),
                        const SizedBox(height: 4),
                        Text(
                          _pairingVerificationCode,
                          style: Theme.of(context).textTheme.titleLarge
                              ?.copyWith(
                                fontFeatures: const [
                                  FontFeature.tabularFigures(),
                                ],
                              ),
                        ),
                        const SizedBox(height: 4),
                        const Text(
                          'PC側に表示されているコードと一致することを確認してください',
                          textAlign: TextAlign.center,
                          style: TextStyle(fontSize: 12, color: Colors.grey),
                        ),
                      ],
                      if (_pairingError != null) ...[
                        const SizedBox(height: 8),
                        Text(
                          _pairingError!,
                          style: TextStyle(
                            fontSize: 12,
                            color: Theme.of(context).colorScheme.error,
                          ),
                        ),
                      ],
                    ],
                  ),
                ),
                const SizedBox(height: 12),
              ] else ...[
                FilledButton.icon(
                  icon: const Icon(Icons.qr_code_scanner),
                  label: Text(_pairedDevices.isEmpty ? 'PCにQRでペアリング' : 'PCを追加'),
                  onPressed: _scanPc,
                  style: FilledButton.styleFrom(
                    minimumSize: const Size.fromHeight(46),
                  ),
                ),
                const SizedBox(height: 8),
                const Center(
                  child: Text(
                    'または',
                    style: TextStyle(fontSize: 12, color: Colors.grey),
                  ),
                ),
                const SizedBox(height: 8),
                TextField(
                  controller: _pcUrl,
                  keyboardType: TextInputType.url,
                  decoration: const InputDecoration(
                    labelText: 'Kernel API URL',
                    hintText: 'https://your-rumi.example.com',
                    prefixIcon: Icon(Icons.dns_outlined),
                  ),
                ),
                const SizedBox(height: 12),
                TextField(
                  controller: _pcToken,
                  obscureText: true,
                  decoration: const InputDecoration(
                    labelText: 'Bearer token',
                    prefixIcon: Icon(Icons.vpn_key_outlined),
                  ),
                ),
                const SizedBox(height: 12),
                _MutationStatusBanner(
                  feedback: _apiFeedback,
                  pending: _saving,
                  pendingMessage: 'API設定とPC接続を保存しています…',
                ),
                if (_apiFeedback != null || _saving) const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton.icon(
                        icon: const Icon(Icons.qr_code_scanner),
                        label: const Text('PC接続QRをスキャン'),
                        onPressed: _scanPc,
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: FilledButton.icon(
                        icon: _saving
                            ? const SizedBox(
                                width: 16,
                                height: 16,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : const Icon(Icons.save_outlined),
                        label: const Text('保存'),
                        onPressed: _saving ? null : () => unawaited(_save()),
                      ),
                    ),
                  ],
                ),
              ],
              if (_pairingError != null && !_pairingInProgress) ...[
                const SizedBox(height: 8),
                Text(
                  _pairingError!,
                  style: TextStyle(
                    fontSize: 12,
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
              ],
              if (_pc != null && _pc!.isConfigured) ...[
                const SizedBox(height: 16),
                FilledButton.tonalIcon(
                  icon: _fetchingCatalog
                      ? const SizedBox(
                          width: 16,
                          height: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.cloud_download_outlined),
                  label: const Text('PCからプロバイダー/モデルを取得'),
                  onPressed: _fetchingCatalog ? null : _fetchPcCatalog,
                ),
                if (_catalogError != null) ...[
                  const SizedBox(height: 8),
                  Text(
                    _catalogError!,
                    style: TextStyle(
                      fontSize: 12,
                      color: Theme.of(context).colorScheme.error,
                    ),
                  ),
                ],
                if (_pcBootstrap != null) ...[
                  const SizedBox(height: 12),
                  _PcInfoCard(bootstrap: _pcBootstrap!, catalog: _pcCatalog),
                ],
                if (_pcCatalog != null) ...[
                  const SizedBox(height: 12),
                  FilledButton.tonalIcon(
                    icon: const Icon(Icons.checklist),
                    label: const Text('モデルを選択'),
                    onPressed: _pickModelFromCatalog,
                  ),
                ],
              ],
              const SizedBox(height: 28),
              _SectionTitle(
                icon: Icons.apps_outlined,
                title: 'アプリについて',
                subtitle: 'TestFlight / App Store は準備中です。',
              ),
              const SizedBox(height: 12),
              const _ComingSoonCard(label: 'TestFlight', sub: 'iOSベータ版'),
              const SizedBox(height: 10),
              const _ComingSoonCard(label: 'App Store', sub: 'iOS / Android'),
              const SizedBox(height: 24),
              Center(
                child: TextButton.icon(
                  icon: const Icon(Icons.cloud_outlined),
                  label: const Text('Cloudflare Pages を開く'),
                  onPressed: () => _openUrl('https://pages.cloudflare.com'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _openUrl(String url) async {
    final uri = Uri.parse(url);
    await const PlatformUrlLauncher().open(uri);
  }
}

int _configuredProviderCount(Iterable<MobileProviderConfig> providers) {
  return providers.where((provider) => provider.isConfigured).length;
}

List<ModelFavoriteConfig> _sortModelFavorites(
  Iterable<ModelFavoriteConfig> favorites,
) {
  final list = favorites.toList();
  list.sort((a, b) {
    final source = a.source.compareTo(b.source);
    if (source != 0) return source;
    return a.effectiveLabel.toLowerCase().compareTo(
      b.effectiveLabel.toLowerCase(),
    );
  });
  return list;
}

class _UnfocusOnTapOutside extends StatelessWidget {
  const _UnfocusOnTapOutside({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Listener(
      behavior: HitTestBehavior.translucent,
      onPointerDown: (event) {
        final focus = FocusManager.instance.primaryFocus;
        if (focus == null) return;
        final renderObject = focus.context?.findRenderObject();
        if (renderObject is RenderBox) {
          final topLeft = renderObject.localToGlobal(Offset.zero);
          final focusedRect = topLeft & renderObject.size;
          if (focusedRect.inflate(8).contains(event.position)) return;
        }
        focus.unfocus();
      },
      child: child,
    );
  }
}

String _mobileApiLabel(
  ApiConfig config,
  Iterable<MobileProviderConfig> providers,
) {
  final providerId = config.providerId.trim();
  if (providerId.isNotEmpty && providerId != 'openai-compatible') {
    for (final provider in providers) {
      if (provider.providerId == providerId) {
        return provider.effectiveLabel;
      }
    }
    return providerId;
  }
  final label = config.label.trim();
  if (label.isNotEmpty) return label;
  final model = config.model.trim();
  if (model.isNotEmpty) return model;
  return '未設定';
}

class _MutationStatusBanner extends StatelessWidget {
  const _MutationStatusBanner({
    required this.feedback,
    required this.pending,
    required this.pendingMessage,
  });

  final _SettingsFeedback? feedback;
  final bool pending;
  final String pendingMessage;

  @override
  Widget build(BuildContext context) {
    final current = feedback;
    if (!pending && current == null) return const SizedBox.shrink();
    final isError = current?.isError ?? false;
    final message = pending ? pendingMessage : current!.message;
    final scheme = Theme.of(context).colorScheme;
    final color = isError ? scheme.error : scheme.primary;
    return Semantics(
      liveRegion: true,
      label: message,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.08),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: color.withValues(alpha: 0.35)),
        ),
        child: Row(
          children: [
            if (pending)
              const SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            else
              Icon(
                isError ? Icons.error_outline : Icons.check_circle_outline,
                size: 20,
                color: color,
              ),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                message,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
            if (!pending && isError && current?.onRetry != null)
              TextButton(
                onPressed: () => unawaited(current!.onRetry!()),
                child: const Text('再試行'),
              ),
          ],
        ),
      ),
    );
  }
}

class _UnsavedDraftBanner extends StatelessWidget {
  const _UnsavedDraftBanner();

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Semantics(
      liveRegion: true,
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
        decoration: BoxDecoration(
          color: scheme.tertiaryContainer.withValues(alpha: 0.45),
          borderRadius: BorderRadius.circular(12),
        ),
        child: const Text('未保存の変更があります。戻る前に保存・破棄・キャンセルを選べます。'),
      ),
    );
  }
}

class _SettingsNavCard extends StatelessWidget {
  const _SettingsNavCard({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.onTap,
  });

  final IconData icon;
  final String title;
  final String subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return InkWell(
      borderRadius: BorderRadius.circular(14),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: Theme.of(context).cardTheme.color,
          borderRadius: BorderRadius.circular(14),
          border: Border.all(
            color: Theme.of(context).dividerTheme.color ?? Colors.transparent,
          ),
        ),
        child: Row(
          children: [
            Icon(icon, color: scheme.primary),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(title, style: Theme.of(context).textTheme.titleSmall),
                  const SizedBox(height: 2),
                  Text(
                    subtitle,
                    style: Theme.of(context).textTheme.bodySmall,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            const Icon(Icons.chevron_right),
          ],
        ),
      ),
    );
  }
}

class _MobileApiSettingsPage extends StatefulWidget {
  const _MobileApiSettingsPage({
    required this.providerConfigs,
    required this.config,
    required this.fetchingCatalog,
    required this.catalogError,
    required this.saving,
    required this.feedback,
    required this.baseUrl,
    required this.apiKey,
    required this.model,
    required this.label,
    required this.systemPrompt,
    required this.isActiveProvider,
    required this.providerRunsOnMobile,
    required this.onScanApi,
    required this.onFetchPcCatalog,
    required this.onSaveDirectConfig,
    required this.onUseProvider,
    required this.onEditProvider,
  });

  final List<MobileProviderConfig> providerConfigs;
  final ApiConfig config;
  final bool fetchingCatalog;
  final String? catalogError;
  final bool saving;
  final _SettingsFeedback? feedback;
  final TextEditingController baseUrl;
  final TextEditingController apiKey;
  final TextEditingController model;
  final TextEditingController label;
  final TextEditingController systemPrompt;
  final bool Function(MobileProviderConfig provider) isActiveProvider;
  final bool Function(MobileProviderConfig provider) providerRunsOnMobile;
  final Future<void> Function() onScanApi;
  final Future<void> Function() onFetchPcCatalog;
  final Future<bool> Function() onSaveDirectConfig;
  final Future<void> Function(MobileProviderConfig provider) onUseProvider;
  final Future<void> Function(MobileProviderConfig provider) onEditProvider;

  @override
  State<_MobileApiSettingsPage> createState() => _MobileApiSettingsPageState();
}

class _MobileApiSettingsPageState extends State<_MobileApiSettingsPage> {
  String _query = '';
  late String _savedDraft;
  bool _dirty = false;
  bool _submitting = false;

  List<TextEditingController> get _draftControllers => [
    widget.baseUrl,
    widget.apiKey,
    widget.model,
    widget.label,
    widget.systemPrompt,
  ];

  @override
  void initState() {
    super.initState();
    _savedDraft = _draftFingerprint();
    for (final controller in _draftControllers) {
      controller.addListener(_onDraftChanged);
    }
  }

  @override
  void didUpdateWidget(covariant _MobileApiSettingsPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.config, widget.config)) {
      _savedDraft = _draftFingerprint();
      _dirty = false;
    }
  }

  @override
  void dispose() {
    for (final controller in _draftControllers) {
      controller.removeListener(_onDraftChanged);
    }
    super.dispose();
  }

  String _draftFingerprint() => [
    widget.baseUrl.text,
    widget.apiKey.text,
    widget.model.text,
    widget.label.text,
    widget.systemPrompt.text,
  ].join('\u0000');

  void _onDraftChanged() {
    final dirty = _draftFingerprint() != _savedDraft;
    if (dirty != _dirty && mounted) setState(() => _dirty = dirty);
  }

  Future<bool> _saveDirectConfig() async {
    if (_submitting || widget.saving) return false;
    setState(() => _submitting = true);
    try {
      final saved = await widget.onSaveDirectConfig();
      if (saved && mounted) {
        setState(() {
          _savedDraft = _draftFingerprint();
          _dirty = false;
        });
      }
      return saved;
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  Future<void> _handleBlockedPop() async {
    if (_submitting || widget.saving) return;
    final action = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('未保存の変更があります'),
        content: const Text('API設定を保存、破棄、または編集を続ける操作を選んでください。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, 'cancel'),
            child: const Text('キャンセル'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, 'discard'),
            child: const Text('破棄'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, 'save'),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    if (!mounted) return;
    if (action == 'save') {
      final saved = await _saveDirectConfig();
      if (saved && mounted) Navigator.of(context).pop();
      return;
    }
    if (action == 'discard') {
      _restoreSavedDraft();
      if (mounted) Navigator.of(context).pop();
    }
  }

  void _restoreSavedDraft() {
    final parts = _savedDraft.split('\u0000');
    for (var index = 0; index < _draftControllers.length; index += 1) {
      _draftControllers[index].text = parts[index];
    }
    if (mounted) setState(() => _dirty = false);
  }

  @override
  Widget build(BuildContext context) {
    final providers = widget.providerConfigs.toList()
      ..sort(
        (a, b) => a.effectiveLabel.toLowerCase().compareTo(
          b.effectiveLabel.toLowerCase(),
        ),
      );
    final filtered = providers.where(_matchesQuery).toList();
    final configuredCount = _configuredProviderCount(widget.providerConfigs);

    final saving = widget.saving || _submitting;
    return PopScope(
      canPop: !_dirty,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) unawaited(_handleBlockedPop());
      },
      child: _UnfocusOnTapOutside(
        child: Scaffold(
          appBar: AppBar(title: const Text('API設定')),
          body: SafeArea(
            child: ListView(
              padding: const EdgeInsets.all(16),
              children: [
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(
                    color: Theme.of(context).cardTheme.color,
                    borderRadius: BorderRadius.circular(14),
                    border: Border.all(
                      color:
                          Theme.of(context).dividerTheme.color ??
                          Colors.transparent,
                    ),
                  ),
                  child: Row(
                    children: [
                      const Icon(Icons.psychology_outlined),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              _mobileApiLabel(
                                widget.config,
                                widget.providerConfigs,
                              ),
                              style: Theme.of(context).textTheme.titleSmall,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                            ),
                            Text(
                              '$configuredCount / ${widget.providerConfigs.length} 件のKey保存済み',
                              style: Theme.of(context).textTheme.bodySmall,
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
                if (widget.feedback != null || saving) ...[
                  const SizedBox(height: 12),
                  _MutationStatusBanner(
                    feedback: widget.feedback,
                    pending: saving,
                    pendingMessage: 'API / provider設定を保存しています…',
                  ),
                ],
                const SizedBox(height: 12),
                Row(
                  children: [
                    Expanded(
                      child: OutlinedButton.icon(
                        icon: const Icon(Icons.qr_code_scanner),
                        label: const Text('QRで取り込む'),
                        onPressed: () => unawaited(widget.onScanApi()),
                      ),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: FilledButton.tonalIcon(
                        icon: widget.fetchingCatalog
                            ? const SizedBox(
                                width: 16,
                                height: 16,
                                child: CircularProgressIndicator(
                                  strokeWidth: 2,
                                ),
                              )
                            : const Icon(Icons.cloud_download_outlined),
                        label: const Text('PCから取得'),
                        onPressed: widget.fetchingCatalog
                            ? null
                            : () => unawaited(widget.onFetchPcCatalog()),
                      ),
                    ),
                  ],
                ),
                if (widget.catalogError != null) ...[
                  const SizedBox(height: 8),
                  Text(
                    widget.catalogError!,
                    style: TextStyle(
                      fontSize: 12,
                      color: Theme.of(context).colorScheme.error,
                    ),
                  ),
                ],
                const SizedBox(height: 20),
                TextField(
                  decoration: const InputDecoration(
                    labelText: 'プロバイダーを検索',
                    prefixIcon: Icon(Icons.search),
                  ),
                  onChanged: (value) => setState(() => _query = value),
                ),
                const SizedBox(height: 12),
                if (widget.providerConfigs.isEmpty) ...[
                  const _ProviderHintCard(),
                ] else if (filtered.isEmpty) ...[
                  const Center(
                    child: Padding(
                      padding: EdgeInsets.symmetric(vertical: 24),
                      child: Text('一致するプロバイダーがありません'),
                    ),
                  ),
                ] else ...[
                  for (final provider in filtered) ...[
                    _MobileProviderCard(
                      provider: provider,
                      active: widget.isActiveProvider(provider),
                      supported: widget.providerRunsOnMobile(provider),
                      onUse: () => unawaited(widget.onUseProvider(provider)),
                      onEdit: () => unawaited(widget.onEditProvider(provider)),
                    ),
                    const SizedBox(height: 10),
                  ],
                ],
                const SizedBox(height: 12),
                Theme(
                  data: Theme.of(context)
                      .copyWith(dividerColor: Colors.transparent),
                  child: ExpansionTile(
                    tilePadding: EdgeInsets.zero,
                    childrenPadding: EdgeInsets.zero,
                    leading: const Icon(Icons.tune_outlined),
                    title: const Text('高度な設定'),
                    subtitle: const Text('OpenAI互換APIをURLから直接設定します。'),
                    children: [
                      const SizedBox(height: 8),
                      if (_dirty) ...[
                        const _UnsavedDraftBanner(),
                        const SizedBox(height: 12),
                      ],
                      TextField(
                        controller: widget.baseUrl,
                        keyboardType: TextInputType.url,
                        decoration: const InputDecoration(
                          labelText: 'API Base URL',
                          hintText: 'https://api.openai.com/v1',
                          prefixIcon: Icon(Icons.cloud_outlined),
                        ),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: widget.apiKey,
                        obscureText: true,
                        decoration: const InputDecoration(
                          labelText: 'API Key',
                          prefixIcon: Icon(Icons.key_outlined),
                        ),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: widget.model,
                        decoration: const InputDecoration(
                          labelText: 'モデル',
                          hintText: 'gpt-4o-mini',
                          prefixIcon: Icon(Icons.model_training_outlined),
                        ),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: widget.label,
                        decoration: const InputDecoration(
                          labelText: 'ラベル (任意)',
                          prefixIcon: Icon(Icons.label_outline),
                        ),
                      ),
                      const SizedBox(height: 12),
                      TextField(
                        controller: widget.systemPrompt,
                        minLines: 2,
                        maxLines: 5,
                        decoration: const InputDecoration(
                          labelText: 'システムプロンプト (任意)',
                          prefixIcon: Icon(Icons.terminal_outlined),
                        ),
                      ),
                      const SizedBox(height: 12),
                      Align(
                        alignment: Alignment.centerRight,
                        child: FilledButton.icon(
                          icon: saving
                              ? const SizedBox(
                                  width: 16,
                                  height: 16,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                  ),
                                )
                              : const Icon(Icons.save_outlined),
                          label: const Text('直接設定を保存'),
                          onPressed: saving
                              ? null
                              : () => unawaited(_saveDirectConfig()),
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  bool _matchesQuery(MobileProviderConfig provider) {
    final query = _query.trim().toLowerCase();
    if (query.isEmpty) return true;
    return provider.effectiveLabel.toLowerCase().contains(query) ||
        provider.displayName.toLowerCase().contains(query) ||
        provider.providerId.toLowerCase().contains(query) ||
        provider.model.toLowerCase().contains(query);
  }
}

class _ModelSettingsPage extends StatefulWidget {
  const _ModelSettingsPage({
    required this.providerConfigs,
    required this.config,
    required this.modelFavorites,
    required this.pcCatalog,
    required this.fetchingCatalog,
    required this.catalogError,
    required this.saving,
    required this.feedback,
    required this.isFavorite,
    required this.providerRunsOnMobile,
    required this.onFetchPcCatalog,
    required this.onToggleFavorite,
    required this.onUseProvider,
    required this.favoriteFromPcProfile,
  });

  final List<MobileProviderConfig> providerConfigs;
  final ApiConfig config;
  final List<ModelFavoriteConfig> modelFavorites;
  final PcCatalog? pcCatalog;
  final bool fetchingCatalog;
  final String? catalogError;
  final bool saving;
  final _SettingsFeedback? feedback;
  final bool Function(ModelFavoriteConfig favorite) isFavorite;
  final bool Function(MobileProviderConfig provider) providerRunsOnMobile;
  final Future<void> Function() onFetchPcCatalog;
  final Future<void> Function(ModelFavoriteConfig favorite, bool enabled)
  onToggleFavorite;
  final Future<void> Function(MobileProviderConfig provider) onUseProvider;
  final ModelFavoriteConfig Function(ProfileEntry profile, {String pcLabel})
  favoriteFromPcProfile;

  @override
  State<_ModelSettingsPage> createState() => _ModelSettingsPageState();
}

class _ModelSettingsPageState extends State<_ModelSettingsPage> {
  String _query = '';

  @override
  Widget build(BuildContext context) {
    final mobileProviders =
        widget.providerConfigs
            .where((provider) => provider.model.trim().isNotEmpty)
            .toList()
          ..sort(
            (a, b) => a.effectiveLabel.toLowerCase().compareTo(
              b.effectiveLabel.toLowerCase(),
            ),
          );
    final pcProfiles = widget.pcCatalog?.selectableProfiles ?? [];
    final filteredMobile = mobileProviders
        .where(_matchesMobileProvider)
        .toList();
    final filteredPc = pcProfiles.where(_matchesPcProfile).toList();

    return _UnfocusOnTapOutside(
      child: Scaffold(
        appBar: AppBar(title: const Text('モデル設定')),
        body: SafeArea(
          child: ListView(
            padding: const EdgeInsets.all(16),
            children: [
              _ModelSummaryCard(
                activeLabel: _mobileApiLabel(
                  widget.config,
                  widget.providerConfigs,
                ),
                favoriteCount: widget.modelFavorites.length,
              ),
              if (widget.feedback != null || widget.saving) ...[
                const SizedBox(height: 12),
                _MutationStatusBanner(
                  feedback: widget.feedback,
                  pending: widget.saving,
                  pendingMessage: 'Star付きモデルを保存しています…',
                ),
              ],
              const SizedBox(height: 12),
              FilledButton.tonalIcon(
                icon: widget.fetchingCatalog
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.cloud_download_outlined),
                label: const Text('PCからモデルを取り込む'),
                onPressed: widget.fetchingCatalog
                    ? null
                    : () => unawaited(widget.onFetchPcCatalog()),
              ),
              if (widget.catalogError != null) ...[
                const SizedBox(height: 8),
                Text(
                  widget.catalogError!,
                  style: TextStyle(
                    fontSize: 12,
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
              ],
              const SizedBox(height: 16),
              TextField(
                decoration: const InputDecoration(
                  labelText: 'モデルを検索',
                  prefixIcon: Icon(Icons.search),
                ),
                onChanged: (value) => setState(() => _query = value),
              ),
              const SizedBox(height: 20),
              _SectionTitle(
                icon: Icons.star_outline,
                title: 'Star付きモデル',
                subtitle: 'チャット画面のモデル選択は、この一覧を優先します。',
              ),
              const SizedBox(height: 12),
              if (widget.modelFavorites.isEmpty)
                const _ProviderHintCard()
              else
                for (final favorite in widget.modelFavorites) ...[
                  _FavoriteModelCard(
                    favorite: favorite,
                    provider: _providerForFavorite(favorite),
                    onUseProvider: favorite.isMobile
                        ? () {
                            final provider = _providerForFavorite(favorite);
                            if (provider != null) {
                              unawaited(widget.onUseProvider(provider));
                            }
                          }
                        : null,
                    onRemove: () =>
                        unawaited(widget.onToggleFavorite(favorite, false)),
                  ),
                  const SizedBox(height: 10),
                ],
              const SizedBox(height: 20),
              _SectionTitle(
                icon: Icons.phone_android_outlined,
                title: 'このスマホ',
                subtitle: 'API Keyを保存したproviderのモデルをstarします。',
              ),
              const SizedBox(height: 12),
              if (filteredMobile.isEmpty)
                const _EmptyModelSettingsHint(label: '一致するスマホモデルがありません')
              else
                for (final provider in filteredMobile) ...[
                  _ModelCandidateCard(
                    title: provider.effectiveLabel,
                    subtitle: [
                      provider.displayName,
                      provider.model,
                    ].where((part) => part.trim().isNotEmpty).join(' · '),
                    sourceLabel: provider.isConfigured ? 'このスマホ' : 'Key未設定',
                    active:
                        widget.config.providerId == provider.providerId &&
                        widget.config.model == provider.model,
                    supported:
                        provider.isConfigured &&
                        widget.providerRunsOnMobile(provider),
                    starred: widget.isFavorite(
                      ModelFavoriteConfig.fromMobileProvider(provider),
                    ),
                    onToggleStar: (starred) => unawaited(
                      widget.onToggleFavorite(
                        ModelFavoriteConfig.fromMobileProvider(provider),
                        starred,
                      ),
                    ),
                    onUse:
                        provider.isConfigured &&
                            widget.providerRunsOnMobile(provider)
                        ? () => unawaited(widget.onUseProvider(provider))
                        : null,
                  ),
                  const SizedBox(height: 10),
                ],
              const SizedBox(height: 20),
              _SectionTitle(
                icon: Icons.desktop_windows_outlined,
                title: 'PC',
                subtitle: 'PCのstar付きモデルは「PCからモデルを取り込む」で同期されます。',
              ),
              const SizedBox(height: 12),
              if (widget.pcCatalog == null)
                const _EmptyModelSettingsHint(label: 'PCからモデルを取り込むと表示されます')
              else if (filteredPc.isEmpty)
                const _EmptyModelSettingsHint(label: '一致するPCモデルがありません')
              else
                for (final profile in filteredPc) ...[
                  _ModelCandidateCard(
                    title: profile.displayLabel,
                    subtitle: [
                      profile.providerDisplayName.isNotEmpty
                          ? profile.providerDisplayName
                          : profile.providerId,
                      profile.modelId,
                    ].where((part) => part.trim().isNotEmpty).join(' · '),
                    sourceLabel: profile.configured || profile.local
                        ? 'PC'
                        : 'PC Key未設定',
                    active: false,
                    supported: profile.configured || profile.local,
                    starred: widget.isFavorite(
                      widget.favoriteFromPcProfile(profile),
                    ),
                    onToggleStar: (starred) => unawaited(
                      widget.onToggleFavorite(
                        widget.favoriteFromPcProfile(profile),
                        starred,
                      ),
                    ),
                    onUse: null,
                  ),
                  const SizedBox(height: 10),
                ],
            ],
          ),
        ),
      ),
    );
  }

  bool _matchesMobileProvider(MobileProviderConfig provider) {
    final query = _query.trim().toLowerCase();
    if (query.isEmpty) return true;
    return provider.effectiveLabel.toLowerCase().contains(query) ||
        provider.displayName.toLowerCase().contains(query) ||
        provider.providerId.toLowerCase().contains(query) ||
        provider.model.toLowerCase().contains(query);
  }

  bool _matchesPcProfile(ProfileEntry profile) {
    final query = _query.trim().toLowerCase();
    if (query.isEmpty) return true;
    return profile.displayLabel.toLowerCase().contains(query) ||
        profile.modelId.toLowerCase().contains(query) ||
        profile.providerId.toLowerCase().contains(query) ||
        profile.providerDisplayName.toLowerCase().contains(query);
  }

  MobileProviderConfig? _providerForFavorite(ModelFavoriteConfig favorite) {
    for (final provider in widget.providerConfigs) {
      if (favorite.matchesMobileProvider(provider)) return provider;
    }
    return null;
  }
}

class _ModelSummaryCard extends StatelessWidget {
  const _ModelSummaryCard({
    required this.activeLabel,
    required this.favoriteCount,
  });

  final String activeLabel;
  final int favoriteCount;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Theme.of(context).cardTheme.color,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: Theme.of(context).dividerTheme.color ?? Colors.transparent,
        ),
      ),
      child: Row(
        children: [
          const Icon(Icons.model_training_outlined),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  activeLabel,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.titleSmall,
                ),
                Text(
                  'Star付き $favoriteCount 件',
                  style: Theme.of(context).textTheme.bodySmall,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _FavoriteModelCard extends StatelessWidget {
  const _FavoriteModelCard({
    required this.favorite,
    required this.provider,
    required this.onUseProvider,
    required this.onRemove,
  });

  final ModelFavoriteConfig favorite;
  final MobileProviderConfig? provider;
  final VoidCallback? onUseProvider;
  final VoidCallback onRemove;

  @override
  Widget build(BuildContext context) {
    final subtitle = favorite.isPc
        ? [
            'PC',
            favorite.pcLabel,
            favorite.providerId,
            favorite.modelId,
          ].where((part) => part.trim().isNotEmpty).join(' · ')
        : [
            'このスマホ',
            provider?.displayName ?? favorite.providerId,
            favorite.modelId,
          ].where((part) => part.trim().isNotEmpty).join(' · ');
    return _ModelCandidateCard(
      title: favorite.effectiveLabel,
      subtitle: subtitle,
      sourceLabel: favorite.isPc ? 'PC' : 'このスマホ',
      active: false,
      supported: favorite.isPc || provider?.isConfigured == true,
      starred: true,
      onToggleStar: (_) => onRemove(),
      onUse: onUseProvider,
    );
  }
}

class _ModelCandidateCard extends StatelessWidget {
  const _ModelCandidateCard({
    required this.title,
    required this.subtitle,
    required this.sourceLabel,
    required this.active,
    required this.supported,
    required this.starred,
    required this.onToggleStar,
    required this.onUse,
  });

  final String title;
  final String subtitle;
  final String sourceLabel;
  final bool active;
  final bool supported;
  final bool starred;
  final ValueChanged<bool> onToggleStar;
  final VoidCallback? onUse;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Theme.of(context).cardTheme.color,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: active
              ? scheme.primary.withValues(alpha: 0.45)
              : Theme.of(context).dividerTheme.color ?? Colors.transparent,
        ),
      ),
      child: Row(
        children: [
          IconButton(
            tooltip: starred ? 'Starを外す' : 'Starする',
            icon: Icon(starred ? Icons.star : Icons.star_border),
            color: starred ? scheme.primary : scheme.onSurfaceVariant,
            onPressed: () => onToggleStar(!starred),
          ),
          const SizedBox(width: 4),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        title,
                        style: Theme.of(context).textTheme.titleSmall,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                    if (active) const SizedBox(width: 6),
                    if (active) const _StatusPill(label: '使用中', active: true),
                  ],
                ),
                const SizedBox(height: 2),
                Text(
                  subtitle,
                  style: Theme.of(context).textTheme.bodySmall,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
                const SizedBox(height: 6),
                Row(
                  children: [
                    _StatusPill(label: sourceLabel, active: supported),
                    const Spacer(),
                    TextButton(onPressed: onUse, child: const Text('使用')),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _EmptyModelSettingsHint extends StatelessWidget {
  const _EmptyModelSettingsHint({required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Theme.of(context).cardTheme.color,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: Theme.of(context).dividerTheme.color ?? Colors.transparent,
        ),
      ),
      child: Text(label, style: Theme.of(context).textTheme.bodySmall),
    );
  }
}

class _PairedDeviceCard extends StatelessWidget {
  const _PairedDeviceCard({
    required this.device,
    required this.active,
    required this.onUse,
    required this.onUnpair,
  });
  final PairedDevice device;
  final bool active;
  final VoidCallback onUse;
  final VoidCallback onUnpair;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Theme.of(context).cardTheme.color,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: Theme.of(context).dividerTheme.color ?? Colors.transparent,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(
                active ? Icons.check_circle : Icons.desktop_windows_outlined,
                size: 18,
                color: active ? scheme.primary : scheme.onSurfaceVariant,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  active ? '使用中のPC' : 'PC接続済み',
                  style: Theme.of(context).textTheme.titleSmall,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            'PC: ${device.displayPcLabel}',
            style: Theme.of(context).textTheme.bodyMedium,
          ),
          Text(
            'デバイス: ${device.label}',
            style: Theme.of(context).textTheme.bodySmall,
          ),
          if (device.scopes.isNotEmpty) ...[
            const SizedBox(height: 6),
            Wrap(
              spacing: 4,
              runSpacing: 4,
              children: device.scopes
                  .map(
                    (s) => Chip(
                      label: Text(s, style: const TextStyle(fontSize: 10)),
                      visualDensity: VisualDensity.compact,
                      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    ),
                  )
                  .toList(),
            ),
          ],
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              if (!active)
                TextButton.icon(
                  icon: const Icon(Icons.check, size: 16),
                  label: const Text('使用'),
                  onPressed: onUse,
                ),
              TextButton.icon(
                icon: const Icon(Icons.link_off, size: 16),
                label: const Text('切断'),
                onPressed: onUnpair,
                style: TextButton.styleFrom(foregroundColor: scheme.error),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _ProviderEditSheet extends StatefulWidget {
  const _ProviderEditSheet({required this.provider});

  final MobileProviderConfig provider;

  @override
  State<_ProviderEditSheet> createState() => _ProviderEditSheetState();
}

class _ProviderEditSheetState extends State<_ProviderEditSheet> {
  late final TextEditingController _label;
  late final TextEditingController _apiKey;
  late final TextEditingController _baseUrl;
  late final TextEditingController _model;

  @override
  void initState() {
    super.initState();
    _label = TextEditingController(text: widget.provider.label);
    _apiKey = TextEditingController(text: widget.provider.apiKey);
    _baseUrl = TextEditingController(text: widget.provider.baseUrl);
    _model = TextEditingController(text: widget.provider.model);
  }

  @override
  void dispose() {
    _label.dispose();
    _apiKey.dispose();
    _baseUrl.dispose();
    _model.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final provider = widget.provider;
    return _UnfocusOnTapOutside(
      child: Padding(
        padding: EdgeInsets.only(
          left: 16,
          right: 16,
          top: 4,
          bottom: MediaQuery.viewInsetsOf(context).bottom + 20,
        ),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                provider.displayName.isNotEmpty
                    ? provider.displayName
                    : provider.providerId,
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _label,
                decoration: const InputDecoration(
                  labelText: 'ラベル',
                  prefixIcon: Icon(Icons.label_outline),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _apiKey,
                obscureText: true,
                decoration: const InputDecoration(
                  labelText: 'API Key',
                  prefixIcon: Icon(Icons.key_outlined),
                ),
              ),
              const SizedBox(height: 12),
              Theme(
                data: Theme.of(context)
                    .copyWith(dividerColor: Colors.transparent),
                child: ExpansionTile(
                  tilePadding: EdgeInsets.zero,
                  childrenPadding: EdgeInsets.zero,
                  title: const Text('高度な設定'),
                  subtitle: const Text(
                    '通常は変更不要です。provider側のURL/モデルが必要な時だけ使います。',
                  ),
                  children: [
                    TextField(
                      controller: _baseUrl,
                      keyboardType: TextInputType.url,
                      decoration: const InputDecoration(
                        labelText: 'API Base URL',
                        prefixIcon: Icon(Icons.cloud_outlined),
                      ),
                    ),
                    const SizedBox(height: 12),
                    TextField(
                      controller: _model,
                      decoration: const InputDecoration(
                        labelText: 'モデル',
                        prefixIcon: Icon(Icons.model_training_outlined),
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              Row(
                children: [
                  Expanded(
                    child: OutlinedButton(
                      onPressed: () => Navigator.pop(context),
                      child: const Text('キャンセル'),
                    ),
                  ),
                  const SizedBox(width: 12),
                  Expanded(
                    child: FilledButton.icon(
                      icon: const Icon(Icons.save_outlined),
                      label: const Text('保存'),
                      onPressed: () {
                        Navigator.pop(
                          context,
                          provider.copyWith(
                            label: _label.text.trim(),
                            apiKey: _apiKey.text.trim(),
                            baseUrl: _baseUrl.text.trim(),
                            model: _model.text.trim(),
                          ),
                        );
                      },
                    ),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _MobileProviderCard extends StatelessWidget {
  const _MobileProviderCard({
    required this.provider,
    required this.active,
    required this.supported,
    required this.onUse,
    required this.onEdit,
  });

  final MobileProviderConfig provider;
  final bool active;
  final bool supported;
  final VoidCallback onUse;
  final VoidCallback onEdit;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final configured = provider.isConfigured;
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Theme.of(context).cardTheme.color,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: active
              ? scheme.primary.withValues(alpha: 0.45)
              : Theme.of(context).dividerTheme.color ?? Colors.transparent,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(
                active ? Icons.check_circle : Icons.cloud_outlined,
                size: 18,
                color: active ? scheme.primary : scheme.onSurfaceVariant,
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  provider.effectiveLabel,
                  style: Theme.of(context).textTheme.titleSmall,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
              _StatusPill(
                label: configured ? 'Key保存済み' : '未設定',
                active: configured,
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            [
              if (provider.displayName.isNotEmpty) provider.displayName,
              provider.model,
            ].where((part) => part.trim().isNotEmpty).join(' · '),
            style: Theme.of(context).textTheme.bodySmall,
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
          ),
          if (!supported) ...[
            const SizedBox(height: 4),
            Text(
              'このproviderはスマホ単体で使うURL/互換形式が未設定です。API Keyから高度な設定を開いてください。',
              style: TextStyle(fontSize: 12, color: scheme.error),
            ),
          ],
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              TextButton.icon(
                icon: const Icon(Icons.key_outlined, size: 16),
                label: const Text('API Key'),
                onPressed: onEdit,
              ),
              TextButton.icon(
                icon: const Icon(Icons.check, size: 16),
                label: const Text('使用'),
                onPressed: configured && supported && !active ? onUse : null,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _ProviderHintCard extends StatelessWidget {
  const _ProviderHintCard();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Theme.of(context).cardTheme.color,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: Theme.of(context).dividerTheme.color ?? Colors.transparent,
        ),
      ),
      child: const Row(
        children: [
          Icon(Icons.cloud_download_outlined),
          SizedBox(width: 10),
          Expanded(
            child: Text('PC接続後に「PCからプロバイダー/モデルを取得」を押すとprovider一覧が入ります。'),
          ),
        ],
      ),
    );
  }
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({required this.label, required this.active});

  final String label;
  final bool active;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final color = active ? scheme.primary : scheme.onSurfaceVariant;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Text(label, style: TextStyle(fontSize: 11, color: color)),
    );
  }
}

class _SectionTitle extends StatelessWidget {
  const _SectionTitle({
    required this.icon,
    required this.title,
    required this.subtitle,
  });
  final IconData icon;
  final String title;
  final String subtitle;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Row(
      children: [
        Icon(icon, color: scheme.primary),
        const SizedBox(width: 10),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(title, style: Theme.of(context).textTheme.titleSmall),
              Text(
                subtitle,
                style: Theme.of(context).textTheme.bodySmall,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ],
          ),
        ),
      ],
    );
  }
}

class _ComingSoonCard extends StatelessWidget {
  const _ComingSoonCard({required this.label, required this.sub});
  final String label;
  final String sub;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 14),
      decoration: BoxDecoration(
        color: Theme.of(context).cardTheme.color,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: Theme.of(context).dividerTheme.color ?? Colors.transparent,
        ),
      ),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(label, style: Theme.of(context).textTheme.titleSmall),
                Text(sub, style: Theme.of(context).textTheme.bodySmall),
              ],
            ),
          ),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
            decoration: BoxDecoration(
              color: Colors.amber.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(999),
              border: Border.all(color: Colors.amber.withValues(alpha: 0.4)),
            ),
            child: Text(
              'Coming soon',
              style: TextStyle(fontSize: 11, color: Colors.amber.shade200),
            ),
          ),
        ],
      ),
    );
  }
}

class _PcInfoCard extends StatelessWidget {
  const _PcInfoCard({required this.bootstrap, required this.catalog});
  final PcBootstrap bootstrap;
  final PcCatalog? catalog;

  @override
  Widget build(BuildContext context) {
    final caps = bootstrap.capabilities;
    final configured = catalog?.configuredProviders ?? const [];
    final visibleConfigured = configured.take(4).toList();
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Theme.of(context).cardTheme.color,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(
          color: Theme.of(context).dividerTheme.color ?? Colors.transparent,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.desktop_windows, size: 18),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  bootstrap.label,
                  style: Theme.of(context).textTheme.titleSmall,
                ),
              ),
              Text(
                bootstrap.version,
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              _CapChip(label: 'チャット', on: caps.chat),
              _CapChip(label: 'ツール', on: caps.tools),
              _CapChip(label: '承認', on: caps.approvals),
              _CapChip(label: 'キー転送', on: caps.credentialTransfer),
            ],
          ),
          if (catalog != null) ...[
            const SizedBox(height: 10),
            Text(
              'プロバイダー ${catalog!.providers.length}件（設定済み ${configured.length}）/ モデル ${catalog!.models.length}件 / プロファイル ${catalog!.profiles.length}件 / テンプレート ${catalog!.templates.length}件',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            if (configured.isNotEmpty) ...[
              const SizedBox(height: 6),
              Wrap(
                spacing: 6,
                runSpacing: 6,
                children:
                    configured
                        .take(4)
                        .map(
                          (p) => Chip(
                            label: Text(p.displayName),
                            avatar: const Icon(Icons.cloud_done, size: 16),
                            visualDensity: VisualDensity.compact,
                          ),
                        )
                        .toList()
                      ..addAll(
                        configured.length > visibleConfigured.length
                            ? [
                                Chip(
                                  label: Text(
                                    '+${configured.length - visibleConfigured.length}',
                                  ),
                                  visualDensity: VisualDensity.compact,
                                ),
                              ]
                            : const [],
                      ),
              ),
            ],
          ],
        ],
      ),
    );
  }
}

class _CapChip extends StatelessWidget {
  const _CapChip({required this.label, required this.on});
  final String label;
  final bool on;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: on ? scheme.primary.withValues(alpha: 0.15) : null,
        borderRadius: BorderRadius.circular(999),
        border: Border.all(
          color: on ? scheme.primary.withValues(alpha: 0.4) : scheme.outline,
        ),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            on ? Icons.check_circle : Icons.remove_circle_outline,
            size: 13,
            color: on ? scheme.primary : scheme.outline,
          ),
          const SizedBox(width: 4),
          Text(
            label,
            style: TextStyle(
              fontSize: 11,
              color: on ? scheme.primary : scheme.onSurfaceVariant,
            ),
          ),
        ],
      ),
    );
  }
}

class _PcModelPicker extends StatefulWidget {
  const _PcModelPicker({required this.catalog});
  final PcCatalog catalog;

  @override
  State<_PcModelPicker> createState() => _PcModelPickerState();
}

class _PcModelPickerState extends State<_PcModelPicker> {
  String? _selectedProvider;
  String _query = '';

  @override
  void initState() {
    super.initState();
    final configured = widget.catalog.configuredProviders;
    _selectedProvider = configured.isNotEmpty
        ? configured.first.providerId
        : null;
  }

  @override
  Widget build(BuildContext context) {
    final providers = widget.catalog.providers;
    final models = widget.catalog.models
        .where(
          (m) => _selectedProvider == null || m.providerId == _selectedProvider,
        )
        .where(
          (m) =>
              _query.isEmpty ||
              m.displayName.toLowerCase().contains(_query.toLowerCase()) ||
              m.modelId.toLowerCase().contains(_query.toLowerCase()),
        )
        .toList();

    return _UnfocusOnTapOutside(
      child: DraggableScrollableSheet(
        initialChildSize: 0.8,
        minChildSize: 0.4,
        maxChildSize: 0.95,
        expand: false,
        builder: (context, scrollController) => Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
              child: Row(
                children: [
                  Text(
                    'モデルを選択',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  const Spacer(),
                  TextButton(
                    onPressed: () => Navigator.pop(context),
                    child: const Text('閉じる'),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: DropdownButtonFormField<String>(
                initialValue: _selectedProvider,
                decoration: const InputDecoration(
                  labelText: 'プロバイダー',
                  prefixIcon: Icon(Icons.cloud_outlined),
                  isDense: true,
                ),
                items: [
                  const DropdownMenuItem(value: null, child: Text('すべて')),
                  ...providers.map(
                    (p) => DropdownMenuItem(
                      value: p.providerId,
                      child: Text(
                        '${p.displayName}${p.configured ? " ✓" : ""}',
                      ),
                    ),
                  ),
                ],
                onChanged: (v) => setState(() => _selectedProvider = v),
              ),
            ),
            const SizedBox(height: 8),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: TextField(
                decoration: const InputDecoration(
                  labelText: '検索',
                  prefixIcon: Icon(Icons.search),
                  isDense: true,
                ),
                onChanged: (v) => setState(() => _query = v),
              ),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: ListView.builder(
                controller: scrollController,
                itemCount: models.length,
                itemBuilder: (context, index) {
                  final m = models[index];
                  return ListTile(
                    leading: Icon(
                      m.supportsVision
                          ? Icons.visibility_outlined
                          : m.supportsThinking
                          ? Icons.psychology_outlined
                          : Icons.chat_outlined,
                      size: 20,
                    ),
                    title: Text(
                      m.displayName,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    subtitle: Text(
                      '${m.providerId} · ${m.modelId}${m.maxContext > 0 ? " · ${_formatContext(m.maxContext)}" : ""}',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                    trailing: Wrap(
                      spacing: 4,
                      children: [
                        if (m.supportsToolCalling)
                          _MiniTag(icon: Icons.build_outlined),
                        if (m.supportsVision)
                          _MiniTag(icon: Icons.image_outlined),
                      ],
                    ),
                    onTap: () => Navigator.pop(context, m),
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }

  String _formatContext(int tokens) {
    if (tokens >= 1000000) {
      return '${(tokens / 1000000).toStringAsFixed(1)}M ctx';
    }
    if (tokens >= 1000) return '${(tokens / 1000).toStringAsFixed(0)}K ctx';
    return '$tokens ctx';
  }
}

List<String> _stringList(Object? value, {List<String> fallback = const []}) {
  if (value is! List) return fallback;
  return value
      .map((e) => e.toString())
      .where((e) => e.trim().isNotEmpty)
      .toList();
}

class _MiniTag extends StatelessWidget {
  const _MiniTag({required this.icon});
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Icon(icon, size: 14, color: Theme.of(context).colorScheme.outline);
  }
}
