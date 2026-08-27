import 'package:flutter/widgets.dart';

/// Whether nonessential motion is allowed by the current platform settings.
bool motionAllowedOf(BuildContext context) {
  return !(MediaQuery.maybeOf(context)?.disableAnimations ?? false);
}

/// Return [duration] unless platform accessibility requests instant changes.
Duration motionDurationOf(BuildContext context, Duration duration) {
  return motionAllowedOf(context) ? duration : Duration.zero;
}
