use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::webview::DownloadEvent;
use tauri::{AppHandle, Emitter, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_opener::OpenerExt;

#[derive(Clone)]
struct FlaskChild(Arc<Mutex<Option<Child>>>);

fn find_ortho_root() -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        let exe_dir = exe.parent().unwrap_or(std::path::Path::new(".")).to_path_buf();
        // Check exe_dir and exe_dir/resources (Tauri bundle layout on some platforms)
        for candidate in [exe_dir.clone(), exe_dir.join("resources")] {
            if candidate.join("app.py").exists() {
                return candidate;
            }
        }
        // Walk up (works in dev where exe is deep inside target/)
        let mut dir = exe_dir;
        for _ in 0..8 {
            if dir.join("app.py").exists() {
                return dir;
            }
            if let Some(p) = dir.parent() {
                dir = p.to_path_buf();
            } else {
                break;
            }
        }
    }
    // Fallback for dev: launcher/ is one level below the Ortho root
    std::env::current_dir()
        .ok()
        .and_then(|d| d.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

#[tauri::command]
fn find_python() -> Result<String, String> {
    for cmd in &["py", "python3", "python"] {
        if Command::new(cmd)
            .arg("--version")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .is_ok()
        {
            return Ok(cmd.to_string());
        }
    }
    Err("Python 3.9+ is required. Download from python.org.".to_string())
}

#[tauri::command]
fn deps_need_install() -> bool {
    let root = find_ortho_root();
    let marker = root.join(".deps_installed");
    let req = root.join("requirements.txt");
    if !marker.exists() {
        return true;
    }
    let m = std::fs::read(&marker).unwrap_or_default();
    let r = std::fs::read(&req).unwrap_or_default();
    m != r
}

#[tauri::command]
fn install_deps(app: AppHandle, python: String) -> Result<(), String> {
    let root = find_ortho_root();
    let req = root.join("requirements.txt");

    let mut cmd = Command::new(&python);
    cmd.args(["-m", "pip", "install", "-r", req.to_str().unwrap_or("requirements.txt")])
        .current_dir(&root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }

    let mut child = cmd.spawn().map_err(|e| e.to_string())?;

    // Read stdout and stderr concurrently to avoid pipe buffer deadlock
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let app2 = app.clone();

    let stderr_thread = std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().flatten() {
            let _ = app2.emit("pip-output", &line);
        }
    });

    for line in BufReader::new(stdout).lines().flatten() {
        let _ = app.emit("pip-output", &line);
    }
    stderr_thread.join().ok();

    let status = child.wait().map_err(|e| e.to_string())?;
    if !status.success() {
        return Err("pip install failed — see log above".to_string());
    }

    // Write marker so the next launch skips install
    let marker = root.join(".deps_installed");
    std::fs::copy(&req, &marker).map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn launch_flask(python: String, state: State<'_, FlaskChild>) -> Result<(), String> {
    let root = find_ortho_root();

    let mut cmd = Command::new(&python);
    cmd.arg("app.py")
        .env("ORTHO_NO_BROWSER", "1")
        .current_dir(&root)
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }

    let child = cmd.spawn().map_err(|e| e.to_string())?;
    *state.0.lock().unwrap() = Some(child);
    Ok(())
}

#[tauri::command]
fn reveal_output(app: AppHandle, url_path: String) -> Result<(), String> {
    // url_path is like "/outputs/uuid.png" — resolve against the Ortho root
    let rel = url_path.trim_start_matches('/');
    let path = find_ortho_root().join(rel);
    app.opener()
        .reveal_item_in_dir(&path)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn navigate_to_flask(window: tauri::WebviewWindow) -> Result<(), String> {
    let url = "http://127.0.0.1:5000".parse().map_err(|e: url::ParseError| e.to_string())?;
    window.navigate(url).map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let flask_state = FlaskChild(Arc::new(Mutex::new(None)));
    let flask_for_handler = flask_state.clone();

    tauri::Builder::default()
        .manage(flask_state)
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Orthographic Template Generator")
                .inner_size(1100.0, 820.0)
                .resizable(true)
                .disable_drag_drop_handler()
                .on_download(|_webview, event| {
                    if let DownloadEvent::Requested { url, destination } = event {
                        if destination.as_os_str().is_empty() {
                            // WebView2 didn't suggest a path; build one from the URL
                            let url_str = url.as_str();
                            let filename = url_str.split('/').last()
                                .and_then(|s| s.split('?').next())
                                .filter(|s| !s.is_empty())
                                .unwrap_or("download");
                            let base = std::env::var("USERPROFILE")
                                .or_else(|_| std::env::var("HOME"))
                                .map(PathBuf::from)
                                .unwrap_or_else(|_| PathBuf::from("."));
                            *destination = base.join("Downloads").join(filename);
                        }
                    }
                    true
                })
                .build()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            find_python,
            deps_need_install,
            install_deps,
            launch_flask,
            navigate_to_flask,
            reveal_output,
        ])
        .on_window_event(move |_window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(mut c) = flask_for_handler.0.lock().unwrap().take() {
                    let _: std::io::Result<()> = c.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
