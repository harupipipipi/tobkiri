import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { AlertTriangle, ArrowLeft, Check, ChevronDown, ChevronUp, ExternalLink, Hand, Loader2, Mic, Radio, RefreshCcw, Settings, Shield, Video, Volume2, VolumeX, X } from "lucide-react";

import { cn } from "../lib/cn";
import { api, defaultspackUrlWithLocalAuth, type Conversation } from "../lib/api";
import {
  authorityRequestSettledStatus,
  resolvePendingAuthorityApproval,
  type AuthorityApproval,
} from "../lib/authorityApproval";
import { subscribeAuthorityApprovalSettlements } from "../lib/authorityApprovalEvents";
import {
  browserAuthorityApprovalPath,
} from "../lib/authorityApprovalBrowserToken";
import { openDefaultsConsoleWindow, openFingerRecordingWindow, openAuthorityApprovalWindow, openDefaultspackMainWindow, openHostPermissionsPageWindow } from "../lib/desktopApproval";
import { LayerPortal } from "../ui/layers/LayerPortal";
import { ambientTriggerClient, type AmbientEventPayload, type AmbientStatus } from "./ambientTriggerClient";
import { ambientConversationCompletionFromSnapshot, waitForAmbientAssistantResponse } from "./ambientConversationCompletion";
import { safeLocalStorageGet, safeLocalStorageSet } from "./ambientStorage";
import type { AmbientFinalAnswerPayload } from "./finalAnswerBridge";
import { AmbientMiniChat } from "./AmbientMiniChat";
import { buildAmbientDispatchTemplateContext, mergeAmbientDispatchMetadata } from "./ambientDispatchContext";
import {
  ambientConversationIdFromResult,
  ambientLatestAssistantFinal,
  ambientLinkedConversationId,
  ambientPendingAuthorityApproval,
} from "./ambientMiniChatState";
import {
  audioCaptureConstraints,
  captureAudioEmbedding,
  deviceLabel,
  probeOsPermissions,
  settleSpeechRecognitionTranscript,
  startPinchAudioRecorder,
  startPinchSpeechRecognition,
  startWakeListening,
  testMicrophoneInput,
  videoCaptureConstraints,
  type ActiveAudioRecorder,
  type SpeechRecognitionLike,
} from "./ambientMedia";
import {
  AMBIENT_AUTHORITY_REQUEST_ID,
  AMBIENT_CAMERA_PERMISSION,
  AMBIENT_MIC_PERMISSION,
  AMBIENT_OS_PERMISSIONS,
  AMBIENT_REQUIRED_PERMISSIONS,
  ambientCopyJa,
  ambientOperationLabels,
  ambientPendingInputLabel,
  ambientRenderableMessage,
  deriveAmbientUiState,
  grantedPermissionCount,
  hasAllOsPermissions,
  hasAllRumiPermissions,
  type AmbientRuntimeStatus,
  type AmbientUiState,
} from "./ambientUiState";
import type { HandTrackingFrame } from "./mediaPipeHandLandmarker";
import type { PinchState } from "./gesturePinchDetector";
import { ChatPickerDialog, CompactRoutingControl, RoutingSettings } from "./AmbientRoutingSettings";
import { PrimaryActionIcon, StateBadge, StatusGlyph, primaryButtonClass } from "./AmbientTriggerVisuals";
import { gestureStatusLabel } from "./AmbientPermissionSections";
import { useFinalAnswerBridge } from "./useFinalAnswerBridge";
import { useAmbientHandTracker } from "./useAmbientHandTracker";
import { useAmbientRouting } from "./useAmbientRouting";

export type AmbientApprovalTarget = {
  kind: "browser" | "runtime" | "authority";
  approveLabel?: string;
  rejectLabel?: string;
  canApprove?: boolean;
  canReject?: boolean;
};

type Props = {
  conversationId?: string | null;
  onOpenInput?: (text?: string) => void;
  approvalTarget?: AmbientApprovalTarget | null;
  onApprovalGesture?: (decision: "approve" | "reject") => void | Promise<void>;
  finalAnswerText?: string | null;
  variant?: "floating" | "window";
  debugMode?: boolean;
  selectedModel?: string | null;
  selectedToolIds?: readonly string[] | null;
  templateAiParams?: Record<string, unknown> | null;
  templateToolPolicy?: Record<string, unknown> | null;
};

type MiniAuthorityApprovalResolution = {
  sourceRequestId: string;
  sourceConversationId: string | null;
  approval: AuthorityApproval | null;
  stale: boolean;
};

type TauriAmbientWindow = Window & {
  __TAURI_INTERNALS__?: unknown;
  __TAURI__?: unknown;
};

const MIC_DEVICE_STORAGE_KEY = "rumi.ambient.selectedMicId";
const CAMERA_DEVICE_STORAGE_KEY = "rumi.ambient.selectedCameraId";
const THUMB_TIP_INDEX = 4;
const INDEX_TIP_INDEX = 8;
const MINI_AUTHORITY_CONTINUATION_PENDING_ERROR = "承認後の続行がまだ完了していません。もう一度送信してください。";
const MINI_AUTHORITY_CONTINUATION_POLL_DELAYS_MS = [700, 1400, 2400, 3600] as const;
const EMPTY_SELECTED_TOOL_IDS: readonly string[] = [];
const HAND_LANDMARK_CONNECTIONS = [
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [5, 9], [9, 10], [10, 11], [11, 12],
  [9, 13], [13, 14], [14, 15], [15, 16],
  [13, 17], [17, 18], [18, 19], [19, 20],
  [0, 17],
] as const;

export function AmbientTriggerPanel({
  conversationId,
  onOpenInput,
  approvalTarget,
  onApprovalGesture,
  finalAnswerText,
  variant = "floating",
  debugMode = false,
  selectedModel,
  selectedToolIds,
  templateAiParams,
  templateToolPolicy,
}: Props) {
  const [status, setStatus] = useState<AmbientStatus | null>(null);
  const standalone = variant === "window";
  const [expanded, setExpanded] = useState(() => standalone);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [manualRumiFallbackOpen, setManualRumiFallbackOpen] = useState(false);
  const [rumiApprovalOpen, setRumiApprovalOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [miniConversationIdOverride, setMiniConversationIdOverride] = useState<string | null>(null);
  const [miniConversation, setMiniConversation] = useState<Conversation | null>(null);
  const [miniChatLoading, setMiniChatLoading] = useState(false);
  const [miniChatError, setMiniChatError] = useState<string | null>(null);
  const [miniAuthorityApprovalResolution, setMiniAuthorityApprovalResolution] = useState<MiniAuthorityApprovalResolution | null>(null);
  const [miniInput, setMiniInput] = useState("");
  const [miniSending, setMiniSending] = useState(false);
  const [miniChatCreating, setMiniChatCreating] = useState(false);
  const [latestSubmittedInput, setLatestSubmittedInput] = useState<string | null>(null);
  const [cameraStream, setCameraStream] = useState<MediaStream | null>(null);
  const [videoElement, setVideoElement] = useState<HTMLVideoElement | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [devicesChecked, setDevicesChecked] = useState(false);
  const [selectedMicId, setSelectedMicId] = useState(() => safeLocalStorageGet(MIC_DEVICE_STORAGE_KEY));
  const [selectedCameraId, setSelectedCameraId] = useState(() => safeLocalStorageGet(CAMERA_DEVICE_STORAGE_KEY));
  const [micListening, setMicListening] = useState(false);
  const [pinchRecording, setPinchRecording] = useState(false);
  const [pinchTranscriptPreview, setPinchTranscriptPreview] = useState("");
  const [recordingStartedAt, setRecordingStartedAt] = useState<number | null>(null);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const [pinchDetectorStatus, setPinchDetectorStatus] = useState("idle");
  const [trackingFrame, setTrackingFrame] = useState<HandTrackingFrame | null>(null);
  const [cameraDebugOpen, setCameraDebugOpen] = useState(false);
  const [privacyOpen, setPrivacyOpen] = useState(false);
  const [micTestBusy, setMicTestBusy] = useState(false);
  const [micTestStatus, setMicTestStatus] = useState("未実行");
  const [micTestLevel, setMicTestLevel] = useState<number | null>(null);
  const [transcriptionTestBusy, setTranscriptionTestBusy] = useState(false);
  const [transcriptionTestStatus, setTranscriptionTestStatus] = useState("未実行");
  const [transcriptionTestText, setTranscriptionTestText] = useState("");
  const cameraStreamRef = useRef<MediaStream | null>(null);
  const cameraStreamCleanupRef = useRef<(() => void) | null>(null);
  const cameraAcquireInFlightRef = useRef(false);
  const audioStopRef = useRef<(() => void) | null>(null);
  const pinchRecorderRef = useRef<ActiveAudioRecorder | null>(null);
  const pinchSpeechRecognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const pinchTranscriptRef = useRef("");
  const lastPinchStateRef = useRef<PinchState | null>(null);
  const choiceHandledAtRef = useRef(0);
  const approvalGestureBusyRef = useRef(false);
  const rumiApprovalAutoOpenRef = useRef(false);
  const miniAuthorityApprovalAutoOpenedRef = useRef(new Set<string>());
  const miniAuthorityContinuationWaitRef = useRef(new Set<string>());
  const miniAuthorityContinuationErrorRequestRef = useRef<string | null>(null);
  const conversationIdRef = useRef<string | null | undefined>(conversationId);
  const onOpenInputRef = useRef<Props["onOpenInput"]>(onOpenInput);
  const approvalTargetRef = useRef<Props["approvalTarget"]>(approvalTarget);
  const onApprovalGestureRef = useRef<Props["onApprovalGesture"]>(onApprovalGesture);
  const miniChatRequestSeqRef = useRef(0);
  const pendingAmbientResponseRef = useRef<{
    conversationId: string;
    previousAssistantMessageId: string | null;
    submittedAt: number;
  } | null>(null);
  const previousSelectedCameraIdRef = useRef(selectedCameraId);
  const cameraVideoRef = useCallback((node: HTMLVideoElement | null) => {
    setVideoElement(node);
  }, []);

  const readoutBlocked = useCallback(() => pinchRecording || Boolean(pinchRecorderRef.current), [pinchRecording]);
  const miniFinalAnswer = useMemo(
    () => standalone ? ambientLatestAssistantFinal(miniConversation) : null,
    [miniConversation, standalone],
  );
  const finalAnswerPayload = useMemo<AmbientFinalAnswerPayload | null>(() => {
    if (miniFinalAnswer) {
      return {
        conversation_id: miniConversation?.id ?? null,
        message_id: miniFinalAnswer.messageId,
        message_created_at: miniFinalAnswer.createdAt,
        text: miniFinalAnswer.text,
        updated_at: miniFinalAnswer.createdAt || Date.now(),
      };
    }
    const text = String(finalAnswerText ?? "").trim();
    return text
      ? { conversation_id: conversationId ?? null, message_id: null, message_created_at: null, text, updated_at: Date.now() }
      : null;
  }, [conversationId, finalAnswerText, miniConversation?.id, miniFinalAnswer]);
  const {
    frontOnFinal,
    setFrontOnFinal,
    frontFlash,
    readoutEnabled,
    setReadoutEnabled,
    stopSpeechReadout,
  } = useFinalAnswerBridge({
    finalAnswer: finalAnswerPayload,
    standalone,
    pinchRecording,
    readoutBlocked,
    onFrontRequested: () => setExpanded(true),
    onMessage: setMessage,
  });
  const routing = useAmbientRouting({
    status,
    conversationId,
    setStatus,
    setBusy,
    setMessage,
    refresh,
  });
  const {
    chatPickerOpen,
    setChatPickerOpen,
    conversationsLoading,
    routingMode,
    routingConversationId,
    routingGroupEnabled,
    routingGroupId,
    setRoutingGroupId,
    routingGroupTitle,
    setRoutingGroupTitle,
    routingModel,
    setRoutingModel,
    aiSendApprovalRequired,
    modelQuery,
    setModelQuery,
    modelResults,
    modelLoading,
    routingNeedsNewChatSettings,
    routingChatItems,
    routingSummary,
    loadConversations,
    openChatPicker,
    saveRouting,
    saveRoutingModel,
    selectConversationForRouting,
    searchRoutingModels,
  } = routing;

  const monitorEnabled = Boolean(status?.ambient_monitor.enabled);
  const cameraDevices = useMemo(() => devices.filter((device) => device.kind === "videoinput"), [devices]);
  const cameraUnavailable = devicesChecked && cameraDevices.length === 0;
  const runtimeStatus = useMemo<AmbientRuntimeStatus>(() => {
    if (pinchDetectorStatus === "error") return "error";
    if (cameraUnavailable || pinchDetectorStatus === "unavailable") return "blocked";
    if (pinchDetectorStatus === "transcribing") return "transcribing";
    if (pinchDetectorStatus === "sending") return "sending";
    if (pinchRecording || pinchDetectorStatus === "recording") return "recording";
    if (monitorEnabled) return "monitoring";
    return "off";
  }, [cameraUnavailable, monitorEnabled, pinchDetectorStatus, pinchRecording]);
  const uiState = useMemo(() => deriveAmbientUiState(status, runtimeStatus), [runtimeStatus, status]);
  const stateCopy = ambientCopyJa.states[uiState];
  const manualFallbackIsOsPermission = uiState === "denied" || uiState === "blocked" || uiState === "osPermissionNeeded";
  const rumiPermissionCount = useMemo(
    () => grantedPermissionCount(status, AMBIENT_REQUIRED_PERMISSIONS, "rumi"),
    [status],
  );
  const osPermissionCount = useMemo(
    () => grantedPermissionCount(status, AMBIENT_OS_PERMISSIONS, "os"),
    [status],
  );
  const allRumiPermissionsGranted = useMemo(() => hasAllRumiPermissions(status), [status]);
  const allOsPermissionsGranted = useMemo(() => hasAllOsPermissions(status), [status]);
  const rumiApprovalPending = rumiApprovalOpen && !allRumiPermissionsGranted;
  const surfaceTitle = standalone && (window.location.pathname === "/finger-recording" || window.location.pathname === "/ambient-debug") ? ambientCopyJa.subtitle : ambientCopyJa.title;
  const pendingApproval = status?.pending_approval ?? null;
  const visibleMessage = useMemo(() => ambientRenderableMessage(message), [message]);
  const ambientDispatchGranted = Boolean(status?.permissions.rumi["ambient.trigger.dispatch"]?.granted);
  const micRumiPermissionGranted = Boolean(status?.permissions.rumi[AMBIENT_MIC_PERMISSION]?.granted);
  const localTranscription = status?.local_transcription ?? null;
  const localTranscriptionConfigured = Boolean(localTranscription?.configured);
  const localTranscriptionQuality = localWhisperQualityLabel(localTranscription?.model_quality);
  const selectedDispatchToolIds = selectedToolIds ?? EMPTY_SELECTED_TOOL_IDS;
  const selectedDispatchToolIdsKey = selectedDispatchToolIds.join("\0");
  const explicitDebugConversationId = debugMode ? cleanString(conversationId) : null;
  const linkedAmbientConversationId = useMemo(
    () => explicitDebugConversationId ?? ambientLinkedConversationId(status, conversationId),
    [
      conversationId,
      explicitDebugConversationId,
      status?.routing?.conversation_id,
      status?.routing?.mode,
      status?.routing?.session_conversation_id,
    ],
  );
  const miniConversationId = miniConversationIdOverride || linkedAmbientConversationId;
  const miniAuthorityApprovalConversationId = miniConversation?.id ?? miniConversationId ?? null;
  const miniAuthorityApprovalCandidate = useMemo(
    () => ambientPendingAuthorityApproval(miniConversation),
    [miniConversation],
  );
  const miniAuthorityApprovalResolved = miniAuthorityApprovalCandidate
    && miniAuthorityApprovalResolution?.sourceRequestId === miniAuthorityApprovalCandidate.requestId
    && miniAuthorityApprovalResolution?.sourceConversationId === miniAuthorityApprovalConversationId
    ? miniAuthorityApprovalResolution
    : null;
  const miniAuthorityApproval = miniAuthorityApprovalResolved?.approval ?? null;
  const miniAuthorityApprovalResolving = Boolean(miniAuthorityApprovalCandidate && !miniAuthorityApprovalResolved);
  const miniAuthorityBlocksInput = miniAuthorityApprovalResolving || Boolean(miniAuthorityApproval);
  const browserApprovalQaEnabled = standalone && debugMode;
  const miniBrowserApprovalDirectUrl = browserApprovalQaEnabled && miniAuthorityApproval && !hasNativeAuthorityApprovalWindow()
    ? browserAuthorityApprovalPath(miniAuthorityApproval.requestId, ambientAuthorityApprovalReturnPath())
    : null;
  const inlineSettingsControlsVisible = !standalone;
  const miniChatRoutingSummary = standalone ? "次の送信で作成" : routingSummary;
  const dispatchTemplateContext = useMemo(() => buildAmbientDispatchTemplateContext({
    model: routingModel || selectedModel || "",
    selectedToolIds: selectedDispatchToolIds,
    templateParams: templateAiParams,
    templateToolPolicy,
  }), [
    routingModel,
    selectedDispatchToolIdsKey,
    selectedModel,
    templateAiParams,
    templateToolPolicy,
  ]);

  useEffect(() => {
    conversationIdRef.current = conversationId;
    onOpenInputRef.current = onOpenInput;
    approvalTargetRef.current = approvalTarget;
    onApprovalGestureRef.current = onApprovalGesture;
  }, [approvalTarget, conversationId, onApprovalGesture, onOpenInput]);

  const activateMiniConversationFromSubmitResult = useCallback((result: Record<string, unknown>, fallbackConversationId?: string | null) => {
    const targetConversationId = ambientSubmittedConversationIdFromResult(result) || cleanString(fallbackConversationId);
    if (targetConversationId) setMiniConversationIdOverride(targetConversationId);
    return targetConversationId;
  }, []);

  const loadMiniConversation = useCallback(async (options?: { conversationId?: string | null; quiet?: boolean }) => {
    const requestSeq = miniChatRequestSeqRef.current + 1;
    miniChatRequestSeqRef.current = requestSeq;
    const targetId = String(options?.conversationId ?? miniConversationId ?? "").trim();
    if (!options?.quiet) setMiniChatLoading(true);
    try {
      let conversation: Conversation | null = null;
      if (targetId) {
        conversation = await api.getConversation(targetId);
      } else {
        const result = await api.listConversations({
          tag: "ambient",
          group_id: "gesture",
          include_messages: true,
          limit: 1,
        });
        conversation = result.conversations[0] ?? null;
      }
      if (miniChatRequestSeqRef.current !== requestSeq) return;
      setMiniConversation((current) => fresherMiniConversation(current, conversation));
      const stuckRequestId = miniAuthorityContinuationErrorRequestRef.current;
      if (stuckRequestId && !miniAuthorityContinuationResolved(conversation, stuckRequestId)) {
        setMiniChatError(MINI_AUTHORITY_CONTINUATION_PENDING_ERROR);
      } else {
        if (stuckRequestId) miniAuthorityContinuationErrorRequestRef.current = null;
        setMiniChatError(null);
      }
    } catch (error) {
      if (miniChatRequestSeqRef.current === requestSeq) {
        setMiniChatError(error instanceof Error ? error.message : "チャットを読み込めませんでした。");
      }
    } finally {
      if (!options?.quiet) {
        setMiniChatLoading(false);
      }
    }
  }, [miniConversationId]);

  const settleAmbientSubmission = useCallback(async ({
    result,
    targetConversationId,
    previousAssistantMessageId,
    submittedAt,
  }: {
    result: Record<string, unknown>;
    targetConversationId: string | null;
    previousAssistantMessageId: string | null;
    submittedAt: number;
  }) => {
    const resultStatus = String(result.status ?? "");
    if (resultStatus === "approval_required") {
      if (targetConversationId) {
        pendingAmbientResponseRef.current = {
          conversationId: targetConversationId,
          previousAssistantMessageId,
          submittedAt,
        };
      }
      const approvalMessage = `${ambientOperationLabels.approvalPending}: AIへ送る前に確認が必要です。`;
      setPinchDetectorStatus("approval_pending");
      setMessage(approvalMessage);
      await loadMiniConversation({ conversationId: targetConversationId, quiet: true });
      return;
    }
    if (resultStatus && resultStatus !== "ok") {
      pendingAmbientResponseRef.current = null;
      const errorMessage = ambientResultMessage(result, "AIに送信できませんでした。");
      setPinchDetectorStatus("error");
      setMessage(errorMessage);
      setMiniChatError(errorMessage);
      return;
    }
    if (!targetConversationId) {
      const waitingMessage = `${ambientOperationLabels.waitingResponse}: AIの返答を待っています。`;
      setPinchDetectorStatus("waiting_response");
      setMessage(waitingMessage);
      await loadMiniConversation({ quiet: true });
      return;
    }

    pendingAmbientResponseRef.current = {
      conversationId: targetConversationId,
      previousAssistantMessageId,
      submittedAt,
    };
    const waitingMessage = `${ambientOperationLabels.waitingResponse}: AIの返答を待っています。`;
    setPinchDetectorStatus("waiting_response");
    setMessage(waitingMessage);
    const outcome = await waitForAmbientAssistantResponse({
      conversationId: targetConversationId,
      previousAssistantMessageId,
      submittedAt,
      fetchConversation: api.getConversation,
    });
    if (outcome.conversation) setMiniConversation((current) => fresherMiniConversation(current, outcome.conversation));
    if (outcome.status === "completed") {
      pendingAmbientResponseRef.current = null;
      const completedMessage = `${ambientOperationLabels.done}: AIの回答が届きました。`;
      setPinchDetectorStatus("completed");
      setLatestSubmittedInput(null);
      setMiniChatError(null);
      setMessage(completedMessage);
      return;
    }
    if (outcome.status === "approval_required") {
      const approvalMessage = `${ambientOperationLabels.approvalPending}: AIへ送る前に確認が必要です。`;
      setPinchDetectorStatus("approval_pending");
      setMessage(approvalMessage);
      return;
    }
    setPinchDetectorStatus("waiting_response");
  }, [loadMiniConversation]);

  useEffect(() => {
    const pending = pendingAmbientResponseRef.current;
    if (!pending || !miniConversation || miniConversation.id !== pending.conversationId) return;
    const outcome = ambientConversationCompletionFromSnapshot({
      conversation: miniConversation,
      previousAssistantMessageId: pending.previousAssistantMessageId,
      submittedAt: pending.submittedAt,
    });
    if (outcome.status === "approval_required") {
      setPinchDetectorStatus("approval_pending");
      return;
    }
    if (outcome.status !== "completed") return;
    pendingAmbientResponseRef.current = null;
    const completedMessage = `${ambientOperationLabels.done}: AIの回答が届きました。`;
    setPinchDetectorStatus("completed");
    setLatestSubmittedInput(null);
    setMiniChatError(null);
    setMessage(completedMessage);
  }, [miniConversation]);

  useEffect(() => {
    void loadMiniConversation();
    const interval = window.setInterval(() => {
      void loadMiniConversation({ quiet: true });
    }, miniConversationId ? 1800 : 3500);
    return () => window.clearInterval(interval);
  }, [loadMiniConversation, miniConversationId]);

  useEffect(() => {
    let cancelled = false;
    if (!miniAuthorityApprovalCandidate) {
      setMiniAuthorityApprovalResolution(null);
      return () => {
        cancelled = true;
      };
    }
    setMiniAuthorityApprovalResolution(null);
    void (async () => {
      const resolvedApproval = await resolveMiniAuthorityApprovalTarget(
        miniAuthorityApprovalCandidate,
        miniAuthorityApprovalConversationId,
      );
      if (cancelled) return;
      setMiniAuthorityApprovalResolution({
        sourceRequestId: miniAuthorityApprovalCandidate.requestId,
        sourceConversationId: miniAuthorityApprovalConversationId,
        approval: resolvedApproval,
        stale: !resolvedApproval,
      });
      if (!resolvedApproval) {
        setMiniChatError("前のAI使用許可は完了済みです。もう一度送信してください。");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [miniAuthorityApprovalCandidate?.requestId, miniAuthorityApprovalConversationId]);

  useEffect(() => {
    if (!miniAuthorityApproval) return;
    const requestId = miniAuthorityApproval.requestId;
    if (miniAuthorityApprovalAutoOpenedRef.current.has(requestId)) return;
    miniAuthorityApprovalAutoOpenedRef.current.add(requestId);
    setExpanded(true);
    void openMiniAuthorityApproval(miniAuthorityApproval, { auto: true });
  }, [miniAuthorityApproval?.requestId]);

  useEffect(() => {
    if (!miniAuthorityApproval) return undefined;
    return subscribeAuthorityApprovalSettlements((event) => {
      if (event.requestId !== miniAuthorityApproval.requestId) return;
      const targetConversationId = event.conversationId || miniConversation?.id || miniConversationId || null;
      if (miniAuthorityContinuationErrorRequestRef.current === event.requestId) {
        miniAuthorityContinuationErrorRequestRef.current = null;
      }
      if (event.status === "denied") {
        setMiniChatError("AIの使用が許可されませんでした。");
        setMessage("AIの使用は許可されませんでした。");
        if (targetConversationId) setMiniConversationIdOverride(targetConversationId);
        void loadMiniConversation({ conversationId: targetConversationId, quiet: true });
        return;
      }
      if (targetConversationId) setMiniConversationIdOverride(targetConversationId);
      setMiniChatError(null);
      setMessage("AIが続きを作成しています。");
      void loadMiniConversation({ conversationId: targetConversationId, quiet: true });
      void waitForMiniAuthorityContinuation(miniAuthorityApproval, targetConversationId);
    }, {
      replayStored: true,
      replayStoredRequestId: miniAuthorityApproval.requestId,
      expected: {
        requestId: miniAuthorityApproval.requestId,
        principalId: miniAuthorityApproval.principalId,
        permissionId: miniAuthorityApproval.permissionId,
        resource: miniAuthorityApproval.resource,
        ...(miniAuthorityApprovalConversationId
          ? { conversationId: miniAuthorityApprovalConversationId }
          : {}),
      },
    });
  }, [
    loadMiniConversation,
    miniAuthorityApproval,
    miniAuthorityApprovalConversationId,
    miniConversation?.id,
    miniConversationId,
  ]);

  useEffect(() => {
    let cancelled = false;
    refresh({ probeOs: true })
      .then(() => {
        if (!cancelled) void refreshDevices();
      })
      .catch((error) => {
        if (!cancelled) setMessage(error instanceof Error ? error.message : "指で録音の状態を確認できませんでした。");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    safeLocalStorageSet(MIC_DEVICE_STORAGE_KEY, selectedMicId);
  }, [selectedMicId]);

  useEffect(() => {
    safeLocalStorageSet(CAMERA_DEVICE_STORAGE_KEY, selectedCameraId);
  }, [selectedCameraId]);

  useEffect(() => {
    if (!pinchRecording || !recordingStartedAt) {
      setRecordingSeconds(0);
      return;
    }
    const update = () => setRecordingSeconds(Math.max(0, Math.floor((performance.now() - recordingStartedAt) / 1000)));
    update();
    const timer = window.setInterval(update, 500);
    return () => window.clearInterval(timer);
  }, [pinchRecording, recordingStartedAt]);

  useEffect(() => subscribeAuthorityApprovalSettlements((event) => {
    if (event.requestId !== AMBIENT_AUTHORITY_REQUEST_ID) return;
    if (event.status === "denied") {
      setMessage("許可しませんでした。必要になったらもう一度許可できます。");
      return;
    }
    verifyRumiPermissionStatus();
  }, {
    replayStored: true,
    replayStoredRequestId: AMBIENT_AUTHORITY_REQUEST_ID,
    expected: { requestId: AMBIENT_AUTHORITY_REQUEST_ID },
  }), []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (!params.has("authority_approved")) return;
    params.delete("authority_approved");
    const nextSearch = params.toString();
    const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`;
    window.history.replaceState(null, "", nextUrl);
    verifyRumiPermissionStatus();
  }, []);

  function verifyRumiPermissionStatus(): void {
    setMessage("Tobkiriの許可状態を確認しています。");
    void loadStatus({ probeOs: true })
      .then((latest) => {
        if (hasAllRumiPermissions(latest)) {
          setRumiApprovalOpen(false);
          setMessage("使えるようになりました。次にMacのマイク/カメラを確認します。");
          return;
        }
        setMessage("許可はまだ確認できません。承認ウィンドウの状態を確認してください。");
      })
      .catch((error) => {
        setMessage(error instanceof Error ? error.message : "Tobkiriの許可状態を確認できませんでした。");
      });
  }

  useEffect(() => {
    if (!rumiApprovalPending) return;
    pinchRecorderRef.current?.cancel();
    pinchRecorderRef.current = null;
    stopPinchSpeechRecognition(true);
    setPinchTranscriptPreview("");
    setPinchRecording(false);
    setRecordingStartedAt(null);
    setPinchDetectorStatus("approval_pending");
    audioStopRef.current?.();
    audioStopRef.current = null;
    setMicListening(false);
  }, [rumiApprovalPending]);

  useEffect(() => {
    if (!status || allRumiPermissionsGranted || rumiApprovalAutoOpenRef.current) return;
    rumiApprovalAutoOpenRef.current = true;
    setExpanded(true);
    void openRumiPermissionApproval();
  }, [allRumiPermissionsGranted, status]);

  function replaceCameraStream(nextStream: MediaStream | null) {
    const current = cameraStreamRef.current;
    cameraStreamCleanupRef.current?.();
    cameraStreamCleanupRef.current = null;
    if (current && current !== nextStream) {
      current.getTracks().forEach((track) => track.stop());
    }
    cameraStreamRef.current = nextStream;
    cameraStreamCleanupRef.current = nextStream ? watchCameraStreamLifecycle(nextStream) : null;
    setCameraStream(nextStream);
  }

  function watchCameraStreamLifecycle(stream: MediaStream): () => void {
    const handleEnded = () => {
      if (cameraStreamRef.current !== stream) return;
      replaceCameraStream(null);
      setTrackingFrame(null);
      setPinchDetectorStatus("unavailable");
      setMessage("カメラの接続が切れました。接続を確認してから、合図待ちをもう一度開始してください。");
      void ambientTriggerClient.stopMonitor()
        .catch(() => undefined)
        .finally(() => refresh({ probeOs: true }).catch(() => undefined));
    };
    const tracks = stream.getVideoTracks();
    for (const track of tracks) {
      track.addEventListener("ended", handleEnded, { once: true });
      track.addEventListener("mute", handleEnded, { once: true });
    }
    return () => {
      for (const track of tracks) {
        track.removeEventListener("ended", handleEnded);
        track.removeEventListener("mute", handleEnded);
      }
    };
  }

  useEffect(() => {
    if (!videoElement) return;
    videoElement.srcObject = cameraStream;
    if (cameraStream) {
      void videoElement.play().catch((error) => {
        console.info("[ambient] camera video play was blocked", error);
      });
    }
    return () => {
      if (videoElement.srcObject === cameraStream) {
        videoElement.srcObject = null;
      }
    };
  }, [cameraStream, videoElement]);

  useEffect(() => () => {
    replaceCameraStream(null);
    pinchRecorderRef.current?.cancel();
    pinchRecorderRef.current = null;
    try {
      pinchSpeechRecognitionRef.current?.abort();
    } catch {
      // Some webviews throw if recognition already stopped.
    }
    pinchSpeechRecognitionRef.current = null;
    pinchTranscriptRef.current = "";
    audioStopRef.current?.();
    audioStopRef.current = null;
  }, []);

  function stopPinchSpeechRecognition(abort = false): string {
    const recognition = pinchSpeechRecognitionRef.current;
    pinchSpeechRecognitionRef.current = null;
    if (recognition) {
      try {
        if (abort) recognition.abort();
        else recognition.stop();
      } catch {
        // Some webviews throw if recognition already stopped.
      }
    }
    return pinchTranscriptRef.current.trim();
  }

  async function settlePinchSpeechRecognition(): Promise<string> {
    const recognition = pinchSpeechRecognitionRef.current;
    pinchSpeechRecognitionRef.current = null;
    return settleSpeechRecognitionTranscript(recognition, () => pinchTranscriptRef.current, { timeoutMs: 900 });
  }

  const finishPinchRecording = useCallback(async (state: PinchState) => {
    const recorder = pinchRecorderRef.current;
    if (!recorder) return;
    pinchRecorderRef.current = null;
    setPinchRecording(false);
    setRecordingStartedAt(null);
    const transcript = await settlePinchSpeechRecognition();
    setPinchTranscriptPreview("");
    setPinchDetectorStatus("transcribing");
    setMessage(`${ambientOperationLabels.transcribing}: 録音音声を文字にしています。`);
    setLatestSubmittedInput(transcript || null);
    try {
      const recording = await recorder.stop();
      if (recording.size <= 0) {
        setMessage(`${ambientOperationLabels.failed}: 録音が空でした。もう一度お試しください。`);
        setMiniChatError("録音が空でした。もう一度お試しください。");
        setPinchDetectorStatus("tracking");
        return;
      }
      if (transcript) {
        setPinchDetectorStatus("sending");
        setMessage(`${ambientOperationLabels.sending}: 文字起こしをAIへ送っています。`);
      }
      const submittedAt = Date.now();
      const requestedConversationId = miniConversationId || conversationIdRef.current || null;
      const previousAssistantMessageId = miniConversation?.id === requestedConversationId
        ? ambientLatestAssistantFinal(miniConversation)?.messageId ?? null
        : null;
      const result = await ambientTriggerClient.submitEvent({
        source: "camera",
        trigger: "pinch",
        mode: "dispatch_audio",
        action_id: "chat.message",
        ...dispatchTemplateContext.eventPayload,
        params: ambientParamsWithTranscriptionLanguage(dispatchTemplateContext.eventPayload.params),
        ...(transcript ? { input_text: transcript } : {}),
        conversation_id: requestedConversationId || undefined,
        confidence: state.confidence,
        duration_ms: recording.durationMs,
        audio_mime_type: recording.mimeType,
        audio_size: recording.size,
        audio_name: `ok-mark-recording.${recording.extension}`,
        metadata: mergeAmbientDispatchMetadata({
          panel: "ambient_mini_window",
          hand: state.hand,
          normalized_distance: state.normalizedDistance,
          hold_to_record: true,
          transcript_available: Boolean(transcript),
          ...(transcript ? { transcript_source: "web_speech_api" } : {}),
        }, dispatchTemplateContext),
        attachments: [
          {
            id: `ambient-audio-${Date.now()}`,
            name: `ok-mark-recording.${recording.extension}`,
            type: recording.mimeType,
            size: recording.size,
            duration_ms: recording.durationMs,
            dataUrl: recording.dataUrl,
            source: "ambient.camera_pinch_hold",
            ephemeral: true,
            do_not_persist: true,
            ...(transcript ? { transcript, transcription: transcript, transcript_source: "web_speech_api" } : {}),
          },
        ],
      });
      const resultConversationId = activateMiniConversationFromSubmitResult(result, requestedConversationId);
      onOpenInputRef.current?.("");
      focusComposer();
      await refresh();
      await settleAmbientSubmission({
        result,
        targetConversationId: resultConversationId || requestedConversationId,
        previousAssistantMessageId,
        submittedAt,
      });
    } catch (error) {
      const errorText = error instanceof Error ? error.message : "送信できませんでした。録音は保存されていません。";
      setMessage(`${ambientOperationLabels.failed}: ${errorText}`);
      setMiniChatError(errorText);
    }
  }, [activateMiniConversationFromSubmitResult, dispatchTemplateContext, miniConversation, miniConversationId, settleAmbientSubmission]);

  useEffect(() => {
    if (!pinchRecording || !recordingStartedAt) return;
    const remainingMs = Math.max(0, 30_000 - (performance.now() - recordingStartedAt));
    const timer = window.setTimeout(() => {
      const fallbackState = lastPinchStateRef.current ?? {
        active: false,
        triggered: false,
        confidence: 1,
        normalizedDistance: 0,
        hand: "Unknown",
      } satisfies PinchState;
      void finishPinchRecording({
        ...fallbackState,
        active: false,
        releasedAt: performance.now(),
        reason: "max_duration",
      });
    }, remainingMs);
    return () => window.clearTimeout(timer);
  }, [finishPinchRecording, pinchRecording, recordingStartedAt]);

  const beginPinchRecording = useCallback(async (state: PinchState) => {
    if (pinchRecorderRef.current) return;
    if (!allRumiPermissionsGranted || !allOsPermissionsGranted || rumiApprovalPending) {
      setMessage("Tobkiriの許可と端末のマイク・カメラ許可がそろってから録音できます。");
      return;
    }
    lastPinchStateRef.current = state;
    stopSpeechReadout();
    pinchTranscriptRef.current = "";
    setPinchTranscriptPreview("");
    setPinchDetectorStatus("recording");
    setMessage(`${ambientOperationLabels.recording}: OKマークを作ったまま話してください。指を開くと送信します。`);
    try {
      const recorder = await startPinchAudioRecorder(selectedMicId || undefined);
      pinchRecorderRef.current = recorder;
      pinchSpeechRecognitionRef.current = startPinchSpeechRecognition((transcript) => {
        pinchTranscriptRef.current = transcript;
        setPinchTranscriptPreview(transcript);
      });
      setPinchRecording(true);
      setRecordingStartedAt(performance.now());
      await ambientTriggerClient.submitEvent({
        source: "camera",
        trigger: "pinch",
        mode: "record_audio_start",
        action_id: "chat.message",
        confidence: state.confidence,
        metadata: {
          panel: "ambient_mini_window",
          hand: state.hand,
          normalized_distance: state.normalizedDistance,
          hold_to_record: true,
        },
      }).catch(() => undefined);
      await refresh();
    } catch (error) {
      pinchRecorderRef.current?.cancel();
      pinchRecorderRef.current = null;
      stopPinchSpeechRecognition(true);
      setPinchTranscriptPreview("");
      setPinchRecording(false);
      setRecordingStartedAt(null);
      setPinchDetectorStatus("tracking");
      setMessage(`${ambientOperationLabels.failed}: ${error instanceof Error ? error.message : "録音を開始できませんでした。"}`);
    }
  }, [allOsPermissionsGranted, allRumiPermissionsGranted, rumiApprovalPending, selectedMicId]);

  const submitFingerChoice = useCallback(async (state: PinchState) => {
    const choice = state.fingerChoice;
    if (choice !== 2 && choice !== 3 && choice !== 4) return;
    const now = performance.now();
    if (now - choiceHandledAtRef.current < 800) return;
    const approvalDecision = approvalDecisionForChoice(choice, approvalTargetRef.current);
    if (!approvalDecision) return;
    choiceHandledAtRef.current = now;
    if (pinchRecorderRef.current) {
      pinchRecorderRef.current.cancel();
      pinchRecorderRef.current = null;
      setPinchRecording(false);
      setRecordingStartedAt(null);
      stopPinchSpeechRecognition(true);
      setPinchTranscriptPreview("");
    }
    await submitApprovalGesture(approvalDecision, state, `choice_${choice}`);
  }, []);

  const handleApprovalSwipe = useCallback(async (state: PinchState) => {
    const decision = state.approvalGesture;
    if (decision !== "approve" && decision !== "reject") return;
    if (!approvalTargetRef.current) return;
    await submitApprovalGesture(decision, state, `swipe_${decision}`);
  }, []);

  const handlePinchState = useCallback((state: PinchState) => {
    lastPinchStateRef.current = state;
    if (state.approvalGestureCommitted) {
      void handleApprovalSwipe(state);
      return;
    }
    if (state.choiceCommitted) {
      if (approvalTargetRef.current) {
        void submitFingerChoice(state);
      }
      return;
    }
    if (state.triggered) {
      void beginPinchRecording(state);
      return;
    }
    if (state.reason === "pinch_released" || state.releasedAt) {
      void finishPinchRecording(state);
    }
  }, [beginPinchRecording, finishPinchRecording, handleApprovalSwipe, submitFingerChoice]);

  useAmbientHandTracker({
    approvalTargetActive: Boolean(approvalTarget),
    cameraStream,
    monitorEnabled,
    onPinchState: handlePinchState,
    rumiApprovalPending,
    setMessage,
    setPinchDetectorStatus,
    setTrackingFrame,
    videoElement,
  });

  useEffect(() => {
    const previous = previousSelectedCameraIdRef.current;
    previousSelectedCameraIdRef.current = selectedCameraId;
    if (previous === selectedCameraId) return;
    if (!monitorEnabled || !allRumiPermissionsGranted || !allOsPermissionsGranted || rumiApprovalPending) return;
    if (!navigator.mediaDevices?.getUserMedia) return;
    let cancelled = false;
    setPinchDetectorStatus("loading");
    navigator.mediaDevices.getUserMedia({ video: videoCaptureConstraints(selectedCameraId || undefined) })
      .then((stream) => {
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        replaceCameraStream(stream);
        void refreshDevices();
      })
      .catch((error) => {
        if (!cancelled) {
          setMessage(error instanceof Error ? error.message : "カメラを切り替えられませんでした。");
          setPinchDetectorStatus(cameraStream ? "tracking" : "unavailable");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    allOsPermissionsGranted,
    allRumiPermissionsGranted,
    cameraStream,
    monitorEnabled,
    rumiApprovalPending,
    selectedCameraId,
  ]);

  useEffect(() => {
    if (!monitorEnabled || cameraStream || cameraAcquireInFlightRef.current) return;
    if (!allRumiPermissionsGranted || !allOsPermissionsGranted || rumiApprovalPending || cameraUnavailable) return;
    let cancelled = false;
    setPinchDetectorStatus("loading");
    (async () => {
      try {
        await acquireCameraForMonitoring();
        if (!cancelled) {
          setMessage("待機を再開しました。OKマークで録音開始、指を開くと送信します。");
        }
      } catch (error) {
        if (cancelled) return;
        const messageText = error instanceof Error ? error.message : "カメラ監視を再開できませんでした。";
        setPinchDetectorStatus("unavailable");
        setMessage(messageText);
        await ambientTriggerClient.stopMonitor().catch(() => undefined);
        await refresh({ probeOs: true }).catch(() => undefined);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    allOsPermissionsGranted,
    allRumiPermissionsGranted,
    cameraStream,
    cameraUnavailable,
    monitorEnabled,
    rumiApprovalPending,
    selectedCameraId,
  ]);

  async function loadStatus(options?: { probeOs?: boolean }): Promise<AmbientStatus> {
    let next = await ambientTriggerClient.status();
    setStatus(next);
    if (options?.probeOs) {
      const statuses = await probeOsPermissions();
      if (Object.keys(statuses).length > 0) {
        next = await ambientTriggerClient.checkOsPermissions(statuses);
        setStatus(next);
      }
    }
    return next;
  }

  async function refresh(options?: { probeOs?: boolean }): Promise<void> {
    await loadStatus(options);
  }

  async function refreshDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) {
      setDevices([]);
      setDevicesChecked(true);
      return;
    }
    try {
      const nextDevices = await navigator.mediaDevices.enumerateDevices();
      setDevices(nextDevices.filter((device) => device.kind === "audioinput" || device.kind === "videoinput"));
    } catch (error) {
      console.info("[ambient] media device listing unavailable", error);
      setDevices([]);
    } finally {
      setDevicesChecked(true);
    }
  }

  async function acquireCameraForMonitoring(): Promise<MediaStream> {
    if (cameraAcquireInFlightRef.current) {
      throw new Error("カメラを起動中です。少し待ってからもう一度お試しください。");
    }
    if (cameraUnavailable) {
      throw new Error("カメラが見つかりません。接続してからデバイス更新を押してください。");
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error("このブラウザではカメラを使用できません。");
    }
    cameraAcquireInFlightRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: videoCaptureConstraints(selectedCameraId || undefined) });
      replaceCameraStream(stream);
      await refreshDevices();
      return stream;
    } catch (error) {
      if (isMediaDeviceNotFound(error)) throw new Error("カメラが見つかりません。接続してからデバイス更新を押してください。");
      throw error;
    } finally {
      cameraAcquireInFlightRef.current = false;
    }
  }

  async function runAction(action: () => Promise<AmbientStatus | Record<string, unknown>>, success?: string) {
    setBusy(true);
    setMessage(null);
    try {
      const result = await action();
      if (isAmbientStatus(result)) setStatus(result);
      else await refresh();
      if (success) setMessage(success);
    } catch (error) {
      setExpanded(true);
      setMessage(error instanceof Error ? error.message : "操作を完了できませんでした。");
      await refresh({ probeOs: true }).catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  function toggleReadoutEnabled() {
    const next = !readoutEnabled;
    setReadoutEnabled(next);
    if (!next) stopSpeechReadout();
  }

  async function openRumiPermissionApproval() {
    setManualRumiFallbackOpen(false);
    setMessage(null);
    try {
      const opened = await openAuthorityApprovalWindow(AMBIENT_AUTHORITY_REQUEST_ID);
      if (opened) {
        setRumiApprovalOpen(true);
        setMessage("Tobkiriの承認ウィンドウを開きました。そこで許可してください。");
        return;
      }
    } catch (error) {
      console.info("[ambient] authority approval window unavailable", error);
    }
    setRumiApprovalOpen(false);
    setManualRumiFallbackOpen(true);
    setMessage("Tobkiri Launcherの承認ウィンドウを開けませんでした。Viewerから開き直して許可してください。");
  }

  async function openMiniAuthorityApproval(approval = miniAuthorityApproval, options?: { auto?: boolean }) {
    if (!approval) return;
    if (!options?.auto) setExpanded(true);
    setMiniChatError(null);
    try {
      const resolvedApproval = await resolveMiniAuthorityApprovalTarget(approval, miniAuthorityApprovalConversationId);
      if (!resolvedApproval) {
        setMiniAuthorityApprovalResolution({
          sourceRequestId: approval.requestId,
          sourceConversationId: miniAuthorityApprovalConversationId,
          approval: null,
          stale: true,
        });
        setMiniChatError("前のAI使用許可は完了済みです。もう一度送信してください。");
        return;
      }
      if (resolvedApproval.requestId !== approval.requestId) {
        setMiniAuthorityApprovalResolution({
          sourceRequestId: miniAuthorityApprovalCandidate?.requestId ?? approval.requestId,
          sourceConversationId: miniAuthorityApprovalConversationId,
          approval: resolvedApproval,
          stale: false,
        });
      }
      const opened = await openAuthorityApprovalWindow(resolvedApproval.requestId);
      if (opened) {
        if (!options?.auto) setMessage("AI使用の承認ウィンドウを開きました。");
        return;
      }
      if (browserApprovalQaEnabled) {
        const approvalUrl = browserAuthorityApprovalPath(resolvedApproval.requestId, ambientAuthorityApprovalReturnPath());
        const popup = window.open(approvalUrl, `rumi-authority-approval-${resolvedApproval.requestId}`, "width=720,height=820,noopener,noreferrer");
        if (popup) {
          if (!options?.auto) setMessage("ブラウザ承認ページを開きました。");
          return;
        }
        setMiniChatError("ブラウザのポップアップがブロックされました。同じタブの承認リンクを開いてください。");
        return;
      }
    } catch (error) {
      console.info("[ambient] authority approval window unavailable", error);
    }
    setMiniChatError("承認ウィンドウを開けませんでした。Tobkiri Launcherから承認を開いてください。");
  }

  async function resolveMiniAuthorityApprovalTarget(
    approval: AuthorityApproval,
    currentConversationId: string | null,
  ): Promise<AuthorityApproval | null> {
    try {
      const pending = await api.listAuthorityRequests({ status: "pending" });
      const resolvedApproval = resolvePendingAuthorityApproval(approval, pending.pending ?? [], {
        conversationId: currentConversationId,
        requireConversationMatch: true,
        requirePrincipalMatch: true,
      });
      if (resolvedApproval) return resolvedApproval;
    } catch (error) {
      console.info("[ambient] pending authority request lookup unavailable", error);
    }

    try {
      const currentRequest = await api.getAuthorityRequest(approval.requestId);
      if (authorityRequestSettledStatus(currentRequest.status)) return null;
      return approval;
    } catch (error) {
      console.info("[ambient] authority request status lookup unavailable", error);
      return approval;
    }
  }

  async function waitForMiniAuthorityContinuation(approval: AuthorityApproval, targetConversationId: string | null) {
    const requestId = approval.requestId;
    if (miniAuthorityContinuationWaitRef.current.has(requestId)) return;
    const conversationId = targetConversationId || miniConversation?.id || miniConversationId || null;
    if (!conversationId) return;
    miniAuthorityContinuationWaitRef.current.add(requestId);
    try {
      for (const delayMs of MINI_AUTHORITY_CONTINUATION_POLL_DELAYS_MS) {
        await sleep(delayMs);
        const latestConversation = await api.getConversation(conversationId);
        setMiniConversationIdOverride(conversationId);
        setMiniConversation((current) => fresherMiniConversation(current, latestConversation));
        if (miniAuthorityContinuationResolved(latestConversation, requestId)) {
          if (miniAuthorityContinuationErrorRequestRef.current === requestId) {
            miniAuthorityContinuationErrorRequestRef.current = null;
          }
          setMiniChatError(null);
          return;
        }
      }
      miniAuthorityContinuationErrorRequestRef.current = requestId;
      setMessage(MINI_AUTHORITY_CONTINUATION_PENDING_ERROR);
      setMiniChatError(MINI_AUTHORITY_CONTINUATION_PENDING_ERROR);
    } catch (error) {
      console.warn("[ambient] authority approval continuation polling failed", error);
      miniAuthorityContinuationErrorRequestRef.current = requestId;
      setMessage(MINI_AUTHORITY_CONTINUATION_PENDING_ERROR);
      setMiniChatError(MINI_AUTHORITY_CONTINUATION_PENDING_ERROR);
    } finally {
      miniAuthorityContinuationWaitRef.current.delete(requestId);
    }
  }

  async function openAmbientWindow() {
    setMessage(null);
    try {
      const opened = await openFingerRecordingWindow();
      if (opened) return;
    } catch (error) {
      console.info("[ambient] finger recording window unavailable", error);
    }
    setMessage("Tobkiri Launcherから開くと、指録音は小さな別ウィンドウで表示されます。");
  }

  async function requestMediaPermissions() {
    if (!allRumiPermissionsGranted || rumiApprovalPending) {
      setExpanded(true);
      await openRumiPermissionApproval();
      return;
    }
    await runAction(async () => {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("このブラウザではマイク・カメラを使用できません。");
      }
      const micStream = await navigator.mediaDevices.getUserMedia({ audio: audioCaptureConstraints(selectedMicId || undefined) });
      micStream.getTracks().forEach((track) => track.stop());
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: videoCaptureConstraints(selectedCameraId || undefined) });
      } catch (error) {
        if (isMediaDeviceNotFound(error)) throw new Error("カメラが見つかりません。接続してからデバイス更新を押してください。");
        throw error;
      }
      replaceCameraStream(stream);
      await refreshDevices();
      return ambientTriggerClient.checkOsPermissions({
        [AMBIENT_MIC_PERMISSION]: "granted",
        [AMBIENT_CAMERA_PERMISSION]: "granted",
      });
    }, "マイクとカメラを使用できます。次は手の認識を開始してください。");
  }

  async function startMonitoring() {
    if (!allRumiPermissionsGranted || !allOsPermissionsGranted || rumiApprovalPending) {
      setExpanded(true);
      setMessage("Tobkiriの許可と端末のマイク・カメラ許可がそろってから合図待ちを開始できます。");
      return;
    }
    await runAction(async () => {
      if (cameraUnavailable) {
        throw new Error("カメラが見つかりません。接続してからデバイス更新を押してください。");
      }
      if (!cameraStream) await acquireCameraForMonitoring();
      return ambientTriggerClient.startMonitor({ voice_wake: true, gesture_pinch: true });
    }, "待機中です。OKマークで録音開始、指を開くと送信します。");
  }

  async function stopMonitoring() {
    await runAction(async () => {
      pinchRecorderRef.current?.cancel();
      pinchRecorderRef.current = null;
      setPinchRecording(false);
      setRecordingStartedAt(null);
      setTrackingFrame(null);
      replaceCameraStream(null);
      return ambientTriggerClient.stopMonitor();
    }, "停止しました。マイク・カメラの監視は止まっています。");
  }

  async function enrollWakeVoice() {
    if (!allRumiPermissionsGranted || !allOsPermissionsGranted || rumiApprovalPending) {
      setMessage("Tobkiriの許可と端末のマイク許可がそろってから声で起動を登録できます。");
      return;
    }
    setBusy(true);
    setMessage("声で起動するための音声を短く録音しています。");
    try {
      const embedding = await captureAudioEmbedding(900, selectedMicId || undefined);
      const result = await ambientTriggerClient.submitEvent({
        source: "microphone",
        trigger: "voice_wake",
        mode: "enroll_wake_voice",
        audio_embedding: embedding,
        metadata: { panel: "ambient_mini_window" },
      });
      setMessage(String(result.reason ?? "声で起動する音声を登録しました。"));
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "声で起動する音声を登録できませんでした。");
    } finally {
      setBusy(false);
    }
  }

  async function toggleMicListening() {
    if (micListening) {
      audioStopRef.current?.();
      audioStopRef.current = null;
      setMicListening(false);
      return;
    }
    try {
      if (!allRumiPermissionsGranted || !allOsPermissionsGranted || rumiApprovalPending) {
        setMessage("Tobkiriの許可と端末のマイク許可がそろってから音声待機を開始できます。");
        return;
      }
      const stop = await startWakeListening(async (embedding) => {
        const result = await ambientTriggerClient.submitEvent({
          source: "microphone",
          trigger: "voice_wake",
          mode: "open_input",
          audio_embedding: embedding,
          metadata: { panel: "ambient_mini_window" },
        });
        if (result.status === "open_input" || result.open_input) {
          onOpenInput?.("");
          focusComposer();
        }
      }, selectedMicId || undefined, {
        onError: (error) => {
          audioStopRef.current = null;
          setMicListening(false);
          setMessage(error instanceof Error ? error.message : "音声待機が停止しました。マイク接続を確認してください。");
        },
      });
      audioStopRef.current = stop;
      setMicListening(true);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "マイクを開始できませんでした。");
    }
  }

  async function runMicInputTest() {
    if (!micRumiPermissionGranted || rumiApprovalPending) {
      setMicTestStatus("Tobkiriのマイク利用許可を完了してください。");
      setExpanded(true);
      return;
    }
    if (pinchRecording || pinchRecorderRef.current) {
      setMicTestStatus("録音中はマイクテストを実行できません。");
      return;
    }
    setMicTestBusy(true);
    setMicTestLevel(null);
    setMicTestStatus("確認中です。1秒ほど話してください。");
    try {
      const result = await testMicrophoneInput(1400, selectedMicId || undefined);
      const level = Math.max(result.peak, result.rms * 4);
      setMicTestLevel(level);
      if (level >= 0.03) {
        setMicTestStatus(`入力OK: 音量 ${formatMicLevel(level)}`);
        await ambientTriggerClient.checkOsPermissions({ [AMBIENT_MIC_PERMISSION]: "granted" }).catch(() => undefined);
        await refresh().catch(() => undefined);
      } else {
        setMicTestStatus(`入力が小さいです: 音量 ${formatMicLevel(level)}。マイク選択やOS許可を確認してください。`);
      }
    } catch (error) {
      setMicTestStatus(error instanceof Error ? error.message : "マイクテストに失敗しました。");
      await ambientTriggerClient.checkOsPermissions({ [AMBIENT_MIC_PERMISSION]: "denied" }).catch(() => undefined);
      await refresh().catch(() => undefined);
    } finally {
      setMicTestBusy(false);
    }
  }

  async function runTranscriptionTest() {
    if (!micRumiPermissionGranted || !ambientDispatchGranted || rumiApprovalPending) {
      setTranscriptionTestStatus("Tobkiriのマイク/トリガー利用許可を完了してください。");
      setExpanded(true);
      return;
    }
    if (pinchRecording || pinchRecorderRef.current) {
      setTranscriptionTestStatus("録音中は文字起こしテストを実行できません。");
      return;
    }
    setTranscriptionTestBusy(true);
    setTranscriptionTestText("");
    setTranscriptionTestStatus("録音中です。3秒ほど話してください。");
    let recorder: ActiveAudioRecorder | null = null;
    try {
      recorder = await startPinchAudioRecorder(selectedMicId || undefined);
      await sleep(3200);
      const recording = await recorder.stop();
      recorder = null;
      setTranscriptionTestStatus("文字起こし中です。");
      const result = await ambientTriggerClient.submitEvent({
        source: "microphone",
        trigger: "transcription_test",
        mode: "transcribe_audio_test",
        action_id: "chat.message",
        duration_ms: recording.durationMs,
        audio_data_url: recording.dataUrl,
        audio_mime_type: recording.mimeType,
        audio_size: recording.size,
        audio_name: `mic-transcription-test.${recording.extension}`,
        model: routingModel || undefined,
        params: ambientParamsWithTranscriptionLanguage(undefined),
        metadata: {
          panel: "ambient_settings",
          test_kind: "transcription",
        },
      });
      const transcript = String(result.transcript ?? "").trim();
      const transcription = result.transcription && typeof result.transcription === "object"
        ? result.transcription as Record<string, unknown>
        : {};
      if (transcript) {
        setTranscriptionTestText(transcript);
        setTranscriptionTestStatus(`文字起こしOK: ${String(transcription.source || transcription.model || "local")}`);
      } else {
        const detail = String(transcription.reason || transcription.code || result.reason || "").trim();
        setTranscriptionTestStatus(detail ? `文字起こしできませんでした: ${detail}` : "文字起こしできませんでした。もう少し長く、はっきり話してください。");
      }
    } catch (error) {
      setTranscriptionTestStatus(error instanceof Error ? error.message : "文字起こしテストに失敗しました。");
    } finally {
      recorder?.cancel();
      setTranscriptionTestBusy(false);
    }
  }

  async function submitMiniChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = miniInput.trim();
    if (!text || miniSending) return;
    if (!ambientDispatchGranted || rumiApprovalPending) {
      const errorText = "AIへ送る許可を完了してから送信できます。";
      setMiniChatError(errorText);
      setExpanded(true);
      return;
    }
    if (miniAuthorityBlocksInput) {
      setMiniChatError("AIの使用許可を開いてから続行します。");
      setExpanded(true);
      return;
    }

    const targetConversationId = miniConversationId || conversationIdRef.current || null;
    const submittedAt = Date.now();
    const previousAssistantMessageId = miniConversation?.id === targetConversationId
      ? ambientLatestAssistantFinal(miniConversation)?.messageId ?? null
      : null;
    setMiniSending(true);
    miniAuthorityContinuationErrorRequestRef.current = null;
    setMiniChatError(null);
    setLatestSubmittedInput(text);
    setMiniInput("");
    try {
      const result = await ambientTriggerClient.submitEvent({
        source: "hook",
        trigger: "external_hook",
        mode: "preset_text",
        action_id: "chat.message",
        ...dispatchTemplateContext.eventPayload,
        input_text: text,
        conversation_id: targetConversationId || undefined,
        metadata: mergeAmbientDispatchMetadata({
          panel: "ambient_mini_window",
          manual_text_input: true,
        }, dispatchTemplateContext),
      });
      const resultConversationId = activateMiniConversationFromSubmitResult(result, targetConversationId);
      await refresh();
      await settleAmbientSubmission({
        result,
        targetConversationId: resultConversationId || targetConversationId,
        previousAssistantMessageId,
        submittedAt,
      });
    } catch (error) {
      const errorText = error instanceof Error ? error.message : "送信できませんでした。";
      setMiniChatError(errorText);
      setMessage(`${ambientOperationLabels.failed}: ${errorText}`);
    } finally {
      setMiniSending(false);
    }
  }

  async function selectMiniChatRoutingConversation(chatId: string) {
    setMiniConversationIdOverride(null);
    await selectConversationForRouting(chatId);
    setChatPickerOpen(false);
  }

  async function createMiniChatRoutingConversation() {
    if (miniChatCreating) return;
    setMiniChatCreating(true);
    setMiniChatError(null);
    try {
      const conversation = await api.createConversation({
        model: routingModel || selectedModel || undefined,
        tags: ["integration:ambient", "ambient"],
        metadata: {
          source: "ambient_finger_recording",
          group_id: routingGroupEnabled ? routingGroupId || "gesture" : undefined,
          group_title: routingGroupEnabled ? routingGroupTitle || "Gesture" : undefined,
        },
      });
      setMiniConversationIdOverride(conversation.id);
      setMiniConversation(conversation);
      await selectConversationForRouting(conversation.id);
      await loadConversations();
      await loadMiniConversation({ conversationId: conversation.id, quiet: true });
      setChatPickerOpen(false);
      setMessage("新規チャットを作成しました。");
    } catch (error) {
      const errorText = error instanceof Error ? error.message : "新規チャットを作成できませんでした。";
      setMiniChatError(errorText);
      setMessage(errorText);
    } finally {
      setMiniChatCreating(false);
    }
  }

  async function openMiniChatConversation() {
    const targetConversationId = miniConversation?.id || miniConversationId || null;
    if (!targetConversationId) return;
    const path = `/chat?chat=${encodeURIComponent(targetConversationId)}`;
    try {
      if (await openDefaultspackMainWindow(path)) return;
      const popup = window.open(defaultspackUrlWithLocalAuth(path), "rumi-defaultspack", "width=980,height=720");
      if (popup) {
        popup.focus();
        return;
      }
    } catch (error) {
      console.info("[ambient] defaultspack main window unavailable", error);
    }
    setMiniChatError("Defaultspack本体ウィンドウを開けませんでした。Tobkiri LauncherからDefaultspackを開いてください。");
  }

  async function submitApprovalGesture(decision: "approve" | "reject", state: PinchState, mode: string) {
    const target = approvalTargetRef.current;
    if (!target || approvalGestureBusyRef.current) return;
    if (decision === "approve" && target.canApprove === false) return;
    if (decision === "reject" && target.canReject === false) {
      setMessage("この承認では拒否ジェスチャーは使えません。");
      return;
    }
    approvalGestureBusyRef.current = true;
    setMessage(decision === "approve" ? "承認ジェスチャーを受け取りました。" : "拒否ジェスチャーを受け取りました。");
    try {
      const auditResult = await ambientTriggerClient.submitEvent({
        source: "camera",
        trigger: "approval_gesture",
        mode,
        action_id: "chat.message",
        confidence: state.confidence,
        decision,
        metadata: {
          panel: "ambient_mini_window",
          approval_kind: target.kind,
          hand: state.hand,
          normalized_distance: state.normalizedDistance,
          finger_choice: state.fingerChoice,
        },
      });
      if (auditResult.status !== "approval_intent") {
        throw new Error("承認ジェスチャーを監査に記録できませんでした。もう一度お試しください。");
      }
      await onApprovalGestureRef.current?.(decision);
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "承認ジェスチャーを処理できませんでした。");
    } finally {
      approvalGestureBusyRef.current = false;
      setPinchDetectorStatus("tracking");
    }
  }

  async function approvePendingApproval() {
    const requestId = pendingApproval?.request_id;
    if (!requestId) return;
    setBusy(true);
    setMessage(null);
    try {
      const pendingResponse = pendingAmbientResponseRef.current;
      const fallbackConversationId = pendingApproval?.conversation_id || pendingResponse?.conversationId || null;
      const result = await ambientTriggerClient.approvePendingApproval(requestId);
      const resultConversationId = activateMiniConversationFromSubmitResult(result, fallbackConversationId);
      onOpenInputRef.current?.("");
      focusComposer();
      await refresh();
      await settleAmbientSubmission({
        result,
        targetConversationId: resultConversationId || fallbackConversationId,
        previousAssistantMessageId: pendingResponse?.previousAssistantMessageId ?? null,
        submittedAt: pendingResponse?.submittedAt ?? Date.now(),
      });
    } catch (error) {
      const errorText = error instanceof Error ? error.message : "送信を許可できませんでした。";
      setMessage(`${ambientOperationLabels.failed}: ${errorText}`);
      setMiniChatError(errorText);
      await refresh().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  async function denyPendingApproval() {
    const requestId = pendingApproval?.request_id;
    if (!requestId) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await ambientTriggerClient.denyPendingApproval(requestId, "user_cancelled");
      setMessage(String(result.status ?? "") === "denied" ? "送信を破棄しました。" : String(result.reason ?? "送信を破棄しました。"));
      await refresh();
    } catch (error) {
      setMessage(`${ambientOperationLabels.failed}: ${error instanceof Error ? error.message : "送信待ちを破棄できませんでした。"}`);
      await refresh().catch(() => undefined);
    } finally {
      setBusy(false);
    }
  }

  async function handlePrimaryAction() {
    if (cameraUnavailable && (uiState === "readyOff" || uiState === "paused" || uiState === "blocked")) {
      setExpanded(true);
      setSettingsOpen(true);
      setMessage("カメラが見つかりません。接続してからデバイス更新を押してください。");
      return;
    }
    switch (uiState) {
      case "setupNeeded":
        setExpanded(true);
        await openRumiPermissionApproval();
        return;
      case "rumiPermissionNeeded":
        setExpanded(true);
        await openRumiPermissionApproval();
        return;
      case "osPermissionNeeded":
        setExpanded(true);
        await requestMediaPermissions();
        return;
      case "readyOff":
      case "paused":
        await startMonitoring();
        return;
      case "monitoring":
        await stopMonitoring();
        return;
      case "recording":
        pinchRecorderRef.current?.cancel();
        pinchRecorderRef.current = null;
        setPinchRecording(false);
        setRecordingStartedAt(null);
        setPinchDetectorStatus("tracking");
        setMessage("録音をキャンセルしました。保存はされていません。");
        return;
      case "denied":
      case "blocked":
        setExpanded(true);
        if (!(await openHostPermissionsPageWindow())) {
          setManualRumiFallbackOpen(true);
        }
        return;
      case "error":
        await refresh({ probeOs: true });
        return;
      case "transcribing":
      case "sending":
        return;
    }
  }

  const settingsSection = (
    <section className="space-y-2">
      <p className="text-[11px] font-semibold uppercase text-zinc-500">設定</p>
      <button
        type="button"
        onClick={toggleReadoutEnabled}
        disabled={rumiApprovalPending}
        aria-pressed={readoutEnabled}
        className={cn("ambient-mini-button w-full justify-between", readoutEnabled && "border-emerald-400/30 text-emerald-200")}
      >
        <span className="inline-flex min-w-0 items-center gap-2">
          {readoutEnabled ? <Volume2 size={14} /> : <VolumeX size={14} />}
          <span className="truncate">読み上げ</span>
        </span>
        <span className="shrink-0 text-[11px]">{readoutEnabled ? "オン" : "オフ"}</span>
      </button>
      {allRumiPermissionsGranted && (
        <RoutingSettings
          busy={busy}
          mode={routingMode}
          summary={routingSummary}
          selectedConversationId={routingConversationId}
          groupEnabled={routingGroupEnabled}
          groupId={routingGroupId}
          groupTitle={routingGroupTitle}
          model={routingModel}
          aiSendApprovalRequired={aiSendApprovalRequired}
          modelQuery={modelQuery}
          modelResults={modelResults}
          modelLoading={modelLoading}
          needsNewChatSettings={routingNeedsNewChatSettings}
          onModeChange={(mode) => void saveRouting({ mode })}
          onPickChat={() => void openChatPicker()}
          onGroupEnabledChange={(enabled) => void saveRouting({ group_enabled: enabled }, enabled ? "新しいチャットをグループ内に作ります。" : "新しいチャットを通常の履歴に作ります。")}
          onGroupIdChange={setRoutingGroupId}
          onGroupTitleChange={setRoutingGroupTitle}
          onGroupCommit={() => void saveRouting({ group_id: routingGroupId, group_title: routingGroupTitle }, "新しいチャットのグループを保存しました。")}
          onModelChange={setRoutingModel}
          onModelCommit={(model) => void saveRoutingModel(model)}
          onModelQueryChange={setModelQuery}
          onModelSearch={() => void searchRoutingModels()}
          onAiSendApprovalRequiredChange={(enabled) => void saveRouting(
            { ai_send_approval_required: enabled },
            enabled ? "AIへ送る前に確認します。" : "AIへすぐ送る設定にしました。",
          )}
        />
      )}
      <label className="block text-[11px] text-zinc-500">
        マイク
        <select
          value={selectedMicId}
          onChange={(event) => setSelectedMicId(event.target.value)}
          className="mt-1 h-8 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-xs text-zinc-200"
        >
          <option value="">デフォルト</option>
          {devices.filter((device) => device.kind === "audioinput").map((device, index) => (
            <option key={device.deviceId || `mic-${index}`} value={device.deviceId}>
              {deviceLabel(device, index, "マイク")}
            </option>
          ))}
        </select>
      </label>
      <label className="block text-[11px] text-zinc-500">
        カメラ
        <select
          value={selectedCameraId}
          onChange={(event) => setSelectedCameraId(event.target.value)}
          disabled={cameraUnavailable}
          className="mt-1 h-8 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-xs text-zinc-200"
        >
          <option value="">デフォルト</option>
          {cameraDevices.map((device, index) => (
            <option key={device.deviceId || `camera-${index}`} value={device.deviceId}>
              {deviceLabel(device, index, "カメラ")}
            </option>
          ))}
        </select>
      </label>
      {cameraUnavailable && (
        <div className="rounded-md border border-red-400/30 bg-red-500/10 px-2 py-1.5 text-[11px] leading-5 text-red-100">
          カメラが見つかりません。接続してからデバイス更新を押してください。
        </div>
      )}
      <div className="grid grid-cols-2 gap-2">
        <button type="button" onClick={() => void refreshDevices()} disabled={rumiApprovalPending} className="ambient-mini-button">
          <Settings size={14} />
          デバイス更新
        </button>
        <button type="button" onClick={() => void refresh({ probeOs: true })} disabled={rumiApprovalPending} className="ambient-mini-button">
          <Shield size={14} />
          許可を再確認
        </button>
      </div>
      <section className="space-y-2 rounded-md border border-zinc-800 bg-zinc-950/60 p-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] font-semibold text-zinc-300">マイク確認</span>
          {micTestBusy || transcriptionTestBusy ? <Loader2 size={13} className="animate-spin text-sky-200" /> : null}
        </div>
        <p className={cn("rounded-md border px-2 py-1 text-[11px] leading-5", localTranscriptionConfigured ? "border-emerald-400/25 bg-emerald-400/10 text-emerald-100" : "border-amber-400/25 bg-amber-400/10 text-amber-100")}>
          文字起こし: {localTranscriptionConfigured
            ? `ローカルWhisper OK・${localTranscriptionQuality}${localTranscription?.command_label ? ` (${localTranscription.command_label})` : ""}`
            : localTranscription?.reason || "ローカルWhisperを確認中です。"}
        </p>
        <div className="grid grid-cols-2 gap-2">
          <button
            type="button"
            onClick={() => void runMicInputTest()}
            disabled={rumiApprovalPending || !micRumiPermissionGranted || micTestBusy || transcriptionTestBusy}
            className="ambient-mini-button"
          >
            <Mic size={14} />
            入力テスト
          </button>
          <button
            type="button"
            onClick={() => void runTranscriptionTest()}
            disabled={rumiApprovalPending || !micRumiPermissionGranted || !ambientDispatchGranted || micTestBusy || transcriptionTestBusy}
            className="ambient-mini-button"
          >
            <Radio size={14} />
            文字起こし
          </button>
        </div>
        <div className="space-y-1 text-[11px] leading-5 text-zinc-400">
          <p className={cn("flex items-center gap-1", micTestLevel !== null && micTestLevel >= 0.03 ? "text-emerald-200" : "")}>
            {micTestLevel !== null && micTestLevel >= 0.03 ? <Check size={12} /> : null}
            <span>{micTestStatus}</span>
          </p>
          {micTestLevel !== null && (
            <div className="h-1.5 overflow-hidden rounded-full bg-zinc-800">
              <div
                className="h-full rounded-full bg-emerald-400"
                style={{ width: formatMicLevel(micTestLevel) }}
              />
            </div>
          )}
          <p>{transcriptionTestStatus}</p>
          {transcriptionTestText && (
            <p className="rounded-md border border-emerald-400/25 bg-emerald-400/10 px-2 py-1 text-emerald-100">
              {transcriptionTestText}
            </p>
          )}
        </div>
      </section>
      <button
        type="button"
        onClick={() => setCameraDebugOpen((value) => !value)}
        disabled={rumiApprovalPending}
        className={cn("ambient-mini-button w-full", cameraDebugOpen && "border-amber-400/30 text-amber-100")}
      >
        <Video size={14} />
        カメラ映像を確認する（開発者向け）
      </button>
      <button
        type="button"
        onClick={() => setFrontOnFinal((value) => !value)}
        className={cn("ambient-mini-button w-full", frontOnFinal && "border-emerald-400/30 text-emerald-200")}
      >
        <Radio size={14} />
        最終回答で前面表示: {frontOnFinal ? "有効" : "無効"}
      </button>
      <button
        type="button"
        onClick={() => void openDefaultsConsoleWindow().then((opened) => {
          if (!opened) setMessage("Tobkiri Launcherから開くと、詳細ログは別ウィンドウで表示されます。");
        })}
        className="ambient-mini-button w-full"
      >
        <ExternalLink size={14} />
        詳細ログを開く
      </button>
      <div className="grid grid-cols-2 gap-2">
        <button type="button" onClick={() => void enrollWakeVoice()} disabled={rumiApprovalPending} className="ambient-mini-button">
          <Mic size={14} />
          声で起動を登録
        </button>
        <button type="button" onClick={() => void toggleMicListening()} disabled={rumiApprovalPending} className="ambient-mini-button">
          <Radio size={14} />
          {micListening ? "音声待機を停止" : "音声待機を開始"}
        </button>
      </div>
      <div className="border-l border-emerald-400/35 pl-2 text-[11px] leading-5 text-zinc-400">
        <p className="font-semibold text-zinc-300">プライバシー</p>
        <p>音声と映像は保存しません。残るのは、使われた時刻と結果だけです。</p>
      </div>
    </section>
  );

  const content = (
    <>
      <section
        className={cn(
          standalone
            ? "flex h-screen w-full flex-col overflow-hidden bg-zinc-950 text-zinc-200"
            : "fixed bottom-4 right-4 flex max-h-[calc(100vh-2rem)] w-[min(400px,calc(100vw-24px))] flex-col overflow-hidden rounded-xl border border-zinc-800/90 bg-zinc-950/96 text-zinc-200 shadow-2xl shadow-black/40 backdrop-blur",
          frontFlash && "border-emerald-300/60 shadow-emerald-500/20",
          stateCopy.tone === "red" && "border-red-400/35",
          stateCopy.tone === "blue" && "border-sky-400/30",
          uiState === "recording" && "shadow-red-500/20",
        )}
        aria-label={surfaceTitle}
      >
        <div className="flex items-start gap-3 border-b border-zinc-800/80 px-3.5 py-3">
          <StatusGlyph uiState={uiState} />
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate text-[15px] font-semibold leading-5 text-zinc-50">{surfaceTitle}</span>
              <StateBadge state={uiState} />
            </div>
            <p className="mt-1 text-[12px] leading-5 text-zinc-300">{stateCopy.headline}</p>
          </div>
          <button
            type="button"
            onClick={() => {
              setSettingsOpen((value) => !value);
              if (!standalone) setExpanded(true);
            }}
            disabled={rumiApprovalPending}
            className={cn(
              "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-35",
              settingsOpen && "border-sky-300/35 bg-sky-400/10 text-sky-100",
            )}
            title="設定"
            aria-label="指録音の設定"
          >
            <Settings size={15} />
          </button>
          {!standalone && (
            <button
              type="button"
              onClick={() => void openAmbientWindow()}
              className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100"
              title="別ウィンドウで開く"
              aria-label="指で録音を別ウィンドウで開く"
            >
              <ExternalLink size={15} />
            </button>
          )}
          {!standalone && (
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100"
              title={expanded ? "閉じる" : "詳しく見る"}
              aria-label={expanded ? "指で録音の詳細を閉じる" : "指で録音の詳細を見る"}
            >
              {expanded ? <ChevronDown size={16} /> : <ChevronUp size={16} />}
            </button>
          )}
        </div>

        {monitorEnabled && !(standalone && settingsOpen) && (
          <RecognitionMonitor
            videoRef={cameraVideoRef}
            frame={trackingFrame}
            status={pinchDetectorStatus}
            recording={pinchRecording}
            recordingSeconds={recordingSeconds}
            debug={cameraDebugOpen}
          />
        )}
        {monitorEnabled && standalone && settingsOpen && (
          <CameraStreamSink videoRef={cameraVideoRef} />
        )}

        <div className="min-h-0 overflow-y-auto overscroll-contain">
        {standalone && settingsOpen ? (
          <div className="space-y-2.5 px-3 py-2.5">
            <button
              type="button"
              onClick={() => setSettingsOpen(false)}
              className="ambient-mini-button h-8 w-full justify-start"
            >
              <ArrowLeft size={14} />
              戻る
            </button>
            {settingsSection}
          </div>
        ) : (
        <>
        <div className="space-y-2 px-3 py-2.5">
          <button
            type="button"
            onClick={() => void handlePrimaryAction()}
            disabled={busy || uiState === "sending" || uiState === "transcribing" || rumiApprovalPending}
            className={cn(
              "inline-flex h-9 w-full items-center justify-center gap-2 rounded-lg px-3 text-sm font-semibold transition",
              primaryButtonClass(uiState),
            )}
          >
            {busy ? <Loader2 size={15} className="animate-spin" /> : <PrimaryActionIcon uiState={uiState} />}
            {uiState === "recording" && recordingSeconds > 0 ? `${stateCopy.primary} ${formatRecordingTime(recordingSeconds)}` : stateCopy.primary}
          </button>
          {rumiApprovalPending && (
            <div className="rounded-lg border border-amber-300/30 bg-amber-400/10 px-2 py-2 text-[11px] leading-5 text-amber-50">
              {ambientOperationLabels.approvalPending}: Tobkiriの承認ウィンドウで確認中です。合図待ち、録音、音声待機は承認が終わるまで停止します。
            </div>
          )}
          {pendingApproval && (
            <div className="rounded-lg border border-amber-300/30 bg-amber-400/10 px-2 py-2 text-[11px] leading-5 text-amber-50">
              <div className="flex min-w-0 items-center gap-2">
                <Shield size={13} className="shrink-0" />
                <span className="min-w-0 flex-1 truncate">
                  {ambientOperationLabels.approvalPending}: {ambientPendingInputLabel(pendingApproval)}
                </span>
                {typeof pendingApproval.pending_count === "number" && pendingApproval.pending_count > 1 && (
                  <span className="shrink-0 rounded border border-amber-200/25 px-1.5 py-0.5 text-[10px]">{pendingApproval.pending_count}</span>
                )}
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2">
                <button type="button" onClick={() => void approvePendingApproval()} disabled={busy} className="ambient-mini-button border-emerald-300/35 text-emerald-100">
                  <Check size={13} />
                  送信
                </button>
                <button type="button" onClick={() => void denyPendingApproval()} disabled={busy} className="ambient-mini-button">
                  <X size={13} />
                  破棄
                </button>
              </div>
            </div>
          )}
          {pinchRecording && (
            <div className="flex items-center gap-2 rounded-lg border border-red-300/25 bg-red-400/10 px-2 py-1.5 text-[11px] text-red-50">
              <Mic size={13} className="shrink-0" />
              <span className="min-w-0 flex-1 truncate">
                {pinchTranscriptPreview
                  ? `${ambientOperationLabels.transcribing}: ${pinchTranscriptPreview}`
                  : `${ambientOperationLabels.recording}: 文字起こしはまだ確定していません。`}
              </span>
            </div>
          )}
          <div
            className={cn(
              "border-l pl-2 text-[11px] leading-5",
              allRumiPermissionsGranted && allOsPermissionsGranted ? "border-emerald-400/45" : stateCopy.tone === "red" ? "border-red-400/40" : "border-sky-400/40",
            )}
          >
            <div className="flex items-start justify-between gap-2">
              <p className={cn("min-w-0 flex-1 font-semibold", allRumiPermissionsGranted && allOsPermissionsGranted ? "text-[13px] text-zinc-50" : "text-[12px] text-zinc-100")}>
                {allRumiPermissionsGranted && allOsPermissionsGranted
                  ? "OKマークで録音開始、指を開くと送信します"
                  : !allRumiPermissionsGranted
                    ? "Tobkiriの利用許可を完了してください"
                    : "Macのマイク・カメラ許可を完了してください"}
              </p>
              <span
                className={cn(
                  "shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold",
                  allRumiPermissionsGranted && allOsPermissionsGranted
                    ? "border-emerald-300/30 bg-emerald-400/10 text-emerald-100"
                    : stateCopy.tone === "red"
                      ? "border-red-300/30 bg-red-500/10 text-red-100"
                      : "border-sky-300/30 bg-sky-400/10 text-sky-100",
                )}
              >
                {allRumiPermissionsGranted && allOsPermissionsGranted
                  ? gestureStatusLabel(pinchDetectorStatus, monitorEnabled)
                  : !allRumiPermissionsGranted
                    ? `${rumiPermissionCount}/${AMBIENT_REQUIRED_PERMISSIONS.length}`
                    : `${osPermissionCount}/${AMBIENT_OS_PERMISSIONS.length}`}
              </span>
            </div>
            <p className="mt-0.5 text-zinc-500">
              {allRumiPermissionsGranted && allOsPermissionsGranted
                ? monitorEnabled
                  ? "カメラ認識がONです。"
                  : "開始するとカメラ認識がONになります。"
                : stateCopy.body}
            </p>
          </div>
          {inlineSettingsControlsVisible && allRumiPermissionsGranted && (
            <CompactRoutingControl
              busy={busy || rumiApprovalPending}
              mode={routingMode}
              summary={routingSummary}
              selectedConversationId={routingConversationId}
              sessionConversationId={status?.routing?.session_conversation_id ?? null}
              model={routingModel}
              modelQuery={modelQuery}
              modelResults={modelResults}
              modelLoading={modelLoading}
              onModeChange={(mode) => void saveRouting({ mode })}
              onPickChat={() => void openChatPicker()}
              onModelChange={setRoutingModel}
              onModelCommit={(model) => void saveRoutingModel(model)}
              onModelQueryChange={setModelQuery}
              onModelSearch={(query) => void searchRoutingModels(query)}
            />
          )}
          <AmbientMiniChat
            conversation={miniConversation}
            conversationId={miniConversation?.id || miniConversationId || null}
            routingSummary={miniChatRoutingSummary}
            loading={miniChatLoading}
            error={miniChatError}
            input={miniInput}
            sending={miniSending}
            disabled={!ambientDispatchGranted || rumiApprovalPending || miniAuthorityBlocksInput}
            latestInputPreview={latestSubmittedInput}
            authorityApproval={miniAuthorityApproval}
            authorityApprovalUrl={miniBrowserApprovalDirectUrl}
            showPicker={standalone || inlineSettingsControlsVisible}
            onInputChange={setMiniInput}
            onSubmit={submitMiniChat}
            onOpenChat={openMiniChatConversation}
            onRefresh={() => void loadMiniConversation()}
            onPickChat={() => void openChatPicker()}
            onOpenAuthorityApproval={() => void openMiniAuthorityApproval()}
          />
          {inlineSettingsControlsVisible && (
            <>
              <div className="flex items-center justify-end text-[11px] leading-4 text-zinc-500">
                <button
                  type="button"
                  onClick={() => setPrivacyOpen((value) => !value)}
                  className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-zinc-800 text-[11px] font-semibold text-zinc-400 hover:border-zinc-700 hover:text-zinc-100"
                  aria-label="プライバシー"
                  title="プライバシー"
                >
                  i
                </button>
              </div>
              {privacyOpen && (
                <div className="border-l border-emerald-400/35 pl-2 text-[11px] leading-5 text-zinc-400">
                  音声と映像は保存しません。残るのは、使われた時刻と結果だけです。
                </div>
              )}
            </>
          )}
        </div>

        {expanded && (settingsOpen || (approvalTarget && monitorEnabled) || manualRumiFallbackOpen || visibleMessage) && (
          <div className="space-y-2.5 border-t border-zinc-800/80 px-3 py-2.5">
            {settingsOpen && settingsSection}

            {approvalTarget && monitorEnabled && (
              <div className="border-l border-sky-400/35 pl-2 text-[11px] text-sky-100">
                {approvalTarget.canReject !== false && <span className="mr-2"><X size={11} className="mr-1 inline" />{approvalTarget.rejectLabel ?? "拒否"}</span>}
                {approvalTarget.canApprove !== false && <span><Check size={11} className="mr-1 inline" />{approvalTarget.approveLabel ?? "許可"}</span>}
              </div>
            )}

            {manualRumiFallbackOpen && (
              <section className="space-y-2 border-t border-red-400/25 pt-3 text-[12px] leading-5">
                <div className="flex items-start gap-2 text-red-100">
                  <AlertTriangle size={15} className="mt-0.5 shrink-0" />
                  <div>
                    <p className="font-medium">{manualFallbackIsOsPermission ? "端末の許可を確認してください" : "承認画面が表示されない場合"}</p>
                    <p className="mt-1 text-red-100/75">
                      {manualFallbackIsOsPermission
                        ? "Rumi側の許可は済んでいます。ブラウザまたはOS設定で、マイクとカメラをこのアプリに許可してください。"
                        : "この画面ではTobkiri許可を保存できません。Tobkiri Launcherの承認ウィンドウから許可してから「再確認」を押してください。"}
                    </p>
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={() => void (async () => {
                      if (manualFallbackIsOsPermission) {
                        const opened = await openHostPermissionsPageWindow();
                        if (!opened) {
                          setMessage("Tobkiri Launcherから開くと、権限一覧は別ウィンドウで表示されます。");
                        }
                        return;
                      }
                      await openRumiPermissionApproval();
                    })()}
                    className="ambient-mini-button"
                  >
                    {manualFallbackIsOsPermission ? <ExternalLink size={14} /> : <Shield size={14} />}
                    {manualFallbackIsOsPermission ? "権限一覧を開く" : "承認画面を開く"}
                  </button>
                  <button type="button" onClick={() => void refresh({ probeOs: true })} className="ambient-mini-button">
                    <RefreshCcw size={14} />
                    許可状態を再確認
                  </button>
                </div>
              </section>
            )}

            {visibleMessage && (
              <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-2 py-1.5 text-[11px] text-zinc-400">
                {visibleMessage}
              </div>
            )}
          </div>
        )}
        </>
        )}
        </div>
      </section>
      {chatPickerOpen && (
        <ChatPickerDialog
          activeChatId={miniConversationId ?? conversationId ?? null}
          selectedChatId={routingConversationId}
          chatItems={routingChatItems}
          loading={conversationsLoading}
          creating={miniChatCreating}
          onRefresh={() => void loadConversations()}
          onNewChat={() => void createMiniChatRoutingConversation()}
          onSelect={(chatId) => void selectMiniChatRoutingConversation(chatId)}
          onClose={() => setChatPickerOpen(false)}
        />
      )}
      </>
  );
  if (standalone) return content;
  return <LayerPortal layer="globalOverlay">{content}</LayerPortal>;
}

function RecognitionMonitor({
  videoRef,
  frame,
  status,
  recording,
  recordingSeconds,
  debug,
}: {
  videoRef: (node: HTMLVideoElement | null) => void;
  frame: HandTrackingFrame | null;
  status: string;
  recording: boolean;
  recordingSeconds: number;
  debug: boolean;
}) {
  const landmarks = frame?.landmarks ?? [];
  const hasHand = landmarks.length > 0;
  const label = recognitionMonitorLabel(status, hasHand, recording, recordingSeconds);
  const toneClass = recognitionMonitorToneClass(status, hasHand, recording);
  const thumbTip = landmarks[THUMB_TIP_INDEX];
  const indexTip = landmarks[INDEX_TIP_INDEX];

  return (
    <section className="relative border-b border-zinc-800/80 bg-black/25 px-3.5 py-2">
      <div className="flex items-center gap-2">
        <div
          className={cn(
            "relative shrink-0 overflow-hidden rounded-md border bg-zinc-950",
            recording ? "h-[54px] w-24 border-red-300/45" : hasHand ? "h-12 w-[72px] border-emerald-300/35" : "h-12 w-[72px] border-zinc-800",
          )}
          aria-hidden="true"
        >
          <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
            <rect width="100" height="100" className={recording ? "fill-red-950/35" : "fill-zinc-950"} />
            {!hasHand && (
              <>
                <path d="M30 74 C34 48 47 34 59 45 C68 53 69 70 61 78" className="fill-none stroke-zinc-700" strokeWidth="4" strokeLinecap="round" />
                <circle cx="43" cy="45" r="5" className="fill-zinc-700" />
                <circle cx="59" cy="45" r="5" className="fill-zinc-700" />
              </>
            )}
            {hasHand && HAND_LANDMARK_CONNECTIONS.map(([from, to]) => {
              const a = landmarks[from];
              const b = landmarks[to];
              if (!a || !b) return null;
              return (
                <line
                  key={`${from}-${to}`}
                  x1={landmarkPercent(a.x)}
                  y1={landmarkPercent(a.y)}
                  x2={landmarkPercent(b.x)}
                  y2={landmarkPercent(b.y)}
                  vectorEffect="non-scaling-stroke"
                  className={recording ? "stroke-red-200/80" : "stroke-emerald-200/75"}
                  strokeWidth={1.6}
                />
              );
            })}
            {thumbTip && indexTip && (
              <line
                x1={landmarkPercent(thumbTip.x)}
                y1={landmarkPercent(thumbTip.y)}
                x2={landmarkPercent(indexTip.x)}
                y2={landmarkPercent(indexTip.y)}
                vectorEffect="non-scaling-stroke"
                className={recording ? "stroke-red-50" : "stroke-sky-100"}
                strokeWidth={3}
              />
            )}
            {hasHand && landmarks.map((landmark, index) => (
              <circle
                key={index}
                cx={landmarkPercent(landmark.x)}
                cy={landmarkPercent(landmark.y)}
                r={index === THUMB_TIP_INDEX || index === INDEX_TIP_INDEX ? 2.6 : 1.45}
                vectorEffect="non-scaling-stroke"
                className={index === THUMB_TIP_INDEX || index === INDEX_TIP_INDEX ? "fill-sky-100 stroke-black/70" : recording ? "fill-red-100 stroke-black/60" : "fill-emerald-200 stroke-black/60"}
                strokeWidth={0.8}
              />
            ))}
          </svg>
        </div>
        <div className="min-w-0 flex-1">
          <span className={cn("inline-flex max-w-full items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium", toneClass)}>
            {status === "transcribing" ? <Loader2 size={12} className="animate-spin" /> : recording ? <Mic size={12} /> : hasHand ? <Hand size={12} /> : <Video size={12} />}
            <span className="truncate">{label}</span>
          </span>
          {frame?.handedness && frame.handedness !== "Unknown" && (
            <p className="mt-1 text-[10px] text-zinc-500">認識: {frame.handedness}</p>
          )}
        </div>
      </div>
      <video
        ref={videoRef}
        className={cn(
          debug
            ? "mt-2 h-auto max-h-[135px] w-full max-w-[240px] rounded-md border border-amber-400/25 object-cover"
            : "pointer-events-none absolute h-px w-px opacity-0",
        )}
        autoPlay
        muted
        playsInline
      />
    </section>
  );
}

function CameraStreamSink({ videoRef }: { videoRef: (node: HTMLVideoElement | null) => void }) {
  return (
    <video
      ref={videoRef}
      className="pointer-events-none absolute h-px w-px opacity-0"
      autoPlay
      muted
      playsInline
      aria-hidden="true"
    />
  );
}

function landmarkPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value * 100));
}

function recognitionMonitorLabel(status: string, hasHand: boolean, recording: boolean, recordingSeconds: number): string {
  if (recording) return `録音中 ${formatRecordingTime(recordingSeconds)}・OKマークを崩すと送信`;
  if (status === "transcribing") return "文字起こし中";
  if (status === "sending") return "送信中";
  if (status === "waiting_response") return "返答待ち";
  if (status === "completed") return "回答受信";
  if (status === "approval_pending") return "承認待ち";
  if (status === "error") return "エラー";
  if (status === "loading") return "合図の認識を準備中";
  if (status === "unavailable") return "合図待ちを開始できません";
  if (hasHand) return "手を認識中・OKマークで録音開始";
  return "手をカメラに入れてください";
}

function recognitionMonitorToneClass(status: string, hasHand: boolean, recording: boolean): string {
  if (recording) return "border-red-300/45 bg-red-500/25 text-red-50";
  if (status === "transcribing") return "border-violet-300/45 bg-violet-500/25 text-violet-50";
  if (status === "sending" || status === "waiting_response") return "border-violet-300/45 bg-violet-500/25 text-violet-50";
  if (status === "completed") return "border-emerald-300/45 bg-emerald-500/20 text-emerald-50";
  if (status === "approval_pending") return "border-amber-300/45 bg-amber-500/20 text-amber-50";
  if (status === "error" || status === "unavailable") return "border-red-300/45 bg-red-500/25 text-red-50";
  if (hasHand) return "border-emerald-300/45 bg-emerald-500/20 text-emerald-50";
  return "border-zinc-700/80 bg-black/55 text-zinc-200";
}

function formatRecordingTime(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function localWhisperQualityLabel(quality: string | null | undefined): string {
  if (quality === "quality") return "高精度";
  if (quality === "balanced") return "標準";
  if (quality === "fast") return "高速";
  if (quality === "custom") return "カスタム";
  return "品質未確認";
}

function formatMicLevel(level: number): string {
  if (!Number.isFinite(level)) return "0%";
  return `${Math.round(Math.max(0, Math.min(1, level)) * 100)}%`;
}

function isAmbientStatus(value: unknown): value is AmbientStatus {
  return Boolean(value && typeof value === "object" && "ambient_monitor" in value);
}

function ambientResultMessage(result: Record<string, unknown>, fallback: string): string {
  const status = String(result.status ?? "");
  const reason = String(result.reason ?? "");
  if (status === "approval_required") {
    return `${ambientOperationLabels.approvalPending}: AIへ送る前に確認が必要です。`;
  }
  if (status === "not_found") {
    return `${ambientOperationLabels.failed}: 送信待ちは見つかりませんでした。`;
  }
  if (status === "transcription_required" || reason === "ambient.audio_transcription_unavailable") {
    const transcription = result.transcription && typeof result.transcription === "object"
      ? result.transcription as Record<string, unknown>
      : {};
    const detail = String(transcription.reason || transcription.code || "").trim();
    return `${ambientOperationLabels.failed}: 録音を文字起こしできないため送信しませんでした。設定の「マイク確認」から文字起こしテストを実行してください。${detail ? ` (${detail})` : ""}`;
  }
  if (status === "ok" || reason === "trigger_dispatched") {
    if (ambientResultHasAssistantReply(result)) return `${ambientOperationLabels.done}: AIの回答が届きました。`;
    return `${ambientOperationLabels.waitingResponse}: ${fallback} 返答を待っています。`;
  }
  return `${ambientOperationLabels.failed}: ${String(result.reason ?? result.status ?? fallback)}`;
}

function ambientResultHasAssistantReply(result: Record<string, unknown>): boolean {
  const record = recordValue(result);
  if (!record) return false;
  const dispatch = recordValue(record.dispatch_result) ?? recordValue(record.dispatch);
  return Boolean(
    cleanString(record.assistant_text)
    || cleanString(record.assistant_message_id)
    || cleanString(dispatch?.assistant_text)
    || cleanString(dispatch?.assistant_message_id)
  );
}

function ambientSubmittedConversationIdFromResult(result: Record<string, unknown> | null | undefined): string | null {
  const direct = ambientConversationIdFromResult(result);
  if (direct) return direct;

  const record = recordValue(result);
  return (
    ambientConversationIdFromNestedResult(record?.pending_approval)
    || ambientConversationIdFromNestedResult(record?.dispatch)
    || ambientConversationIdFromNestedResult(record?.dispatch_result)
    || null
  );
}

function ambientConversationIdFromNestedResult(value: unknown, depth = 0): string | null {
  if (depth > 4) return null;
  const record = recordValue(value);
  if (!record) return null;
  const direct = cleanString(record.conversation_id ?? record.conversationId);
  if (direct) return direct;
  return (
    ambientConversationIdFromNestedResult(record.pending_approval, depth + 1)
    || ambientConversationIdFromNestedResult(record.pendingApproval, depth + 1)
    || ambientConversationIdFromNestedResult(record.dispatch, depth + 1)
    || ambientConversationIdFromNestedResult(record.dispatch_result, depth + 1)
    || ambientConversationIdFromNestedResult(record.dispatchResult, depth + 1)
    || ambientConversationIdFromNestedResult(record.result, depth + 1)
    || ambientConversationIdFromNestedResult(record.data, depth + 1)
    || null
  );
}

function miniAuthorityContinuationResolved(conversation: Conversation | null, requestId: string): boolean {
  if (!conversation) return false;
  const pending = ambientPendingAuthorityApproval(conversation);
  if (pending) return pending.requestId !== requestId;
  return Boolean(ambientLatestAssistantFinal(conversation));
}

function fresherMiniConversation(current: Conversation | null, next: Conversation | null): Conversation | null {
  if (!current || !next) return next;
  if (current.id !== next.id) return next;
  return conversationMessageCount(current) > conversationMessageCount(next) ? current : next;
}

function conversationMessageCount(conversation: Conversation): number {
  return Array.isArray(conversation.messages) ? conversation.messages.length : 0;
}

function isMediaDeviceNotFound(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const record = error as Record<string, unknown>;
  const name = String(record.name ?? "").toLowerCase();
  const message = String(record.message ?? "").toLowerCase();
  return name.includes("notfound") || name.includes("notreadable") || message.includes("requested device not found");
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function focusComposer() {
  window.setTimeout(() => {
    const composer = document.querySelector("textarea");
    if (composer instanceof HTMLTextAreaElement) composer.focus();
  }, 0);
}

function ambientParamsWithTranscriptionLanguage(params: AmbientEventPayload["params"] | undefined): AmbientEventPayload["params"] | undefined {
  const next = recordValue(params) ? { ...params } : {};
  return Object.keys(next).length ? next : undefined;
}

function approvalDecisionForChoice(choice: 2 | 3 | 4, target: AmbientApprovalTarget | null | undefined): "approve" | "reject" | null {
  if (!target) return null;
  if (choice === 2 && target.canReject !== false) return "reject";
  if (choice === 2 && target.canReject === false && target.canApprove !== false) return "approve";
  if (choice === 3 && target.canApprove !== false) return "approve";
  return null;
}

function hasNativeAuthorityApprovalWindow(): boolean {
  if (typeof window === "undefined") return false;
  const maybeWindow = window as TauriAmbientWindow;
  return Boolean(maybeWindow.__TAURI__ || maybeWindow.__TAURI_INTERNALS__);
}

function ambientAuthorityApprovalReturnPath(): string {
  try {
    const url = new URL(window.location.href);
    url.searchParams.delete("authority_approved");
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return "/ambient-debug";
  }
}

function cleanString(value: unknown): string | null {
  const text = String(value ?? "").trim();
  return text || null;
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
