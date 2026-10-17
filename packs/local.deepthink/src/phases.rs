//! Pack-owned phase contracts and prompts, independent of Wasm and providers.

use serde_json::{Value, json};

fn text() -> Value {
    json!({"type":"string"})
}
fn nonempty() -> Value {
    json!({"type":"string","minLength":1})
}
fn strings() -> Value {
    json!({"type":"array","items":nonempty()})
}
fn array(items: Value, minimum: usize) -> Value {
    json!({"type":"array","items":items,"minItems":minimum})
}
fn object(properties: Value) -> Value {
    let required = properties
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    json!({"type":"object","properties":properties,"required":required,"additionalProperties":false})
}
fn checks() -> Value {
    array(
        object(json!({"criterion_id":nonempty(),"criterion":nonempty(),
        "passed":{"type":"boolean"},"evidence":text()})),
        1,
    )
}

/// Full JSON contract for a phase, including an explicit real-tool request lane.
pub fn schema(phase: &str) -> Value {
    let body = match phase.split(':').next().unwrap_or(phase) {
        "goal_intake" => json!({"objective":nonempty(),
            "requirements":array(object(json!({"id":nonempty(),"description":nonempty(),"acceptance_criteria":strings()})),1),
            "deliverables":array(object(json!({"id":nonempty(),"title":nonempty(),"format":nonempty()})),1),
            "quality_terms":array(object(json!({"term":nonempty(),"context":nonempty()})),0),
            "production_kind":nonempty(),"constraints":strings(),"assumptions":strings(),"clarifications":strings()}),
        "quality_terms" => json!({"term":nonempty(),"definition":text(),
            "metrics":array(object(json!({"name":nonempty(),"measurement":nonempty(),"target":nonempty()})),0),
            "resolved":{"type":"boolean"},"assumptions":strings(),"clarifications":strings()}),
        "goal_contract" => json!({"objective":nonempty(),
            "success_criteria":array(object(json!({"id":nonempty(),"requirement_id":nonempty(),"criterion":nonempty(),"measurement":nonempty(),"target":nonempty()})),1),
            "deliverables":array(object(json!({"id":nonempty(),"title":nonempty(),"format":nonempty()})),1),
            "constraints":strings(),"assumptions":strings(),"clarifications":strings(),"confirmed":{"type":"boolean"}}),
        "preflight" => {
            json!({"checks":array(object(json!({"requirement":nonempty(),"passed":{"type":"boolean"},"evidence":text()})),1),
            "blockers":strings(),"clarifications":strings()})
        }
        "answer_plan" => {
            json!({"sections":array(object(json!({"id":nonempty(),"title":nonempty(),"objective":nonempty(),"acceptance_criteria":strings()})),1),
            "integration_plan":nonempty(),"research_required":{"type":"boolean"},"reference_search_required":{"type":"boolean"},"clarifications":strings()})
        }
        "capability_assessment" => {
            json!({"capabilities":array(object(json!({"requirement":nonempty(),"available":{"type":"boolean"},"method":text(),"limitations":text()})),1),
            "blockers":strings(),"clarifications":strings()})
        }
        "means_selection" => {
            json!({"selected_tools":strings(),"selected_skills":strings(),"workflow":array(nonempty(),1),"gaps":strings(),"clarifications":strings()})
        }
        "work_environment" => {
            json!({"workspace_plan":nonempty(),"editing_environment":nonempty(),"validation_environment":nonempty(),
            "checks":array(object(json!({"capability":nonempty(),"status":{"enum":["available","planned","unavailable"]},"evidence":text()})),1),
            "setup_actions":strings(),"limitations":strings(),"clarifications":strings(),"ready":{"type":"boolean"}})
        }
        "profile_check" => {
            json!({"passed":{"type":"boolean"},"findings":strings(),"clarifications":strings()})
        }
        "reference_plan" => {
            json!({"queries":array(nonempty(),1),"comparison_criteria":object(json!({"source_url":nonempty(),"rights":nonempty(),"viewpoints":nonempty(),"shape":nonempty(),"materials":nonempty(),"dimensions":nonempty()})),
            "selection_method":nonempty(),"editing_environment":nonempty(),"validation_environment":nonempty(),"assumptions":strings(),"clarifications":strings()})
        }
        "reference_search" => {
            json!({"references":array(object(json!({"id":nonempty(),"title":nonempty(),"source_url":text(),
            "rights":object(json!({"status":{"enum":["confirmed","unknown","restricted"]},"basis":text()})),
            "viewpoints":strings(),"shape":text(),"materials":strings(),"dimensions":text(),"evidence":text()})),0),
            "comparison":strings(),"selected_reference_ids":strings(),"assumptions":strings(),"clarifications":strings()})
        }
        "evidence" => {
            json!({"sources":array(object(json!({"id":nonempty(),"claim":nonempty(),"source_url":text(),"reliability":text(),"limitations":text()})),0),
            "facts":strings(),"assumptions":strings(),"clarifications":strings(),"ready":{"type":"boolean"}})
        }
        "section" => {
            json!({"section_id":nonempty(),"title":nonempty(),"content":nonempty(),"evidence_ids":strings(),"artifact_paths":strings(),"verification":strings(),"open_issues":strings()})
        }
        "integration" => {
            json!({"content":nonempty(),"artifact_paths":strings(),"acceptance_results":checks(),"open_issues":strings()})
        }
        "review" => {
            json!({"approved":{"type":"boolean"},"feedback":text(),"blocking_issues":strings(),"acceptance_results":checks()})
        }
        "revision" => {
            json!({"content":nonempty(),"changes":array(nonempty(),1),"artifact_paths":strings(),"open_issues":strings()})
        }
        _ => unreachable!("the scheduler uses a closed phase set"),
    };
    let summary = object(
        json!({"objective":nonempty(),"assumptions":strings(),"decision":nonempty(),"next_action":nonempty()}),
    );
    let mut final_schema = object(body);
    if let Some(clarifications) = final_schema.pointer_mut("/properties/clarifications") {
        clarifications["description"] = json!(
            "Only unresolved questions requiring a user answer before work can proceed. Otherwise []. Notes, unused tools, limitations and completed checks are not questions."
        );
    }
    object(json!({"summary":summary,
        "tool_calls":array(object(json!({"id":nonempty(),"name":nonempty(),"arguments":{"type":"object"}})),0),
        "result":{"oneOf":[final_schema,{"type":"null"}]}}))
}

/// Describe observable work and decisions, never private reasoning traces.
pub fn prompt(phase: &str) -> &'static str {
    match phase.split(':').next().unwrap_or(phase) {
        "goal_intake" => {
            "ユーザー要求を目的・要件r1等・成果物・制約に分解する。高品質/完全/リアル等をquality_termsへ列挙し、まだ一括定義しない。独立に具体化可能な部分を先に整理し、不可欠な未知だけ少数clarificationsへ。用途、寸法、権利を捏造せず仮定と質問を区別する。3Dならproduction_kind=3d。"
        }
        "quality_terms" => {
            "current_quality_termの1項目だけ、定義・測定手順・合格閾値を明示。他の用語をまとめない。重要な閾値を勝手に確定せず仮定を記録し、不可欠な回答が必要ならresolved=falseと少数質問。"
        }
        "goal_contract" => {
            "全requirements idにsuccess_criteriaを対応させ、c1等の一意で安定したid、測定方法・合格閾値を定義。ユーザー要求を黙って縮小しない。confirmedは整合確認であり新しい承認の捏造ではない。未確定なら質問。"
        }
        "preflight" => {
            "Goal・入力・範囲・依存と実environmentのモデル/承認方針/実行条件を前確認。provider_defaultは独自出力token上限を指定しない意味。未提示設定を推測しない。注記はchecks.evidenceへ、ユーザー回答が不可欠でなければclarifications=[]。"
        }
        "answer_plan" => {
            "Goalを満たす通常1〜5章の制作計画、必要なら増やす。章idはs1等で一意。各章の目的・合格基準、統合と調査・参照計画を定義。ここで完成を装わない。"
        }
        "capability_assessment" => {
            "実tool_inventory/skill_inventory/environmentだけで能力を評価。無いfs/shell/3D/画像/Computer useをあると主張しない。必須能力欠落はblockers。不要toolはblockerでも質問でもない。このphaseは能力評価だけで制作・最終review済みと書かない。"
        }
        "means_selection" => {
            "実inventoryから必要なtool.nameとskill.nameだけ選び、workflowを定義。skillは実際の提供contentを読み適用条件を満たす場合のみ使う。不要なら空配列。必須の不足だけgaps、注記を質問にしない。"
        }
        "work_environment" => {
            "最大限の能力を発揮する実作業場所・編集・検証・再現環境を確認。選定した実toolだけ実行可能。計画をavailable/readyと偽装しない。storage artifactを物理workspace fileと呼ばない。必要環境が欠ければready=falseと説明。"
        }
        "profile_check" => {
            "current_profile_checkの宣言型追加工程を既存成果と照合。コードとして実行しない。根拠のない合格にせず必須不合格はfindingsへ。"
        }
        "reference_plan" => {
            "3Dなら近い完成品参照を優先。無ければ複数優良参考をsource URL/権利/視点/形状/材質/寸法で比較し組合せ計画。編集・Computer use・検証環境が必要なら実機能と権限内で準備。参照と完成品を同視点・同尺度で横並び比較する計画。不足寸法を捏造しない。"
        }
        "reference_search" => {
            "利用できる実検索と提供資料だけで参照調査しURLと観察証拠を記録。未閲覧や許諾を捏造しない。権利不明はunknownで記録し、閲覧/分析だけならunknownを一律停止理由にしない。実利用制限が要件に抵触する場合のみ確認。"
        }
        "evidence" => {
            "実調査・資料・検証から根拠を収集。確認済事実と仮定を分け、URL・信頼性・制約を記録。未実施調査を捏造しない。不可欠な根拠が欠けるときready=false。"
        }
        "section" => {
            "current_sectionの1章だけ制作。completed_sections、根拠、選定手段/skillを使用。必要なら実toolでartifactを制作・検証。保存していないartifact_pathsを記録しない。物理fileとstorage artifactを区別。未検証事項はopen_issuesへ。"
        }
        "integration" => {
            "全章を統合し全Goal success_criteriaの全idへacceptance_resultsを1件ずつ対応。欠落/重複/未知idは不可。必要なartifactを実toolで保存しpathを列挙。未達を合格としない。"
        }
        "review" => {
            "独立review。candidate/実artifact/検証証拠をGoal全success_criteria idと1件ずつ照合、欠落/重複/未知id不可。厳密boolean approvedで合格判断し、証拠不足・未達はfalseとblocking_issues。合格を前提にせず具体的改善feedbackを返す。"
        }
        "revision" => {
            "独立reviewの具体的指摘を修正し、必要なら実artifactも更新・検証。変更点・artifact_paths・未解決open_issuesを正直に記録。承認は次reviewで行う。"
        }
        _ => unreachable!("closed phase"),
    }
}

/// Validate the closed schema subset used above without a host dependency.
pub fn validate(value: &Value, schema: &Value) -> Result<(), String> {
    if let Some(choices) = schema.get("oneOf").and_then(Value::as_array) {
        return if choices
            .iter()
            .filter(|choice| validate(value, choice).is_ok())
            .count()
            == 1
        {
            Ok(())
        } else {
            Err("phase result does not match exactly one contract".into())
        };
    }
    if let Some(choices) = schema.get("enum").and_then(Value::as_array) {
        if !choices.contains(value) {
            return Err("phase enum is invalid".into());
        }
    }
    match schema.get("type").and_then(Value::as_str) {
        Some("object") => {
            let object = value.as_object().ok_or("phase field must be an object")?;
            if let Some(properties) = schema.get("properties").and_then(Value::as_object) {
                for key in schema
                    .get("required")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if !object.contains_key(key.as_str().unwrap()) {
                        return Err(format!("missing field {key}"));
                    }
                }
                for (key, item) in object {
                    if let Some(child) = properties.get(key) {
                        validate(item, child)?;
                    } else if schema.get("additionalProperties") == Some(&Value::Bool(false)) {
                        return Err(format!("unexpected field {key}"));
                    }
                }
            }
        }
        Some("array") => {
            let items = value.as_array().ok_or("phase field must be an array")?;
            if items.len() < schema.get("minItems").and_then(Value::as_u64).unwrap_or(0) as usize {
                return Err("phase array is empty".into());
            }
            if schema
                .get("maxItems")
                .and_then(Value::as_u64)
                .is_some_and(|max| items.len() > max as usize)
            {
                return Err("phase array exceeds its declared limit".into());
            }
            for item in items {
                validate(item, &schema["items"])?;
            }
        }
        Some("string") => {
            let value = value.as_str().ok_or("phase field must be a string")?;
            if schema.get("minLength").and_then(Value::as_u64) == Some(1) && value.trim().is_empty()
            {
                return Err("phase text must not be empty".into());
            }
            if schema
                .get("maxLength")
                .and_then(Value::as_u64)
                .is_some_and(|maximum| value.chars().count() > maximum as usize)
            {
                return Err("tool text exceeds its declared length".into());
            }
        }
        Some("boolean") if !value.is_boolean() => return Err("phase boolean must be strict".into()),
        Some("integer") => {
            if !value.is_i64() && !value.is_u64() {
                return Err("tool integer must be exact".into());
            }
            if schema
                .get("minimum")
                .and_then(Value::as_i64)
                .is_some_and(|min| value.as_i64().is_some_and(|number| number < min))
            {
                return Err("tool integer is below its declared minimum".into());
            }
            if schema
                .get("maximum")
                .and_then(Value::as_u64)
                .is_some_and(|max| value.as_u64().is_some_and(|number| number > max))
            {
                return Err("tool integer exceeds its declared maximum".into());
            }
        }
        Some("null") if !value.is_null() => return Err("phase result must be null".into()),
        _ => (),
    }
    Ok(())
}
