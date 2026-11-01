# Rumi Media Capture Host Service Pack

This pack owns descriptors for screen, microphone, system-audio and camera
capture plus audio/speech output. It does not open devices, retain raw media, or
send media over the network. Core Authority and the Viewer host broker remain
the approval and execution owners.

Capture and output are independently permissioned contracts. Recording duration
is bounded to five minutes and client approval material is rejected.

Validation was not executed by the implementation agent.
Independent testing is required before merge.

