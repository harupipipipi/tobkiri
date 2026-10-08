# Profile Pack revisions

The Profile version selector lists verified bundled and admitted signed
revisions. Saving a choice appends an immutable Profile definition under the
reviewed definition, store-generation and catalog CAS. It does not activate a
Profile or transfer an existing Pack approval to new bytes.

Required Pack choices remain in `packs`. Optional choices are recorded only as
`optional_pack_revisions`, containing a Pack ID and exact artifact digest.
These rows are not resolution roots, requested edges, enablement flags or
authority. Disabled choices therefore add no operations to the resolved plan.
After reviewing and activating the Profile, install and approve the selected
optional revision through Pack control before enabling it. Disabling the Pack
retains its version choice.

The running Profile retains its captured artifacts and exact historical source
while newer definitions await activation. Pack install and approval checks use
those captured revisions, not a pending definition. An intact predecessor
install is displayed as requiring installation for a newly selected, disabled
revision; altered predecessor contents still fail verification. Previous
artifacts, definitions and activation plans remain available for review and
rollback through the existing activation ceremony.
