import {
  Bot,
  BookOpen,
  BriefcaseBusiness,
  Bug,
  Calendar,
  ChartNoAxesColumn,
  Cloud,
  Coffee,
  Database,
  FlaskConical,
  Folder,
  Globe,
  Heart,
  Image,
  Mail,
  Map as MapIcon,
  MessageSquare,
  Music,
  Palette,
  PenLine,
  Search,
  Server,
  Settings,
  Shield,
  ShoppingCart,
  Terminal,
  Video,
  Wrench,
  Zap,
} from "lucide-react";

import type { ConversationPresentation } from "../../features/conversations/conversationPresentation";
import { cn } from "../../lib/cn";

const CONVERSATION_ICON_COMPONENTS = {
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

export function ConversationGlyph({
  presentation,
  fallbackKind = "chat",
  size = 14,
  tone = "text-zinc-500",
  historyCompatibility = false,
}: {
  presentation: ConversationPresentation;
  fallbackKind?: "chat" | "code" | "research";
  size?: number;
  tone?: string;
  historyCompatibility?: boolean;
}) {
  const iconId = presentation.iconId ?? "";
  const Icon = CONVERSATION_ICON_COMPONENTS[iconId as keyof typeof CONVERSATION_ICON_COMPONENTS]
    ?? (fallbackKind === "research" ? Globe : fallbackKind === "code" ? Terminal : MessageSquare);
  const compatibilityAttributes = historyCompatibility ? {
    "data-history-chat-icon": "true",
    "data-history-chat-icon-id": iconId || undefined,
    "data-history-chat-icon-size": size,
  } : {};

  return (
    <span
      aria-hidden="true"
      data-conversation-glyph="true"
      data-conversation-icon-id={iconId || undefined}
      data-conversation-id={presentation.conversationId}
      {...compatibilityAttributes}
      className={cn(
        "flex shrink-0 items-center justify-center overflow-hidden leading-none [&>svg]:block [&>svg]:h-full [&>svg]:w-full",
        tone,
      )}
      style={{ width: size, height: size, flexBasis: size }}
    >
      <Icon size={size} strokeWidth={2} />
    </span>
  );
}
