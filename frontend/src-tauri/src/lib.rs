use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::Emitter;

#[cfg(target_os = "windows")]
const BACKEND_BYTES: &[u8] = include_bytes!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/binaries/bipai-backend-x86_64-pc-windows-msvc.exe"
));
const BACKEND_FILE_NAME: &str = concat!("bipai-backend-", env!("CARGO_PKG_VERSION"), ".exe");

const READ_LDXP_TOKEN_EXPRESSION: &str = r#"
(() => {
  try {
    if (!/(^|\.)ldxp\.cn$/i.test(window.location.hostname)) return "";
    const unwrap = (raw) => {
      if (!raw) return "";
      let value = String(raw).trim();
      try {
        const parsed = JSON.parse(value);
        if (typeof parsed === "string") value = parsed;
        else if (parsed && typeof parsed === "object") {
          value = parsed.value || parsed.token || parsed.access_token ||
            (parsed.data && (parsed.data.token || parsed.data.value)) || value;
        }
      } catch (_) {}
      return String(value || "").trim();
    };
    const preferred = ["auth-token", "Merchant-Token", "merchant-token", "token", "access_token"];
    for (const key of preferred) {
      const hit = unwrap(window.localStorage.getItem(key)) || unwrap(window.sessionStorage.getItem(key));
      if (hit) return hit;
    }
    for (const store of [window.localStorage, window.sessionStorage]) {
      for (let i = 0; i < store.length; i++) {
        const key = store.key(i) || "";
        if (!/token|auth|merchant/i.test(key)) continue;
        const hit = unwrap(store.getItem(key));
        if (hit && hit.length >= 8) return hit;
      }
    }
    return "";
  } catch (_) {
    return "";
  }
})()
"#;

const LDXP_LOGIN_URL: &str = "https://www.ldxp.cn/merchant/";
const CATFK_LOGIN_URL: &str = "https://catfk.com/merchant/";
const PUBLIC_VERIFICATION_URL: &str = "https://pay.ldxp.cn/";

const EDGE_FIRST_RUN_DISABLED: &str = "msEdgeFirstRunExperience";

/// 链动与云猫是同一套发卡系统，登录流程一致，只有域名、配置目录、导入
/// 端点与前端事件名不同。用一个目标描述把差异集中在一处，避免复制整段逻辑。
#[derive(Clone, Copy)]
struct LoginTarget {
    login_url: &'static str,
    login_host: &'static str,
    profile_name: &'static str,
    import_path: &'static str,
    event_captured: &'static str,
    event_error: &'static str,
    event_progress: &'static str,
    log_prefix: &'static str,
    timeout_message: &'static str,
}

const LDXP_TARGET: LoginTarget = LoginTarget {
    login_url: LDXP_LOGIN_URL,
    login_host: "ldxp.cn",
    profile_name: "login-browser",
    import_path: "/api/ldxp-token/import",
    event_captured: "ldxp-token-captured",
    event_error: "ldxp-token-capture-error",
    event_progress: "ldxp-token-capture-progress",
    log_prefix: "ldxp",
    timeout_message: "链动登录等待已超时，请重新打开登录窗口。",
};

const CATFK_TARGET: LoginTarget = LoginTarget {
    login_url: CATFK_LOGIN_URL,
    login_host: "catfk.com",
    profile_name: "login-browser-catfk",
    import_path: "/api/catfk-token/import",
    event_captured: "catfk-token-captured",
    event_error: "catfk-token-capture-error",
    event_progress: "catfk-token-capture-progress",
    log_prefix: "catfk",
    timeout_message: "云猫登录等待已超时，请重新打开登录窗口。",
};

#[derive(Default)]
struct LoginBrowserState {
    child: Option<Child>,
    session: u64,
    debug_port: Option<u16>,
    profile_dir: Option<PathBuf>,
}

/// 与商家登录窗口分开管理，真人验证不会覆盖或读取任何账号浏览器状态。
struct PublicVerificationBrowser(Arc<Mutex<LoginBrowserState>>);

fn bipai_data_dir() -> PathBuf {
    std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("Bipai")
}

#[cfg(target_os = "windows")]
fn ensure_backend_executable() -> Result<PathBuf, String> {
    let runtime_dir = bipai_data_dir().join("runtime");
    fs::create_dir_all(&runtime_dir).map_err(|error| error.to_string())?;
    let backend_path = runtime_dir.join(BACKEND_FILE_NAME);

    let needs_update = fs::read(&backend_path)
        .map(|current| current.as_slice() != BACKEND_BYTES)
        .unwrap_or(true);
    if needs_update {
        let temporary = runtime_dir.join(format!("{BACKEND_FILE_NAME}.new"));
        fs::write(&temporary, BACKEND_BYTES).map_err(|error| error.to_string())?;
        if backend_path.exists() {
            fs::remove_file(&backend_path).map_err(|error| error.to_string())?;
        }
        fs::rename(&temporary, &backend_path).map_err(|error| error.to_string())?;
    }

    // 清掉旧版本的运行时副本；若仍被占用，删除失败也不影响本次启动。
    if let Ok(entries) = fs::read_dir(&runtime_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            let is_old_backend =
                path.file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| {
                        name.starts_with("bipai-backend-")
                            && name.ends_with(".exe")
                            && name != BACKEND_FILE_NAME
                    });
            if is_old_backend {
                let _ = fs::remove_file(path);
            }
        }
    }

    Ok(backend_path)
}

fn backend_is_ready() -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], 8756));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, Duration::from_millis(250)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
    if stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return false;
    }
    let mut response = Vec::with_capacity(2048);
    let mut chunk = [0_u8; 512];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(length) => {
                response.extend_from_slice(&chunk[..length]);
                let text = String::from_utf8_lossy(&response);
                if text.contains("200 OK") && text.contains("\"ok\":true") {
                    return true;
                }
                if response.len() >= 8192 {
                    break;
                }
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                break;
            }
            Err(_) => return false,
        }
    }
    false
}

#[cfg(target_os = "windows")]
fn start_backend() -> Result<Option<Child>, String> {
    if backend_is_ready() {
        return Ok(None);
    }

    let backend_path = ensure_backend_executable()?;
    let mut command = Command::new(&backend_path);
    command.current_dir(
        backend_path
            .parent()
            .ok_or_else(|| "后端运行目录无效".to_string())?,
    );
    use std::os::windows::process::CommandExt;
    command.creation_flags(0x08000000); // CREATE_NO_WINDOW

    let mut child = command.spawn().map_err(|error| error.to_string())?;
    for _ in 0..60 {
        if backend_is_ready() {
            return Ok(Some(child));
        }
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!("内置后端提前退出：{status}"));
        }
        thread::sleep(Duration::from_millis(250));
    }

    stop_process_tree(child);
    Err("内置后端启动超时".to_string())
}

#[cfg(not(target_os = "windows"))]
fn start_backend() -> Result<Option<Child>, String> {
    Err("当前便携版仅支持 Windows".to_string())
}

fn stop_process_tree(mut child: Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;

        // PyInstaller 与 Edge 都可能再启动子进程，/T 保证整个进程树一起退出。
        let taskkill = std::env::var_os("SystemRoot")
            .map(std::path::PathBuf::from)
            .map(|path| path.join("System32").join("taskkill.exe"))
            .unwrap_or_else(|| std::path::PathBuf::from("taskkill.exe"));
        let mut command = Command::new(taskkill);
        command
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .creation_flags(0x08000000);
        let _ = command.status();
    }

    let _ = child.kill();
}

fn import_token(import_path: &str, token: &str) -> Result<String, String> {
    let body = serde_json::json!({ "token": token }).to_string();
    let address = SocketAddr::from(([127, 0, 0, 1], 8756));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))
        .map_err(|_| "比牌后端尚未就绪，请稍后重试。".to_string())?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(12)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));

    let request = format!(
        "POST {import_path} HTTP/1.1\r\n\
         Host: 127.0.0.1:8756\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|error| format!("发送登录状态失败：{error}"))?;

    let mut response = Vec::with_capacity(4096);
    stream
        .read_to_end(&mut response)
        .map_err(|error| format!("读取验证结果失败：{error}"))?;
    let response = String::from_utf8_lossy(&response);
    let (_, response_body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(|| "验证服务返回了无效响应。".to_string())?;
    let result: serde_json::Value = serde_json::from_str(response_body)
        .map_err(|_| "验证服务返回了无法识别的结果。".to_string())?;
    let message = result
        .get("message")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("登录状态验证失败。")
        .to_string();

    if result.get("ok").and_then(serde_json::Value::as_bool) == Some(true) {
        Ok(message)
    } else {
        Err(message)
    }
}

fn import_public_clearance(
    cookie: &str,
    user_agent: &str,
    debug_port: u16,
) -> Result<String, String> {
    let body = serde_json::json!({
        "cookie": cookie,
        "user_agent": user_agent,
        "debug_port": debug_port,
    })
    .to_string();
    let address = SocketAddr::from(([127, 0, 0, 1], 8756));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))
        .map_err(|_| "比牌后端尚未就绪，请稍后重试。".to_string())?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(8)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
    let request = format!(
        "POST /api/public-clearance/import HTTP/1.1\r\n\
         Host: 127.0.0.1:8756\r\n\
         Content-Type: application/json\r\n\
         Content-Length: {}\r\n\
         Connection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|error| format!("同步真人验证状态失败：{error}"))?;
    let mut response = Vec::with_capacity(4096);
    stream
        .read_to_end(&mut response)
        .map_err(|error| format!("读取真人验证结果失败：{error}"))?;
    let response = String::from_utf8_lossy(&response);
    let (_, response_body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(|| "真人验证服务返回了无效响应。".to_string())?;
    let result: serde_json::Value = serde_json::from_str(response_body)
        .map_err(|_| "真人验证服务返回了无法识别的结果。".to_string())?;
    let message = result
        .get("message")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("真人验证状态同步失败。")
        .to_string();
    if result.get("ok").and_then(serde_json::Value::as_bool) == Some(true) {
        Ok(message)
    } else {
        Err(message)
    }
}

/// 后端会启动一个只监听 127.0.0.1 的受限 CONNECT 代理。它只允许链动、
/// PickAI、云猫和阿里验证资源，并把出站 socket 绑定到物理网卡，因此全局
/// TUN 开着时也不需要关闭或重载用户的代理客户端。
fn physical_direct_proxy_port() -> Option<u16> {
    let address = SocketAddr::from(([127, 0, 0, 1], 8756));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(6)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    stream
        .write_all(
            b"GET /api/network-route HTTP/1.1\r\nHost: 127.0.0.1:8756\r\nConnection: close\r\n\r\n",
        )
        .ok()?;
    let mut response = Vec::with_capacity(2048);
    stream.read_to_end(&mut response).ok()?;
    let response = String::from_utf8_lossy(&response);
    let (_, body) = response.split_once("\r\n\r\n")?;
    let result: serde_json::Value = serde_json::from_str(body).ok()?;
    if result.get("available")?.as_bool()? != true {
        return None;
    }
    result
        .get("proxy_port")?
        .as_u64()
        .and_then(|port| u16::try_from(port).ok())
        .filter(|port| *port > 0)
}

fn edge_executable() -> Option<PathBuf> {
    [
        std::env::var_os("ProgramFiles(x86)"),
        std::env::var_os("ProgramFiles"),
        std::env::var_os("LOCALAPPDATA"),
    ]
    .into_iter()
    .flatten()
    .map(PathBuf::from)
    .map(|root| {
        root.join("Microsoft")
            .join("Edge")
            .join("Application")
            .join("msedge.exe")
    })
    .find(|path| path.is_file())
}

#[cfg(windows)]
fn run_powershell(script: &str) -> Option<String> {
    use std::os::windows::process::CommandExt;
    let output = Command::new("powershell")
        .args(["-NoProfile", "-NonInteractive", "-Command", script])
        .creation_flags(0x08000000)
        .output()
        .ok()?;
    Some(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

#[cfg(not(windows))]
fn run_powershell(_script: &str) -> Option<String> {
    None
}

fn profile_marker(profile_dir: &std::path::Path) -> String {
    profile_dir.display().to_string().replace('\'', "''")
}

/// 是否还有使用该登录配置目录的 Edge 进程存活。
fn edge_running_for_profile(profile_dir: &std::path::Path) -> bool {
    let script = format!(
        "$m='{}'; @(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" -ErrorAction SilentlyContinue | Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains($m) }}).Count",
        profile_marker(profile_dir)
    );
    run_powershell(&script)
        .and_then(|out| out.trim().parse::<u32>().ok())
        .map(|count| count > 0)
        .unwrap_or(false)
}

/// 强制关闭使用该登录配置目录的所有 Edge 进程。
fn kill_edge_for_profile(profile_dir: &std::path::Path) {
    let script = format!(
        "$m='{}'; \
         $p=@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" -ErrorAction SilentlyContinue | Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains($m) }}); \
         $before=$p.Count; \
         $p | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}; \
         Start-Sleep -Milliseconds 250; \
         $after=@(Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" -ErrorAction SilentlyContinue | Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains($m) }}).Count; \
         Write-Output \"$before,$after\"",
        profile_marker(profile_dir)
    );
    let result = run_powershell(&script).unwrap_or_else(|| "unknown".into());
    login_capture_log(&format!("profile kill result={result}"));
}

fn login_capture_log(message: &str) {
    let path = bipai_data_dir().join("login-capture.log");
    let line = format!("{} {}\n", chrono_like_now(), message.replace('\n', " "));
    let _ = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .and_then(|mut file| file.write_all(line.as_bytes()));
}

fn chrono_like_now() -> String {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| format!("{}", d.as_secs()))
        .unwrap_or_else(|_| "0".into())
}

fn unwrap_auth_token_value(raw: &str) -> String {
    let value = raw.trim();
    if value.is_empty() {
        return String::new();
    }
    if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(value) {
        if let Some(text) = parsed.as_str() {
            return text.trim().to_string();
        }
        if let Some(object) = parsed.as_object() {
            for key in ["value", "token", "access_token"] {
                if let Some(text) = object.get(key).and_then(serde_json::Value::as_str) {
                    let text = text.trim();
                    if !text.is_empty() {
                        return text.to_string();
                    }
                }
            }
            if let Some(data) = object.get("data").and_then(serde_json::Value::as_object) {
                for key in ["value", "token", "access_token"] {
                    if let Some(text) = data.get(key).and_then(serde_json::Value::as_str) {
                        let text = text.trim();
                        if !text.is_empty() {
                            return text.to_string();
                        }
                    }
                }
            }
        }
    }
    value.to_string()
}

fn extract_auth_tokens_from_bytes(bytes: &[u8]) -> Vec<String> {
    let text = String::from_utf8_lossy(bytes);
    let mut tokens = Vec::new();
    let mut search_from = 0;
    while let Some(rel) = text[search_from..].find("auth-token") {
        let idx = search_from + rel;
        let window_end = (idx + 240).min(text.len());
        let window = &text[idx..window_end];
        if let Some(start) = window.find("{\"value\":\"") {
            let rest = &window[start + 10..];
            if let Some(end) = rest.find('"') {
                let token = rest[..end].trim();
                if token.len() >= 8 && !tokens.iter().any(|existing| existing == token) {
                    tokens.push(token.to_string());
                }
            }
        }
        search_from = idx + 10;
    }
    tokens
}

fn read_token_from_profile(profile_dir: &std::path::Path) -> Option<String> {
    let leveldb = profile_dir
        .join("Default")
        .join("Local Storage")
        .join("leveldb");
    let entries = fs::read_dir(&leveldb).ok()?;
    let mut newest: Option<(std::time::SystemTime, String)> = None;
    for entry in entries.flatten() {
        let path = entry.path();
        let name = path.file_name()?.to_string_lossy();
        if !(name.ends_with(".log") || name.ends_with(".ldb")) {
            continue;
        }
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let modified = path
            .metadata()
            .and_then(|meta| meta.modified())
            .unwrap_or(std::time::UNIX_EPOCH);
        for token in extract_auth_tokens_from_bytes(&bytes) {
            if newest
                .as_ref()
                .map(|(time, _)| modified >= *time)
                .unwrap_or(true)
            {
                newest = Some((modified, token));
            }
        }
    }
    newest.map(|(_, token)| token)
}

fn local_http_get(port: u16, path: &str) -> Result<String, String> {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_millis(600))
        .map_err(|error| error.to_string())?;
    let _ = stream.set_read_timeout(Some(Duration::from_millis(800)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let request =
        format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    stream
        .write_all(request.as_bytes())
        .map_err(|error| error.to_string())?;
    let mut response = Vec::with_capacity(8192);
    let mut buffer = [0_u8; 4096];
    loop {
        match stream.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => {
                response.extend_from_slice(&buffer[..read]);
                let Some(header_end) = response.windows(4).position(|part| part == b"\r\n\r\n")
                else {
                    continue;
                };
                let headers = String::from_utf8_lossy(&response[..header_end]);
                let content_length = headers.lines().find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().ok())
                        .flatten()
                });
                if content_length.is_some_and(|length| response.len() >= header_end + 4 + length) {
                    break;
                }
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) && !response.is_empty() =>
            {
                break;
            }
            Err(error) => return Err(error.to_string()),
        }
    }
    let response = String::from_utf8_lossy(&response);
    let (_, body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(|| "Edge 调试服务返回了无效响应。".to_string())?;
    Ok(body.to_string())
}

fn login_page_targets(port: u16, host_suffix: &str) -> Vec<(i32, String, String)> {
    let Ok(body) = local_http_get(port, "/json/list") else {
        return Vec::new();
    };
    let Ok(targets) = serde_json::from_str::<serde_json::Value>(&body) else {
        return Vec::new();
    };
    let Some(pages) = targets.as_array() else {
        return Vec::new();
    };
    let dotted_suffix = format!(".{}", host_suffix.to_ascii_lowercase());
    let mut candidates = pages
        .iter()
        .filter_map(|target| {
            let is_page = target.get("type").and_then(serde_json::Value::as_str) == Some("page");
            let url = target
                .get("url")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default()
                .to_string();
            let host = url
                .split_once("://")
                .and_then(|(_, rest)| rest.split('/').next())
                .unwrap_or_default()
                .split(':')
                .next()
                .unwrap_or_default()
                .to_ascii_lowercase();
            // 按登录目标域名匹配：链动 ldxp.cn、云猫 catfk.com 各读各的页面。
            let host_matches =
                host == host_suffix.to_ascii_lowercase() || host.ends_with(&dotted_suffix);
            if !(is_page && host_matches) {
                return None;
            }
            let websocket = target
                .get("webSocketDebuggerUrl")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)?;
            let lower = url.to_ascii_lowercase();
            let score = if lower.contains("/merchant/login") {
                1
            } else if lower.contains("/merchant") {
                3
            } else {
                2
            };
            Some((score, url, websocket))
        })
        .collect::<Vec<_>>();
    candidates.sort_by(|a, b| b.0.cmp(&a.0));
    candidates
}

fn cdp_connect(websocket_url: &str) -> Result<tungstenite::WebSocket<TcpStream>, String> {
    use tungstenite::client::IntoClientRequest;
    let mut request = websocket_url
        .into_client_request()
        .map_err(|error| error.to_string())?;
    // Edge 调试端口对带 Origin 的握手常直接 403，必须去掉。
    request.headers_mut().remove("Origin");
    request.headers_mut().remove("origin");
    // 手动带超时建连，避免握手/读取在异常时永久阻塞监控线程。
    let host_port = websocket_url
        .strip_prefix("ws://")
        .and_then(|rest| rest.split('/').next())
        .ok_or_else(|| "无效的调试地址".to_string())?;
    let address: SocketAddr = host_port
        .parse()
        .map_err(|_| "无效的调试地址".to_string())?;
    let stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))
        .map_err(|error| error.to_string())?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
    let (socket, _) = tungstenite::client(request, stream).map_err(|error| error.to_string())?;
    Ok(socket)
}

fn cdp_call(
    socket: &mut tungstenite::WebSocket<TcpStream>,
    id: i64,
    method: &str,
    params: serde_json::Value,
) -> Result<serde_json::Value, String> {
    let request = serde_json::json!({
        "id": id,
        "method": method,
        "params": params
    })
    .to_string();
    socket
        .send(tungstenite::Message::Text(request.into()))
        .map_err(|error| error.to_string())?;
    for _ in 0..12 {
        let message = socket.read().map_err(|error| error.to_string())?;
        let Ok(text) = message.into_text() else {
            continue;
        };
        let response: serde_json::Value =
            serde_json::from_str(&text).map_err(|error| error.to_string())?;
        if response.get("id").and_then(serde_json::Value::as_i64) != Some(id) {
            continue;
        }
        if let Some(error) = response.get("error") {
            return Err(error.to_string());
        }
        return Ok(response
            .get("result")
            .cloned()
            .unwrap_or(serde_json::json!({})));
    }
    Err("CDP 调用超时".into())
}

fn read_ldxp_token_from_edge(websocket_url: &str) -> Result<String, String> {
    let mut socket = cdp_connect(websocket_url)?;

    let runtime = cdp_call(
        &mut socket,
        1,
        "Runtime.evaluate",
        serde_json::json!({
            "expression": READ_LDXP_TOKEN_EXPRESSION,
            "returnByValue": true,
            "awaitPromise": false
        }),
    )?;
    let runtime_token = runtime
        .pointer("/result/value")
        .and_then(serde_json::Value::as_str)
        .map(unwrap_auth_token_value)
        .unwrap_or_default();
    if !runtime_token.is_empty() {
        return Ok(runtime_token);
    }

    let _ = cdp_call(&mut socket, 2, "DOMStorage.enable", serde_json::json!({}));
    let storage = cdp_call(
        &mut socket,
        3,
        "DOMStorage.getDOMStorageItems",
        serde_json::json!({
            "storageId": {
                "securityOrigin": "https://www.ldxp.cn",
                "isLocalStorage": true
            }
        }),
    );
    if let Ok(storage) = storage {
        if let Some(entries) = storage.get("entries").and_then(serde_json::Value::as_array) {
            for entry in entries {
                let key = entry
                    .get(0)
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default();
                let value = entry
                    .get(1)
                    .and_then(serde_json::Value::as_str)
                    .unwrap_or_default();
                if key.eq_ignore_ascii_case("auth-token")
                    || key.to_ascii_lowercase().contains("token")
                {
                    let token = unwrap_auth_token_value(value);
                    if !token.is_empty() {
                        return Ok(token);
                    }
                }
            }
        }
    }

    Ok(String::new())
}

fn cdp_alive(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    TcpStream::connect_timeout(&address, Duration::from_millis(250)).is_ok()
}

fn cdp_browser_websocket(port: u16) -> Option<String> {
    let body = local_http_get(port, "/json/version").ok()?;
    let version: serde_json::Value = serde_json::from_str(&body).ok()?;
    version
        .get("webSocketDebuggerUrl")
        .and_then(serde_json::Value::as_str)
        .map(str::to_owned)
}

fn close_edge_via_cdp(port: u16) {
    let Some(websocket_url) = cdp_browser_websocket(port) else {
        return;
    };
    let Ok(mut socket) = cdp_connect(&websocket_url) else {
        return;
    };
    let _ = cdp_call(&mut socket, 99, "Browser.close", serde_json::json!({}));
}

fn close_login_browser(browser: &Arc<Mutex<LoginBrowserState>>, session: Option<u64>) {
    let (child, profile_dir) = {
        let mut browser = browser.lock().unwrap();
        if session.is_some_and(|session| browser.session != session) {
            return;
        }
        browser.debug_port.take();
        (browser.child.take(), browser.profile_dir.take())
    };
    // 不再等待 Browser.close：当 Edge 复用了其他调试端口时会白等数秒。
    // 直接按专用 profile 关闭所有进程，既不会误伤用户日常 Edge，也最可靠。
    if let Some(profile) = profile_dir.as_deref() {
        kill_edge_for_profile(profile);
    }
    if let Some(child) = child {
        stop_process_tree(child);
    }
}

fn collect_candidate_tokens(
    cdp_ok: bool,
    debug_port: u16,
    profile_dir: &std::path::Path,
    login_host: &str,
) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut push = |token: String, source: &str| {
        let token = token.trim().to_string();
        if token.len() < 8 {
            return;
        }
        if !tokens.iter().any(|existing| existing == &token) {
            login_capture_log(&format!(
                "candidate source={source} token_len={}",
                token.len()
            ));
            tokens.push(token);
        }
    };

    // 磁盘配置目录最可靠也最快，先读它；即便 CDP 卡住也不影响拿到 token。
    let mut found_profile_token = false;
    if let Some(token) = read_token_from_profile(profile_dir) {
        found_profile_token = token.trim().len() >= 8;
        push(token, "profile-leveldb");
    }

    // CDP 能在登录当下拿到尚未落盘的内存 token，作为补充（带超时，不会卡死）。
    if !found_profile_token && cdp_ok {
        for (score, url, websocket) in login_page_targets(debug_port, login_host) {
            match read_ldxp_token_from_edge(&websocket) {
                Ok(token) if !token.is_empty() => {
                    push(token, &format!("cdp:{score}:{url}"));
                }
                Ok(_) => {}
                Err(error) => {
                    login_capture_log(&format!("cdp error page={url} err={error}"));
                }
            }
        }
    }

    tokens
}

fn monitor_login(
    app: tauri::AppHandle,
    browser: Arc<Mutex<LoginBrowserState>>,
    session: u64,
    debug_port: u16,
    profile_dir: PathBuf,
    target: LoginTarget,
) {
    let deadline = Instant::now() + Duration::from_secs(20 * 60);
    let mut last_token = String::new();
    let mut last_attempt: Option<Instant> = None;
    let mut last_error = String::new();
    let mut ready_announced = false;
    let mut seen_running = false;
    login_capture_log(&format!(
        "[{}] monitor start session={session} port={debug_port} profile={}",
        target.log_prefix,
        profile_dir.display()
    ));

    while Instant::now() < deadline {
        {
            let guard = browser.lock().unwrap();
            if guard.session != session {
                return;
            }
        }

        // 判活：调试端口优先（无需 powershell），端口没起来时退回进程检查。
        let cdp_ok = cdp_alive(debug_port);
        let running = cdp_ok || edge_running_for_profile(&profile_dir);
        if running {
            seen_running = true;
            if !ready_announced {
                ready_announced = true;
                let _ = app.emit(
                    target.event_progress,
                    serde_json::json!({ "message": "登录窗口已就绪，完成登录后会自动同步。" }),
                );
            }
        }

        // 读取 token：CDP（登录当下最快）+ 磁盘配置目录（不依赖调试端口）。
        let candidates =
            collect_candidate_tokens(cdp_ok, debug_port, &profile_dir, target.login_host);
        let retry_due = last_attempt
            .map(|attempt| attempt.elapsed() >= Duration::from_secs(5))
            .unwrap_or(true);
        for token in candidates {
            if token == last_token && !retry_due {
                continue;
            }
            last_token.clone_from(&token);
            last_attempt = Some(Instant::now());
            let _ = app.emit(
                target.event_progress,
                serde_json::json!({ "message": "已检测到登录状态，正在验证…" }),
            );
            match import_token(target.import_path, &token) {
                Ok(message) => {
                    login_capture_log(&format!("[{}] import success", target.log_prefix));
                    let _ = app.emit(
                        target.event_captured,
                        serde_json::json!({ "message": message }),
                    );
                    close_login_browser(&browser, Some(session));
                    return;
                }
                Err(message) if message != last_error => {
                    login_capture_log(&format!("[{}] import fail: {message}", target.log_prefix));
                    last_error.clone_from(&message);
                    let _ = app.emit(
                        target.event_error,
                        serde_json::json!({ "message": message }),
                    );
                }
                Err(_) => {}
            }
        }

        if seen_running && !running {
            // 窗口关了再兜底扫一次磁盘，避免登录成功瞬间关窗漏检。
            if let Some(token) = read_token_from_profile(&profile_dir) {
                if let Ok(message) = import_token(target.import_path, &token) {
                    login_capture_log(&format!(
                        "[{}] import success (post-close)",
                        target.log_prefix
                    ));
                    let _ = app.emit(
                        target.event_captured,
                        serde_json::json!({ "message": message }),
                    );
                    return;
                }
            }
            login_capture_log(&format!(
                "[{}] window closed without valid token",
                target.log_prefix
            ));
            let _ = app.emit(
                target.event_error,
                serde_json::json!({ "message": "登录窗口已关闭，未检测到有效登录状态。" }),
            );
            return;
        }

        thread::sleep(Duration::from_millis(1200));
    }

    close_login_browser(&browser, Some(session));
    let _ = app.emit(
        target.event_error,
        serde_json::json!({ "message": target.timeout_message }),
    );
}

fn open_login(
    app: tauri::AppHandle,
    login_browser: &Arc<Mutex<LoginBrowserState>>,
    target: LoginTarget,
) -> Result<(), String> {
    let edge = edge_executable()
        .ok_or_else(|| "没有找到 Microsoft Edge，无法启动自动登录窗口。".to_string())?;
    // 固定配置目录：登录态可复用，也方便从 LevelDB 兜底读取 token。
    let profile_dir = bipai_data_dir().join(target.profile_name);
    fs::create_dir_all(&profile_dir).map_err(|error| error.to_string())?;

    let (session, previous_child, previous_port) = {
        let mut browser = login_browser.lock().unwrap();
        browser.session = browser.session.wrapping_add(1);
        (
            browser.session,
            browser.child.take(),
            browser.debug_port.take(),
        )
    };
    if let Some(port) = previous_port {
        close_edge_via_cdp(port);
    }
    if let Some(child) = previous_child {
        stop_process_tree(child);
    }
    // 清掉占用同配置目录的残留 Edge，否则新窗口会复用旧进程、忽略新调试端口。
    kill_edge_for_profile(&profile_dir);
    thread::sleep(Duration::from_millis(500));
    let physical_proxy_port = physical_direct_proxy_port();

    let spawn_edge = |debug_port: u16| -> Result<Child, String> {
        let mut command = Command::new(&edge);
        command.arg(format!("--app={}", target.login_url));
        if let Some(port) = physical_proxy_port {
            command
                .arg(format!("--proxy-server=http://127.0.0.1:{port}"))
                .arg("--disable-quic");
        } else {
            // 普通系统代理仍可直接绕开；全局 TUN 则由上面的本地代理处理。
            command.arg("--no-proxy-server");
        }
        command
            .arg("--no-first-run")
            .arg("--no-default-browser-check")
            .arg(format!("--disable-features={EDGE_FIRST_RUN_DISABLED}"))
            .arg("--remote-debugging-address=127.0.0.1")
            .arg("--remote-allow-origins=*")
            .arg(format!("--remote-debugging-port={debug_port}"))
            .arg(format!("--user-data-dir={}", profile_dir.display()))
            .arg("--window-size=1100,760")
            .spawn()
            .map_err(|error| format!("无法启动 Edge 登录窗口：{error}"))
    };
    let free_port = || -> Result<u16, String> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|error| error.to_string())?;
        let port = listener
            .local_addr()
            .map_err(|error| error.to_string())?
            .port();
        drop(listener);
        Ok(port)
    };

    let mut debug_port = free_port()?;
    let mut child = spawn_edge(debug_port)?;

    // 等调试端口就绪；若 8 秒没起来（多半是复用了旧进程），强杀后换端口重开一次。
    let mut cdp_up = false;
    for _ in 0..32 {
        if cdp_alive(debug_port) {
            cdp_up = true;
            break;
        }
        thread::sleep(Duration::from_millis(250));
    }
    if !cdp_up {
        login_capture_log(&format!(
            "[{}] debug port not up, relaunching once",
            target.log_prefix
        ));
        kill_edge_for_profile(&profile_dir);
        stop_process_tree(child);
        thread::sleep(Duration::from_millis(700));
        debug_port = free_port()?;
        child = spawn_edge(debug_port)?;
    }

    {
        let mut browser = login_browser.lock().unwrap();
        if browser.session != session {
            drop(browser);
            kill_edge_for_profile(&profile_dir);
            stop_process_tree(child);
            return Err("登录请求已被新的窗口替换。".to_string());
        }
        browser.debug_port = Some(debug_port);
        browser.profile_dir = Some(profile_dir.clone());
        browser.child.replace(child);
    }

    login_capture_log(&format!(
        "[{}] opened edge session={session} port={debug_port} profile={}",
        target.log_prefix,
        profile_dir.display()
    ));
    let monitor_browser = Arc::clone(login_browser);
    thread::spawn(move || {
        monitor_login(
            app,
            monitor_browser,
            session,
            debug_port,
            profile_dir,
            target,
        )
    });
    Ok(())
}

fn is_public_clearance_cookie(name: &str) -> bool {
    let lower = name.to_ascii_lowercase();
    matches!(lower.as_str(), "acw_tc" | "cdn_sec_tc" | "acw_sc__v2")
        || lower.starts_with("aliyun_waf_")
        || lower.starts_with("waf_")
        || lower.starts_with("esa_")
}

fn read_public_verification_from_edge(
    websocket_url: &str,
) -> Result<(bool, bool, bool, String, String), String> {
    let mut socket = cdp_connect(websocket_url)?;
    let runtime = cdp_call(
        &mut socket,
        20,
        "Runtime.evaluate",
        serde_json::json!({
            "expression": r#"(() => {
              const html = (document.documentElement && document.documentElement.innerHTML || '').slice(0, 80000);
              const title = String(document.title || '');
              return {
                ready: document.readyState === 'complete',
                challenge: /滑动验证页面|aliyunCaptcha|captcha-element|Request ID|验证您是真人/i.test(title + ' ' + html),
                errorPage: location.href.startsWith('chrome-error://') ||
                  /ERR_[A-Z_]+|无法访问此页面|关闭了连接|连接已重置/i.test(title + ' ' + html),
                userAgent: String(navigator.userAgent || '')
              };
            })()"#,
            "returnByValue": true,
            "awaitPromise": false
        }),
    )?;
    let state = runtime
        .pointer("/result/value")
        .cloned()
        .unwrap_or_else(|| serde_json::json!({}));
    let ready = state
        .get("ready")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    let challenge = state
        .get("challenge")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(true);
    let error_page = state
        .get("errorPage")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false);
    let user_agent = state
        .get("userAgent")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();

    let _ = cdp_call(&mut socket, 21, "Network.enable", serde_json::json!({}));
    let cookies = cdp_call(
        &mut socket,
        22,
        "Network.getCookies",
        serde_json::json!({ "urls": [PUBLIC_VERIFICATION_URL] }),
    )?;
    let mut cookie_parts = cookies
        .get("cookies")
        .and_then(serde_json::Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|cookie| {
            let domain = cookie
                .get("domain")
                .and_then(serde_json::Value::as_str)
                .unwrap_or_default()
                .trim_start_matches('.')
                .to_ascii_lowercase();
            let name = cookie
                .get("name")
                .and_then(serde_json::Value::as_str)?
                .trim();
            let value = cookie
                .get("value")
                .and_then(serde_json::Value::as_str)?
                .trim();
            (domain == "pay.ldxp.cn"
                && is_public_clearance_cookie(name)
                && !value.is_empty()
                && !value
                    .chars()
                    .any(|char| matches!(char, ';' | ',' | '\r' | '\n')))
            .then(|| format!("{name}={value}"))
        })
        .collect::<Vec<_>>();
    cookie_parts.sort();
    cookie_parts.dedup();
    Ok((
        ready,
        challenge,
        error_page,
        cookie_parts.join("; "),
        user_agent,
    ))
}

fn public_api_verified_in_edge(websocket_url: &str) -> Result<bool, String> {
    let mut socket = cdp_connect(websocket_url)?;
    let runtime = cdp_call(
        &mut socket,
        24,
        "Runtime.evaluate",
        serde_json::json!({
            "expression": r#"(async () => {
              try {
                const response = await fetch('/shopApi/Shop/info', {
                  method: 'POST',
                  credentials: 'include',
                  cache: 'no-store',
                  headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Visitorid': 'bipaiweb'
                  },
                  body: JSON.stringify({ token: 'ldxp', category_key: '' })
                });
                const text = await response.text();
                let payload = null;
                try { payload = JSON.parse(text); } catch (_) {}
                return Boolean(
                  response.ok && payload && typeof payload === 'object' &&
                  Object.prototype.hasOwnProperty.call(payload, 'code')
                );
              } catch (_) {
                return false;
              }
            })()"#,
            "returnByValue": true,
            "awaitPromise": true
        }),
    )?;
    Ok(runtime
        .pointer("/result/value")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false))
}

fn minimize_browser_window(websocket_url: &str) {
    let Ok(mut socket) = cdp_connect(websocket_url) else {
        return;
    };
    let Ok(window) = cdp_call(
        &mut socket,
        30,
        "Browser.getWindowForTarget",
        serde_json::json!({}),
    ) else {
        return;
    };
    let Some(window_id) = window.get("windowId").and_then(serde_json::Value::as_i64) else {
        return;
    };
    let _ = cdp_call(
        &mut socket,
        31,
        "Browser.setWindowBounds",
        serde_json::json!({
            "windowId": window_id,
            "bounds": { "windowState": "minimized" }
        }),
    );
}

/// Edge 会把上一次 CDP 最小化后的窗口状态写进隔离 profile。若下一轮真人
/// 验证直接复用该 profile，新进程虽然已经启动，窗口却会立刻缩进任务栏，
/// 看起来就像“打开秒关”。每次新会话绑定到页面后都显式恢复并置前。
fn restore_browser_window(websocket_url: &str) -> bool {
    let Ok(mut socket) = cdp_connect(websocket_url) else {
        return false;
    };
    let Ok(window) = cdp_call(
        &mut socket,
        32,
        "Browser.getWindowForTarget",
        serde_json::json!({}),
    ) else {
        return false;
    };
    let Some(window_id) = window.get("windowId").and_then(serde_json::Value::as_i64) else {
        return false;
    };
    if cdp_call(
        &mut socket,
        33,
        "Browser.setWindowBounds",
        serde_json::json!({
            "windowId": window_id,
            "bounds": { "windowState": "normal" }
        }),
    )
    .is_err()
    {
        return false;
    }
    let _ = cdp_call(&mut socket, 34, "Page.bringToFront", serde_json::json!({}));
    true
}

fn monitor_public_verification(
    app: tauri::AppHandle,
    browser: Arc<Mutex<LoginBrowserState>>,
    session: u64,
    debug_port: u16,
    profile_dir: PathBuf,
) {
    let deadline = Instant::now() + Duration::from_secs(5 * 60);
    let started_at = Instant::now();
    let mut seen_running = false;
    let mut challenge_announced = false;
    let mut challenge_page: Option<String> = None;
    let mut normal_streak = 0_u8;
    let mut error_page_announced = false;
    let mut last_api_probe: Option<Instant> = None;
    let mut window_restored = false;
    login_capture_log(&format!(
        "[public-verification] monitor start session={session} port={debug_port} profile={}",
        profile_dir.display()
    ));
    while Instant::now() < deadline {
        {
            let guard = browser.lock().unwrap();
            if guard.session != session {
                return;
            }
        }
        let cdp_ok = cdp_alive(debug_port);
        if cdp_ok {
            seen_running = true;
            let targets = login_page_targets(debug_port, "pay.ldxp.cn");
            let candidates = if let Some(bound) = challenge_page.as_ref() {
                vec![(0, String::new(), bound.clone())]
            } else {
                targets
            };
            for (_, _, websocket) in candidates {
                if !window_restored && restore_browser_window(&websocket) {
                    window_restored = true;
                    login_capture_log("[public-verification] restored window");
                }
                match read_public_verification_from_edge(&websocket) {
                    Ok((ready, true, _, _, _)) if ready => {
                        challenge_page.get_or_insert_with(|| websocket.clone());
                        normal_streak = 0;
                        error_page_announced = false;
                        if !challenge_announced {
                            challenge_announced = true;
                            let _ = app.emit(
                                "public-verification-progress",
                                serde_json::json!({
                                    "message": "原站滑块已打开：只需拖一次，完成后比牌会自动接管并重搜。"
                                }),
                            );
                        }
                    }
                    Ok((true, false, true, _, _)) => {
                        normal_streak = 0;
                        if !error_page_announced {
                            error_page_announced = true;
                            let _ = app.emit(
                                "public-verification-progress",
                                serde_json::json!({
                                    "message": "原站暂时断开连接；窗口不会再自动关闭，请在验证窗口里点“刷新”重试。"
                                }),
                            );
                        }
                    }
                    Ok((true, false, false, cookie, user_agent)) if !cookie.is_empty() => {
                        // 加载中页面也会短暂出现 ready + 非滑块。必须先连续稳定，
                        // 再让同一个页面实打实请求公开 JSON API；只有拿到 JSON
                        // 信封才算通过，不能再凭标题/Cookie 猜测成功。
                        normal_streak = normal_streak.saturating_add(1);
                        let stable_after_challenge = challenge_page.is_some() && normal_streak >= 2;
                        let stable_existing_session = challenge_page.is_none()
                            && started_at.elapsed() >= Duration::from_secs(3)
                            && normal_streak >= 2;
                        if !(stable_after_challenge || stable_existing_session) {
                            continue;
                        }
                        if last_api_probe
                            .is_some_and(|last| last.elapsed() < Duration::from_secs(5))
                        {
                            continue;
                        }
                        last_api_probe = Some(Instant::now());
                        let api_verified = public_api_verified_in_edge(&websocket).unwrap_or(false);
                        login_capture_log(&format!(
                            "[public-verification] api probe verified={api_verified} streak={normal_streak}"
                        ));
                        if !api_verified {
                            normal_streak = 0;
                            continue;
                        }
                        match import_public_clearance(&cookie, &user_agent, debug_port) {
                            Ok(message) => {
                                login_capture_log("[public-verification] import success");
                                let _ = app.emit(
                                    "public-verification-complete",
                                    serde_json::json!({ "message": message }),
                                );
                                // 保留隔离浏览器并最小化：后端会在这个已验证的真实
                                // 浏览器上下文执行公开 API，请求不再依赖可被指纹绑定的
                                // Cookie 搬运。应用退出时仍会统一关闭整个进程树。
                                minimize_browser_window(&websocket);
                                return;
                            }
                            Err(message) => {
                                login_capture_log(&format!(
                                    "[public-verification] import fail: {message}"
                                ));
                                let _ = app.emit(
                                    "public-verification-error",
                                    serde_json::json!({ "message": message }),
                                );
                            }
                        }
                        close_login_browser(&browser, Some(session));
                        return;
                    }
                    Ok(_) => {}
                    Err(error) => {
                        login_capture_log(&format!("[public-verification] cdp read error: {error}"))
                    }
                }
            }
        } else if seen_running {
            let _ = app.emit(
                "public-verification-error",
                serde_json::json!({
                    "message": "真人验证窗口已关闭，滑块没有完成。"
                }),
            );
            return;
        }
        thread::sleep(Duration::from_millis(650));
    }
    close_login_browser(&browser, Some(session));
    let _ = app.emit(
        "public-verification-error",
        serde_json::json!({ "message": "真人验证等待超时，请重新点一次。" }),
    );
}

fn open_public_verification_browser(
    app: tauri::AppHandle,
    browser: &Arc<Mutex<LoginBrowserState>>,
) -> Result<(), String> {
    let edge = edge_executable()
        .ok_or_else(|| "没有找到 Microsoft Edge，无法打开真人验证窗口。".to_string())?;
    let profile_dir = bipai_data_dir().join("public-verification-browser");
    fs::create_dir_all(&profile_dir).map_err(|error| error.to_string())?;
    let (session, previous_child, previous_port) = {
        let mut browser = browser.lock().unwrap();
        browser.session = browser.session.wrapping_add(1);
        (
            browser.session,
            browser.child.take(),
            browser.debug_port.take(),
        )
    };
    if let Some(port) = previous_port {
        close_edge_via_cdp(port);
    }
    if let Some(child) = previous_child {
        stop_process_tree(child);
    }
    kill_edge_for_profile(&profile_dir);
    thread::sleep(Duration::from_millis(350));
    let physical_proxy_port = physical_direct_proxy_port();

    let spawn_edge = |debug_port: u16| -> Result<Child, String> {
        let mut command = Command::new(&edge);
        command.arg(format!("--app={PUBLIC_VERIFICATION_URL}"));
        if let Some(port) = physical_proxy_port {
            command
                .arg(format!("--proxy-server=http://127.0.0.1:{port}"))
                .arg("--disable-quic");
        } else {
            command.arg("--no-proxy-server");
        }
        command
            .arg("--no-first-run")
            .arg("--no-default-browser-check")
            .arg(format!("--disable-features={EDGE_FIRST_RUN_DISABLED}"))
            .arg("--remote-debugging-address=127.0.0.1")
            .arg("--remote-allow-origins=*")
            .arg(format!("--remote-debugging-port={debug_port}"))
            .arg(format!("--user-data-dir={}", profile_dir.display()))
            .arg("--window-size=620,580")
            .spawn()
            .map_err(|error| format!("无法启动真人验证窗口：{error}"))
    };
    let free_port = || -> Result<u16, String> {
        let listener = TcpListener::bind(("127.0.0.1", 0)).map_err(|error| error.to_string())?;
        let port = listener
            .local_addr()
            .map_err(|error| error.to_string())?
            .port();
        drop(listener);
        Ok(port)
    };
    let mut debug_port = free_port()?;
    let mut child = spawn_edge(debug_port)?;
    let mut cdp_up = false;
    for _ in 0..32 {
        if cdp_alive(debug_port) {
            cdp_up = true;
            break;
        }
        thread::sleep(Duration::from_millis(250));
    }
    if !cdp_up {
        kill_edge_for_profile(&profile_dir);
        stop_process_tree(child);
        thread::sleep(Duration::from_millis(500));
        debug_port = free_port()?;
        child = spawn_edge(debug_port)?;
    }
    {
        let mut guard = browser.lock().unwrap();
        if guard.session != session {
            drop(guard);
            kill_edge_for_profile(&profile_dir);
            stop_process_tree(child);
            return Err("真人验证请求已被新的窗口替换。".to_string());
        }
        guard.debug_port = Some(debug_port);
        guard.profile_dir = Some(profile_dir.clone());
        guard.child.replace(child);
    }
    let monitor_browser = Arc::clone(browser);
    thread::spawn(move || {
        monitor_public_verification(app, monitor_browser, session, debug_port, profile_dir)
    });
    Ok(())
}

#[tauri::command]
fn open_ldxp_login(
    app: tauri::AppHandle,
    login_browser: tauri::State<'_, Arc<Mutex<LoginBrowserState>>>,
) -> Result<(), String> {
    open_login(app, login_browser.inner(), LDXP_TARGET)
}

#[tauri::command]
fn open_catfk_login(
    app: tauri::AppHandle,
    login_browser: tauri::State<'_, Arc<Mutex<LoginBrowserState>>>,
) -> Result<(), String> {
    open_login(app, login_browser.inner(), CATFK_TARGET)
}

#[tauri::command]
fn open_public_verification(
    app: tauri::AppHandle,
    browser: tauri::State<'_, PublicVerificationBrowser>,
) -> Result<(), String> {
    open_public_verification_browser(app, &browser.0)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
    let backend_setup = Arc::clone(&backend);
    let backend_exit = Arc::clone(&backend);
    let login_browser = Arc::new(Mutex::new(LoginBrowserState::default()));
    let login_browser_exit = Arc::clone(&login_browser);
    let public_verification_browser = Arc::new(Mutex::new(LoginBrowserState::default()));
    let public_verification_browser_exit = Arc::clone(&public_verification_browser);

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(login_browser)
        .manage(PublicVerificationBrowser(public_verification_browser))
        .invoke_handler(tauri::generate_handler![
            open_ldxp_login,
            open_catfk_login,
            open_public_verification
        ])
        .setup(move |app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            match start_backend() {
                Ok(Some(child)) => {
                    backend_setup.lock().unwrap().replace(child);
                }
                Ok(None) => {
                    log::info!("using an already running Bipai backend");
                }
                Err(error) => {
                    log::error!("failed to start embedded backend: {error}");
                }
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(move |_app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                if let Some(child) = backend_exit.lock().unwrap().take() {
                    stop_process_tree(child);
                }
                let (child, debug_port, profile_dir) = {
                    let mut browser = login_browser_exit.lock().unwrap();
                    (
                        browser.child.take(),
                        browser.debug_port.take(),
                        browser.profile_dir.take(),
                    )
                };
                if let Some(port) = debug_port {
                    close_edge_via_cdp(port);
                }
                if let Some(profile) = profile_dir.as_deref() {
                    kill_edge_for_profile(profile);
                }
                if let Some(child) = child {
                    stop_process_tree(child);
                }
                let (child, debug_port, profile_dir) = {
                    let mut browser = public_verification_browser_exit.lock().unwrap();
                    (
                        browser.child.take(),
                        browser.debug_port.take(),
                        browser.profile_dir.take(),
                    )
                };
                if let Some(port) = debug_port {
                    close_edge_via_cdp(port);
                }
                if let Some(profile) = profile_dir.as_deref() {
                    kill_edge_for_profile(profile);
                }
                if let Some(child) = child {
                    stop_process_tree(child);
                }
            }
        });
}
