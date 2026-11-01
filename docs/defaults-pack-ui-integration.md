# Defaults Pack UI integration

The source baseline is PR #1496 at
`51bbc8180732f7f6f525256d0ab8a0f3dc878899`. Draft PR #1500 integrates the
workspace tab, history organization, semantic attention, and Beautiful UI
adaptations. It also wires verified public views into all Application slots,
with generic dirty/pending navigation guards and the shared voice/TaskPet
source dependencies. Catalog generation and distribution belong to the final
integration branch.

The fixed `conversation_thread` renderer reuses the Application's message and
composer renderers. A descriptor declares canonical conversation/message paths
and captured send, stop, and read operations. The optional Side Chat Pack in
PR #1503 supplies this descriptor; the Application contains no side-chat-specific
route or provider selection. Its inherited model is a read-only label. Voice,
attachments, tools, model selection, and grant overrides are disabled on this
initial text surface. The Side Chat Pack documents unsupported advanced parent
context resolution separately.

Sending preserves the exact text bytes and owner revisions under a new secure
turn ID. A lost reply retains the draft and ticket and offers an explicit record
read; it does not resend automatically. A terminal label alone cannot clear the
draft: the saved receipt and canonical child revision/messages must agree.
Stopping requests cancellation and continues reading owner state. Confirmed
cancellation and failure remain distinct. Buffered owner results are shown as
buffered results, without invented token streaming or progress. Reads pause while
the document is hidden and ignore replies after the captured view unmounts.

`metadata.is_hidden === true` omits an owner record from the main history and
recent search display without deleting it or changing its permissions. Canonical
owner snapshots and directly admitted resources remain intact. Server search
projection must apply the same neutral flag for records absent from the current
frontend list.

The local frontend tests cover canonical identity, exact text, UTF-8 limits,
authority override rejection, late revisions, saved receipts, static shared
rendering, and text composer controls. Real runtime Computer Use acceptance for
this source revision is still pending; a separately installed Launcher does not
prove this bundle's behavior.
