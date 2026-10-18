//! Bounded semantic assessment of a Goal contract, without effect authority.
//!
//! Model assessments remain assessments, not a proof of semantic truth. Original
//! contracts, captured phase inputs and paid receipts are never rewritten here.

use super::{Failure, Phase, Run, checked_ids, digest, invalid, texts};
use crate::phases;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub const POLICY: &str = "deepthink.goal-contract-semantics.v1";
pub const MAX_REVIEWS: usize = 2;
const GLOBAL_CHECKS: [&str; 4] = [
    "objective_fidelity",
    "deliverable_fidelity",
    "constraint_fidelity",
    "joint_satisfiability",
];

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Candidate {
    pub contract: Value,
    pub parent_digest: Option<String>,
    pub source_phase: String,
    pub source_generation_key: Option<String>,
    pub source_attempt_id: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Assessment {
    pub candidate_digest: String,
    pub result: Value,
    pub source_phase: String,
    pub source_generation_key: Option<String>,
    pub source_attempt_id: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Adoption {
    pub expected_revision: String,
    pub expected_snapshot_digest: String,
    pub candidate_digest: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnswerRecord {
    pub source_phase: String,
    pub source_generation_key: Option<String>,
    pub answer_index: usize,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContractGate {
    pub policy: String,
    pub original_goal: Value,
    pub original_contract_digest: String,
    pub candidates: BTreeMap<String, Candidate>,
    pub reviews: Vec<Assessment>,
    pub current_candidate_digest: String,
    pub effective_revision_digest: Option<String>,
    pub state: String,
    pub deferred: VecDeque<Phase>,
    pub adoption: Option<Adoption>,
    pub last_summary: Option<Value>,
    #[serde(default)]
    pub answer_records: Vec<AnswerRecord>,
}

pub fn is_phase(phase: &str) -> bool {
    matches!(
        phase.split(':').next().unwrap_or(phase),
        "goal_contract_review" | "goal_contract_revision"
    )
}

#[cfg(test)]
#[path = "goal_contract_gate_tests.rs"]
pub(crate) mod tests;

fn failure(code: &str, message: &str) -> Failure {
    Failure {
        code: code.into(),
        message: message.into(),
    }
}

fn exhausted() -> Failure {
    failure(
        "goal-contract-gate-exhausted",
        "The bounded Goal contract assessment is exhausted. Its records and costs were retained; ordinary continuation cannot reset it.",
    )
}

fn hash(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn nonempty() -> Value {
    json!({"type":"string","minLength":1,"maxLength":2048})
}
fn strings(maximum: usize) -> Value {
    json!({"type":"array","items":nonempty(),"maxItems":maximum})
}
fn array(item: Value) -> Value {
    json!({"type":"array","items":item,"minItems":1})
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

pub fn schema(phase: &str) -> Value {
    let result = if phase == "goal_contract_revision:1" {
        object(json!({
            "contract":phases::schema("goal_contract")["properties"]["result"].clone(),
            "changes":{"type":"array","items":nonempty(),"minItems":1,"maxItems":16}
        }))
    } else {
        object(json!({
            "candidate_digest":{"type":"string","minLength":1,"maxLength":64},
            "approved":{"type":"boolean"},
            "global_checks":array(object(json!({"id":{"enum":GLOBAL_CHECKS},"passed":{"type":"boolean"},"evidence":nonempty()}))),
            "requirement_checks":array(object(json!({"requirement_id":nonempty(),"criterion_ids":array(nonempty()),"faithful":{"type":"boolean"},"source_pointer":nonempty(),"candidate_pointer":nonempty(),"evidence":nonempty()}))),
            "criterion_checks":array(object(json!({"criterion_id":nonempty(),"satisfiable":{"type":"boolean"},"measurement_aligned":{"type":"boolean"},"target_aligned":{"type":"boolean"},"dependencies_aligned":{"type":"boolean"},"source_pointer":nonempty(),"candidate_pointer":nonempty(),"evidence":nonempty()}))),
            "issues":{"type":"array","maxItems":16,"items":object(json!({"id":nonempty(),"category":{"enum":["scope","contradiction","measurement","dependency","unsupported_assumption","missing_information","other"]},"requirement_ids":array(nonempty()),"criterion_ids":array(nonempty()),"source_pointer":nonempty(),"candidate_pointer":nonempty(),"description":nonempty(),"suggested_change":nonempty()}))},
            "clarifications":strings(3)
        }))
    };
    object(json!({
        "summary":object(json!({"objective":nonempty(),"assumptions":strings(16),"decision":nonempty(),"next_action":nonempty()})),
        "tool_calls":phases::schema("goal_contract")["properties"]["tool_calls"].clone(),
        "result":result
    }))
}

pub fn prompt(phase: &str) -> &'static str {
    if phase == "goal_contract_revision:1" {
        "原ユーザー要求・受理済み回答・original_goalを保持し、直前の独立意味レビューの指摘だけを根拠に新しいGoal契約候補を作る。元contractや過去phaseを書き換えない。requirement/criterion/deliverableのIDと対応を保持し、要件を省略・強化・追加しない。criterion/measurement/targetで対象、中間値と最終値、単位、依存順序を混同しない。契約候補はcontract、変更点はchangesへ。実作業・tool実行・最終回答はしない。真に不可欠な利用者質問だけcontract.clarificationsへ。"
    } else {
        "独立したGoal契約の意味レビューだけを行う。contract_candidateを原ユーザー要求・受理済み回答・original_goalと照合する。全requirementと全criterionを各1件評価し、原要件への忠実性、暗黙の強化や省略の有無、条件全体の充足可能性、測定対象とmeasurement/targetの整合、中間結果と最終結果・単位・対象期間の区別、依存/実行順序を確認する。単にJSONが正しい、confirmed=true、先の作成者が合格と述べたことは合格理由にならない。全global_checksとIDチェックを埋め、source_pointerはoriginal_goal自体をrootとする該当requirementの既存child field、candidate_pointerはcontract_candidate自体をrootとする該当criterionの既存child fieldへのJSON pointerにする。例えばsource_pointer=/requirements/0/description、candidate_pointer=/success_criteria/0/measurement又は/success_criteria/0/criterion又は/success_criteria/0/targetを使い、実際のindexは各IDの対応に合わせる。/requirements/0や/success_criteria/0のような項目全体を指すwhole-object pointerは禁止。contextやoriginal_goalというwrapper名をpointerへ足さず、存在しないfield/index/IDを例から捏造しない。joint_satisfiabilityのevidenceには原要件と全candidate条件を同時に満たせるかの短い公開検証結果だけを書く。比較対象・中間値と最終値の別・単位・等式や不等式の対応を明示し、異なる対象の値に同じ閾値を黙って課さない。矛盾や未検証の仮定が残るのに、合格という主張を繰り返してtrueにしない。この整合確認を実taskやtoolの実行証拠と呼ばない。evidence/issueは短い検証結果と根拠だけで、私的推論過程を出さない。矛盾、誤った比較、過大な合格条件、必要な情報不足があればapproved=falseと具体的issues。真に不可欠な利用者質問だけclarificationsへ、注記・未使用tool・不確実な自己評価を質問にしない。実際のtool使用・制作・最終回答済みと主張しない。全checkがtrueでissues/clarificationsが空の時だけapproved=true。意味的正しさを数学的に保証できるとの主張はしない。"
    }
}

fn ids(values: &Value) -> Result<BTreeSet<String>, Failure> {
    let array = values
        .as_array()
        .ok_or_else(|| invalid("Goal review requires an identity array"))?;
    let mut found = BTreeSet::new();
    for value in array {
        let id = value
            .as_str()
            .filter(|id| !id.trim().is_empty())
            .ok_or_else(|| invalid("Goal review has an invalid identity"))?;
        if !found.insert(id.to_owned()) {
            return Err(invalid("Goal review has duplicate identities"));
        }
    }
    Ok(found)
}

fn criterion_requirements(contract: &Value) -> Result<BTreeMap<String, String>, Failure> {
    checked_ids(contract, "success_criteria")?;
    contract["success_criteria"]
        .as_array()
        .unwrap()
        .iter()
        .map(|criterion| {
            Ok((
                criterion["id"].as_str().unwrap().to_owned(),
                criterion["requirement_id"]
                    .as_str()
                    .ok_or_else(|| invalid("criterion has no original requirement"))?
                    .to_owned(),
            ))
        })
        .collect()
}

fn validate_candidate(
    original: &Value,
    candidate: &Value,
    previous: Option<&Value>,
) -> Result<(), Failure> {
    phases::validate(
        candidate,
        &phases::schema("goal_contract")["properties"]["result"],
    )
    .map_err(|_| invalid("Goal revision violates the closed contract"))?;
    let requirements = checked_ids(original, "requirements")?;
    let mapping = criterion_requirements(candidate)?;
    if mapping.values().cloned().collect::<BTreeSet<_>>() != requirements
        || checked_ids(candidate, "deliverables")? != checked_ids(original, "deliverables")?
        || previous.is_some_and(|previous| {
            criterion_requirements(previous).ok().as_ref() != Some(&mapping)
        })
    {
        return Err(invalid(
            "Goal revision changed original requirement, criterion or deliverable identities",
        ));
    }
    Ok(())
}

fn check_pointer(source: &Value, pointer: &Value, prefix: &str) -> Result<(), Failure> {
    let pointer = pointer
        .as_str()
        .ok_or_else(|| invalid("Goal review evidence has no pointer"))?;
    if !pointer.starts_with(prefix) || source.pointer(pointer).is_none() {
        return Err(invalid(&format!(
            "Goal review evidence must reference an existing field under {prefix}; requirement/criterion item roots are not child-field pointers"
        )));
    }
    Ok(())
}

fn index_of(source: &Value, key: &str, id: &str) -> Result<usize, Failure> {
    source[key]
        .as_array()
        .and_then(|items| items.iter().position(|item| item["id"] == id))
        .ok_or_else(|| invalid("Goal review references an unknown original identity"))
}

fn validate_review(
    original: &Value,
    candidate: &Value,
    candidate_digest: &str,
    review: &Value,
) -> Result<(), Failure> {
    if review["candidate_digest"] != candidate_digest {
        return Err(invalid("Goal review candidate digest changed"));
    }
    let expected_requirements = checked_ids(original, "requirements")?;
    let criteria = criterion_requirements(candidate)?;
    let expected_criteria = criteria.keys().cloned().collect::<BTreeSet<_>>();
    let mut globals = BTreeSet::new();
    let mut all_passed = true;
    for check in review["global_checks"].as_array().unwrap() {
        let id = check["id"].as_str().unwrap();
        if !globals.insert(id.to_owned()) {
            return Err(invalid("Goal review repeated a global check"));
        }
        all_passed &= check["passed"] == true;
    }
    if globals
        != GLOBAL_CHECKS
            .iter()
            .map(|item| (*item).to_owned())
            .collect()
    {
        return Err(invalid("Goal review omitted a global check"));
    }
    let mut actual_requirements = BTreeSet::new();
    for check in review["requirement_checks"].as_array().unwrap() {
        let id = check["requirement_id"].as_str().unwrap();
        if !actual_requirements.insert(id.to_owned()) {
            return Err(invalid("Goal review repeated a requirement"));
        }
        let index = index_of(original, "requirements", id)?;
        check_pointer(
            original,
            &check["source_pointer"],
            &format!("/requirements/{index}/"),
        )?;
        let expected = criteria
            .iter()
            .filter(|(_, requirement)| requirement.as_str() == id)
            .map(|(id, _)| id.clone())
            .collect::<BTreeSet<_>>();
        if ids(&check["criterion_ids"])? != expected {
            return Err(invalid("Goal review changed criterion coverage"));
        }
        let mut valid_pointer = false;
        for criterion in &expected {
            let index = index_of(candidate, "success_criteria", criterion)?;
            valid_pointer |= check_pointer(
                candidate,
                &check["candidate_pointer"],
                &format!("/success_criteria/{index}/"),
            )
            .is_ok();
        }
        if !valid_pointer {
            return Err(invalid(
                "Goal requirement evidence references another criterion",
            ));
        }
        all_passed &= check["faithful"] == true;
    }
    let mut actual_criteria = BTreeSet::new();
    for check in review["criterion_checks"].as_array().unwrap() {
        let id = check["criterion_id"].as_str().unwrap();
        if !actual_criteria.insert(id.to_owned()) {
            return Err(invalid("Goal review repeated a criterion"));
        }
        let requirement = criteria
            .get(id)
            .ok_or_else(|| invalid("Goal review invented a criterion"))?;
        let source = index_of(original, "requirements", requirement)?;
        let target = index_of(candidate, "success_criteria", id)?;
        check_pointer(
            original,
            &check["source_pointer"],
            &format!("/requirements/{source}/"),
        )?;
        check_pointer(
            candidate,
            &check["candidate_pointer"],
            &format!("/success_criteria/{target}/"),
        )?;
        for key in [
            "satisfiable",
            "measurement_aligned",
            "target_aligned",
            "dependencies_aligned",
        ] {
            all_passed &= check[key] == true;
        }
    }
    if actual_requirements != expected_requirements || actual_criteria != expected_criteria {
        return Err(invalid(
            "Goal review omitted or invented original identities",
        ));
    }
    let mut issues = BTreeSet::new();
    for issue in review["issues"].as_array().unwrap() {
        if !issues.insert(issue["id"].as_str().unwrap())
            || !ids(&issue["requirement_ids"])?.is_subset(&expected_requirements)
            || !ids(&issue["criterion_ids"])?.is_subset(&expected_criteria)
        {
            return Err(invalid("Goal review has invalid issue identities"));
        }
        check_pointer(original, &issue["source_pointer"], "/")?;
        check_pointer(candidate, &issue["candidate_pointer"], "/")?;
    }
    let approved = review["approved"] == true;
    let clean = all_passed
        && review["issues"].as_array().unwrap().is_empty()
        && texts(review, "clarifications").is_empty();
    if approved != clean
        || (!approved
            && review["issues"].as_array().unwrap().is_empty()
            && texts(review, "clarifications").is_empty())
    {
        return Err(invalid(
            "Goal review approval disagrees with its checks and issues",
        ));
    }
    Ok(())
}

impl ContractGate {
    fn candidate(&self) -> Result<&Value, Failure> {
        self.candidates
            .get(&self.current_candidate_digest)
            .map(|candidate| &candidate.contract)
            .ok_or_else(|| invalid("Goal assessment has no captured candidate"))
    }

    fn validate(&self, run: &Run) -> Result<(), Failure> {
        if self.policy != POLICY
            || self.reviews.len() > MAX_REVIEWS
            || self.candidates.len() > 2
            || self.original_contract_digest != digest(&run.goal["contract"])
            || self.original_goal["requirements"] != run.goal["requirements"]
            || self.original_goal["deliverables"] != run.goal["deliverables"]
            || self.original_goal["objective"] != run.goal["objective"]
            || self.original_goal["contract"] != run.goal["contract"]
            || self.original_goal["constraints"] != run.goal["constraints"]
            || self.answer_records.len() > 2 + run.settings.max_contract_retries as usize
            || self
                .answer_records
                .iter()
                .any(|record| record.answer_index >= run.answers.len())
            || self
                .candidates
                .get(&self.original_contract_digest)
                .is_none_or(|candidate| candidate.contract != run.goal["contract"])
            || !matches!(
                self.state.as_str(),
                "reviewing" | "revising" | "needs_input" | "accepted" | "rejected"
            )
        {
            return Err(invalid(
                "Goal assessment binding or bounded history changed",
            ));
        }
        for (key, candidate) in &self.candidates {
            if !hash(key) || *key != digest(&candidate.contract) {
                return Err(invalid("Goal candidate digest is invalid"));
            }
            validate_candidate(&self.original_goal, &candidate.contract, None)?;
        }
        for (index, record) in self.reviews.iter().enumerate() {
            let candidate = self
                .candidates
                .get(&record.candidate_digest)
                .ok_or_else(|| invalid("review lost its original candidate"))?;
            if record.source_phase != format!("goal_contract_review:{}", index + 1) {
                return Err(invalid("Goal assessment rounds changed"));
            }
            phases::validate(
                &record.result,
                &schema(&record.source_phase)["properties"]["result"],
            )
            .map_err(|_| invalid("Retained Goal review violates its closed schema"))?;
            validate_review(
                &self.original_goal,
                &candidate.contract,
                &record.candidate_digest,
                &record.result,
            )?;
        }
        let accepted = self.state == "accepted";
        if accepted != self.effective_revision_digest.is_some()
            || self.effective_revision_digest.as_ref().is_some_and(|key| {
                key != &self.current_candidate_digest
                    || self.reviews.last().is_none_or(|review| {
                        review.candidate_digest != *key || review.result["approved"] != true
                    })
            })
        {
            return Err(invalid(
                "Goal effective revision has no matching accepted assessment",
            ));
        }
        let round_state_valid = match self.state.as_str() {
            "reviewing" => {
                (self.reviews.is_empty()
                    && self.candidates.len() == 1
                    && self.current_candidate_digest == self.original_contract_digest)
                    || (self.reviews.len() == 1
                        && self.candidates.len() == 2
                        && self.current_candidate_digest != self.original_contract_digest)
            }
            "revising" | "needs_input" => {
                self.reviews.len() == 1
                    && self.candidates.len() == 1
                    && self.reviews[0].result["approved"] == false
            }
            "accepted" => !self.reviews.is_empty() && self.deferred.is_empty(),
            "rejected" => {
                self.reviews.len() == MAX_REVIEWS
                    && self
                        .reviews
                        .last()
                        .is_some_and(|review| review.result["approved"] == false)
            }
            _ => false,
        };
        if !round_state_valid
            || (!accepted
                && (self.deferred.len() != 1
                    || self
                        .deferred
                        .front()
                        .is_none_or(|phase| phase.id != "preflight")))
        {
            return Err(invalid(
                "Goal assessment schedule or bounded rounds changed",
            ));
        }
        for (key, candidate) in &self.candidates {
            if (key == &self.original_contract_digest) != candidate.parent_digest.is_none()
                || candidate
                    .parent_digest
                    .as_ref()
                    .is_some_and(|parent| parent != &self.original_contract_digest)
                || candidate.contract["confirmed"] != true
            {
                return Err(invalid(
                    "Goal candidate ancestry or draft confirmation changed",
                ));
            }
        }
        self.candidate()?;
        Ok(())
    }
}

impl Run {
    fn goal_candidate_source(&self, phase: &str) -> (Option<String>, Option<String>) {
        let key = self
            .last_generation
            .as_ref()
            .filter(|point| point.base_key.starts_with(&format!("{phase}@")))
            .map(|point| point.key.clone());
        let attempt = key.as_ref().and_then(|key| {
            self.attempts
                .iter()
                .find(|(_, attempt)| {
                    attempt.pointer("/context/generation_key") == Some(&json!(key))
                })
                .map(|(id, _)| id.clone())
        });
        (key, attempt)
    }

    pub(super) fn initialize_contract_gate(
        &mut self,
        deferred: VecDeque<Phase>,
        adoption: Option<Adoption>,
    ) -> Result<(), Failure> {
        if self.contract_gate.is_some() {
            return Err(invalid("An existing Goal assessment cannot be reset"));
        }
        let contract = self.goal["contract"].clone();
        validate_candidate(&self.goal, &contract, None)?;
        if contract["confirmed"] != true || !texts(&contract, "clarifications").is_empty() {
            return Err(invalid(
                "Only the captured original confirmed draft can enter independent assessment",
            ));
        }
        let candidate_digest = digest(&contract);
        let (key, attempt) = self.goal_candidate_source("goal_contract");
        let candidate = Candidate {
            contract,
            parent_digest: None,
            source_phase: "goal_contract".into(),
            source_generation_key: key,
            source_attempt_id: attempt,
        };
        self.contract_gate = Some(ContractGate {
            policy: POLICY.into(),
            original_goal: self.goal.clone(),
            original_contract_digest: candidate_digest.clone(),
            candidates: BTreeMap::from([(candidate_digest.clone(), candidate)]),
            reviews: vec![],
            current_candidate_digest: candidate_digest,
            effective_revision_digest: None,
            state: "reviewing".into(),
            deferred,
            adoption,
            last_summary: None,
            answer_records: vec![],
        });
        self.push("goal_contract_review:1", json!({"semantic_policy":POLICY}));
        Ok(())
    }

    /// Prepare only an explicitly selected, settled preflight boundary. The
    /// runtime must atomically CAS this snapshot before returning its stream.
    pub fn prepare_goal_contract_review(
        &mut self,
        candidate_digest: &str,
        expected_revision: &str,
        expected_snapshot_digest: &str,
    ) -> Result<(), Failure> {
        self.pause_receipt()?;
        if self.contract_gate.is_some()
            || !hash(candidate_digest)
            || candidate_digest != digest(&self.goal["contract"])
            || expected_revision
                .parse::<u64>()
                .ok()
                .is_none_or(|value| value.to_string() != expected_revision)
            || !hash(expected_snapshot_digest)
            || self.pending.len() != 1
            || self
                .pending
                .front()
                .is_none_or(|phase| phase.id != "preflight")
            || self.phases.last().is_none_or(|phase| {
                phase["phase"] != "goal_contract" || phase["result"] != self.goal["contract"]
            })
            || self.phases.iter().any(|phase| {
                !matches!(
                    phase["phase"]
                        .as_str()
                        .unwrap_or("")
                        .split(':')
                        .next()
                        .unwrap_or(""),
                    "goal_intake" | "quality_terms" | "goal_contract"
                )
            })
            || !self.tool_receipts.is_empty()
            || !self.sections.is_empty()
            || !self.candidate.is_empty()
            || !self.artifacts.is_empty()
        {
            return Err(failure(
                "conflict",
                "Goal review adoption requires the exact settled original contract before any work phase; it cannot reset prior assessments or effects.",
            ));
        }
        let deferred = self.pending.clone();
        // initialize may validate the candidate; it has no partial mutation on
        // failure and only the following successful preparation changes queues.
        self.initialize_contract_gate(
            deferred,
            Some(Adoption {
                expected_revision: expected_revision.into(),
                expected_snapshot_digest: expected_snapshot_digest.into(),
                candidate_digest: candidate_digest.into(),
            }),
        )?;
        self.pending.pop_front();
        self.last_generation = None;
        self.status = "running".into();
        Ok(())
    }

    /// Compact metadata only. Full review results remain in immutable phase and
    /// public generation records, rather than being duplicated into STATUS.
    pub fn contract_gate_status(&self) -> Value {
        let Some(gate) = &self.contract_gate else {
            return Value::Null;
        };
        if gate.validate(self).is_err() {
            return json!({"policy":POLICY,"state":"invalid","effective_revision_digest":null});
        }
        json!({"policy":gate.policy,"state":gate.state,"original_goal_digest":digest(&gate.original_goal),"original_contract_digest":gate.original_contract_digest,
            "current_candidate_digest":gate.current_candidate_digest,"effective_revision_digest":gate.effective_revision_digest,
            "review_count":gate.reviews.len(),"maximum_reviews":MAX_REVIEWS,"maximum_revisions":1,"clarification_answer_count":gate.answer_records.len(),"adoption":gate.adoption,
            "candidates":gate.candidates.iter().map(|(key,value)| json!({"digest":key,"parent_digest":value.parent_digest,"source_phase":value.source_phase,"source_generation_key":value.source_generation_key,"source_attempt_id":value.source_attempt_id})).collect::<Vec<_>>(),
            "reviews":gate.reviews.iter().map(|record|json!({"candidate_digest":record.candidate_digest,"review_digest":digest(&record.result),"approved":record.result["approved"],"issue_count":record.result["issues"].as_array().map(Vec::len),"clarification_count":record.result["clarifications"].as_array().map(Vec::len),"source_phase":record.source_phase,"source_generation_key":record.source_generation_key,"source_attempt_id":record.source_attempt_id})).collect::<Vec<_>>()})
    }

    pub fn effective_goal_contract(&self) -> Option<&Value> {
        let gate = self.contract_gate.as_ref()?;
        if gate.validate(self).is_err() || gate.state != "accepted" {
            return None;
        }
        gate.candidates
            .get(gate.effective_revision_digest.as_ref()?)
            .map(|candidate| &candidate.contract)
    }

    pub(super) fn goal_gate_context(&self) -> Result<Value, Failure> {
        let gate = self
            .contract_gate
            .as_ref()
            .ok_or_else(|| invalid("Goal review has no prepared gate"))?;
        gate.validate(self)?;
        Ok(
            json!({"goal":gate.original_goal,"original_goal":gate.original_goal,"original_goal_digest":digest(&gate.original_goal),"model":self.model,
            "user_answers":self.answers,"semantic_policy":POLICY,"contract_candidate":gate.candidate()?,
            "contract_candidate_digest":gate.current_candidate_digest,"previous_review":gate.reviews.last().map(|review|&review.result),
            "review_count":gate.reviews.len(),"maximum_reviews":MAX_REVIEWS,"maximum_revisions":1}),
        )
    }

    pub(super) fn require_goal_gate_for_phase(&self, phase: &str) -> Result<(), Failure> {
        if matches!(
            phase.split(':').next().unwrap_or(phase),
            "goal_intake" | "quality_terms" | "goal_contract"
        ) {
            return Ok(());
        }
        let Some(gate) = &self.contract_gate else {
            return Err(failure(
                "goal-contract-review-required",
                "The original Goal contract has no semantic assessment. Use the explicitly selected Goal review input before any work; it was not silently upgraded.",
            ));
        };
        gate.validate(self)?;
        if matches!(gate.state.as_str(), "rejected") {
            return Err(exhausted());
        }
        if is_phase(phase) {
            let expected = match gate.state.as_str() {
                "reviewing" => format!("goal_contract_review:{}", gate.reviews.len() + 1),
                "revising" => "goal_contract_revision:1".into(),
                _ => {
                    return Err(invalid(
                        "Goal assessment phase cannot execute in this state",
                    ));
                }
            };
            if expected != phase {
                return Err(invalid("Goal assessment phase order changed"));
            }
        } else if self.effective_goal_contract().is_none() {
            return Err(failure(
                "goal-contract-review-required",
                "No accepted effective Goal contract exists; work was not started.",
            ));
        }
        Ok(())
    }

    pub(super) fn ensure_goal_generation_budget(&self, phase: &str) -> Result<(), Failure> {
        if is_phase(phase)
            && self
                .attempts
                .values()
                .filter(|attempt| attempt["phase"] == phase)
                .count()
                >= 1 + self.settings.max_contract_retries as usize
        {
            return Err(exhausted());
        }
        Ok(())
    }

    pub(super) fn validate_goal_gate_result(
        &self,
        phase: &str,
        result: &Value,
    ) -> Result<(), Failure> {
        if !is_phase(phase) {
            return Ok(());
        }
        self.require_goal_gate_for_phase(phase)?;
        phases::validate(result, &schema(phase)["properties"]["result"])
            .map_err(|_| invalid("Goal assessment result violates its closed schema"))?;
        let gate = self.contract_gate.as_ref().unwrap();
        if phase == "goal_contract_revision:1" {
            validate_candidate(
                &gate.original_goal,
                &result["contract"],
                Some(&self.goal["contract"]),
            )?;
            if result["contract"]["confirmed"] != true
                && texts(&result["contract"], "clarifications").is_empty()
            {
                return Err(invalid(
                    "An unconfirmed Goal correction must identify its actual unresolved questions",
                ));
            }
        } else {
            validate_review(
                &gate.original_goal,
                gate.candidate()?,
                &gate.current_candidate_digest,
                result,
            )?;
        }
        Ok(())
    }

    pub(super) fn apply_goal_gate_result(
        &mut self,
        phase: &str,
        result: &Value,
    ) -> Result<(), Failure> {
        self.validate_goal_gate_result(phase, result)?;
        let (key, attempt) = self.goal_candidate_source(phase);
        if phase == "goal_contract_revision:1"
            && !texts(&result["contract"], "clarifications").is_empty()
        {
            // The unfinished proposal remains in its paid public generation
            // and phase result. A genuine answer may retry this same bounded
            // revision phase at a new input epoch; it cannot erase a review.
            self.needs_input(texts(&result["contract"], "clarifications"));
            return Ok(());
        }
        let gate = self.contract_gate.as_mut().unwrap();
        if phase == "goal_contract_revision:1" {
            let contract = result["contract"].clone();
            let revised = digest(&contract);
            if gate.candidates.contains_key(&revised) {
                return Err(invalid("Goal correction produced an unchanged candidate"));
            }
            gate.candidates.insert(
                revised.clone(),
                Candidate {
                    contract,
                    parent_digest: Some(gate.current_candidate_digest.clone()),
                    source_phase: phase.into(),
                    source_generation_key: key,
                    source_attempt_id: attempt,
                },
            );
            gate.current_candidate_digest = revised;
            gate.state = "reviewing".into();
            self.push("goal_contract_review:2", json!({"semantic_policy":POLICY}));
        } else {
            gate.reviews.push(Assessment {
                candidate_digest: gate.current_candidate_digest.clone(),
                result: result.clone(),
                source_phase: phase.into(),
                source_generation_key: key,
                source_attempt_id: attempt,
            });
            if result["approved"] == true {
                gate.state = "accepted".into();
                gate.effective_revision_digest = Some(gate.current_candidate_digest.clone());
                self.pending.extend(gate.deferred.drain(..));
            } else if gate.reviews.len() >= MAX_REVIEWS {
                gate.state = "rejected".into();
                return Err(failure(
                    "goal-contract-review-rejected",
                    "The Goal contract failed the bounded independent semantic assessments. Original and corrected candidates, model receipts and costs were retained; no work or final answer was approved.",
                ));
            } else {
                gate.state = if texts(result, "clarifications").is_empty() {
                    "revising"
                } else {
                    "needs_input"
                }
                .into();
                self.push(
                    "goal_contract_revision:1",
                    json!({"semantic_policy":POLICY}),
                );
                if !texts(result, "clarifications").is_empty() {
                    self.needs_input(texts(result, "clarifications"));
                }
            }
        }
        Ok(())
    }

    pub(super) fn answer_goal_gate(&mut self, answer: &str) -> Result<bool, Failure> {
        let Some(gate) = &self.contract_gate else {
            return Ok(false);
        };
        let review_question = gate.state == "needs_input";
        let revision_question = gate.state == "revising"
            && self.status == "needs_input"
            && self
                .waiting
                .as_ref()
                .and_then(|point| point.pending.front())
                .is_some_and(|phase| phase.id == "goal_contract_revision:1");
        if !review_question && !revision_question {
            return Ok(false);
        }
        if self.status != "needs_input"
            || answer.trim().is_empty()
            || self.waiting.is_none()
            || texts(&self.goal, "clarifications").is_empty()
            || (review_question
                && self
                    .pending
                    .front()
                    .is_none_or(|phase| phase.id != "goal_contract_revision:1"))
            || (revision_question && !self.pending.is_empty())
        {
            return Err(invalid(
                "A Goal correction answer requires its real pending question",
            ));
        }
        self.require_goal_gate_records()?;
        self.ensure_goal_generation_budget("goal_contract_revision:1")?;
        let point = self.waiting.as_ref().unwrap();
        let phase = point
            .pending
            .front()
            .ok_or_else(|| invalid("Goal question lost its phase"))?
            .id
            .clone();
        let next_epoch = if revision_question {
            Some(
                self.phase_epochs
                    .get(&phase)
                    .copied()
                    .unwrap_or(0)
                    .checked_add(1)
                    .ok_or_else(exhausted)?,
            )
        } else {
            None
        };
        let pending = point.pending.clone();
        let prior_status = point.goal["status"].clone();
        let source_key = self.last_generation.as_ref().map(|point| point.key.clone());
        let answer_index = self.answers.len();
        self.answers.push(answer.to_owned());
        self.messages.push(super::clarification_message(answer));
        self.goal["clarifications"] = json!([]);
        self.goal["status"] = prior_status;
        if let Some(epoch) = next_epoch {
            self.pending = pending;
            self.phase_epochs.insert(phase.clone(), epoch);
        }
        let gate = self.contract_gate.as_mut().unwrap();
        gate.answer_records.push(AnswerRecord {
            source_phase: phase,
            source_generation_key: source_key,
            answer_index,
        });
        gate.state = "revising".into();
        self.waiting = None;
        self.entry = None;
        self.error = None;
        self.status = "running".into();
        Ok(true)
    }

    fn require_goal_gate_records(&self) -> Result<(), Failure> {
        self.contract_gate
            .as_ref()
            .ok_or_else(|| invalid("Goal assessment disappeared"))?
            .validate(self)
    }
}
