"""Internal QEMU transport ordering marker; never an authentication proof.

The real guest agent emits this once after opening its data-stream descriptor.
The Host still verifies the independent nonce-bound, signed attestation before
admitting the VM. This fixed marker carries no authority or guest identity.
"""

SERIAL_READY_FRAME = b'{"kind":"tobkiri.packvm.guest.transport-ready.v1","version":1}'
