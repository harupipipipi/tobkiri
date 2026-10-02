# macOS unsigned distribution

Tobkiri Launcher is distributed as open-source software without a paid Apple
Developer ID identity or Apple notarization. The macOS release jobs pass
`--no-sign` to Tauri. Local development builds may still carry an ad-hoc code
signature created by the toolchain, but that is not a stable publisher
identity.

The production application identifier remains `dev.rumiai.app`. Keeping this
identifier preserves the published application-data location. It does not make
an unsigned or ad-hoc build equivalent to a Developer ID-signed application.

## macOS constraints

- Gatekeeper can block or warn about a downloaded build because Apple has not
  notarized it. Users should inspect the source and release checks and decide
  through the normal macOS security UI whether to run it.
- Browsers and archive tools can attach the `com.apple.quarantine` attribute.
  Tobkiri does not remove or bypass quarantine automatically.
- Privacy permissions managed by TCC, including Accessibility, Screen
  Recording, and Automation, are tied to more than the bundle identifier. An
  ad-hoc signature is not a stable designated requirement, so replacing or
  rebuilding the application can cause macOS to request permission again.
- The project therefore does not promise TCC permission continuity between
  independently built or downloaded unsigned artifacts.

Release notes must describe the artifact as unsigned/ad-hoc. They must not
claim Developer ID signing, notarization, Gatekeeper pre-approval, or guaranteed
TCC permission inheritance.
