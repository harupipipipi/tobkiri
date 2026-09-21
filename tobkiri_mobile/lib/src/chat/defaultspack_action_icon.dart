import 'package:flutter/material.dart';

enum DefaultspackActionIconKind { newChat }

class DefaultspackActionIcon extends StatelessWidget {
  const DefaultspackActionIcon({
    super.key,
    required this.kind,
    this.size = 22,
    this.strokeWidth = 2,
  });

  final DefaultspackActionIconKind kind;
  final double size;
  final double strokeWidth;

  @override
  Widget build(BuildContext context) {
    final color = IconTheme.of(context).color ??
        DefaultTextStyle.of(context).style.color ??
        Colors.white;
    return SizedBox.square(
      dimension: size,
      child: CustomPaint(
        painter: _DefaultspackActionIconPainter(
          kind: kind,
          color: color,
          strokeWidth: strokeWidth,
        ),
      ),
    );
  }
}

class _DefaultspackActionIconPainter extends CustomPainter {
  const _DefaultspackActionIconPainter({
    required this.kind,
    required this.color,
    required this.strokeWidth,
  });

  final DefaultspackActionIconKind kind;
  final Color color;
  final double strokeWidth;

  @override
  void paint(Canvas canvas, Size size) {
    final scale = size.shortestSide / 24;
    canvas
      ..save()
      ..scale(scale, scale);

    final paint = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = strokeWidth
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;

    switch (kind) {
      case DefaultspackActionIconKind.newChat:
        _paintNewChat(canvas, paint);
    }

    canvas.restore();
  }

  void _paintNewChat(Canvas canvas, Paint paint) {
    final body = Path()
      ..moveTo(11, 4)
      ..lineTo(7.5, 4)
      ..cubicTo(5.57, 4, 4, 5.57, 4, 7.5)
      ..lineTo(4, 16.5)
      ..cubicTo(4, 18.43, 5.57, 20, 7.5, 20)
      ..lineTo(16.5, 20)
      ..cubicTo(18.43, 20, 20, 18.43, 20, 16.5)
      ..lineTo(20, 13);

    final pencil = Path()
      ..moveTo(18.5, 2.5)
      ..cubicTo(19.33, 1.67, 20.67, 1.67, 21.5, 2.5)
      ..cubicTo(22.33, 3.33, 22.33, 4.67, 21.5, 5.5)
      ..lineTo(12, 15)
      ..lineTo(8, 16)
      ..lineTo(9, 12)
      ..close();

    canvas
      ..drawPath(body, paint)
      ..drawPath(pencil, paint);
  }

  @override
  bool shouldRepaint(covariant _DefaultspackActionIconPainter oldDelegate) {
    return oldDelegate.kind != kind ||
        oldDelegate.color != color ||
        oldDelegate.strokeWidth != strokeWidth;
  }
}
