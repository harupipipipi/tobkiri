"""Media処理ドメインロジック"""

import os


def read_image(path):
    """画像メタデータを取得する（スタブ実装）。

    pathの存在チェックを行い、存在すればファイルサイズを取得する。
    width / height / format はダミー値を返す。

    Args:
        path: 画像ファイルパス

    Returns:
        dict: 画像メタデータ

    Raises:
        FileNotFoundError: パスが存在しない場合
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Image not found: {path}")

    size_bytes = os.path.getsize(path)

    # スタブ: 実際のデコードは行わずダミー値を返す
    ext = os.path.splitext(path)[1].lstrip(".").upper() or "UNKNOWN"
    return {
        "path": path,
        "width": 0,
        "height": 0,
        "format": ext,
        "size_bytes": size_bytes,
    }


def transform_image(path, operations):
    """画像変換（スタブ実装）。

    operations を記録して返すだけで実際の変換は行わない。

    Args:
        path: 画像ファイルパス
        operations: 適用する変換操作のリスト

    Returns:
        dict: 変換結果情報
    """
    applied = [op.get("type", "unknown") for op in (operations or [])]
    return {
        "output_path": path,
        "operations_applied": applied,
    }


def parse_document(path):
    """ドキュメントをパースする（スタブ実装）。

    Args:
        path: ドキュメントファイルパス

    Returns:
        str: パースされたコンテンツ文字列
    """
    return f"parsed content from {path}"
