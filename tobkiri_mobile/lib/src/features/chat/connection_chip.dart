import 'package:flutter/material.dart';

import '../../data/pc/device_store.dart';
import '../../domain/connection_state.dart';

class ConnectionChip extends StatelessWidget {
  const ConnectionChip({
    super.key,
    required this.connectionView,
    this.pairedDevice,
    this.onTap,
  });

  final DeviceConnectionView connectionView;
  final PairedDevice? pairedDevice;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;

    final isOnline = connectionView.isPcOnline;
    final isPaired = connectionView.isPaired;

    final Color color;
    final IconData icon;
    final String label;

    if (isPaired && isOnline) {
      color = scheme.primary;
      icon = Icons.desktop_windows;
      label = pairedDevice?.displayPcLabel ?? 'PC';
    } else if (isPaired && !isOnline) {
      color = scheme.error;
      icon = Icons.desktop_windows;
      label = '${pairedDevice?.displayPcLabel ?? "PC"} オフライン';
    } else {
      color = scheme.onSurfaceVariant;
      icon = Icons.phone_android;
      label = 'スマホ単体';
    }

    return InkWell(
      borderRadius: BorderRadius.circular(999),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(999),
          color: color.withValues(alpha: 0.12),
          border: Border.all(color: color.withValues(alpha: 0.3)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 8,
              height: 8,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: color,
              ),
            ),
            const SizedBox(width: 6),
            Icon(icon, size: 14, color: color),
            const SizedBox(width: 4),
            ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 112),
              child: Text(
                label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: FontWeight.w500,
                  color: color,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
