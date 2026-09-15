import React, { useState, useEffect, useMemo, useRef } from 'react';
import { ErrorNotice } from './ErrorNotice';
import {
  DndContext,
  DragOverlay,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  defaultDropAnimationSideEffects,
  useDroppable,
  useSensor,
  useSensors,
  DragStartEvent,
  DragOverEvent,
  DragEndEvent,
  rectIntersection,
  pointerWithin,
  CollisionDetection,
} from '@dnd-kit/core';
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
  useSortable,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  Bot, BookOpen, BriefcaseBusiness, Bug, Calendar, ChartNoAxesColumn,
  Cloud, Coffee, Database, FlaskConical, Globe, Heart, Image, Mail, Map as MapIcon,
  MessageSquare, Music, Palette, PenLine, Search, Server, Settings,
  Shield, ShoppingCart, Terminal, Video, Wrench, Zap,
  Plus, ChevronRight,
  GripVertical, FolderOpen, Folder, KanbanSquare, Monitor, PanelLeftOpen, PanelLeftClose, X,
} from 'lucide-react';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

import { HISTORY_CHAT_DROP_MIME, HISTORY_CHAT_KANBAN_DROP_EVENT, historyChatDragPayload } from '../lib/historyComposer';
import type { CodingWorkspaceRecord } from '../lib/api';
import { ConversationPinStarMenu } from './history/ConversationPinStarMenu';
import { ConversationSearchBar } from './history/ConversationSearchBar';
import { ConversationTagFilter } from './history/ConversationTagFilter';
import { WarmActionIcon } from './WarmActionIcon';
import {
  PROJECTS_CHANGED_EVENT,
  loadProjects,
  newProjectId,
  saveProjects,
  type ProjectInfo,
} from '../features/projects/projectStorage';

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

// ============================================================
// Types
// ============================================================

export type ChatItem = {
  id: string;
  title: string;
  date: string;
  type: 'research' | 'code' | 'chat';
  parentId?: string | null;
  conversationKind?: string;
  sectionId?: string | null;
  sectionTitle?: string | null;
  tags?: string[];
  isStarred?: boolean;
  isPinned?: boolean;
  companyId?: string | null;
  workspaceId?: string | null;
  metadata?: Record<string, unknown> | null;
  children?: ChatItem[];
};

const HISTORY_CHAT_ICON_SIZE = 14;
const HISTORY_ICON_COMPONENTS = {
  ai: Bot,
  book: BookOpen,
  briefcase: BriefcaseBusiness,
  bug: Bug,
  calendar: Calendar,
  chart: ChartNoAxesColumn,
  chat: MessageSquare,
  cloud: Cloud,
  code: Terminal,
  coffee: Coffee,
  database: Database,
  email: Mail,
  folder: Folder,
  globe: Globe,
  heart: Heart,
  image: Image,
  lightning: Zap,
  map: MapIcon,
  music: Music,
  paint: Palette,
  science: FlaskConical,
  search: Search,
  security: Shield,
  server: Server,
  settings: Settings,
  shield: Shield,
  shopping: ShoppingCart,
  terminal: Terminal,
  tools: Wrench,
  video: Video,
  write: PenLine,
} as const;

function HistoryChatIcon({ chat, tone = "text-zinc-500" }: { chat: ChatItem; tone?: string }) {
  const iconId = typeof chat.metadata?.icon_id === "string" ? chat.metadata.icon_id : "";
  const Icon = HISTORY_ICON_COMPONENTS[iconId as keyof typeof HISTORY_ICON_COMPONENTS]
    ?? (chat.type === "research"
      ? Globe
      : chat.type === "code"
        ? Terminal
        : MessageSquare);
  const className = cn(
    "flex h-3.5 w-3.5 min-h-3.5 min-w-3.5 shrink-0 items-center justify-center overflow-hidden leading-none [&>svg]:block [&>svg]:h-full [&>svg]:w-full",
    tone,
  );
  const style = {
    width: HISTORY_CHAT_ICON_SIZE,
    height: HISTORY_CHAT_ICON_SIZE,
    flexBasis: HISTORY_CHAT_ICON_SIZE,
  };

  return (
    <span
      aria-hidden="true"
      data-history-chat-icon="true"
      data-history-chat-icon-id={iconId || undefined}
      data-history-chat-icon-size={HISTORY_CHAT_ICON_SIZE}
      className={className}
      style={style}
    >
      <Icon size={HISTORY_CHAT_ICON_SIZE} strokeWidth={2} />
    </span>
  );
}

export type ChatGroup = {
  id: string;
  sourceGroupId?: string;
  title: string;
  chats: ChatItem[];
  subGroups: ChatGroup[];
  isCollapsed?: boolean;
  custom?: boolean;
  workspaceId?: string | null;
  workspaceLabel?: string | null;
  workspaceRoot?: string | null;
  rumiDataPath?: string | null;
};

/** @deprecated API/storage compatibility alias. Use ProjectInfo in new UI code. */
export type CustomGroupInfo = ProjectInfo;

export type HistoryBoardNewTaskOptions = {
  groupId?: string;
  workspaceId?: string | null;
  workspaceLabel?: string | null;
  workspaceRoot?: string | null;
  rumiDataPath?: string | null;
};

export type AccountInfo = {
  display_name?: string;
  email?: string;
  plan_label?: string;
  avatar_url?: string;
  initial?: string;
  source?: string;
};

// ============================================================
// External data adapters
// ============================================================

function classifyChatType(chat: ChatItem): ChatItem['type'] {
  const title = chat.title.toLowerCase();
  if (
    title.includes('code') ||
    title.includes('build') ||
    title.includes('debug') ||
    title.includes('fix') ||
    title.includes('react') ||
    title.includes('rust') ||
    title.includes('api')
  ) {
    return 'code';
  }
  if (
    title.includes('research') ||
    title.includes('調査') ||
    title.includes('分析') ||
    title.includes('market') ||
    title.includes('trend')
  ) {
    return 'research';
  }
  return 'chat';
}

function groupDateLabel(dateText: string): 'today' | 'recent' | 'older' {
  if (dateText === 'Today') {
    return 'today';
  }
  if (dateText === 'Yesterday' || dateText === 'Previous 7 Days') {
    return 'recent';
  }
  return 'older';
}

function cleanTag(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, "-").slice(0, 40);
}

function chatTags(chat: ChatItem): string[] {
  return [...new Set((chat.tags ?? []).map((tag) => cleanTag(String(tag))).filter(Boolean))];
}

function chatCompanyId(chat: ChatItem): string {
  const metadata = chat.metadata ?? {};
  return String(chat.companyId ?? metadata.company_id ?? metadata.companyId ?? "").trim();
}

function chatWorkspaceId(chat: ChatItem): string {
  const metadata = chat.metadata ?? {};
  return String(chat.workspaceId ?? metadata.workspace_id ?? metadata.workspaceId ?? "").trim();
}

function chatGroupId(chat: ChatItem): string {
  const metadata = chat.metadata ?? {};
  return String(metadata.group_id ?? metadata.groupId ?? "").trim();
}

type ClientPoint = { x: number; y: number };

function clientPointFromEvent(event: Event | null | undefined): ClientPoint | null {
  const pointer = event as (Event & { clientX?: number; clientY?: number; touches?: TouchList; changedTouches?: TouchList }) | null | undefined;
  if (!pointer) return null;
  if (typeof pointer.clientX === "number" && typeof pointer.clientY === "number") {
    return { x: pointer.clientX, y: pointer.clientY };
  }
  const touch = pointer.touches?.[0] ?? pointer.changedTouches?.[0];
  if (touch) return { x: touch.clientX, y: touch.clientY };
  return null;
}

function kanbanColumnIdAtPoint(point: ClientPoint): string | null {
  const elements = document.elementsFromPoint(point.x, point.y);
  for (const element of elements) {
    const column = element.closest<HTMLElement>("[data-kanban-column-id]");
    if (column?.dataset.kanbanColumnId) return column.dataset.kanbanColumnId;
  }
  return null;
}

function dispatchHistoryChatKanbanDrop(chat: ChatItem, columnId: string): void {
  const payload = historyChatDragPayload({ ...chat, groupId: chatGroupId(chat) || undefined });
  window.dispatchEvent(new CustomEvent(HISTORY_CHAT_KANBAN_DROP_EVENT, {
    detail: {
      columnId,
      rawPayload: JSON.stringify(payload),
    },
  }));
}

function isCompanyChat(chat: ChatItem): boolean {
  const tags = chatTags(chat);
  const groupId = String(chat.metadata?.group_id ?? "").trim();
  return Boolean(
    chatCompanyId(chat)
    || groupId.startsWith("company:")
    || chat.conversationKind === "operations_company"
    || chat.conversationKind === "mimo_coding_company"
    || tags.includes("company")
    || tags.includes("operations-company")
    || tags.includes("mimo-coding-company")
  );
}

function isCodingChat(chat: ChatItem): boolean {
  const tags = chatTags(chat);
  const mode = String(chat.metadata?.mode ?? "").trim();
  return Boolean(
    chatWorkspaceId(chat)
    || chat.conversationKind === "coding"
    || mode === "coding"
    || tags.includes("coding")
  );
}

function hasWorkspaceGroupingMetadata(chat: ChatItem): boolean {
  return Boolean(chat.isPinned || chat.isStarred || chatTags(chat).length || isCompanyChat(chat) || isCodingChat(chat));
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

export function loadCustomGroups(): CustomGroupInfo[] {
  return loadProjects();
}

function saveCustomGroups(groups: CustomGroupInfo[]) {
  saveProjects(groups);
}

function collectGroupIds(groups: ChatGroup[], ids = new Set<string>()): Set<string> {
  for (const group of groups) {
    ids.add(group.id);
    collectGroupIds(group.subGroups, ids);
  }
  return ids;
}

function uniqueHistoryGroupId(baseId: string, usedIds: Set<string>, prefix: string): string {
  const normalizedBaseId = stringOrNull(baseId) ?? "group";
  let candidate = usedIds.has(normalizedBaseId) ? `${prefix}${normalizedBaseId}` : normalizedBaseId;
  let suffix = 2;
  while (usedIds.has(candidate)) {
    candidate = `${prefix}${normalizedBaseId}-${suffix}`;
    suffix += 1;
  }
  usedIds.add(candidate);
  return candidate;
}

function uniquifyGroupTreeIds(groups: ChatGroup[], usedIds: Set<string>, prefix: string): ChatGroup[] {
  return groups.map((group) => {
    const id = uniqueHistoryGroupId(group.id, usedIds, prefix);
    return {
      ...group,
      id,
      sourceGroupId: id === group.id ? group.sourceGroupId : group.sourceGroupId ?? group.id,
      subGroups: uniquifyGroupTreeIds(group.subGroups, usedIds, prefix),
    };
  });
}

export function buildGroupsFromChats(chatItems: ChatItem[], customGroups: CustomGroupInfo[] = []): ChatGroup[] {
  const dateBuckets: Record<'today' | 'recent' | 'older', ChatItem[]> = {
    today: [],
    recent: [],
    older: [],
  };
  const metadataBuckets: Record<'pinned' | 'company' | 'coding' | 'recent', ChatItem[]> = {
    pinned: [],
    company: [],
    coding: [],
    recent: [],
  };
  const tagBuckets = new Map<string, ChatItem[]>();
  const integrationGroups = new Map<string, ChatGroup>();
  const metadataGroupsById = new Map(customGroups.map((group) => [group.id, group]));
  const customChatBuckets = new Map(customGroups.map((group) => [group.id, [] as ChatItem[]]));
  const useMetadataGrouping = chatItems.some(hasWorkspaceGroupingMetadata);

  chatItems.forEach((chat) => {
    const normalized = {
      ...chat,
      type: classifyChatType(chat),
    };
    const customGroupId = stringOrNull(normalized.metadata?.group_id ?? normalized.metadata?.groupId);
    if (customGroupId) {
      if (!metadataGroupsById.has(customGroupId)) {
        metadataGroupsById.set(customGroupId, {
          id: customGroupId,
          title: stringOrNull(normalized.metadata?.group_title ?? normalized.metadata?.groupTitle) ?? customGroupId,
        });
        customChatBuckets.set(customGroupId, []);
      }
      customChatBuckets.get(customGroupId)?.push(normalized);
      return;
    }
    const sectionId = typeof normalized.sectionId === "string" ? normalized.sectionId.trim() : "";
    const sectionTitle = typeof normalized.sectionTitle === "string" ? normalized.sectionTitle.trim() : "";
    if (sectionId && sectionTitle) {
      const existing = integrationGroups.get(sectionId);
      if (existing) {
        existing.chats.push(normalized);
      } else {
        integrationGroups.set(sectionId, {
          id: sectionId,
          title: sectionTitle,
          isCollapsed: false,
          chats: [normalized],
          subGroups: [],
        });
      }
      return;
    }
    if (!useMetadataGrouping) {
      dateBuckets[groupDateLabel(chat.date)].push(normalized);
      return;
    }
    if (normalized.isPinned) {
      metadataBuckets.pinned.push(normalized);
      return;
    }
    if (isCompanyChat(normalized)) {
      metadataBuckets.company.push(normalized);
      return;
    }
    if (isCodingChat(normalized)) {
      metadataBuckets.coding.push(normalized);
      return;
    }
    const tags = chatTags(normalized).filter((tag) => !["company", "operations-company", "mimo-coding-company", "coding"].includes(tag));
    if (tags.length > 0) {
      const primary = tags[0];
      const bucket = tagBuckets.get(primary) ?? [];
      bucket.push(normalized);
      tagBuckets.set(primary, bucket);
      return;
    }
    metadataBuckets.recent.push(normalized);
  });

  const groups: ChatGroup[] = useMetadataGrouping
    ? [
        {
          id: 'group-pinned',
          title: 'Pinned',
          isCollapsed: false,
          chats: metadataBuckets.pinned,
          subGroups: [],
        },
        {
          id: 'group-company',
          title: 'Team',
          isCollapsed: false,
          chats: metadataBuckets.company,
          subGroups: [],
        },
        {
          id: 'group-coding',
          title: 'Coding',
          isCollapsed: false,
          chats: metadataBuckets.coding,
          subGroups: [],
        },
        {
          id: 'group-tags',
          title: 'Tags',
          isCollapsed: false,
          chats: [],
          subGroups: [...tagBuckets.entries()]
            .sort(([left], [right]) => left.localeCompare(right))
            .map(([tag, chats]) => ({
              id: `group-tag-${tag}`,
              title: `#${tag}`,
              isCollapsed: false,
              chats,
              subGroups: [],
            })),
        },
        {
          id: 'group-recent',
          title: 'Recent',
          isCollapsed: false,
          chats: metadataBuckets.recent,
          subGroups: [],
        },
      ]
    : [
        {
          id: 'group-today',
          title: 'Today',
          isCollapsed: false,
          chats: dateBuckets.today,
          subGroups: [],
        },
        {
          id: 'group-recent',
          title: 'Recent',
          isCollapsed: false,
          chats: dateBuckets.recent,
          subGroups: [],
        },
        {
          id: 'group-older',
          title: 'Older',
          isCollapsed: false,
          chats: dateBuckets.older,
          subGroups: [],
        },
      ];

  const visibleGroups = groups.filter((group) => group.chats.length > 0 || group.subGroups.length > 0);
  const reservedGroupIds = collectGroupIds(visibleGroups);
  const integration = uniquifyGroupTreeIds([...integrationGroups.values()], reservedGroupIds, "integration-");
  const usedGroupIds = collectGroupIds([...integration, ...visibleGroups]);
  const custom = [...metadataGroupsById.values()].map((group) => ({
    id: uniqueHistoryGroupId(group.id, usedGroupIds, "custom-"),
    sourceGroupId: group.id,
    title: group.title,
    workspaceId: group.workspaceId ?? null,
    workspaceLabel: group.workspaceLabel ?? null,
    workspaceRoot: group.workspaceRoot ?? null,
    rumiDataPath: group.rumiDataPath ?? null,
    isCollapsed: false,
    chats: customChatBuckets.get(group.id) ?? [],
    subGroups: [],
    custom: true,
  }));
  return [...custom, ...integration, ...visibleGroups];
}

// ============================================================
// Utility functions
// ============================================================

function countChats(group: ChatGroup): number {
  let count = group.chats.reduce((total, chat) => total + countChatWithChildren(chat), 0);
  for (const sub of group.subGroups) count += countChats(sub);
  return count;
}

function countChatWithChildren(chat: ChatItem): number {
  return 1 + (chat.children ?? []).reduce((total, child) => total + countChatWithChildren(child), 0);
}

function findGroupContainingChat(groups: ChatGroup[], chatId: string): string | null {
  for (const g of groups) {
    if (g.chats.some(c => c.id === chatId)) return g.id;
    const found = findGroupContainingChat(g.subGroups, chatId);
    if (found) return found;
  }
  return null;
}

function removeChatFromTree(groups: ChatGroup[], chatId: string): { groups: ChatGroup[]; chat: ChatItem | null } {
  let removedChat: ChatItem | null = null;
  const newGroups = groups.map(g => {
    if (removedChat) return g;
    const idx = g.chats.findIndex(c => c.id === chatId);
    if (idx !== -1) {
      removedChat = g.chats[idx];
      return { ...g, chats: g.chats.filter(c => c.id !== chatId) };
    }
    const result = removeChatFromTree(g.subGroups, chatId);
    if (result.chat) {
      removedChat = result.chat;
      return { ...g, subGroups: result.groups };
    }
    return g;
  });
  return { groups: newGroups, chat: removedChat };
}

function removeGroupFromTree(groups: ChatGroup[], groupId: string): { groups: ChatGroup[]; removed: ChatGroup | null } {
  const idx = groups.findIndex(g => g.id === groupId);
  if (idx !== -1) {
    return { groups: groups.filter(g => g.id !== groupId), removed: groups[idx] };
  }
  let removed: ChatGroup | null = null;
  const newGroups = groups.map(g => {
    if (removed) return g;
    const result = removeGroupFromTree(g.subGroups, groupId);
    if (result.removed) {
      removed = result.removed;
      return { ...g, subGroups: result.groups };
    }
    return g;
  });
  return { groups: newGroups, removed };
}

function addChatToGroup(groups: ChatGroup[], groupId: string, chat: ChatItem, position?: number): ChatGroup[] {
  return groups.map(g => {
    if (g.id === groupId) {
      const newChats = [...g.chats];
      if (position !== undefined) newChats.splice(position, 0, chat);
      else newChats.push(chat);
      return { ...g, chats: newChats };
    }
    return { ...g, subGroups: addChatToGroup(g.subGroups, groupId, chat, position) };
  });
}

function findGroupById(groups: ChatGroup[], id: string): ChatGroup | null {
  for (const g of groups) {
    if (g.id === id) return g;
    const found = findGroupById(g.subGroups, id);
    if (found) return found;
  }
  return null;
}

function mapGroups(groups: ChatGroup[], fn: (g: ChatGroup) => ChatGroup): ChatGroup[] {
  return groups.map(g => {
    const mapped = fn(g);
    return { ...mapped, subGroups: mapGroups(mapped.subGroups, fn) };
  });
}

function getAllChatIds(groups: ChatGroup[]): string[] {
  const ids: string[] = [];
  for (const g of groups) {
    ids.push(...g.chats.flatMap(getChatIds));
    ids.push(...getAllChatIds(g.subGroups));
  }
  return ids;
}

function getChatIds(chat: ChatItem): string[] {
  return [chat.id, ...(chat.children ?? []).flatMap(getChatIds)];
}

function getAllGroupDragIds(groups: ChatGroup[]): string[] {
  const ids: string[] = [];
  for (const g of groups) {
    ids.push(`drag-col-${g.id}`);
    ids.push(...getAllGroupDragIds(g.subGroups));
  }
  return ids;
}

export type CompactHistoryRailItem =
  | { type: "group"; id: string; title: string; depth: number; isCollapsed: boolean; total: number; group: ChatGroup }
  | { type: "chat"; id: string; title: string; depth: number; chat: ChatItem };

export function buildCompactHistoryRailItems(groups: ChatGroup[]): CompactHistoryRailItem[] {
  const items: CompactHistoryRailItem[] = [];
  const visitChat = (chat: ChatItem, depth: number) => {
    items.push({ type: "chat", id: chat.id, title: chat.title, depth, chat });
    for (const child of chat.children ?? []) visitChat(child, depth + 1);
  };
  const visitGroup = (group: ChatGroup, depth: number) => {
    items.push({
      type: "group",
      id: group.id,
      title: group.title,
      depth,
      isCollapsed: Boolean(group.isCollapsed),
      total: countChats(group),
      group,
    });
    if (group.isCollapsed) return;
    for (const chat of group.chats) visitChat(chat, depth + 1);
    for (const subGroup of group.subGroups) visitGroup(subGroup, depth + 1);
  };
  for (const group of groups) visitGroup(group, 0);
  return items;
}

// ============================================================
// Custom collision detection
// ============================================================

function createCustomCollision(activeType: string | null): CollisionDetection {
  return (args) => {
    if (activeType === 'ColumnDrag') {
      const columnContainers = args.droppableContainers.filter(container => {
        const type = container.data?.current?.type;
        return type === 'Column' || type === 'SubGroup' || container.id === 'extract-to-top-level';
      });
      return rectIntersection({ ...args, droppableContainers: columnContainers });
    }
    return pointerWithin(args);
  };
}

// ============================================================
// SortableChatItem
// ============================================================

interface SortableChatItemProps {
  chat: ChatItem;
  activeChatId: string | null;
  selectedChatId?: string | null;
  selectionMode?: boolean;
  selectionLabel?: string;
  onChatSelect: (chatId: string) => void;
  onRename: (id: string, newTitle: string) => void;
  onTogglePinned?: (chat: ChatItem) => void;
  onToggleStarred?: (chat: ChatItem) => void;
  onToggleChildren: (chatId: string) => void;
  isChildrenExpanded: (chatId: string) => boolean;
  depth?: number;
}

function SortableChatItem({ chat, activeChatId, selectedChatId = null, selectionMode = false, selectionLabel = "選択中", onChatSelect, onRename, onTogglePinned, onToggleStarred, onToggleChildren, isChildrenExpanded, depth = 0 }: SortableChatItemProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [title, setTitle] = useState(chat.title);
  const children = chat.children ?? [];
  const hasChildren = children.length > 0;
  const expanded = hasChildren && isChildrenExpanded(chat.id);
  const isActive = activeChatId === chat.id;
  const isSelected = selectedChatId === chat.id;

  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: chat.id,
    data: { type: 'Chat', chat },
    disabled: isEditing || selectionMode,
  });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.3 : 1,
    paddingLeft: `${depth * 14 + 6}px`,
  };

  const handleBlur = () => {
    setIsEditing(false);
    if (title.trim() && title !== chat.title) onRename(chat.id, title);
    else setTitle(chat.title);
  };

  const icon = <HistoryChatIcon chat={chat} />;

  return (
    <>
      <div
        ref={setNodeRef}
        style={style}
        {...(selectionMode ? { role: "button", "aria-selected": isSelected } : attributes)}
        {...(selectionMode ? {} : listeners)}
        draggable={!selectionMode}
        onDragStart={(event) => {
          if (selectionMode) return;
          const payload = historyChatDragPayload({ ...chat, groupId: chatGroupId(chat) || undefined });
          event.dataTransfer.setData(HISTORY_CHAT_DROP_MIME, JSON.stringify(payload));
          event.dataTransfer.setData("text/plain", chat.title);
          event.dataTransfer.effectAllowed = "copyMove";
        }}
        data-testid={`history-chat-card-${chat.id}`}
        className={cn(
          "box-border w-full max-w-full min-h-7 flex items-center gap-1.5 pr-1.5 py-1 rounded-[3px] text-left group/chat transition-colors cursor-grab active:cursor-grabbing outline-none",
          selectionMode && "cursor-pointer active:cursor-pointer",
          isSelected ? "bg-emerald-500/15 ring-1 ring-inset ring-emerald-400/25" : isActive ? "bg-zinc-800/80" : "hover:bg-zinc-800/50",
          chat.conversationKind === "subagent" && "text-zinc-400",
          isDragging && "ring-1 ring-emerald-500/50 rumi-layer-modal"
        )}
        onClick={() => { if (!isEditing) onChatSelect(chat.id); }}
        onKeyDown={(event) => {
          if (!selectionMode || isEditing) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onChatSelect(chat.id);
          }
        }}
        onDoubleClick={(e) => { e.stopPropagation(); setIsEditing(true); }}
        tabIndex={0}
      >
        <GripVertical size={10} className="w-3 text-zinc-700 group-hover/chat:text-zinc-500 flex-shrink-0" />
        {icon}
        {hasChildren && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onToggleChildren(chat.id);
            }}
            className="flex h-4 w-4 flex-shrink-0 items-center justify-center rounded text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
            title={expanded ? "子スレッドを閉じる" : "子スレッドを開く"}
          >
            <ChevronRight size={13} className={cn("transition-transform", expanded && "rotate-90")} />
          </button>
        )}
        {isEditing ? (
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={handleBlur}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); handleBlur(); }
              if (e.key === 'Escape') { setIsEditing(false); setTitle(chat.title); }
            }}
            onClick={(e) => e.stopPropagation()}
            className="bg-zinc-900 text-zinc-100 text-[13px] px-1 py-0.5 rounded outline-none w-full border border-emerald-500/50"
          />
        ) : (
          <span className={cn(
            "min-w-0 text-[13px] truncate flex-1 select-none",
            isActive ? "text-zinc-100" : "text-zinc-300 group-hover/chat:text-zinc-100"
          )}>{chat.title}</span>
        )}
        {!isEditing && chat.date && (
          <span className="ml-auto hidden shrink-0 font-mono text-[10px] leading-none text-zinc-600 opacity-0 transition-opacity group-hover/chat:inline group-hover/chat:opacity-100 group-focus-within/chat:inline group-focus-within/chat:opacity-100">
            {chat.date}
          </span>
        )}
        {selectionMode && isSelected && (
          <span className="ml-auto shrink-0 rounded border border-emerald-400/25 bg-emerald-400/10 px-1.5 py-0.5 text-[10px] leading-none text-emerald-100">
            {selectionLabel}
          </span>
        )}
        <ConversationPinStarMenu
          isPinned={chat.isPinned}
          isStarred={chat.isStarred}
          onTogglePinned={onTogglePinned ? () => onTogglePinned(chat) : undefined}
          onToggleStarred={onToggleStarred ? () => onToggleStarred(chat) : undefined}
        />
      </div>
      {expanded && (
        <div className="space-y-0.5">
          {children.map((child) => (
            <SortableChatItem
              key={child.id}
              chat={child}
              activeChatId={activeChatId}
              selectedChatId={selectedChatId}
              selectionMode={selectionMode}
              selectionLabel={selectionLabel}
              onChatSelect={onChatSelect}
              onRename={onRename}
              onTogglePinned={onTogglePinned}
              onToggleStarred={onToggleStarred}
              onToggleChildren={onToggleChildren}
              isChildrenExpanded={isChildrenExpanded}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </>
  );
}

// ============================================================
// SubGroup (VSCode folder style, recursive)
// ============================================================

interface SubGroupProps {
  group: ChatGroup;
  activeChatId: string | null;
  selectedChatId?: string | null;
  selectionMode?: boolean;
  selectionLabel?: string;
  onChatSelect: (chatId: string) => void;
  onChatRename: (chatId: string, newTitle: string) => void;
  onToggleCollapse: (id: string) => void;
  onRenameGroup: (id: string, newTitle: string) => void;
  onUngroup: (groupId: string) => void;
  onTogglePinned?: (chat: ChatItem) => void;
  onToggleStarred?: (chat: ChatItem) => void;
  onToggleChatChildren: (chatId: string) => void;
  isChatChildrenExpanded: (chatId: string) => boolean;
  onGroupHeaderClick: (group: ChatGroup) => void;
  depth: number;
}

function SubGroup({ group, activeChatId, selectedChatId = null, selectionMode = false, selectionLabel = "選択中", onChatSelect, onChatRename, onToggleCollapse, onRenameGroup, onUngroup, onTogglePinned, onToggleStarred, onToggleChatChildren, isChatChildrenExpanded, onGroupHeaderClick, depth }: SubGroupProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [title, setTitle] = useState(group.title);

  const {
    attributes,
    listeners,
    setNodeRef: setSortRef,
    transform,
    transition,
    isDragging,
  } = useSortable({
    id: `drag-col-${group.id}`,
    data: { type: 'ColumnDrag', group },
    disabled: isEditing || selectionMode,
  });

  const { setNodeRef: setDropRef, isOver } = useDroppable({
    id: `subgroup-drop-${group.id}`,
    data: { type: 'SubGroup', group },
  });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.3 : 1,
  };

  const handleBlur = () => {
    setIsEditing(false);
    if (title.trim() && title !== group.title) onRenameGroup(group.id, title);
    else setTitle(group.title);
  };

  const total = countChats(group);

  return (
    <div
      ref={(node) => { setSortRef(node); setDropRef(node); }}
      style={style}
      className={cn(
        "transition-colors rounded-[3px]",
        isOver && !isDragging && "bg-emerald-500/5 ring-1 ring-emerald-500/20",
        isDragging && "ring-1 ring-emerald-500/50"
      )}
    >
      <div
        className="flex h-7 items-center gap-1 px-1 rounded-[3px] hover:bg-zinc-800/50 cursor-default group/folder"
        style={{ paddingLeft: `${depth * 14 + 4}px` }}
        onClick={() => onGroupHeaderClick(group)}
      >
        <ChevronRight size={13} className={cn("text-zinc-600 transition-transform duration-200 flex-shrink-0", !group.isCollapsed && "rotate-90")} />
        {group.isCollapsed
          ? <Folder size={13} className="text-zinc-500 flex-shrink-0" />
          : <FolderOpen size={13} className="text-zinc-400 flex-shrink-0" />}
        {isEditing ? (
          <input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onBlur={handleBlur}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleBlur();
              if (e.key === 'Escape') { setIsEditing(false); setTitle(group.title); }
            }}
            onClick={(e) => e.stopPropagation()}
            className="bg-zinc-900 text-zinc-100 text-[12px] px-1 py-0.5 rounded outline-none flex-1 border border-emerald-500/50"
          />
        ) : (
          <span
            className="min-w-0 text-[12px] font-medium text-zinc-400 truncate flex-1 select-none group-hover/folder:text-zinc-200"
            onDoubleClick={(e) => { e.stopPropagation(); setIsEditing(true); }}
          >
            {group.title}
          </span>
        )}
        <span className="text-[10px] text-zinc-600 mr-1">{total}</span>
        <div
          {...attributes}
          {...listeners}
          className="flex h-5 w-4 items-center justify-center text-zinc-700 hover:text-zinc-400 opacity-0 group-hover/folder:opacity-100 transition-all cursor-grab active:cursor-grabbing"
          onClick={(e) => e.stopPropagation()}
          title="Drag to move"
        >
          <GripVertical size={10} />
        </div>
        <button
          onClick={(e) => { e.stopPropagation(); onUngroup(group.id); }}
          className="flex h-5 w-5 items-center justify-center text-zinc-600 hover:text-zinc-300 opacity-0 group-hover/folder:opacity-100 transition-all"
          title="Remove from project"
        >
          <X size={11} />
        </button>
      </div>

      <div
        className={cn(
          "rumi-history-collapse overflow-hidden",
          group.isCollapsed && "is-collapsed"
        )}
      >
        <div className="rumi-history-collapse-inner">
          <SortableContext items={group.chats.map(c => c.id)} strategy={verticalListSortingStrategy}>
            {group.chats.map(chat => (
              <SortableChatItem
                key={chat.id}
                chat={chat}
                activeChatId={activeChatId}
                selectedChatId={selectedChatId}
                selectionMode={selectionMode}
                selectionLabel={selectionLabel}
                onChatSelect={onChatSelect}
                onRename={onChatRename}
                onTogglePinned={onTogglePinned}
                onToggleStarred={onToggleStarred}
                onToggleChildren={onToggleChatChildren}
                isChildrenExpanded={isChatChildrenExpanded}
                depth={depth + 1}
              />
            ))}
          </SortableContext>
          {group.subGroups.map(sub => (
            <SubGroup
              key={sub.id}
              group={sub}
              activeChatId={activeChatId}
              selectedChatId={selectedChatId}
              selectionMode={selectionMode}
              selectionLabel={selectionLabel}
              onChatSelect={onChatSelect}
              onChatRename={onChatRename}
              onToggleCollapse={onToggleCollapse}
              onRenameGroup={onRenameGroup}
              onUngroup={onUngroup}
              onTogglePinned={onTogglePinned}
              onToggleStarred={onToggleStarred}
              onToggleChatChildren={onToggleChatChildren}
              isChatChildrenExpanded={isChatChildrenExpanded}
              onGroupHeaderClick={onGroupHeaderClick}
              depth={depth + 1}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// ============================================================
// DroppableColumn (top-level group)
// ============================================================

interface DroppableColumnProps {
  group: ChatGroup;
  activeChatId: string | null;
  selectedChatId?: string | null;
  selectionMode?: boolean;
  selectionLabel?: string;
  onChatSelect: (chatId: string) => void;
  onNewTask: (groupId: string) => void;
  onSettingsClick: () => void;
  onRename: (id: string, newTitle: string) => void;
  onToggleCollapse: (id: string) => void;
  onChatRename: (chatId: string, newTitle: string) => void;
  onUngroup: (groupId: string) => void;
  onTogglePinned?: (chat: ChatItem) => void;
  onToggleStarred?: (chat: ChatItem) => void;
  onToggleChatChildren: (chatId: string) => void;
  isChatChildrenExpanded: (chatId: string) => boolean;
  onGroupHeaderClick: (group: ChatGroup) => void;
  isDraggedOver: boolean;
  isDragging: boolean;
  dragHandleProps?: React.HTMLAttributes<HTMLDivElement>;
}

function DroppableColumn({ group, activeChatId, selectedChatId = null, selectionMode = false, selectionLabel = "選択中", onChatSelect, onNewTask, onSettingsClick, onRename, onToggleCollapse, onChatRename, onUngroup, onTogglePinned, onToggleStarred, onToggleChatChildren, isChatChildrenExpanded, onGroupHeaderClick, isDraggedOver, isDragging, dragHandleProps }: DroppableColumnProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [title, setTitle] = useState(group.title);

  const { setNodeRef: setDropRef } = useDroppable({
    id: group.id,
    data: { type: 'Column', group },
  });

  const handleBlur = () => {
    setIsEditing(false);
    if (title.trim() && title !== group.title) onRename(group.id, title);
    else setTitle(group.title);
  };

  const totalChats = countChats(group);
  const workspaceText = workspaceSummary(group.workspaceId, group.workspaceLabel, group.workspaceRoot);

  return (
    <div
      ref={setDropRef}
      className={cn(
        "w-full flex-shrink-0 border-b border-zinc-900/80 bg-[#09090b] flex flex-col transition-all duration-300",
        isDraggedOver && !isDragging && "ring-2 ring-inset ring-emerald-500/50 bg-emerald-500/[0.08]",
      )}
    >
      {/* Header */}
      <div
        onClick={() => onGroupHeaderClick(group)}
        className={cn(
          "h-7 flex items-center px-2 border-b border-zinc-900/70 justify-between hover:bg-zinc-900/50 transition-colors cursor-pointer group/colheader",
          isDraggedOver && !isDragging && "bg-emerald-500/15"
        )}
      >
        <div className="flex items-center gap-1.5 text-zinc-100 font-medium flex-1 min-w-0">
          <div
            {...dragHandleProps}
            onClick={(event) => event.stopPropagation()}
            className={cn(
              "flex h-5 w-3 flex-shrink-0 items-center justify-center rounded text-zinc-700 transition-all cursor-grab active:cursor-grabbing hover:bg-zinc-800 hover:text-zinc-400",
              group.isCollapsed ? "opacity-100" : "opacity-0 group-hover/colheader:opacity-100"
            )}
            title="Drag project"
          >
            <GripVertical size={10} />
          </div>
          <ChevronRight size={13} className={cn("transition-transform duration-200 text-zinc-500 flex-shrink-0", !group.isCollapsed && "rotate-90")} />
          {group.isCollapsed
            ? <Folder size={13} className="text-zinc-500 flex-shrink-0" />
            : <FolderOpen size={13} className="text-zinc-400 flex-shrink-0" />}
          {isEditing ? (
            <input
              autoFocus
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              onBlur={handleBlur}
              onKeyDown={(e) => {
                if (e.key === 'Enter') handleBlur();
                if (e.key === 'Escape') { setIsEditing(false); setTitle(group.title); }
              }}
              onClick={(e) => e.stopPropagation()}
              className="bg-zinc-800 text-zinc-100 text-[12px] px-1 py-0.5 rounded outline-none w-full border border-emerald-500/50"
            />
          ) : (
            <span
              onDoubleClick={(e) => { e.stopPropagation(); setIsEditing(true); }}
              className="min-w-0 truncate flex-1 cursor-text select-none hover:text-white transition-colors text-[12px]"
            >
              {group.title}
            </span>
          )}
          {workspaceText && (
            <span
              className="hidden max-w-[78px] flex-shrink truncate rounded border border-emerald-500/20 bg-emerald-500/10 px-1 py-px text-[9px] font-normal text-emerald-200 min-[260px]:inline"
              title={group.workspaceRoot || group.workspaceId || workspaceText}
            >
              {workspaceText}
            </span>
          )}
          <span className="ml-auto text-[10px] text-zinc-600 flex-shrink-0">{totalChats}</span>
        </div>
        <div className="flex items-center gap-0.5 opacity-0 group-hover/colheader:opacity-100 transition-opacity" onClick={(e) => e.stopPropagation()}>
          <button onClick={() => onNewTask(group.id)} className="flex h-5 w-5 items-center justify-center text-zinc-500 hover:text-emerald-400 transition-colors" title="New chat in project">
            <Plus size={13} />
          </button>
        </div>
      </div>

      {/* Content */}
      <div
        className={cn(
          "rumi-history-collapse overflow-hidden",
          group.isCollapsed && "is-collapsed"
        )}
      >
        <div className="rumi-history-collapse-inner px-1 py-0.5 space-y-px">
          <SortableContext items={group.chats.map(c => c.id)} strategy={verticalListSortingStrategy}>
            {group.chats.map(chat => (
              <SortableChatItem
                key={chat.id}
                chat={chat}
                activeChatId={activeChatId}
                selectedChatId={selectedChatId}
                selectionMode={selectionMode}
                selectionLabel={selectionLabel}
                onChatSelect={onChatSelect}
                onRename={onChatRename}
                onTogglePinned={onTogglePinned}
                onToggleStarred={onToggleStarred}
                onToggleChildren={onToggleChatChildren}
                isChildrenExpanded={isChatChildrenExpanded}
                depth={0}
              />
            ))}
          </SortableContext>
          {group.subGroups.map(sub => (
            <SubGroup
              key={sub.id}
              group={sub}
              activeChatId={activeChatId}
              selectedChatId={selectedChatId}
              selectionMode={selectionMode}
              selectionLabel={selectionLabel}
              onChatSelect={onChatSelect}
              onChatRename={onChatRename}
              onToggleCollapse={onToggleCollapse}
              onRenameGroup={onRename}
              onUngroup={onUngroup}
              onTogglePinned={onTogglePinned}
              onToggleStarred={onToggleStarred}
              onToggleChatChildren={onToggleChatChildren}
              isChatChildrenExpanded={isChatChildrenExpanded}
              onGroupHeaderClick={onGroupHeaderClick}
              depth={0}
            />
          ))}

          {isDraggedOver && !isDragging && (
            <div className="mx-2 my-2 p-3 border-2 border-dashed border-emerald-500/40 rounded-lg text-center">
              <FolderOpen size={18} className="text-emerald-400 mx-auto mb-1" />
              <p className="text-[11px] text-emerald-400 font-medium">フォルダとして追加</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ============================================================
// DraggableColumnHandle
// ============================================================

function DraggableColumnHandle({ group, children }: { group: ChatGroup; children: (dragHandleProps: React.HTMLAttributes<HTMLDivElement>) => React.ReactNode }) {
  const { attributes, listeners, setNodeRef, isDragging } = useSortable({
    id: `drag-col-${group.id}`,
    data: { type: 'ColumnDrag', group },
  });
  const dragHandleProps = { ...attributes, ...listeners } as React.HTMLAttributes<HTMLDivElement>;

  return (
    <div ref={setNodeRef} className={cn("relative", isDragging && "opacity-30")}>
      <div>{children(dragHandleProps)}</div>
    </div>
  );
}

// ============================================================
// ExtractDropZone
// ============================================================

function ExtractDropZone() {
  const { setNodeRef, isOver } = useDroppable({ id: 'extract-to-top-level' });

  return (
    <div
      ref={setNodeRef}
      className={cn(
        "w-[180px] flex-shrink-0 flex items-center justify-center border-r border-dashed border-zinc-800/60 transition-all duration-200",
        isOver ? "bg-emerald-500/10 border-emerald-500/40" : "bg-zinc-900/30"
      )}
    >
      <div className={cn(
        "text-center p-4 rounded-xl border-2 border-dashed transition-all",
        isOver ? "border-emerald-500/50 text-emerald-400 scale-105" : "border-zinc-800 text-zinc-600"
      )}>
        <Plus size={24} className="mx-auto mb-2" />
        <p className="text-xs font-medium">ドロップで<br/>独立カラムに</p>
      </div>
    </div>
  );
}

// ============================================================
// HistoryBoard (main export)
// ============================================================

interface HistoryBoardProps {
  activeChatId: string | null;
  chatItems: ChatItem[];
  account?: AccountInfo;
  onChatSelect: (chatId: string) => void;
  onNewTask: (options?: HistoryBoardNewTaskOptions) => void;
  onCalendarOpen?: () => void;
  isCalendarActive?: boolean;
  onKanbanOpen?: () => void;
  onGroupKanbanOpen?: (group: ChatGroup) => void;
  onGroupSelect?: (group: ChatGroup) => void;
  isKanbanActive?: boolean;
  onDesktopsOpen?: () => void;
  isDesktopsActive?: boolean;
  onSettingsClick: () => void;
  onChatMetadataChange?: (chatId: string, updates: { is_pinned?: boolean; is_starred?: boolean; tags?: string[] }) => void;
  onMinimize?: () => void;
  onRestore?: () => void;
  isCompact?: boolean;
  codingWorkspaces?: CodingWorkspaceRecord[];
  selectedCodingWorkspaceId?: string | null;
  onCodingWorkspaceCreate?: (rootPath: string) => Promise<CodingWorkspaceRecord | null | undefined>;
  onDirectorySelect?: () => Promise<string | null | undefined>;
  onGroupDataPathPrepare?: (rootPath: string) => Promise<{ rootPath: string; rumiDataPath: string } | null | undefined>;
  onCodingWorkspacesRefresh?: () => void | Promise<void>;
  selectionMode?: boolean;
  selectedChatId?: string | null;
  selectionLabel?: string;
}

type GroupWorkspaceChoice = "none" | "current" | "custom";
type GroupCreationStep = "details" | "workspace";

function workspaceSummary(workspaceId?: string | null, workspaceLabel?: string | null, workspaceRoot?: string | null): string {
  if (workspaceLabel) return workspaceLabel;
  if (workspaceRoot) return workspaceRoot.split("/").filter(Boolean).pop() || workspaceRoot;
  return workspaceId || "";
}

function newTaskOptionsForGroup(group: ChatGroup | null, fallbackGroupId: string): HistoryBoardNewTaskOptions {
  return {
    groupId: group?.id ?? fallbackGroupId,
    workspaceId: group?.workspaceId ?? null,
    workspaceLabel: group?.workspaceLabel ?? null,
    workspaceRoot: group?.workspaceRoot ?? null,
    rumiDataPath: group?.rumiDataPath ?? null,
  };
}

function visitChats(chats: ChatItem[], visitor: (chat: ChatItem) => void) {
  for (const chat of chats) {
    visitor(chat);
    visitChats(chat.children ?? [], visitor);
  }
}

export type HistoryCalendarSummary = {
  total: number;
  today: number;
  recent: number;
  older: number;
  pinned: number;
  starred: number;
};

export type CalendarMonthCell = {
  day: number;
  isToday: boolean;
} | null;

export function buildHistoryCalendarSummary(chatItems: ChatItem[]): HistoryCalendarSummary {
  const summary: HistoryCalendarSummary = {
    total: 0,
    today: 0,
    recent: 0,
    older: 0,
    pinned: 0,
    starred: 0,
  };

  visitChats(chatItems, (chat) => {
    summary.total += 1;
    if (chat.date === "Today") summary.today += 1;
    else if (chat.date === "Yesterday" || chat.date === "Previous 7 Days") summary.recent += 1;
    else summary.older += 1;
    if (chat.isPinned) summary.pinned += 1;
    if (chat.isStarred) summary.starred += 1;
  });

  return summary;
}

export function buildCalendarMonthDays(reference = new Date()): CalendarMonthCell[] {
  const year = reference.getFullYear();
  const month = reference.getMonth();
  const firstDay = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  const today = new Date();

  return Array.from({ length: firstDay + daysInMonth }, (_, index) => {
    if (index < firstDay) return null;
    const day = index - firstDay + 1;
    return {
      day,
      isToday: today.getFullYear() === year && today.getMonth() === month && today.getDate() === day,
    };
  });
}

function HistoryCalendarPanel({
  chatItems,
  onClose,
}: {
  chatItems: ChatItem[];
  onClose: () => void;
}) {
  const summary = useMemo(() => buildHistoryCalendarSummary(chatItems), [chatItems]);
  const monthCells = useMemo(() => buildCalendarMonthDays(), []);
  const monthLabel = useMemo(() => new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(new Date()), []);
  const buckets = [
    { label: "Today", value: summary.today, tone: "text-zinc-100 border-zinc-600/50 bg-zinc-800/70" },
    { label: "Recent", value: summary.recent, tone: "text-zinc-200 border-zinc-700 bg-zinc-900/75" },
    { label: "Older", value: summary.older, tone: "text-zinc-400 border-zinc-800 bg-zinc-950/70" },
  ];

  return (
    <div className="rounded-2xl border border-zinc-800/90 bg-[#111110]/95 p-3 shadow-[0_22px_60px_rgba(0,0,0,0.36)]">
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <WarmActionIcon kind="calendar" size="md" />
          <div className="min-w-0">
            <p className="truncate text-[12px] font-semibold text-zinc-200">Calendar widget</p>
            <p className="truncate text-[10px] text-zinc-600">{summary.total} chats in history</p>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-600 transition-colors hover:bg-zinc-800 hover:text-zinc-200"
          aria-label="Close calendar"
        >
          <X size={13} />
        </button>
      </div>

      <div className="rounded-xl border border-zinc-800/80 bg-black/20 p-2">
        <div className="mb-2 flex items-center justify-between px-1">
          <span className="text-[11px] font-medium text-zinc-300">{monthLabel}</span>
          <span className="text-[10px] text-zinc-600">quick view</span>
        </div>
        <div className="grid grid-cols-7 gap-1 text-center text-[9px] font-medium uppercase tracking-wide text-zinc-700">
          {["S", "M", "T", "W", "T", "F", "S"].map((day, index) => (
            <span key={`${day}-${index}`}>{day}</span>
          ))}
        </div>
        <div className="mt-1 grid grid-cols-7 gap-1">
          {monthCells.map((cell, index) => (
            <span
              key={cell ? `day-${cell.day}` : `blank-${index}`}
              className={cn(
                "flex h-7 items-center justify-center rounded-lg text-[10px]",
                cell?.isToday
                  ? "bg-zinc-100 font-semibold text-zinc-950 shadow-[0_8px_18px_rgba(255,255,255,0.08)]"
                  : cell
                    ? "bg-zinc-900/70 text-zinc-400"
                    : "bg-transparent",
              )}
            >
              {cell?.day ?? ""}
            </span>
          ))}
        </div>
      </div>

      <div className="mt-3 grid grid-cols-3 gap-1.5">
        {buckets.map((bucket) => (
          <div key={bucket.label} className={cn("rounded-xl border px-2 py-2", bucket.tone)}>
            <div className="text-[15px] font-semibold leading-none">{bucket.value}</div>
            <div className="mt-1 text-[9px] uppercase tracking-wide opacity-75">{bucket.label}</div>
          </div>
        ))}
      </div>

      <div className="mt-2 flex gap-1.5 text-[10px] text-zinc-500">
        <span className="rounded-full border border-zinc-800 px-2 py-1">Pinned {summary.pinned}</span>
        <span className="rounded-full border border-zinc-800 px-2 py-1">Starred {summary.starred}</span>
      </div>
    </div>
  );
}

function filterChatTree(chats: ChatItem[], query: string, activeTag: string | null): ChatItem[] {
  const normalizedQuery = query.trim().toLowerCase();
  const tag = activeTag ? cleanTag(activeTag) : null;
  const matches = (chat: ChatItem): boolean => {
    const haystack = [
      chat.title,
      chat.conversationKind ?? "",
      chatCompanyId(chat),
      chatWorkspaceId(chat),
      ...chatTags(chat),
    ].join(" ").toLowerCase();
    return (!normalizedQuery || haystack.includes(normalizedQuery)) && (!tag || chatTags(chat).includes(tag));
  };
  const filterOne = (chat: ChatItem): ChatItem | null => {
    const children = (chat.children ?? []).map(filterOne).filter((child): child is ChatItem => Boolean(child));
    if (matches(chat) || children.length > 0) return { ...chat, children };
    return null;
  };
  return chats.map(filterOne).filter((chat): chat is ChatItem => Boolean(chat));
}

export function HistoryBoard({
  activeChatId,
  chatItems,
  account,
  onChatSelect,
  onNewTask,
  onCalendarOpen,
  isCalendarActive = false,
  onKanbanOpen,
  onGroupKanbanOpen,
  onGroupSelect,
  isKanbanActive = false,
  onDesktopsOpen,
  isDesktopsActive = false,
  onSettingsClick,
  onChatMetadataChange,
  onMinimize,
  onRestore,
  isCompact = false,
  codingWorkspaces = [],
  selectedCodingWorkspaceId = null,
  onCodingWorkspaceCreate,
  onDirectorySelect,
  onGroupDataPathPrepare,
  onCodingWorkspacesRefresh,
  selectionMode = false,
  selectedChatId = null,
  selectionLabel = "選択中",
}: HistoryBoardProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const visibleChatItems = useMemo(() => filterChatTree(chatItems, searchQuery, activeTag), [activeTag, chatItems, searchQuery]);
  const [customGroups, setCustomGroups] = useState<CustomGroupInfo[]>(() => loadCustomGroups());
  const [groups, setGroups] = useState<ChatGroup[]>(() => buildGroupsFromChats(visibleChatItems, customGroups));
  const [expandedChatIds, setExpandedChatIds] = useState<Set<string>>(() => new Set());
  const [isCreateGroupOpen, setIsCreateGroupOpen] = useState(false);
  const [newGroupStep, setNewGroupStep] = useState<GroupCreationStep>("details");
  const [newGroupTitle, setNewGroupTitle] = useState("");
  const [newGroupWorkspaceChoice, setNewGroupWorkspaceChoice] = useState<GroupWorkspaceChoice>("none");
  const [newGroupCustomPath, setNewGroupCustomPath] = useState("");
  const [newGroupError, setNewGroupError] = useState<string | null>(null);
  const [isCreatingGroup, setIsCreatingGroup] = useState(false);
  const [isSelectingGroupDirectory, setIsSelectingGroupDirectory] = useState(false);

  useEffect(() => {
    const refreshProjects = () => setCustomGroups(loadProjects());
    window.addEventListener(PROJECTS_CHANGED_EVENT, refreshProjects);
    return () => window.removeEventListener(PROJECTS_CHANGED_EVENT, refreshProjects);
  }, []);

  const selectedCodingWorkspace = useMemo(
    () => codingWorkspaces.find((workspace) => workspace.workspace_id === selectedCodingWorkspaceId) ?? null,
    [codingWorkspaces, selectedCodingWorkspaceId],
  );

  useEffect(() => {
    setGroups((previousGroups) => {
      const collapsedById = new Map(previousGroups.map((group) => [group.id, group.isCollapsed]));
      return buildGroupsFromChats(visibleChatItems, customGroups).map((group) => ({
        ...group,
        isCollapsed: collapsedById.get(group.id) ?? group.isCollapsed,
      }));
    });
  }, [visibleChatItems, customGroups]);

  const [activeColumnDrag, setActiveColumnDrag] = useState<ChatGroup | null>(null);
  const [activeChat, setActiveChat] = useState<ChatItem | null>(null);
  const [overColumnId, setOverColumnId] = useState<string | null>(null);
  const [activeType, setActiveType] = useState<string | null>(null);
  const activeDragStartPointRef = useRef<ClientPoint | null>(null);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  );

  // --- Drag Start ---
  const handleDragStart = (event: DragStartEvent) => {
    const { active } = event;
    if (active.data.current?.type === 'ColumnDrag') {
      setActiveColumnDrag(active.data.current.group);
      setActiveType('ColumnDrag');
      activeDragStartPointRef.current = null;
    } else if (active.data.current?.type === 'Chat') {
      setActiveChat(active.data.current.chat);
      setActiveType('Chat');
      activeDragStartPointRef.current = clientPointFromEvent(event.activatorEvent);
    }
  };

  // --- Drag Over ---
  const handleDragOver = (event: DragOverEvent) => {
    const { active, over } = event;
    if (!over) { setOverColumnId(null); return; }

    if (active.data.current?.type === 'ColumnDrag') {
      if (over.data.current?.type === 'Column') {
        setOverColumnId(over.id as string);
      } else {
        setOverColumnId(null);
      }
      return;
    }

    if (active.data.current?.type !== 'Chat') return;
    if (active.id === over.id) return;

    const isOverChat = over.data.current?.type === 'Chat';
    const isOverColumn = over.data.current?.type === 'Column';
    const isOverSubGroup = over.data.current?.type === 'SubGroup';

    if (isOverChat) {
      setGroups(prev => {
        const activeGroupId = findGroupContainingChat(prev, active.id as string);
        const overGroupId = findGroupContainingChat(prev, over.id as string);
        if (!activeGroupId || !overGroupId) return prev;

        if (activeGroupId === overGroupId) {
          return mapGroups(prev, g => {
            if (g.id === activeGroupId) {
              const oldIdx = g.chats.findIndex(c => c.id === active.id);
              const newIdx = g.chats.findIndex(c => c.id === over.id);
              if (oldIdx === -1 || newIdx === -1) return g;
              return { ...g, chats: arrayMove(g.chats, oldIdx, newIdx) };
            }
            return g;
          });
        } else {
          const { groups: stripped, chat } = removeChatFromTree(prev, active.id as string);
          if (!chat) return prev;
          return mapGroups(stripped, g => {
            if (g.id === overGroupId) {
              const overIdx = g.chats.findIndex(c => c.id === over.id);
              const newChats = [...g.chats];
              newChats.splice(overIdx, 0, chat);
              return { ...g, chats: newChats };
            }
            return g;
          });
        }
      });
    }

    if (isOverColumn || isOverSubGroup) {
      const targetId = isOverSubGroup ? over.data.current?.group?.id : over.id as string;
      if (!targetId) return;
      setGroups(prev => {
        const currentGroupId = findGroupContainingChat(prev, active.id as string);
        if (currentGroupId === targetId) return prev;
        const { groups: stripped, chat } = removeChatFromTree(prev, active.id as string);
        if (!chat) return prev;
        return addChatToGroup(stripped, targetId, chat);
      });
    }
  };

  // --- Drag End ---
  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    const draggedChat = active.data.current?.type === 'Chat' ? active.data.current.chat as ChatItem : null;
    const dragStartPoint = activeDragStartPointRef.current;
    if (draggedChat && dragStartPoint) {
      const finalPoint = {
        x: dragStartPoint.x + event.delta.x,
        y: dragStartPoint.y + event.delta.y,
      };
      const kanbanColumnId = kanbanColumnIdAtPoint(finalPoint);
      if (kanbanColumnId) {
        dispatchHistoryChatKanbanDrop(draggedChat, kanbanColumnId);
      }
    }
    activeDragStartPointRef.current = null;
    setActiveColumnDrag(null);
    setActiveChat(null);
    setOverColumnId(null);
    setActiveType(null);

    if (!over || active.id === over.id) return;

    // Column → Column: nest inside
    if (active.data.current?.type === 'ColumnDrag' && over.data.current?.type === 'Column') {
      const draggedGroupId = active.data.current.group.id;
      const targetGroupId = over.id as string;
      if (draggedGroupId === targetGroupId) return;

      setGroups(prev => {
        const draggedGroup = findGroupById(prev, draggedGroupId);
        if (!draggedGroup) return prev;
        if (findGroupById(draggedGroup.subGroups, targetGroupId)) return prev;

        const { groups: stripped, removed } = removeGroupFromTree(prev, draggedGroupId);
        if (!removed) return prev;

        return mapGroups(stripped, g => {
          if (g.id === targetGroupId) {
            return { ...g, subGroups: [...g.subGroups, { ...removed, isCollapsed: false }] };
          }
          return g;
        });
      });
    }

    // Column → SubGroup: nest inside subgroup
    if (active.data.current?.type === 'ColumnDrag' && over.data.current?.type === 'SubGroup') {
      const draggedGroupId = active.data.current.group.id;
      const targetGroupId = over.data.current.group.id;
      if (draggedGroupId === targetGroupId) return;

      setGroups(prev => {
        const draggedGroup = findGroupById(prev, draggedGroupId);
        if (!draggedGroup) return prev;
        if (findGroupById(draggedGroup.subGroups, targetGroupId)) return prev;

        const { groups: stripped, removed } = removeGroupFromTree(prev, draggedGroupId);
        if (!removed) return prev;

        return mapGroups(stripped, g => {
          if (g.id === targetGroupId) {
            return { ...g, subGroups: [...g.subGroups, { ...removed, isCollapsed: false }] };
          }
          return g;
        });
      });
    }

    // Column → extract zone: promote to top-level
    if (active.data.current?.type === 'ColumnDrag' && over.id === 'extract-to-top-level') {
      const draggedGroupId = active.data.current.group.id;
      setGroups(prev => {
        const { groups: stripped, removed } = removeGroupFromTree(prev, draggedGroupId);
        if (!removed) return prev;
        return [...stripped, { ...removed, isCollapsed: false }];
      });
    }
  };

  // --- Actions ---
  const handleRenameGroup = (id: string, newTitle: string) => {
    const sourceGroupId = findGroupById(groups, id)?.sourceGroupId ?? id;
    setGroups(prev => mapGroups(prev, g => g.id === id ? { ...g, title: newTitle } : g));
    const nextCustomGroups = customGroups.map((group) => group.id === sourceGroupId ? { ...group, title: newTitle } : group);
    saveCustomGroups(nextCustomGroups);
    setCustomGroups(nextCustomGroups);
  };

  const handleToggleCollapse = (id: string) => {
    setGroups(prev => mapGroups(prev, g => g.id === id ? { ...g, isCollapsed: !g.isCollapsed } : g));
  };

  const handleGroupHeaderClick = (group: ChatGroup) => {
    onGroupSelect?.(group);
    if (searchQuery.trim() && onGroupKanbanOpen) {
      onGroupKanbanOpen(group);
      return;
    }
    handleToggleCollapse(group.id);
  };

  const handleNewTaskInGroup = (groupId: string) => {
    onNewTask(newTaskOptionsForGroup(findGroupById(groups, groupId), groupId));
  };

  const handleRenameChat = (chatId: string, newTitle: string) => {
    setGroups(prev => mapGroups(prev, g => ({
      ...g,
      chats: g.chats.map(c => c.id === chatId ? { ...c, title: newTitle } : c),
    })));
  };

  const handleUngroup = (subGroupId: string) => {
    setGroups(prev => mapGroups(prev, g => {
      const subIdx = g.subGroups.findIndex(s => s.id === subGroupId);
      if (subIdx !== -1) {
        const sub = g.subGroups[subIdx];
        return {
          ...g,
          chats: [...g.chats, ...sub.chats],
          subGroups: [...g.subGroups.filter(s => s.id !== subGroupId), ...sub.subGroups],
        };
      }
      return g;
    }));
  };

  const openCreateGroup = () => {
    setNewGroupTitle(`Project ${customGroups.length + 1}`);
    setNewGroupStep("details");
    setNewGroupWorkspaceChoice("none");
    setNewGroupCustomPath("");
    setNewGroupError(null);
    setIsCreateGroupOpen((value) => !value);
  };

  const closeCreateGroup = () => {
    if (isCreatingGroup) return;
    setIsCreateGroupOpen(false);
    setNewGroupError(null);
  };

  const advanceGroupCreation = () => {
    setNewGroupError(null);
    setNewGroupStep("workspace");
  };

  const handleMinimizeHistory = () => {
    onMinimize?.();
  };

  const createCustomGroup = (customGroup: CustomGroupInfo) => {
    const nextCustomGroups = [...customGroups, customGroup];
    saveCustomGroups(nextCustomGroups);
    setCustomGroups(nextCustomGroups);
    const newGroup: ChatGroup = {
      ...customGroup,
      chats: [],
      subGroups: [],
      isCollapsed: false,
      custom: true,
    };
    setGroups(prev => [...prev, newGroup]);
  };

  const handleSelectGroupDirectory = async () => {
    if (isSelectingGroupDirectory) return;
    if (!onDirectorySelect) {
      setNewGroupError("Folder selection is unavailable.");
      return;
    }
    setIsSelectingGroupDirectory(true);
    setNewGroupError(null);
    try {
      const selected = await onDirectorySelect();
      if (selected) {
        setNewGroupWorkspaceChoice("custom");
        setNewGroupCustomPath(selected);
      }
    } catch (error) {
      setNewGroupError(error instanceof Error ? error.message : "Failed to select folder.");
    } finally {
      setIsSelectingGroupDirectory(false);
    }
  };

  const handleCreateGroup = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (isCreatingGroup) return;
    setIsCreatingGroup(true);
    setNewGroupError(null);
    const title = newGroupTitle.trim() || `Project ${customGroups.length + 1}`;
    let workspace: Pick<CodingWorkspaceRecord, "workspace_id" | "label" | "root_path"> | null = null;
    let rumiDataPath: string | null = null;
    try {
      if (newGroupWorkspaceChoice === "current") {
        if (!selectedCodingWorkspaceId) {
          setNewGroupError("Current coding workspace is not selected.");
          return;
        }
        workspace = selectedCodingWorkspace ?? {
          workspace_id: selectedCodingWorkspaceId,
          label: selectedCodingWorkspaceId,
          root_path: "",
        };
        if (!workspace.root_path) {
          setNewGroupError("Current coding workspace has no folder path.");
          return;
        }
      } else if (newGroupWorkspaceChoice === "custom") {
        const rootPath = newGroupCustomPath.trim();
        if (!rootPath) {
          setNewGroupError("保存先フォルダを選択してください。");
          return;
        }
        workspace = codingWorkspaces.find((candidate) => candidate.root_path === rootPath) ?? null;
        if (!workspace && !onCodingWorkspaceCreate) {
          setNewGroupError("Workspace creation is unavailable.");
          return;
        }
        if (!workspace) {
          const created = await onCodingWorkspaceCreate?.(rootPath);
          if (!created?.workspace_id) {
            setNewGroupError("Workspace creation did not return a workspace.");
            return;
          }
          workspace = created;
          await onCodingWorkspacesRefresh?.();
        }
      }

      if (workspace?.root_path) {
        if (!onGroupDataPathPrepare) {
          setNewGroupError(".rumiDP storage preparation is unavailable.");
          return;
        }
        const prepared = await onGroupDataPathPrepare(workspace.root_path);
        if (!prepared?.rumiDataPath) {
          setNewGroupError(".rumiDP storage preparation did not return a path.");
          return;
        }
        rumiDataPath = prepared.rumiDataPath;
      }

      const customGroup: CustomGroupInfo = {
        id: newProjectId(),
        title,
        workspaceId: workspace?.workspace_id ?? null,
        workspaceLabel: workspace?.label ?? null,
        workspaceRoot: workspace?.root_path ?? null,
        rumiDataPath,
      };
      createCustomGroup(customGroup);
      setIsCreateGroupOpen(false);
    } catch (error) {
      setNewGroupError(error instanceof Error ? error.message : "Failed to create project.");
    } finally {
      setIsCreatingGroup(false);
    }
  };

  const handleCreateChat = () => {
    onNewTask();
  };

  const handleToggleChatChildren = (chatId: string) => {
    setExpandedChatIds((prev) => {
      const next = new Set(prev);
      if (next.has(chatId)) next.delete(chatId);
      else next.add(chatId);
      return next;
    });
  };

  const isChatChildrenExpanded = (chatId: string) => expandedChatIds.has(chatId);
  const allTags = useMemo(() => {
    const counts = new Map<string, number>();
    visitChats(chatItems, (chat) => {
      for (const tag of chatTags(chat)) counts.set(tag, (counts.get(tag) ?? 0) + 1);
    });
    return [...counts.entries()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0])).map(([tag]) => tag);
  }, [chatItems]);
  const visibleChatCount = useMemo(() => {
    let count = 0;
    visitChats(visibleChatItems, () => {
      count += 1;
    });
    return count;
  }, [visibleChatItems]);

  const handleTogglePinned = (chat: ChatItem) => {
    onChatMetadataChange?.(chat.id, { is_pinned: !chat.isPinned });
  };

  const handleToggleStarred = (chat: ChatItem) => {
    onChatMetadataChange?.(chat.id, { is_starred: !chat.isStarred });
  };

  const allSortableIds = [
    ...getAllGroupDragIds(groups),
    ...getAllChatIds(groups),
  ];

  const collisionDetection = createCustomCollision(activeType);
  const accountName = account?.display_name || account?.email || 'Developer';
  const accountPlan = account?.plan_label || 'Local Account';
  const accountInitial = account?.initial || accountName.charAt(0).toUpperCase();
  const accountIcon = account?.avatar_url || '';
  const accountIconIsImage = /^(https?:|data:image|\/)/.test(accountIcon);
  const compactRailItems = useMemo(() => buildCompactHistoryRailItems(groups), [groups]);
  const currentWorkspaceText = selectedCodingWorkspace
    ? workspaceSummary(selectedCodingWorkspace.workspace_id, selectedCodingWorkspace.label, selectedCodingWorkspace.root_path)
    : "";
  const createGroupForm = isCreateGroupOpen ? (
    <form
      data-new-project-flow="progressive"
      onSubmit={(event) => {
        if (newGroupStep === "details") {
          event.preventDefault();
          advanceGroupCreation();
          return;
        }
        void handleCreateGroup(event);
      }}
      className={cn(
        "rumi-layer-modal flex w-full flex-col gap-3 rounded-xl border border-zinc-800/90 bg-gradient-to-b from-zinc-900 to-zinc-950 p-3 text-xs shadow-[0_18px_44px_rgba(0,0,0,0.42)]",
        isCompact && "absolute left-full top-full mt-2 ml-2 w-72"
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-zinc-100">New Project</span>
            <span className="rounded-full border border-zinc-700/80 bg-zinc-900/80 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-[0.12em] text-zinc-500">
              Step {newGroupStep === "details" ? "1" : "2"} / 2
            </span>
          </div>
          <p className="mt-1 text-[10px] text-zinc-500">
            {newGroupStep === "details" ? "Name this project." : "Link an existing folder when it helps."}
          </p>
        </div>
        <button
          type="button"
          onClick={closeCreateGroup}
          disabled={isCreatingGroup}
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-50"
          aria-label="Close new project form"
        >
          <X size={13} />
        </button>
      </div>
      <div className="grid grid-cols-2 gap-1" aria-label="New project setup progress">
        {([
          ["details", "Name"],
          ["workspace", "Workspace"],
        ] as const).map(([step, label]) => {
          const active = newGroupStep === step;
          const completed = newGroupStep === "workspace" && step === "details";
          return (
            <div
              key={step}
              className={cn(
                "flex items-center gap-1.5 rounded-lg border px-2 py-1 text-[10px] transition-colors",
                active || completed
                  ? "border-emerald-500/35 bg-emerald-500/10 text-emerald-100"
                  : "border-zinc-800 bg-zinc-950/50 text-zinc-600",
              )}
            >
              <span className={cn(
                "flex h-4 w-4 items-center justify-center rounded-full text-[9px] font-semibold",
                active || completed ? "bg-emerald-400 text-zinc-950" : "bg-zinc-800 text-zinc-500",
              )}>
                {step === "details" ? "1" : "2"}
              </span>
              <span>{label}</span>
            </div>
          );
        })}
      </div>

      {newGroupStep === "details" ? (
        <>
          <label className="flex flex-col gap-1.5 text-[10px] font-medium text-zinc-400" htmlFor="new-history-group-title">
            Project name
            <input
              id="new-history-group-title"
              autoFocus
              value={newGroupTitle}
              onChange={(event) => setNewGroupTitle(event.target.value)}
              className="h-9 rounded-lg border border-zinc-800 bg-black/20 px-2.5 text-[12px] font-medium text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-emerald-500/60 focus:ring-2 focus:ring-emerald-500/10"
              placeholder={`Project ${customGroups.length + 1}`}
            />
          </label>
          <p className="rounded-lg border border-zinc-800/80 bg-black/15 px-2.5 py-2 text-[10px] leading-relaxed text-zinc-500">
            Keep it standalone or link it to an existing workspace folder.
          </p>
          <button
            type="button"
            onClick={advanceGroupCreation}
            className="flex h-9 items-center justify-center gap-1.5 rounded-lg bg-zinc-100 px-2.5 text-[11px] font-semibold text-zinc-950 hover:bg-white"
          >
            Continue
            <ChevronRight size={13} />
          </button>
        </>
      ) : (
        <>
          <div role="radiogroup" aria-label="Workspace for the new project" className="flex flex-col gap-1.5">
            {([
              ["none", "No workspace", "Keep this as a standalone project"],
              ["current", "Current workspace", currentWorkspaceText || "No coding workspace selected"],
              ["custom", "Choose a folder", "Create or reuse a coding workspace"],
            ] as const).map(([value, label, description]) => {
              const disabled = value === "current" && !selectedCodingWorkspaceId;
              const selected = newGroupWorkspaceChoice === value;
              return (
                <button
                  key={value}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  disabled={disabled}
                  onClick={() => {
                    setNewGroupError(null);
                    setNewGroupWorkspaceChoice(value);
                  }}
                  className={cn(
                    "flex min-h-11 items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors",
                    selected
                      ? "border-emerald-500/50 bg-emerald-500/10 text-zinc-100"
                      : "border-zinc-800 bg-black/15 text-zinc-400 hover:border-zinc-700 hover:bg-zinc-900/80 hover:text-zinc-100",
                    disabled && "cursor-not-allowed opacity-45 hover:border-zinc-800 hover:bg-black/15 hover:text-zinc-400",
                  )}
                >
                  <span className={cn(
                    "flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
                    selected ? "border-emerald-400 bg-emerald-400" : "border-zinc-600",
                  )}>
                    {selected && <span className="h-1.5 w-1.5 rounded-full bg-zinc-950" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block text-[11px] font-medium">{label}</span>
                    <span className="mt-0.5 block truncate text-[10px] text-zinc-500" title={value === "current" ? selectedCodingWorkspace?.root_path ?? "" : undefined}>
                      {description}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>

          {newGroupWorkspaceChoice === "custom" && (
            <div className="rounded-lg border border-zinc-800 bg-black/20 p-2">
              <button
                type="button"
                onClick={() => void handleSelectGroupDirectory()}
                disabled={isSelectingGroupDirectory}
                className="flex h-8 w-full items-center justify-center gap-1.5 rounded-md border border-zinc-700 bg-zinc-900 px-2 text-[11px] font-semibold text-zinc-100 hover:bg-zinc-800 disabled:cursor-wait disabled:opacity-60"
              >
                <FolderOpen size={12} />
                {isSelectingGroupDirectory ? "選択中..." : "ファイルを設定"}
              </button>
              <p
                className={cn(
                  "mt-1.5 truncate px-1 font-mono text-[10px]",
                  newGroupCustomPath ? "text-zinc-300" : "text-zinc-500",
                )}
                title={newGroupCustomPath || undefined}
              >
                {newGroupCustomPath || "保存先フォルダ未選択"}
              </p>
            </div>
          )}

          {newGroupError && (
            <ErrorNotice
              className="px-2.5 py-2 text-[10px]"
              copyLabel="プロジェクト作成エラーをコピー"
              message={newGroupError}
            />
          )}

          <div className="grid grid-cols-[auto_1fr] gap-2">
            <button
              type="button"
              onClick={() => {
                setNewGroupError(null);
                setNewGroupStep("details");
              }}
              disabled={isCreatingGroup}
              className="h-9 rounded-lg border border-zinc-800 px-3 text-[11px] font-medium text-zinc-400 hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-50"
            >
              Back
            </button>
            <button
              type="submit"
              disabled={isCreatingGroup}
              className="h-9 rounded-lg bg-zinc-100 px-2.5 text-[11px] font-semibold text-zinc-950 hover:bg-white disabled:cursor-wait disabled:opacity-60"
            >
              {isCreatingGroup ? "Creating..." : "Create Project"}
            </button>
          </div>
        </>
      )}
    </form>
  ) : null;

  if (isCompact) {
    return (
      <div className="relative flex h-full w-full flex-col items-center bg-[#09090b] text-zinc-400">
        <div className="flex w-full flex-col items-center gap-1 border-b border-zinc-800/60 px-1.5 py-2">
          {onRestore && (
            <button
              type="button"
              onClick={onRestore}
              className="flex h-9 w-9 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
              title="サイドバーを開く"
              aria-label="サイドバーを開く"
            >
              <PanelLeftOpen size={18} aria-hidden="true" />
            </button>
          )}
          {!selectionMode && (
            <>
              <button
                onClick={handleCreateChat}
                className="flex h-9 w-9 items-center justify-center rounded-xl text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
                title="New Chat"
                aria-label="New Chat"
              >
                <WarmActionIcon kind="newChat" size="sm" iconClassName="h-3.5 w-3.5" />
              </button>
              <div className="relative flex h-11 w-11 shrink-0 items-center justify-center">
                <button
                  onClick={openCreateGroup}
                  className="flex h-11 w-11 items-center justify-center rounded-xl text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
                  title="New Project"
                  aria-label="New Project"
                  aria-expanded={isCreateGroupOpen}
                >
                  <WarmActionIcon kind="group" size="sm" iconClassName="h-3.5 w-3.5" />
                </button>
                {createGroupForm}
              </div>
              <button
                type="button"
                onClick={() => {
                  onCalendarOpen?.();
                }}
                className={cn(
                  "flex h-9 w-9 items-center justify-center rounded-xl transition-colors",
                  isCalendarActive ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100",
                )}
                title="Calendar"
                aria-label="Calendar"
              >
                <WarmActionIcon kind="calendar" size="sm" iconClassName="h-3.5 w-3.5" />
              </button>
              <button
                type="button"
                onClick={() => {
                  onKanbanOpen?.();
                }}
                className={cn(
                  "flex h-9 w-9 items-center justify-center rounded-xl transition-colors",
                  isKanbanActive ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100",
                )}
                title="Kanban"
                aria-label="Kanban"
              >
                <KanbanSquare size={14} />
              </button>
              <button
                type="button"
                onClick={() => {
                  onDesktopsOpen?.();
                }}
                className={cn(
                  "flex h-9 w-9 items-center justify-center rounded-xl transition-colors",
                  isDesktopsActive ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:bg-zinc-800 hover:text-zinc-100",
                )}
                title="Desktops"
                aria-label="Desktops"
                aria-current={isDesktopsActive ? "page" : undefined}
              >
                <Monitor size={14} />
              </button>
            </>
          )}
        </div>

        <div className="flex min-h-0 w-full flex-1 flex-col items-center gap-1 overflow-y-auto px-1.5 py-2">
          {compactRailItems.map((item) => {
            if (item.type === "group") {
              return (
                <button
                  key={`group-${item.id}`}
                  type="button"
                  onClick={() => handleGroupHeaderClick(item.group)}
                  className={cn(
                    "relative flex h-9 min-h-9 w-9 min-w-9 shrink-0 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800/70 hover:text-zinc-100",
                    item.isCollapsed && "bg-zinc-900/80 text-zinc-400"
                  )}
                  title={`${item.title} (${item.total})`}
                  aria-label={`${item.title} (${item.total})`}
                >
                  {item.isCollapsed
                    ? <Folder size={14} className="flex-shrink-0" />
                    : <FolderOpen size={14} className="flex-shrink-0" />}
                </button>
              );
            }

            const chat = item.chat;
            const isActive = activeChatId === chat.id;
            return (
              <button
                key={chat.id}
                type="button"
                onClick={() => onChatSelect(chat.id)}
                className={cn(
                  "relative flex h-9 min-h-9 w-9 min-w-9 shrink-0 items-center justify-center rounded-md transition-colors",
                  isActive ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:bg-zinc-800/70 hover:text-zinc-100"
                )}
                title={chat.title}
                aria-label={chat.title}
              >
                <HistoryChatIcon chat={chat} tone={isActive ? "text-zinc-100" : "text-zinc-500"} />
                {isActive && <span className="absolute left-0 h-5 w-0.5 rounded-r bg-emerald-400" />}
              </button>
            );
          })}
        </div>

        <div className="flex w-full flex-col items-center border-t border-zinc-800/60 px-1.5 py-2">
          <button
            type="button"
            onClick={onSettingsClick}
            className="flex h-9 w-9 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
            title="Settings"
            aria-label="Settings"
          >
            <Settings size={14} />
          </button>
        </div>
      </div>
    );
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={collisionDetection}
      onDragStart={handleDragStart}
      onDragOver={handleDragOver}
      onDragEnd={handleDragEnd}
    >
      <div
        data-history-pane-content="true"
        className={cn(
          "relative flex h-full min-w-0 origin-left flex-col overflow-hidden bg-[#09090b] transition-[opacity,transform] duration-200 ease-out motion-reduce:transition-none",
        )}
      >
        {/* Top action bar */}
        <header className="flex flex-shrink-0 flex-col gap-1 border-b border-zinc-800/60 bg-[#09090b] px-4 py-4">
          <div className="flex h-8 items-center justify-between gap-2 px-2.5">
            <span className="text-xs font-semibold tracking-wide text-zinc-400">Tobkiri</span>
            {onMinimize && (
              <button
                type="button"
                onClick={handleMinimizeHistory}
                className="flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
                title="サイドバーを閉じる"
                aria-label="サイドバーを閉じる"
              >
                <PanelLeftClose size={18} aria-hidden="true" />
              </button>
            )}
          </div>
          {!selectionMode && (
            <>
              <div className="mt-2 flex flex-col gap-1.5">
                <button
                  onClick={handleCreateChat}
                  className="flex min-w-0 items-center gap-2 rounded-lg px-1.5 py-1.5 text-left text-xs font-medium text-zinc-300 transition-colors hover:bg-zinc-900/70 hover:text-zinc-100"
                  title="New Chat"
                >
                  <WarmActionIcon kind="newChat" size="sm" />
                  <span className="truncate">New Chat</span>
                </button>
              </div>
              <button
                type="button"
                onClick={() => {
                  onCalendarOpen?.();
                }}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg px-1.5 py-1.5 text-left text-xs font-medium transition-colors",
                  isCalendarActive
                    ? "bg-zinc-800/80 text-zinc-100"
                    : "text-zinc-400 hover:bg-zinc-900/70 hover:text-zinc-100",
                )}
                title="Calendar"
                aria-expanded={isCalendarActive}
              >
                <WarmActionIcon kind="calendar" size="sm" />
                <span className="truncate">Calendar</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  onKanbanOpen?.();
                }}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg px-1.5 py-1.5 text-left text-xs font-medium transition-colors",
                  isKanbanActive
                    ? "bg-zinc-800/80 text-zinc-100"
                    : "text-zinc-400 hover:bg-zinc-900/70 hover:text-zinc-100",
                )}
                title="Kanban"
                aria-expanded={isKanbanActive}
              >
                <KanbanSquare size={15} className="shrink-0 text-zinc-500" />
                <span className="truncate">Kanban</span>
              </button>
              <button
                type="button"
                onClick={() => {
                  onDesktopsOpen?.();
                }}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg px-1.5 py-1.5 text-left text-xs font-medium transition-colors",
                  isDesktopsActive
                    ? "bg-zinc-800/80 text-zinc-100"
                    : "text-zinc-400 hover:bg-zinc-900/70 hover:text-zinc-100",
                )}
                title="Desktops"
                aria-current={isDesktopsActive ? "page" : undefined}
              >
                <Monitor size={15} className="shrink-0 text-zinc-500" />
                <span className="truncate">Desktops</span>
              </button>
            </>
          )}
          <ConversationSearchBar value={searchQuery} resultCount={visibleChatCount} onChange={setSearchQuery} />
          <ConversationTagFilter tags={allTags} activeTag={activeTag} onChange={setActiveTag} />
        </header>

        {/* Columns */}
        <SortableContext items={allSortableIds} strategy={verticalListSortingStrategy}>
          <div className="flex flex-1 flex-col overflow-x-hidden overflow-y-auto pb-12">
            {!selectionMode && (
              <div className="relative border-b border-zinc-800/70 bg-[#09090b] px-3 py-1">
                <div className="flex min-h-8 items-center justify-between gap-3 px-1">
                  <div className="flex min-w-0 items-center gap-2">
                    <FolderOpen size={14} className="shrink-0 text-zinc-500" aria-hidden="true" />
                    <span className="truncate text-[10px] font-semibold uppercase tracking-[0.12em] text-zinc-500">
                      Projects
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={openCreateGroup}
                    className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900/70 text-zinc-400 transition-colors hover:border-emerald-500/40 hover:bg-emerald-500/10 hover:text-emerald-300"
                    title="New Project"
                    aria-label="New Project"
                    aria-expanded={isCreateGroupOpen}
                  >
                    <Plus size={15} aria-hidden="true" />
                  </button>
                </div>
                {createGroupForm}
              </div>
            )}
            {groups.map((group) => (
              <DraggableColumnHandle key={group.id} group={group}>
                {(dragHandleProps) => (
                  <DroppableColumn
                    group={group}
                    activeChatId={activeChatId}
                    selectedChatId={selectedChatId}
                    selectionMode={selectionMode}
                    selectionLabel={selectionLabel}
                    onChatSelect={onChatSelect}
                    onNewTask={handleNewTaskInGroup}
                    onSettingsClick={onSettingsClick}
                    onRename={handleRenameGroup}
                    onToggleCollapse={handleToggleCollapse}
                    onChatRename={handleRenameChat}
                    onUngroup={handleUngroup}
                    onTogglePinned={onChatMetadataChange ? handleTogglePinned : undefined}
                    onToggleStarred={onChatMetadataChange ? handleToggleStarred : undefined}
                    onToggleChatChildren={handleToggleChatChildren}
                    isChatChildrenExpanded={isChatChildrenExpanded}
                    onGroupHeaderClick={handleGroupHeaderClick}
                    isDraggedOver={overColumnId === group.id}
                    isDragging={activeColumnDrag?.id === group.id}
                    dragHandleProps={dragHandleProps}
                  />
                )}
              </DraggableColumnHandle>
            ))}

            {activeColumnDrag && <ExtractDropZone />}
          </div>
        </SortableContext>

        {/* Fixed Account Bar */}
        <div className="absolute bottom-0 left-0 right-0 h-12 px-3 border-t border-zinc-800/60 bg-[#09090b]/95 backdrop-blur-sm rumi-layer-global-overlay flex items-center">
          <div className="flex items-center gap-2.5 px-1 w-full">
            {accountIcon && accountIconIsImage ? (
              <img src={accountIcon} alt="" className="w-7 h-7 rounded-full object-cover flex-shrink-0 bg-zinc-800" />
            ) : (
              <div className="w-7 h-7 rounded-full bg-gradient-to-tr from-zinc-700 to-zinc-600 flex-shrink-0 flex items-center justify-center text-white text-[10px] font-medium">
                {accountIcon || accountInitial}
              </div>
            )}
            <div className="flex-1 overflow-hidden min-w-0">
              <p className="text-xs font-medium text-zinc-200 truncate">{accountName}</p>
              <p className="text-[10px] text-zinc-500 truncate">{accountPlan}</p>
            </div>
            <button
              onClick={onSettingsClick}
              className="p-1.5 hover:bg-zinc-800 rounded-md transition-colors text-zinc-500 hover:text-zinc-300 flex-shrink-0"
              title="Settings"
            >
              <Settings size={14} />
            </button>
          </div>
        </div>
      </div>

      <DragOverlay dropAnimation={{ sideEffects: defaultDropAnimationSideEffects({ styles: { active: { opacity: '0.3' } } }) }}>
        {activeColumnDrag ? (
          <div className="w-[260px] h-10 flex items-center px-4 border border-emerald-500/50 bg-zinc-900 rounded-lg shadow-2xl">
            <Folder size={16} className="text-emerald-400 mr-2" />
            <span className="truncate text-sm text-zinc-100 font-medium">{activeColumnDrag.title}</span>
          </div>
        ) : activeChat ? (
          <div className="w-[220px] flex items-center gap-2 px-3 py-2 rounded-lg bg-zinc-800 border border-emerald-500/50 shadow-2xl">
            <GripVertical size={12} className="text-zinc-500" />
            <HistoryChatIcon chat={activeChat} tone="text-zinc-400" />
            <span className="text-sm truncate text-zinc-100">{activeChat.title}</span>
          </div>
        ) : null}
      </DragOverlay>
    </DndContext>
  );
}
