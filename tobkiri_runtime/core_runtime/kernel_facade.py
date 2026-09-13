"""
kernel_facade.py - Pack コード向け最小限 API ラッパー

Wave 17-A: カーネルオブジェクト漏洩の封じ込め

Pack コード（HTTP サーバー、flow.construct.* 等）に公開する
最小限の API を提供する。Kernel 内部への直接アクセスを遮断する。

設計原則:
- __slots__ で属性列挙による内部参照漏洩を防止
- __getattr__ で未定義属性アクセス時に SecurityError を raise
- __repr__ で内部参照を漏らさない
- __setattr__ / __delattr__ をブロックしイミュータブルを維持
- スレッドセーフ（インスタンス自体は immutable）
"""

from __future__ import annotations

from typing import Any, Optional


class KernelSecurityError(Exception):
    """KernelFacade を通じた不正アクセス試行時に送出される例外。"""
    pass


class KernelFacade:
    """Pack コード（HTTP サーバー等）に公開する最小限 API。
    Kernel 内部への直接アクセスを遮断する。"""

    __slots__ = ("__kernel",)

    def __init__(self, kernel: Any) -> None:
        # name-mangling: self.__kernel -> self._KernelFacade__kernel
        # object.__setattr__ で直接設定し、__setattr__ ブロックを回避する。
        object.__setattr__(self, "_KernelFacade__kernel", kernel)

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def get_interface(self, key: str, strategy: str = "last") -> Any:
        """InterfaceRegistry から値を取得（read-only）。"""
        return self.__kernel.interface_registry.get(key, strategy=strategy)

    def list_interfaces(self, prefix: Optional[str] = None) -> dict:
        """InterfaceRegistry のキー一覧を取得（read-only）。

        prefix が指定された場合、そのプレフィックスで始まるキーのみ返す。
        """
        all_items = self.__kernel.interface_registry.list() or {}
        if prefix is None:
            return dict(all_items)
        return {k: v for k, v in all_items.items() if k.startswith(prefix)}

    def emit(self, event_name: str, data: Any = None) -> None:
        """EventBus にイベントを発火する。"""
        if self.__kernel.event_bus:
            self.__kernel.event_bus.publish(event_name, data)

    # ------------------------------------------------------------------
    # アクセス制御
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """未定義属性へのアクセスを遮断する。"""
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise KernelSecurityError(
            f"Access to '{name}' is not permitted through KernelFacade. "
            f"Only get_interface(), list_interfaces(), and emit() are available."
        )

    def __setattr__(self, name: str, value: Any) -> None:
        """属性の設定を禁止する（イミュータブル維持）。"""
        raise KernelSecurityError(
            f"Cannot set attribute '{name}' on KernelFacade. "
            f"KernelFacade is immutable."
        )

    def __delattr__(self, name: str) -> None:
        """属性の削除を禁止する。"""
        raise KernelSecurityError(
            f"Cannot delete attribute '{name}' from KernelFacade. "
            f"KernelFacade is immutable."
        )

    # ------------------------------------------------------------------
    # 安全な表現
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return "<KernelFacade: restricted API proxy>"

    def __str__(self) -> str:
        return "<KernelFacade: restricted API proxy>"

    # ------------------------------------------------------------------
    # dir() による属性列挙を制限
    # ------------------------------------------------------------------

    def __dir__(self):
        return ["get_interface", "list_interfaces", "emit"]


__all__ = ["KernelFacade", "KernelSecurityError"]
