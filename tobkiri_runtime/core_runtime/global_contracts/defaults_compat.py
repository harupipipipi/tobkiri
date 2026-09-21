"""Immutable exact-key Defaults compatibility metadata projection."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .legacy_projection import LegacyProjectionRule
from .models import Cardinality

_FAILURE_SEMANTICS = "fail_closed"
_LEGACY_ID = re.compile(
    r"^defaults\.([a-z0-9]+(?:[._-][a-z0-9]+)*)$"
)


@dataclass(frozen=True, slots=True)
class DefaultsCompatibilityTarget:
    """Immutable function-manifest metadata for one compatibility target."""

    function_id: str
    operation: str | None
    requires: tuple[str, ...]
    caller_requires: tuple[str, ...]
    risk: str
    failure_semantics: str = _FAILURE_SEMANTICS


@dataclass(frozen=True, slots=True)
class DefaultsCompatibilityEntry:
    """One exact legacy ID bound to one versioned global action contract."""

    legacy_id: str
    component_id: str
    contract_id: str
    contract_version: str
    component_manifest_blob: str
    targets: tuple[DefaultsCompatibilityTarget, ...]

    @property
    def projection_rule(self) -> LegacyProjectionRule:
        """Return the corresponding exact-key read-only projection rule."""
        return LegacyProjectionRule(
            self.legacy_id,
            self.contract_id,
            version=self.contract_version,
            cardinality=Cardinality.ONE,
            exact_key=True,
        )


@dataclass(frozen=True, slots=True)
class DefaultsLegacyInventoryItem:
    """Approved in-memory legacy connectivity evidence."""

    legacy_id: str
    component_id: str
    component_version: str


@dataclass(frozen=True, slots=True)
class DefaultsCompatibilitySelection:
    """Authorized metadata selection; it contains no executable provider."""

    legacy_id: str
    contract_id: str
    contract_version: str
    function_id: str
    operation: str | None
    requires: tuple[str, ...]
    caller_requires: tuple[str, ...]
    risk: str
    failure_semantics: str


@dataclass(frozen=True, slots=True)
class DefaultsCompatibilityHandle:
    """Immutable exact-key selector over the approved Defaults inventory."""

    _entries: Mapping[str, DefaultsCompatibilityEntry] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_entries",
            MappingProxyType(dict(self._entries)),
        )

    @property
    def entries(self) -> Mapping[str, DefaultsCompatibilityEntry]:
        """Return the immutable exact-key compatibility table."""
        return self._entries

    @property
    def legacy_ids(self) -> tuple[str, ...]:
        """Return all compatibility IDs in deterministic order."""
        return tuple(self._entries)

    def select(
        self,
        legacy_id: str,
        *,
        operation: str | None = None,
        granted_capabilities: Collection[str] = (),
        caller_capabilities: Collection[str] = (),
    ) -> DefaultsCompatibilitySelection | None:
        """Select authorized metadata or fail closed with no result."""
        if not isinstance(legacy_id, str):
            return None
        if operation is not None and (
            not isinstance(operation, str) or not operation
        ):
            return None
        entry = self._entries.get(legacy_id)
        if entry is None:
            return None
        matches = tuple(
            target
            for target in entry.targets
            if target.operation == operation
        )
        if len(matches) != 1:
            return None
        granted = _normalize_capabilities(granted_capabilities)
        caller = _normalize_capabilities(caller_capabilities)
        if granted is None or caller is None:
            return None
        target = matches[0]
        if not set(target.requires).issubset(granted):
            return None
        if not set(target.caller_requires).issubset(caller):
            return None
        return DefaultsCompatibilitySelection(
            legacy_id=entry.legacy_id,
            contract_id=entry.contract_id,
            contract_version=entry.contract_version,
            function_id=target.function_id,
            operation=target.operation,
            requires=target.requires,
            caller_requires=target.caller_requires,
            risk=target.risk,
            failure_semantics=target.failure_semantics,
        )


_FROZEN_ROWS = (('defaults.agent.add_instruction',
  'agent',
  'rumi.action.legacy.agent.add_instruction.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_add_instruction', None, ('agent.add.instruction',), (), 'medium'),)),
 ('defaults.agent.approve',
  'agent',
  'rumi.action.legacy.agent.approve.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_approve', None, ('agent.approve',), (), 'medium'),)),
 ('defaults.agent.cancel',
  'agent',
  'rumi.action.legacy.agent.cancel.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_cancel', None, ('agent.cancel',), (), 'medium'),)),
 ('defaults.agent.execute',
  'agent',
  'rumi.action.legacy.agent.execute.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_execute', None, ('agent.execute',), (), 'medium'),)),
 ('defaults.agent.multi_execute',
  'agent',
  'rumi.action.legacy.agent.multi_execute.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_multi_execute', None, ('agent.multi.execute',), (), 'medium'),)),
 ('defaults.agent.multi_message',
  'agent',
  'rumi.action.legacy.agent.multi_message.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_multi_message', None, ('agent.multi.message',), (), 'medium'),)),
 ('defaults.agent.multi_status',
  'agent',
  'rumi.action.legacy.agent.multi_status.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_multi_status', None, (), (), 'low'),)),
 ('defaults.agent.plan',
  'agent',
  'rumi.action.legacy.agent.plan.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_plan', None, ('agent.plan',), (), 'medium'),)),
 ('defaults.agent.reject',
  'agent',
  'rumi.action.legacy.agent.reject.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_reject', None, ('agent.reject',), (), 'medium'),)),
 ('defaults.agent.status',
  'agent',
  'rumi.action.legacy.agent.status.v1',
  '1.0.0',
  '5c907bbd8a02caf3290daf3dcde8540aa58ca610',
  (('agent_status', None, (), (), 'low'),)),
 ('defaults.ai.complete',
  'ai_client',
  'rumi.action.legacy.ai.complete.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_complete', None, ('ai.complete',), (), 'medium'),)),
 ('defaults.ai.embed',
  'ai_client',
  'rumi.action.legacy.ai.embed.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_embed', None, ('ai.embed',), (), 'medium'),)),
 ('defaults.ai.image_analyze',
  'ai_client',
  'rumi.action.legacy.ai.image_analyze.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_image_analyze', None, ('ai.image.analyze',), (), 'medium'),)),
 ('defaults.ai.image_gen',
  'ai_client',
  'rumi.action.legacy.ai.image_gen.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_image_gen', None, ('ai.image.gen',), (), 'medium'),)),
 ('defaults.ai.models',
  'ai_client',
  'rumi.action.legacy.ai.models.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_models', None, (), (), 'low'),)),
 ('defaults.ai.providers',
  'ai_client',
  'rumi.action.legacy.ai.providers.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_providers', None, (), (), 'low'),)),
 ('defaults.ai.stream',
  'ai_client',
  'rumi.action.legacy.ai.stream.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_stream', None, ('ai.stream',), (), 'medium'),)),
 ('defaults.ai.transcribe',
  'ai_client',
  'rumi.action.legacy.ai.transcribe.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_transcribe', None, ('ai.transcribe',), (), 'medium'),)),
 ('defaults.ai.tts',
  'ai_client',
  'rumi.action.legacy.ai.tts.v1',
  '1.0.0',
  '35c76b7be0e4663426df83b902b419e6b71e24c1',
  (('ai_tts', None, ('ai.tts',), (), 'medium'),)),
 ('defaults.chat.add_message',
  'chat',
  'rumi.action.legacy.chat.add_message.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_add_message', None, ('chat.add.message',), (), 'medium'),)),
 ('defaults.chat.auto_trim',
  'chat',
  'rumi.action.legacy.chat.auto_trim.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_auto_trim', None, ('chat.auto.trim',), (), 'medium'),)),
 ('defaults.chat.branch',
  'chat',
  'rumi.action.legacy.chat.branch.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_branch', None, ('chat.branch',), (), 'medium'),)),
 ('defaults.chat.create_conversation',
  'chat',
  'rumi.action.legacy.chat.create_conversation.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_create_conversation', None, ('chat.create.conversation',), (), 'medium'),)),
 ('defaults.chat.delete_conversation',
  'chat',
  'rumi.action.legacy.chat.delete_conversation.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_delete_conversation',
    None,
    ('chat.delete.conversation',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.chat.delete_message',
  'chat',
  'rumi.action.legacy.chat.delete_message.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_delete_message',
    None,
    ('chat.delete.message',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.chat.export_conversation',
  'chat',
  'rumi.action.legacy.chat.export_conversation.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_export_conversation', None, (), (), 'low'),)),
 ('defaults.chat.get_conversation',
  'chat',
  'rumi.action.legacy.chat.get_conversation.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_get_conversation', None, (), (), 'low'),)),
 ('defaults.chat.get_message',
  'chat',
  'rumi.action.legacy.chat.get_message.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_get_message', None, (), (), 'low'),)),
 ('defaults.chat.list_conversations',
  'chat',
  'rumi.action.legacy.chat.list_conversations.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_list_conversations', None, (), (), 'low'),)),
 ('defaults.chat.regenerate',
  'chat',
  'rumi.action.legacy.chat.regenerate.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_regenerate', None, ('chat.regenerate',), (), 'medium'),)),
 ('defaults.chat.search',
  'chat',
  'rumi.action.legacy.chat.search.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_search', None, (), (), 'low'),)),
 ('defaults.chat.send',
  'chat',
  'rumi.action.legacy.chat.send.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_send', None, ('chat.send',), (), 'medium'),)),
 ('defaults.chat.stop',
  'chat',
  'rumi.action.legacy.chat.stop.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_stop', None, ('chat.stop',), (), 'medium'),)),
 ('defaults.chat.stream',
  'chat',
  'rumi.action.legacy.chat.stream.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_stream', None, ('chat.stream',), (), 'medium'),)),
 ('defaults.chat.summarize_and_trim',
  'chat',
  'rumi.action.legacy.chat.summarize_and_trim.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_summarize_and_trim', None, ('chat.summarize.and.trim',), (), 'medium'),)),
 ('defaults.chat.update_conversation',
  'chat',
  'rumi.action.legacy.chat.update_conversation.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_update_conversation', None, ('chat.update.conversation',), (), 'medium'),)),
 ('defaults.chat.update_message',
  'chat',
  'rumi.action.legacy.chat.update_message.v1',
  '1.0.0',
  '3060150da9f46f7039dd0d82ff3b4e74eed1c190',
  (('chat_update_message', None, ('chat.update.message',), (), 'medium'),)),
 ('defaults.coding.file_create',
  'coding',
  'rumi.action.legacy.coding.file_create.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_file_create',
    None,
    ('coding.file.create',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.coding.file_delete',
  'coding',
  'rumi.action.legacy.coding.file_delete.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_file_delete',
    None,
    ('coding.file.delete',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.coding.file_list',
  'coding',
  'rumi.action.legacy.coding.file_list.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_file_list', None, (), (), 'low'),)),
 ('defaults.coding.file_read',
  'coding',
  'rumi.action.legacy.coding.file_read.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_file_read', None, (), (), 'low'),)),
 ('defaults.coding.file_search',
  'coding',
  'rumi.action.legacy.coding.file_search.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_file_search', None, (), (), 'low'),)),
 ('defaults.coding.file_write',
  'coding',
  'rumi.action.legacy.coding.file_write.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_file_write', None, ('coding.file.write',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.coding.git_commit',
  'coding',
  'rumi.action.legacy.coding.git_commit.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_git_commit', None, ('coding.git.commit',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.coding.git_diff',
  'coding',
  'rumi.action.legacy.coding.git_diff.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_git_diff', None, (), (), 'low'),)),
 ('defaults.coding.git_push',
  'coding',
  'rumi.action.legacy.coding.git_push.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_git_push', None, ('coding.git.push',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.coding.git_status',
  'coding',
  'rumi.action.legacy.coding.git_status.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_git_status', None, (), (), 'low'),)),
 ('defaults.coding.terminal_exec',
  'coding',
  'rumi.action.legacy.coding.terminal_exec.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_terminal_exec',
    None,
    ('coding.terminal.exec',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.coding.terminal_stream',
  'coding',
  'rumi.action.legacy.coding.terminal_stream.v1',
  '1.0.0',
  'aabd1b8a04508c3941834fd5b018dd8f213023bf',
  (('coding_terminal_stream',
    None,
    ('coding.terminal.stream',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.dev.edit_prompt_live',
  'dev',
  'rumi.action.legacy.dev.edit_prompt_live.v1',
  '1.0.0',
  '48468449cf0fff8068ff8fa8fadfb65c731a05a4',
  (('dev_edit_prompt_live',
    None,
    ('dev.edit.prompt.live',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.dev.inspect',
  'dev',
  'rumi.action.legacy.dev.inspect.v1',
  '1.0.0',
  '48468449cf0fff8068ff8fa8fadfb65c731a05a4',
  (('dev_inspect', None, (), (), 'low'),)),
 ('defaults.dev.prompt_history',
  'dev',
  'rumi.action.legacy.dev.prompt_history.v1',
  '1.0.0',
  '48468449cf0fff8068ff8fa8fadfb65c731a05a4',
  (('dev_prompt_history', None, (), (), 'low'),)),
 ('defaults.dev.replay',
  'dev',
  'rumi.action.legacy.dev.replay.v1',
  '1.0.0',
  '48468449cf0fff8068ff8fa8fadfb65c731a05a4',
  (('dev_replay', None, ('dev.replay',), (), 'medium'),)),
 ('defaults.frontend.emit',
  'frontend',
  'rumi.action.legacy.frontend.emit.v1',
  '1.0.0',
  '050a56d2391ce147d5065fc00b1adbe88326bfe4',
  (('frontend_emit', None, ('frontend.emit',), (), 'medium'),)),
 ('defaults.frontend.start',
  'frontend',
  'rumi.action.legacy.frontend.start.v1',
  '1.0.0',
  '050a56d2391ce147d5065fc00b1adbe88326bfe4',
  (('frontend_start', None, ('frontend.start',), (), 'medium'),)),
 ('defaults.frontend.stop',
  'frontend',
  'rumi.action.legacy.frontend.stop.v1',
  '1.0.0',
  '050a56d2391ce147d5065fc00b1adbe88326bfe4',
  (('frontend_stop', None, ('frontend.stop',), (), 'medium'),)),
 ('defaults.knowledge.create',
  'knowledge',
  'rumi.action.legacy.knowledge.create.v1',
  '1.0.0',
  '33e889f2e71fa5cf5703b4f604d5e79e5b01abfa',
  (('knowledge_create', None, ('knowledge.create',), (), 'medium'),)),
 ('defaults.knowledge.delete',
  'knowledge',
  'rumi.action.legacy.knowledge.delete.v1',
  '1.0.0',
  '33e889f2e71fa5cf5703b4f604d5e79e5b01abfa',
  (('knowledge_delete', None, ('knowledge.delete',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.knowledge.get',
  'knowledge',
  'rumi.action.legacy.knowledge.get.v1',
  '1.0.0',
  '33e889f2e71fa5cf5703b4f604d5e79e5b01abfa',
  (('knowledge_get', None, (), (), 'low'),)),
 ('defaults.knowledge.list',
  'knowledge',
  'rumi.action.legacy.knowledge.list.v1',
  '1.0.0',
  '33e889f2e71fa5cf5703b4f604d5e79e5b01abfa',
  (('knowledge_list', None, (), (), 'low'),)),
 ('defaults.knowledge.search',
  'knowledge',
  'rumi.action.legacy.knowledge.search.v1',
  '1.0.0',
  '33e889f2e71fa5cf5703b4f604d5e79e5b01abfa',
  (('knowledge_search', None, (), (), 'low'),)),
 ('defaults.knowledge.update',
  'knowledge',
  'rumi.action.legacy.knowledge.update.v1',
  '1.0.0',
  '33e889f2e71fa5cf5703b4f604d5e79e5b01abfa',
  (('knowledge_update', None, ('knowledge.update',), (), 'medium'),)),
 ('defaults.media.clipboard_read',
  'media',
  'rumi.action.legacy.media.clipboard_read.v1',
  '1.0.0',
  'b09411f023fce4b24794ad4e132689ca61fcb252',
  (('media_clipboard_read',
    None,
    ('media.clipboard.read',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.media.clipboard_write',
  'media',
  'rumi.action.legacy.media.clipboard_write.v1',
  '1.0.0',
  'b09411f023fce4b24794ad4e132689ca61fcb252',
  (('media_clipboard_write',
    None,
    ('media.clipboard.write',),
    ('user.approved.high_risk',),
    'high'),)),
 ('defaults.media.doc_parse',
  'media',
  'rumi.action.legacy.media.doc_parse.v1',
  '1.0.0',
  'b09411f023fce4b24794ad4e132689ca61fcb252',
  (('media_doc_parse', None, (), (), 'low'),)),
 ('defaults.media.image_read',
  'media',
  'rumi.action.legacy.media.image_read.v1',
  '1.0.0',
  'b09411f023fce4b24794ad4e132689ca61fcb252',
  (('media_image_read', None, (), (), 'low'),)),
 ('defaults.media.image_transform',
  'media',
  'rumi.action.legacy.media.image_transform.v1',
  '1.0.0',
  'b09411f023fce4b24794ad4e132689ca61fcb252',
  (('media_image_transform', None, ('media.image.transform',), (), 'medium'),)),
 ('defaults.media.screenshot',
  'media',
  'rumi.action.legacy.media.screenshot.v1',
  '1.0.0',
  'b09411f023fce4b24794ad4e132689ca61fcb252',
  (('media_screenshot', None, ('media.screenshot',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.memory.project_context',
  'memory',
  'rumi.action.legacy.memory.project_context.v1',
  '1.0.0',
  '24ea479952e0ae2d61ca538df19f9cce94fb4031',
  (('memory_project_context', None, (), (), 'low'),)),
 ('defaults.memory.recall',
  'memory',
  'rumi.action.legacy.memory.recall.v1',
  '1.0.0',
  '24ea479952e0ae2d61ca538df19f9cce94fb4031',
  (('memory_recall', None, (), (), 'low'),)),
 ('defaults.memory.store',
  'memory',
  'rumi.action.legacy.memory.store.v1',
  '1.0.0',
  '24ea479952e0ae2d61ca538df19f9cce94fb4031',
  (('memory_store', None, ('memory.store',), (), 'medium'),)),
 ('defaults.memory.vector_query',
  'memory',
  'rumi.action.legacy.memory.vector_query.v1',
  '1.0.0',
  '24ea479952e0ae2d61ca538df19f9cce94fb4031',
  (('memory_vector_query', None, (), (), 'low'),)),
 ('defaults.memory.vector_store',
  'memory',
  'rumi.action.legacy.memory.vector_store.v1',
  '1.0.0',
  '24ea479952e0ae2d61ca538df19f9cce94fb4031',
  (('memory_vector_store', None, ('memory.vector.store',), (), 'medium'),)),
 ('defaults.prompt.convert',
  'prompt',
  'rumi.action.legacy.prompt.convert.v1',
  '1.0.0',
  'bc586ba0cea7365814c4bfbd424859a0164d324a',
  (('prompt_convert', None, (), (), 'low'),)),
 ('defaults.prompt.create',
  'prompt',
  'rumi.action.legacy.prompt.create.v1',
  '1.0.0',
  'bc586ba0cea7365814c4bfbd424859a0164d324a',
  (('prompt_create', None, ('prompt.create',), (), 'medium'),)),
 ('defaults.prompt.delete',
  'prompt',
  'rumi.action.legacy.prompt.delete.v1',
  '1.0.0',
  'bc586ba0cea7365814c4bfbd424859a0164d324a',
  (('prompt_delete', None, ('prompt.delete',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.prompt.list',
  'prompt',
  'rumi.action.legacy.prompt.list.v1',
  '1.0.0',
  'bc586ba0cea7365814c4bfbd424859a0164d324a',
  (('prompt_list', None, (), (), 'low'),)),
 ('defaults.prompt.render',
  'prompt',
  'rumi.action.legacy.prompt.render.v1',
  '1.0.0',
  'bc586ba0cea7365814c4bfbd424859a0164d324a',
  (('prompt_render', None, (), (), 'low'),)),
 ('defaults.prompt.system',
  'prompt',
  'rumi.action.legacy.prompt.system.v1',
  '1.0.0',
  'bc586ba0cea7365814c4bfbd424859a0164d324a',
  (('prompt_system_get', 'get', (), (), 'low'),
   ('prompt_system_set', 'set', ('prompt.system.set',), (), 'medium'))),
 ('defaults.prompt.update',
  'prompt',
  'rumi.action.legacy.prompt.update.v1',
  '1.0.0',
  'bc586ba0cea7365814c4bfbd424859a0164d324a',
  (('prompt_update', None, ('prompt.update',), (), 'medium'),)),
 ('defaults.tool.consent_check',
  'tool',
  'rumi.action.legacy.tool.consent_check.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_consent_check', None, (), (), 'low'),)),
 ('defaults.tool.consent_confirm',
  'tool',
  'rumi.action.legacy.tool.consent_confirm.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_consent_confirm', None, ('tool.consent.confirm',), (), 'medium'),)),
 ('defaults.tool.create',
  'tool',
  'rumi.action.legacy.tool.create.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_create', None, ('tool.create',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.tool.delete',
  'tool',
  'rumi.action.legacy.tool.delete.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_delete', None, ('tool.delete',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.tool.export',
  'tool',
  'rumi.action.legacy.tool.export.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_export', None, (), (), 'low'),)),
 ('defaults.tool.invoke',
  'tool',
  'rumi.action.legacy.tool.invoke.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_invoke', None, ('tool.invoke',), (), 'medium'),)),
 ('defaults.tool.list',
  'tool',
  'rumi.action.legacy.tool.list.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_list', None, (), (), 'low'),)),
 ('defaults.tool.mcp_connect',
  'tool',
  'rumi.action.legacy.tool.mcp_connect.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_mcp_connect', None, ('tool.mcp.connect',), ('user.approved.high_risk',), 'high'),)),
 ('defaults.tool.mcp_list',
  'tool',
  'rumi.action.legacy.tool.mcp_list.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_mcp_list', None, (), (), 'low'),)),
 ('defaults.tool.schema',
  'tool',
  'rumi.action.legacy.tool.schema.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_schema', None, (), (), 'low'),)),
 ('defaults.tool.update',
  'tool',
  'rumi.action.legacy.tool.update.v1',
  '1.0.0',
  '64347583d5897bba59008efc9083c87614b4dea7',
  (('tool_update', None, ('tool.update',), ('user.approved.high_risk',), 'high'),)))


def _expected_contract_id(legacy_id: str) -> str:
    match = _LEGACY_ID.fullmatch(legacy_id)
    if match is None:
        raise ValueError(f"invalid Defaults compatibility ID: {legacy_id!r}")
    return f"rumi.action.legacy.{match.group(1)}.v1"


def _normalize_capabilities(
    values: Collection[str],
) -> frozenset[str] | None:
    if isinstance(values, (str, bytes)):
        return None
    try:
        normalized = frozenset(values)
    except TypeError:
        return None
    if any(not isinstance(value, str) or not value for value in normalized):
        return None
    return normalized


def _build_api_inventory() -> tuple[DefaultsCompatibilityEntry, ...]:
    entries: list[DefaultsCompatibilityEntry] = []
    for (
        legacy_id,
        component_id,
        contract_id,
        contract_version,
        component_manifest_blob,
        raw_targets,
    ) in _FROZEN_ROWS:
        if contract_id != _expected_contract_id(legacy_id):
            raise ValueError(f"contract ID does not match legacy ID: {legacy_id}")
        if contract_version != "1.0.0":
            raise ValueError(f"unsupported compatibility version: {legacy_id}")
        targets = tuple(
            DefaultsCompatibilityTarget(
                function_id=function_id,
                operation=operation,
                requires=requires,
                caller_requires=caller_requires,
                risk=risk,
            )
            for function_id, operation, requires, caller_requires, risk in raw_targets
        )
        operations = {target.operation for target in targets}
        if legacy_id == "defaults.prompt.system":
            if operations != {"get", "set"} or len(targets) != 2:
                raise ValueError("prompt system requires exact get/set targets")
        elif operations != {None} or len(targets) != 1:
            raise ValueError(f"direct compatibility target is ambiguous: {legacy_id}")
        entries.append(
            DefaultsCompatibilityEntry(
                legacy_id=legacy_id,
                component_id=component_id,
                contract_id=contract_id,
                contract_version=contract_version,
                component_manifest_blob=component_manifest_blob,
                targets=targets,
            )
        )
    if len(entries) != 91:
        raise ValueError("Defaults compatibility table must contain 91 entries")
    if len({entry.legacy_id for entry in entries}) != len(entries):
        raise ValueError("Defaults compatibility IDs must be unique")
    return tuple(entries)


_DEFAULTS_API_INVENTORY = _build_api_inventory()


def defaults_compatibility_api_inventory(
) -> tuple[DefaultsCompatibilityEntry, ...]:
    """Return the frozen reviewed API metadata inventory."""
    return _DEFAULTS_API_INVENTORY


def legacy_inventory_from_components(
    components: Iterable[Any],
) -> tuple[DefaultsLegacyInventoryItem, ...]:
    """Extract approved Defaults connectivity metadata without file access."""
    inventory: list[DefaultsLegacyInventoryItem] = []
    for component in components:
        pack_id = _component_value(component, "pack_id")
        if pack_id != "defaults":
            continue
        manifest = _component_value(component, "manifest")
        if not isinstance(manifest, Mapping):
            raise TypeError("approved component manifest must be a mapping")
        component_id = manifest.get("id", _component_value(component, "id"))
        component_version = manifest.get(
            "version",
            _component_value(component, "version"),
        )
        connectivity = manifest.get("connectivity")
        if not isinstance(component_id, str) or not component_id:
            raise TypeError("approved component ID must be a non-empty string")
        if not isinstance(component_version, str):
            raise TypeError("approved component version must be a string")
        if not isinstance(connectivity, Mapping):
            raise TypeError("approved component connectivity must be a mapping")
        provides = connectivity.get("provides")
        if not isinstance(provides, (list, tuple)):
            raise TypeError("approved connectivity provides must be a sequence")
        for legacy_id in provides:
            if not isinstance(legacy_id, str):
                raise TypeError("approved legacy ID must be a string")
            inventory.append(
                DefaultsLegacyInventoryItem(
                    legacy_id=legacy_id,
                    component_id=component_id,
                    component_version=component_version,
                )
            )
    return tuple(sorted(inventory, key=lambda item: item.legacy_id))


def build_defaults_compatibility_handle(
    legacy_inventory: Iterable[DefaultsLegacyInventoryItem],
    api_inventory: Iterable[DefaultsCompatibilityEntry],
) -> DefaultsCompatibilityHandle:
    """Validate the exact inventories and return an immutable selector."""
    entries = tuple(api_inventory)
    if entries != _DEFAULTS_API_INVENTORY:
        raise ValueError("API inventory does not match reviewed compatibility evidence")
    by_id = {entry.legacy_id: entry for entry in entries}
    items = tuple(legacy_inventory)
    if len(items) != 91:
        raise ValueError("approved Defaults inventory must contain 91 IDs")
    inventory_by_id: dict[str, DefaultsLegacyInventoryItem] = {}
    for item in items:
        if not isinstance(item, DefaultsLegacyInventoryItem):
            raise TypeError("legacy inventory contains an invalid item")
        if item.legacy_id in inventory_by_id:
            raise ValueError(f"duplicate approved legacy ID: {item.legacy_id}")
        inventory_by_id[item.legacy_id] = item
    if set(inventory_by_id) != set(by_id):
        raise ValueError("approved Defaults inventory is incomplete or unknown")
    for legacy_id, entry in by_id.items():
        item = inventory_by_id[legacy_id]
        if item.component_id != entry.component_id:
            raise ValueError(f"component identity mismatch: {legacy_id}")
        if item.component_version != entry.contract_version:
            raise ValueError(f"component version mismatch: {legacy_id}")
    return DefaultsCompatibilityHandle(by_id)


def _component_value(component: Any, key: str) -> Any:
    if isinstance(component, Mapping):
        return component.get(key)
    return getattr(component, key, None)
