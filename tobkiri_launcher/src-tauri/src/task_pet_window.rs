//! Presentation-only companion window bound to the admitted Shell Profile.
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, WebviewWindow};

/// Fixed label for the Shell's presentation-only companion window.
pub(crate) const LABEL: &str = "task-pet";
const MAX_SAFE_REVISION: u64 = 9_007_199_254_740_991;

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct Presentation {
    profile_id: String,
    view: View,
    enabled: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct View {
    key: Option<String>,
    revision: u64,
    mood: Mood,
    label: String,
    title: String,
    detail: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
enum Mood {
    Idle,
    Thinking,
    Waiting,
    Completed,
    Error,
    Cancelled,
}

#[derive(Default)]
pub(crate) struct PetState {
    binding: Option<(String, tauri::Url)>,
    presentation: Option<Presentation>,
    user_hidden: bool,
}
pub(crate) type SharedPetState = Arc<Mutex<PetState>>;

fn pet_url(port: u16, profile: &str) -> Result<tauri::Url, String> {
    let encoded = crate::health_check::encode_profile_path_segment(profile)
        .map_err(|error| error.to_string())?;
    tauri::Url::parse(&format!(
        "http://127.0.0.1:{port}/p/{encoded}/chat?surface=task-pet"
    ))
    .map_err(|error| error.to_string())
}

/// Bind the companion once to the same Profile as the admitted Shell.
pub(crate) fn bind(state: &SharedPetState, port: u16, profile: &str) -> Result<(), String> {
    let url = pet_url(port, profile)?;
    let mut state = state.lock().map_err(|error| error.to_string())?;
    match state.binding.as_ref() {
        Some(current) if current != &(profile.to_owned(), url.clone()) => {
            Err("task pet binding cannot change in a live Shell".into())
        }
        Some(_) => Ok(()),
        None => {
            state.binding = Some((profile.into(), url));
            Ok(())
        }
    }
}

/// Keep the companion on its exact passive surface without authentication URLs.
pub(crate) fn navigation_allowed(state: &SharedPetState, url: &tauri::Url) -> bool {
    state
        .lock()
        .ok()
        .and_then(|state| state.binding.clone())
        .is_some_and(|(_, admitted)| admitted == *url)
}

fn caller_allowed(
    label: &str,
    url: &tauri::Url,
    binding: &(String, tauri::Url),
    child: bool,
) -> bool {
    if child {
        return label == LABEL && url == &binding.1;
    }
    label == "main"
        && url.scheme() == binding.1.scheme()
        && url.host_str() == binding.1.host_str()
        && url.port() == binding.1.port()
        && url.username().is_empty()
        && url.password().is_none()
        && url.path() == binding.1.path()
        && !url.query_pairs().any(|(key, _)| key == "surface")
}

fn validate(presentation: &Presentation, profile: &str) -> Result<(), String> {
    let view = &presentation.view;
    let identifier = |value: &str| {
        !value.is_empty()
            && value.len() <= 256
            && value
                .bytes()
                .enumerate()
                .all(|(i, c)| c.is_ascii_alphanumeric() || (i > 0 && b"._:-".contains(&c)))
    };
    if presentation.profile_id != profile
        || !identifier(profile)
        || view.revision > MAX_SAFE_REVISION
        || view.label.encode_utf16().count() > 80
        || view.title.encode_utf16().count() > 160
        || view.detail.encode_utf16().count() > 180
        || [&view.label, &view.title, &view.detail]
            .iter()
            .any(|text| text.chars().any(|c| c.is_control()))
    {
        return Err("invalid task pet presentation".into());
    }
    if let Some(key) = &view.key {
        if key.len() > 1024 {
            return Err("invalid task pet scope".into());
        }
        let ids: Vec<String> = serde_json::from_str(key).map_err(|_| "invalid task pet scope")?;
        if ids.len() != 3 || ids[0] != profile || !ids.iter().all(|id| identifier(id)) {
            return Err("invalid task pet scope".into());
        }
    }
    Ok(())
}

fn checked_binding(
    window: &WebviewWindow,
    state: &PetState,
    child: bool,
) -> Result<(String, tauri::Url), String> {
    let binding = state
        .binding
        .as_ref()
        .ok_or("Shell Profile is not admitted")?;
    let url = window.url().map_err(|error| error.to_string())?;
    if !caller_allowed(window.label(), &url, binding, child) {
        return Err("task pet caller is not admitted".into());
    }
    Ok(binding.clone())
}

fn fence_view(current: Option<&Presentation>, incoming: &mut Presentation) {
    if let Some(current) = current {
        let same_key = current.view.key.is_some() && current.view.key == incoming.view.key;
        let terminal = matches!(
            current.view.mood,
            Mood::Completed | Mood::Error | Mood::Cancelled
        );
        let incoming_terminal = matches!(
            incoming.view.mood,
            Mood::Completed | Mood::Error | Mood::Cancelled
        );
        if same_key
            && (incoming.view.revision < current.view.revision
                || (incoming.view.revision == current.view.revision
                    && terminal
                    && !incoming_terminal))
        {
            incoming.view = current.view.clone();
        }
    }
}

fn native_hide_profile(label: &str, state: &PetState) -> Result<String, String> {
    if label != LABEL {
        return Err("native task pet close label is invalid".into());
    }
    state
        .binding
        .as_ref()
        .map(|(profile, _)| profile.clone())
        .ok_or("Shell Profile is not admitted".into())
}

fn should_show(state: &mut PetState, presentation: &Presentation, open: bool) -> bool {
    if open && presentation.enabled {
        state.user_hidden = false;
    }
    presentation.enabled && open && !state.user_hidden
}

/// Publish bounded display state; only an explicit opening request shows a window.
#[tauri::command]
pub(crate) async fn sync_task_pet(
    app: AppHandle,
    window: WebviewWindow,
    state: tauri::State<'_, SharedPetState>,
    presentation: Presentation,
    open: bool,
) -> Result<(), String> {
    let state = state.inner().clone();
    let app_ui = app.clone();
    crate::run_ui_work_on_main_thread(&app, "sync task pet", move || {
        let mut state = state.lock().map_err(|error| error.to_string())?;
        let (profile, url) = checked_binding(&window, &state, false)?;
        validate(&presentation, &profile)?;
        let mut presentation = presentation;
        fence_view(state.presentation.as_ref(), &mut presentation);
        let show = should_show(&mut state, &presentation, open);
        if state.user_hidden {
            presentation.enabled = false;
        }
        state.presentation = Some(presentation.clone());
        drop(state);
        if show {
            if let Some(pet) = app_ui.get_webview_window(LABEL) {
                pet.show().map_err(|error| error.to_string())?;
            } else {
                let mut builder = tauri::WebviewWindowBuilder::new(
                    &app_ui,
                    LABEL,
                    tauri::WebviewUrl::External(url),
                )
                .title("Tobkiri ペット")
                .inner_size(336.0, 350.0)
                .decorations(false)
                .transparent(true)
                .background_color(tauri::utils::config::Color(0, 0, 0, 0))
                .resizable(false)
                .always_on_top(true)
                .focused(false)
                .visible(false)
                .skip_taskbar(true);
                if let Some(monitor) = window
                    .current_monitor()
                    .map_err(|error| error.to_string())?
                {
                    let scale = monitor.scale_factor();
                    let area = monitor.work_area();
                    let size = &area.size;
                    let position = &area.position;
                    builder = builder.position(
                        f64::from(position.x) / scale
                            + (f64::from(size.width) / scale - 352.0).max(0.0),
                        f64::from(position.y) / scale
                            + (f64::from(size.height) / scale - 390.0).max(0.0),
                    );
                }
                let pet = builder.build().map_err(|error| error.to_string())?;
                pet.show().map_err(|error| error.to_string())?;
            }
        } else if !presentation.enabled {
            if let Some(pet) = app_ui.get_webview_window(LABEL) {
                pet.hide().map_err(|error| error.to_string())?;
            }
        }
        app_ui
            .emit_to(LABEL, "task-pet-state", &presentation)
            .map_err(|error| error.to_string())
    })
}

/// Read the initial display from the admitted companion surface.
#[tauri::command]
pub(crate) async fn task_pet_context(
    app: AppHandle,
    window: WebviewWindow,
    state: tauri::State<'_, SharedPetState>,
) -> Result<Presentation, String> {
    let state = state.inner().clone();
    crate::run_ui_work_on_main_thread(&app, "read task pet", move || {
        let state = state.lock().map_err(|error| error.to_string())?;
        checked_binding(&window, &state, true)?;
        state
            .presentation
            .clone()
            .ok_or("task pet presentation is unavailable".into())
    })
}

/// Delegate a primary-button drag to the companion's native window.
#[tauri::command]
pub(crate) async fn drag_task_pet(
    app: AppHandle,
    window: WebviewWindow,
    state: tauri::State<'_, SharedPetState>,
) -> Result<(), String> {
    let state = state.inner().clone();
    crate::run_ui_work_on_main_thread(&app, "drag task pet", move || {
        {
            let locked = state.lock().map_err(|error| error.to_string())?;
            checked_binding(&window, &locked, true)?;
        }
        window.start_dragging().map_err(|error| error.to_string())
    })
}

/// Handle native close even while the companion's page is still loading.
pub(crate) fn hide_on_main_thread(
    window: &WebviewWindow,
    state: &SharedPetState,
) -> Result<(), String> {
    let mut state = state.lock().map_err(|error| error.to_string())?;
    let profile = native_hide_profile(window.label(), &state)?;
    state.user_hidden = true;
    if let Some(presentation) = state.presentation.as_mut() {
        presentation.enabled = false;
    }
    drop(state);
    window.hide().map_err(|error| error.to_string())?;
    window
        .app_handle()
        .emit_to(
            "main",
            "task-pet-hidden",
            serde_json::json!({"profileId": profile}),
        )
        .map_err(|error| error.to_string())
}

/// Hide the companion after checking its actual IPC caller surface.
#[tauri::command]
pub(crate) async fn hide_task_pet(
    app: AppHandle,
    window: WebviewWindow,
    state: tauri::State<'_, SharedPetState>,
) -> Result<(), String> {
    let state = state.inner().clone();
    crate::run_ui_work_on_main_thread(&app, "hide task pet", move || {
        {
            let locked = state.lock().map_err(|error| error.to_string())?;
            checked_binding(&window, &locked, true)?;
        }
        hide_on_main_thread(&window, &state)
    })
}

/// Closing the companion must leave the main conversation window alive.
pub(crate) fn closes_shell(label: &str) -> bool {
    label == "main"
}

#[cfg(test)]
mod tests {
    use super::*;
    fn presentation() -> Presentation {
        serde_json::from_value(serde_json::json!({"profileId":"profile-a", "enabled":true,
            "view":{"key":null,"revision":0,"mood":"idle","label":"Pet","title":"Idle","detail":""}})).unwrap()
    }
    #[test]
    fn native_close_needs_binding_and_label_but_not_loaded_page_url() {
        let state = Arc::new(Mutex::new(PetState::default()));
        assert!(native_hide_profile(LABEL, &state.lock().unwrap()).is_err());
        bind(&state, 8766, "profile-a").unwrap();
        let state = state.lock().unwrap();
        assert_eq!(native_hide_profile(LABEL, &state).unwrap(), "profile-a");
        assert!(native_hide_profile("main", &state).is_err());
    }
    #[test]
    fn older_projection_cannot_replace_newer_terminal_view() {
        let mut current = presentation();
        current.view.key = Some("scope".into());
        current.view.revision = 5;
        current.view.mood = Mood::Completed;
        let mut incoming = current.clone();
        incoming.view.revision = 4;
        incoming.view.mood = Mood::Thinking;
        fence_view(Some(&current), &mut incoming);
        assert_eq!(incoming.view.revision, 5);
        assert!(matches!(incoming.view.mood, Mood::Completed));
        incoming.view.mood = Mood::Thinking;
        fence_view(Some(&current), &mut incoming);
        assert!(matches!(incoming.view.mood, Mood::Completed));
    }
    #[test]
    fn pet_url_has_no_auth_data_and_exact_profile() {
        let url = pet_url(8766, "profile-a").unwrap();
        assert_eq!(
            url.as_str(),
            "http://127.0.0.1:8766/p/profile-a/chat?surface=task-pet"
        );
        assert!(url.fragment().is_none());
    }
    #[test]
    fn callers_are_label_origin_and_surface_bound() {
        let pet = pet_url(8766, "profile-a").unwrap();
        let binding = ("profile-a".into(), pet.clone());
        let main = tauri::Url::parse("http://127.0.0.1:8766/p/profile-a/chat").unwrap();
        assert!(caller_allowed("main", &main, &binding, false));
        assert!(caller_allowed(LABEL, &pet, &binding, true));
        assert!(!caller_allowed("main", &pet, &binding, false));
        assert!(!caller_allowed(LABEL, &main, &binding, true));
        assert!(!caller_allowed(
            "main",
            &tauri::Url::parse("http://127.0.0.1:8767/p/profile-a/chat").unwrap(),
            &binding,
            false
        ));
    }
    #[test]
    fn payload_rejects_cross_profile_oversize_unsafe_revision_and_unknown_fields() {
        let mut p = presentation();
        assert!(validate(&p, "profile-a").is_ok());
        assert!(validate(&p, "profile-b").is_err());
        p.view.revision = MAX_SAFE_REVISION + 1;
        assert!(validate(&p, "profile-a").is_err());
        p.view.revision = 0;
        p.view.title = "x".repeat(161);
        assert!(validate(&p, "profile-a").is_err());
        let mut value = serde_json::to_value(presentation()).unwrap();
        value["approved"] = true.into();
        assert!(serde_json::from_value::<Presentation>(value.clone()).is_err());
        value.as_object_mut().unwrap().remove("approved");
        value["view"]["mood"] = "unknown".into();
        assert!(serde_json::from_value::<Presentation>(value).is_err());
    }
    #[test]
    fn updates_never_reopen_hidden_pet_and_close_is_label_specific() {
        let mut state = PetState {
            user_hidden: true,
            ..Default::default()
        };
        assert!(!should_show(&mut state, &presentation(), false));
        assert!(state.user_hidden);
        assert!(should_show(&mut state, &presentation(), true));
        assert!(closes_shell("main"));
        assert!(!closes_shell(LABEL));
    }
    #[test]
    fn binding_and_navigation_cannot_rotate_or_escape() {
        let state = Arc::new(Mutex::new(PetState::default()));
        bind(&state, 8766, "profile-a").unwrap();
        assert!(bind(&state, 8766, "profile-b").is_err());
        assert!(navigation_allowed(
            &state,
            &pet_url(8766, "profile-a").unwrap()
        ));
        assert!(!navigation_allowed(
            &state,
            &tauri::Url::parse("http://127.0.0.1:8766/approval").unwrap()
        ));
    }
}
