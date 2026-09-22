"""
pack_validator.py - Pack ecosystem.json 検証ツール

standalone 実行用のバリデーション。
- connectivity フィールドの存在・空チェック
- pack_id とディレクトリ名の不一致チェック
- ${ctx.*} 変数参照が connectivity 先に含まれるか簡易チェック
- W18-B: required_secrets, required_network, host_execution バリデーション
- W19-A: validate_host_execution() — host_execution: true Pack の起動時拒否ガード
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection, Dict, List, Optional, Set, Tuple

from .paths import (
    PackLocation,
    discover_pack_locations,
    get_pack_flow_dirs,
)
from .pack_boundary import finite_children, finite_files

logger = logging.getLogger(__name__)

# Pack type validation
_VALID_PACK_TYPES = frozenset({"rule", "application", "library"})
_VALID_RUNTIME_TYPES = frozenset({"python", "binary", "command"})

# W18-B: Secret key pattern for required_secrets validation
_SECRET_KEY_PATTERN = re.compile(r"^[A-Z0-9_]{1,64}$")

# W24-C: function_id pattern — lowercase alphanumeric + underscore, starts with letter
_FUNCTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

# ${ctx.PACK_ID.anything} パターン — PACK_ID 部分を抽出
_CTX_REF_PATTERN = re.compile(r"\$\{ctx\.([^.}]+)")


# ======================================================================
# データクラス
# ======================================================================

@dataclass
class ValidationReport:
    """Pack 検証結果レポート"""
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    pack_count: int = 0
    valid_count: int = 0


# ======================================================================
# 公開 API
# ======================================================================

def validate_packs(ecosystem_dir: Optional[str] = None) -> ValidationReport:
    """
    全 Pack の ecosystem.json を検証し、ValidationReport を返す。

    Args:
        ecosystem_dir: エコシステムルート。None なら paths.ECOSYSTEM_DIR を使用。

    Returns:
        ValidationReport
    """
    report = ValidationReport()

    # --- ecosystem_dir 存在チェック ---
    if ecosystem_dir is not None:
        eco_path = Path(ecosystem_dir)
        if not eco_path.is_dir():
            msg = f"Ecosystem directory does not exist: {ecosystem_dir}"
            logger.error(msg)
            report.errors.append(msg)
            return report

    # --- Pack 一覧を取得 ---
    try:
        locations = discover_pack_locations(ecosystem_dir)
    except Exception as exc:
        msg = f"Failed to discover packs: {exc}"
        logger.error(msg)
        report.errors.append(msg)
        return report

    report.pack_count = len(locations)

    # 全 pack_id のセット（connectivity 参照先の存在確認用）
    all_pack_ids: Set[str] = {loc.pack_id for loc in locations}

    for loc in locations:
        pack_warnings, pack_errors = _validate_single_pack(loc, all_pack_ids)
        report.warnings.extend(pack_warnings)
        report.errors.extend(pack_errors)

    report.valid_count = report.pack_count - _count_packs_with_issues(
        locations, report.warnings, report.errors,
    )

    return report


def validate_host_execution(
    ecosystem_dir: Optional[str] = None,
    pack_ids: Optional[Collection[str]] = None,
) -> List[str]:
    """
    W19-A: host_execution: true の Pack を検出し、未承認なら起動を拒否する。

    - host_execution: true の Pack が1つ以上ある場合、WARNING を出力
    - RUMI_ALLOW_HOST_EXECUTION 環境変数が "true" でなければ sys.exit(1) で終了
    - RUMI_ALLOW_HOST_EXECUTION=true の場合は WARNING を出力して続行

    Args:
        ecosystem_dir: エコシステムルート。None なら paths.ECOSYSTEM_DIR を使用。
        pack_ids: 起動対象の Pack ID。指定時は、選択された Pack だけを検証する。

    Returns:
        host_execution: true の Pack ID リスト

    Raises:
        SystemExit: RUMI_ALLOW_HOST_EXECUTION 未設定/非 "true" で
                    host_execution: true の Pack が存在する場合
    """
    # --- Pack 一覧を取得 ---
    try:
        locations = discover_pack_locations(ecosystem_dir)
    except Exception as exc:
        logger.warning("Failed to discover packs for host_execution check: %s", exc)
        return []

    selected_pack_ids = None if pack_ids is None else {str(pack_id) for pack_id in pack_ids}
    host_exec_packs: List[str] = []

    for loc in locations:
        if selected_pack_ids is not None and loc.pack_id not in selected_pack_ids:
            continue
        try:
            with open(loc.ecosystem_json_path, "r", encoding="utf-8") as f:
                eco_data = json.load(f)
        except Exception:
            continue

        if not isinstance(eco_data, dict):
            continue

        if eco_data.get("host_execution") is True:
            host_exec_packs.append(loc.pack_id)

    if not host_exec_packs:
        return []

    # host_execution: true の Pack が検出された
    pack_list_str = ", ".join(sorted(host_exec_packs))
    print(
        f"WARNING: The following Packs request host_execution: {pack_list_str}",
        file=sys.stderr,
    )

    allow_flag = os.environ.get("RUMI_ALLOW_HOST_EXECUTION", "").lower()
    if allow_flag == "true":
        print(
            "WARNING: RUMI_ALLOW_HOST_EXECUTION=true is set. "
            "Allowing host_execution Packs to run.",
            file=sys.stderr,
        )
        return host_exec_packs

    print(
        "FATAL: Packs with host_execution: true require explicit approval. "
        "Set RUMI_ALLOW_HOST_EXECUTION=true to allow.",
        file=sys.stderr,
    )
    sys.exit(1)


# ======================================================================
# 内部関数
# ======================================================================

def _count_packs_with_issues(
    locations: List[PackLocation],
    warnings: List[str],
    errors: List[str],
) -> int:
    """警告またはエラーが1件以上ある Pack の数を返す。"""
    flagged: Set[str] = set()
    all_messages = warnings + errors
    for loc in locations:
        prefix = f"[{loc.pack_id}]"
        for msg in all_messages:
            if msg.startswith(prefix):
                flagged.add(loc.pack_id)
                break
    return len(flagged)


def _validate_single_pack(
    loc: PackLocation,
    all_pack_ids: Set[str],
) -> tuple[List[str], List[str]]:
    """
    単一 Pack を検証する。

    Returns:
        (warnings, errors)
    """
    warnings: List[str] = []
    errors: List[str] = []
    pid = loc.pack_id

    # --- ecosystem.json の読み込み ---
    eco_data: Optional[Dict[str, Any]] = None
    try:
        with open(loc.ecosystem_json_path, "r", encoding="utf-8") as f:
            eco_data = json.load(f)
    except json.JSONDecodeError as exc:
        msg = f"[{pid}] ecosystem.json is invalid JSON: {exc}"
        logger.error(msg)
        errors.append(msg)
        return warnings, errors
    except OSError as exc:
        msg = f"[{pid}] Cannot read ecosystem.json: {exc}"
        logger.error(msg)
        errors.append(msg)
        return warnings, errors

    if not isinstance(eco_data, dict):
        msg = f"[{pid}] ecosystem.json root is not an object"
        logger.error(msg)
        errors.append(msg)
        return warnings, errors

    # --- (1) connectivity フィールド存在チェック ---
    connectivity: Optional[Any] = eco_data.get("connectivity")
    connectivity_list: List[str] = []

    if connectivity is None:
        msg = f"[{pid}] 'connectivity' field is not declared in ecosystem.json"
        logger.warning(msg)
        warnings.append(msg)
    elif isinstance(connectivity, list):
        if len(connectivity) == 0:
            msg = f"[{pid}] 'connectivity' is declared but empty ([])"
            logger.warning(msg)
            warnings.append(msg)
        else:
            connectivity_list = [
                str(c) for c in connectivity if isinstance(c, str)
            ]
            # PC-8 fix: 非文字列要素に警告を出す（タイポ検知）
            non_str_items = [c for c in connectivity if not isinstance(c, str)]
            if non_str_items:
                msg = (
                    f"[{pid}] 'connectivity' contains non-string elements "
                    f"that were ignored: {non_str_items}"
                )
                logger.warning(msg)
                warnings.append(msg)
    elif isinstance(connectivity, dict):
        requires = connectivity.get("requires", [])
        provides = connectivity.get("provides", [])

        if not isinstance(requires, list):
            msg = (
                f"[{pid}] 'connectivity.requires' field is not a list: "
                f"{type(requires).__name__}"
            )
            logger.warning(msg)
            warnings.append(msg)
        if not isinstance(provides, list):
            msg = (
                f"[{pid}] 'connectivity.provides' field is not a list: "
                f"{type(provides).__name__}"
            )
            logger.warning(msg)
            warnings.append(msg)

        if isinstance(requires, list):
            connectivity_list = [
                str(c) for c in requires if isinstance(c, str)
            ]
            non_str_requires = [c for c in requires if not isinstance(c, str)]
            if non_str_requires:
                msg = (
                    f"[{pid}] 'connectivity.requires' contains non-string elements "
                    f"that were ignored: {non_str_requires}"
                )
                logger.warning(msg)
                warnings.append(msg)

        if isinstance(provides, list):
            non_str_provides = [c for c in provides if not isinstance(c, str)]
            if non_str_provides:
                msg = (
                    f"[{pid}] 'connectivity.provides' contains non-string elements "
                    f"that were ignored: {non_str_provides}"
                )
                logger.warning(msg)
                warnings.append(msg)

        if (
            isinstance(requires, list)
            and isinstance(provides, list)
            and len(requires) == 0
            and len(provides) == 0
        ):
            msg = f"[{pid}] 'connectivity' is declared but empty ({'{'}{'}'})"
            logger.warning(msg)
            warnings.append(msg)
    else:
        msg = (
            f"[{pid}] 'connectivity' field is not a list: "
            f"{type(connectivity).__name__}"
        )
        logger.warning(msg)
        warnings.append(msg)

    # --- (2) pack_id 不一致チェック ---
    declared_id = eco_data.get("pack_id")
    if declared_id and declared_id != pid:
        msg = (
            f"[{pid}] pack_id mismatch: ecosystem.json declares "
            f"'{declared_id}' but directory name is '{pid}'"
        )
        logger.warning(msg)
        warnings.append(msg)

    # --- (3) ${ctx.*} 参照チェック ---
    ctx_warnings = _check_ctx_references(loc, connectivity_list, all_pack_ids)
    for w in ctx_warnings:
        logger.warning(w)
    warnings.extend(ctx_warnings)

    # --- W18-B (4) required_secrets バリデーション ---
    if "required_secrets" in eco_data:
        rs = eco_data["required_secrets"]
        if not isinstance(rs, list):
            msg = f"[{pid}] required_secrets must be a list"
            logger.error(msg)
            errors.append(msg)
        else:
            for key in rs:
                if not isinstance(key, str) or not _SECRET_KEY_PATTERN.match(key):
                    msg = f"[{pid}] invalid secret key '{key}'"
                    logger.error(msg)
                    errors.append(msg)

    # --- W18-B (5) required_network バリデーション ---
    if "required_network" in eco_data:
        rn = eco_data["required_network"]
        if not isinstance(rn, dict):
            msg = f"[{pid}] required_network must be a dict"
            logger.error(msg)
            errors.append(msg)
        else:
            ad = rn.get("allowed_domains", [])
            ap = rn.get("allowed_ports", [])
            if not isinstance(ad, list):
                msg = f"[{pid}] allowed_domains must be a list"
                logger.error(msg)
                errors.append(msg)
            if not isinstance(ap, list):
                msg = f"[{pid}] allowed_ports must be a list of integers"
                logger.error(msg)
                errors.append(msg)
            else:
                for p in ap:
                    if not isinstance(p, int) or p < 0 or p > 65535:
                        msg = f"[{pid}] invalid port {p}"
                        logger.error(msg)
                        errors.append(msg)

    # --- W18-B (6) host_execution バリデーション ---
    if "host_execution" in eco_data:
        he = eco_data["host_execution"]
        if not isinstance(he, bool):
            msg = f"[{pid}] host_execution must be a boolean"
            logger.error(msg)
            errors.append(msg)


    # --- (7a) pack_type バリデーション ---
    pack_type = eco_data.get("pack_type")
    if pack_type is not None:
        if not isinstance(pack_type, str) or pack_type not in _VALID_PACK_TYPES:
            msg = (
                f"[{pid}] invalid pack_type '{pack_type}': "
                f"must be one of {sorted(_VALID_PACK_TYPES)}"
            )
            logger.error(msg)
            errors.append(msg)
    else:
        # 省略時のデフォルトは "application" — ここでは警告なし（後方互換）
        pack_type = "application"

    # --- (7b) provides_runtime バリデーション ---
    if "provides_runtime" in eco_data:
        pr = eco_data["provides_runtime"]
        if pack_type != "rule":
            msg = (
                f"[{pid}] provides_runtime is only valid for "
                f"pack_type 'rule', but pack_type is '{pack_type}'"
            )
            logger.error(msg)
            errors.append(msg)
        elif not isinstance(pr, list) or not all(isinstance(r, str) for r in pr):
            msg = f"[{pid}] provides_runtime must be a list of strings"
            logger.error(msg)
            errors.append(msg)

    # --- (7c) runtime_type バリデーション ---
    if "runtime_type" in eco_data:
        rt = eco_data["runtime_type"]
        if not isinstance(rt, str) or rt not in _VALID_RUNTIME_TYPES:
            msg = (
                f"[{pid}] invalid runtime_type '{rt}': "
                f"must be one of {sorted(_VALID_RUNTIME_TYPES)}"
            )
            logger.error(msg)
            errors.append(msg)

    # --- W24-C (7) functions/ manifest.json バリデーション ---
    func_warnings, func_errors = _validate_functions(loc.pack_subdir, pid)
    warnings.extend(func_warnings)
    errors.extend(func_errors)

    return warnings, errors


def _check_ctx_references(
    loc: PackLocation,
    connectivity_list: List[str],
    all_pack_ids: Set[str],
) -> List[str]:
    """
    Flow ファイル内の ${ctx.PACK_ID.*} 参照を検出し、
    参照先が connectivity に含まれていない場合に warning を返す。

    best-effort: Flow ファイルが存在しない・読めない場合はスキップ。
    """
    warnings: List[str] = []
    pid = loc.pack_id

    flow_dirs = get_pack_flow_dirs(loc.pack_subdir)
    if not flow_dirs:
        return warnings

    connectivity_set: Set[str] = set(connectivity_list)

    for flow_dir in flow_dirs:
        try:
            flow_files = finite_files(
                flow_dir, (".json", ".yaml", ".yml"), recursive=True
            )
        except OSError:
            continue

        for fpath in flow_files:
            if not fpath.is_file():
                continue
            suffix = fpath.suffix.lower()
            if suffix not in (".json", ".yaml", ".yml"):
                continue

            try:
                content = fpath.read_text(encoding="utf-8")
            except OSError:
                continue

            referenced_packs = set(_CTX_REF_PATTERN.findall(content))
            for ref_pack_id in sorted(referenced_packs):
                # 自身への参照はスキップ
                if ref_pack_id == pid:
                    continue
                if ref_pack_id not in connectivity_set:
                    rel_path = fpath.relative_to(loc.pack_subdir)
                    msg = (
                        f"[{pid}] Flow '{rel_path}' references "
                        f"${{ctx.{ref_pack_id}.*}} but '{ref_pack_id}' "
                        f"is not in connectivity"
                    )
                    warnings.append(msg)

    return warnings



def _validate_functions(
    pack_subdir: Path,
    pid: str,
) -> tuple[List[str], List[str]]:
    """
    W24-C: Pack 内 functions/ ディレクトリの manifest.json を検証する。

    functions/ が存在しなければ何もしない（後方互換）。

    Returns:
        (warnings, errors)
    """
    warnings: List[str] = []
    errors: List[str] = []

    functions_dir = pack_subdir / "functions"
    if not functions_dir.is_dir():
        return warnings, errors

    try:
        func_dirs = tuple(
            d
            for d in finite_children(functions_dir, directories_only=True)
            if not d.name.startswith(".") and not d.name.startswith("__")
        )
    except OSError:
        return warnings, errors

    for func_dir in func_dirs:
        func_name = func_dir.name
        prefix = f"[{pid}] functions/{func_name}"

        # --- manifest.json 存在チェック ---
        manifest_path = func_dir / "manifest.json"
        if not manifest_path.is_file():
            msg = f"{prefix}: manifest.json not found"
            logger.error(msg)
            errors.append(msg)
            continue

        # --- JSON パース ---
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except json.JSONDecodeError as exc:
            msg = f"{prefix}: manifest.json is invalid JSON: {exc}"
            logger.error(msg)
            errors.append(msg)
            continue

        if not isinstance(manifest, dict):
            msg = f"{prefix}: manifest.json root is not an object"
            logger.error(msg)
            errors.append(msg)
            continue

        # --- function_id ---
        fid = manifest.get("function_id")
        if fid is None or not isinstance(fid, str):
            msg = f"{prefix}: function_id is missing or not a string"
            logger.error(msg)
            errors.append(msg)
        else:
            if fid != func_name:
                msg = (
                    f"{prefix}: function_id '{fid}' does not match "
                    f"directory name '{func_name}'"
                )
                logger.error(msg)
                errors.append(msg)
            if not _FUNCTION_ID_PATTERN.match(fid):
                msg = (
                    f"{prefix}: function_id '{fid}' does not match "
                    f"required pattern ^[a-z][a-z0-9_]*$"
                )
                logger.error(msg)
                errors.append(msg)

        # --- requires ---
        requires = manifest.get("requires")
        if requires is None:
            msg = f"{prefix}: requires field is missing"
            logger.error(msg)
            errors.append(msg)
        elif not isinstance(requires, list) or not all(
            isinstance(r, str) for r in requires
        ):
            msg = f"{prefix}: requires must be a list of strings"
            logger.error(msg)
            errors.append(msg)

        # --- caller_requires (optional) ---
        if "caller_requires" in manifest:
            cr = manifest["caller_requires"]
            if not isinstance(cr, list) or not all(
                isinstance(c, str) for c in cr
            ):
                msg = f"{prefix}: caller_requires must be a list of strings"
                logger.error(msg)
                errors.append(msg)

        # --- host_execution (optional) ---
        if "host_execution" in manifest:
            he = manifest["host_execution"]
            if not isinstance(he, bool):
                msg = f"{prefix}: host_execution must be a boolean"
                logger.error(msg)
                errors.append(msg)
            elif he is True:
                msg = (
                    f"{prefix}: host_execution is true — "
                    f"this function runs in host environment. "
                    f"Approval is required."
                )
                logger.warning(msg)
                warnings.append(msg)

        # --- tags (optional) ---
        if "tags" in manifest:
            tags = manifest["tags"]
            if not isinstance(tags, list) or not all(
                isinstance(t, str) for t in tags
            ):
                msg = f"{prefix}: tags must be a list of strings"
                logger.error(msg)
                errors.append(msg)
        else:
            msg = (
                f"{prefix}: tags field is not set — "
                f"discoverability may be reduced"
            )
            logger.warning(msg)
            warnings.append(msg)

        # --- input_schema (optional) ---
        if "input_schema" in manifest:
            if not isinstance(manifest["input_schema"], dict):
                msg = f"{prefix}: input_schema must be a dict"
                logger.error(msg)
                errors.append(msg)

        # --- output_schema (optional) ---
        if "output_schema" in manifest:
            if not isinstance(manifest["output_schema"], dict):
                msg = f"{prefix}: output_schema must be a dict"
                logger.error(msg)
                errors.append(msg)

        # --- main.py 存在チェック ---
        main_py = func_dir / "main.py"
        if not main_py.is_file():
            msg = f"{prefix}: main.py not found"
            logger.error(msg)
            errors.append(msg)

        # --- description (warning) ---
        desc = manifest.get("description")
        if desc is None or (isinstance(desc, str) and desc.strip() == ""):
            msg = f"{prefix}: description is missing or empty"
            logger.warning(msg)
            warnings.append(msg)

    return warnings, errors


# ======================================================================
# W19-D: host_execution ガード
# ======================================================================

def validate_host_execution_single(pack_config: dict) -> Tuple[bool, str]:
    """
    Pack の host_execution フィールドを検証する。

    host_execution が true の場合、環境変数 RUMI_ALLOW_HOST_EXECUTION が
    "true" でなければ起動を拒否する。

    Args:
        pack_config: ecosystem.json をパースした dict

    Returns:
        (ok, message)
        - ok=False の場合は起動拒否。message にエラー理由。
        - ok=True かつ message が空文字列なら問題なし。
        - ok=True かつ message が非空なら WARNING。
    """
    host_exec = pack_config.get("host_execution", False)
    if not host_exec:
        return (True, "")

    env_val = os.environ.get("RUMI_ALLOW_HOST_EXECUTION")
    if env_val == "true":
        logger.warning("host_execution enabled for pack")
        return (True, "WARNING: host_execution enabled")

    return (False, "host_execution requires RUMI_ALLOW_HOST_EXECUTION=true")
