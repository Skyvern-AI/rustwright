use std::collections::hash_map::DefaultHasher;
use std::env;
use std::fs::{self, OpenOptions};
use std::hash::{Hash, Hasher};
#[cfg(unix)]
use std::io::Read;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::Serialize;

const POSTHOG_ENDPOINT: &str = "https://us.i.posthog.com/i/v0/e/";
// PostHog public write-only project token, safe to embed by design.
const POSTHOG_API_KEY: &str = "phc_bVT2ugnZhMHRWqMvSRHPdeTjaPxQqT3QSsI3r5FlQR5";
const TELEMETRY_TIMEOUT: Duration = Duration::from_secs(3);
const DISABLE_ENV_VARS: [&str; 3] = [
    "DISABLE_TELEMETRY",
    "RUSTWRIGHT_DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
];

static TELEMETRY_STARTED: AtomicBool = AtomicBool::new(false);
static ID_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Serialize)]
struct TelemetryPayload {
    api_key: &'static str,
    event: &'static str,
    distinct_id: String,
    properties: TelemetryProperties,
}

#[derive(Serialize)]
struct TelemetryProperties {
    rustwright_version: &'static str,
    os: &'static str,
    arch: &'static str,
    #[serde(rename = "$process_person_profile")]
    process_person_profile: bool,
    #[serde(rename = "$geoip_disable")]
    geoip_disable: bool,
}

pub(crate) fn telemetry_disabled() -> bool {
    DISABLE_ENV_VARS.iter().any(|name| {
        env::var_os(name).is_some_and(|value| match value.to_str() {
            Some("0") => false,
            Some(value) if value.eq_ignore_ascii_case("false") => false,
            _ => true,
        })
    })
}

pub(crate) fn record_engine_launched(_runtime: &tokio::runtime::Runtime) {
    if telemetry_disabled() || TELEMETRY_STARTED.swap(true, Ordering::Relaxed) {
        return;
    }

    // The send runs on its own thread with its own single-thread runtime so it
    // neither occupies a CDP worker nor dies with the browser runtime when the
    // caller closes the browser immediately after launch.
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let _ = std::thread::spawn(move || {
            let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                let payload = engine_launched_payload(load_or_create_telemetry_id());
                let Ok(body) = serde_json::to_vec(&payload) else {
                    return;
                };
                let Ok(runtime) = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                else {
                    return;
                };
                // tokio::time::timeout and reqwest's total-timeout Sleep both arm their
                // timers eagerly, so every piece of this must be constructed inside the
                // runtime context, not in block_on's argument position.
                let _ = runtime.block_on(async {
                    tokio::time::timeout(TELEMETRY_TIMEOUT, async {
                        let Ok(client) = reqwest::Client::builder()
                            .timeout(TELEMETRY_TIMEOUT)
                            .build()
                        else {
                            return;
                        };
                        let _ = client
                            .post(POSTHOG_ENDPOINT)
                            .header(reqwest::header::CONTENT_TYPE, "application/json")
                            .body(body)
                            .send()
                            .await;
                    })
                    .await
                });
            }));
        });
    }));
}

fn engine_launched_payload(distinct_id: String) -> TelemetryPayload {
    TelemetryPayload {
        api_key: POSTHOG_API_KEY,
        event: "engine_launched",
        distinct_id,
        properties: TelemetryProperties {
            rustwright_version: env!("CARGO_PKG_VERSION"),
            os: env::consts::OS,
            arch: env::consts::ARCH,
            process_person_profile: false,
            geoip_disable: true,
        },
    }
}

fn load_or_create_telemetry_id() -> String {
    let Some(path) = telemetry_id_path() else {
        return generate_telemetry_id();
    };
    load_or_create_telemetry_id_at(&path)
}

fn load_or_create_telemetry_id_at(path: &Path) -> String {
    let generated = generate_telemetry_id();

    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_file() => {
            if let Ok(existing) = fs::read_to_string(path) {
                if let Some(existing) = valid_telemetry_id(&existing) {
                    return existing.to_string();
                }
            }
        }
        Ok(_) => return generated,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return generated,
    }

    let Some(parent) = path.parent() else {
        return generated;
    };
    if fs::create_dir_all(parent).is_err() {
        return generated;
    }

    create_telemetry_id_file(path, generated)
}

fn create_telemetry_id_file(path: &Path, generated: String) -> String {
    create_telemetry_id_file_with(path, generated, |file, contents| file.write_all(contents))
}

fn create_telemetry_id_file_with<F>(path: &Path, generated: String, write: F) -> String
where
    F: FnOnce(&mut fs::File, &[u8]) -> std::io::Result<()>,
{
    match OpenOptions::new().write(true).create_new(true).open(path) {
        Ok(mut file) => {
            if write(&mut file, generated.as_bytes()).is_err() {
                drop(file);
                let _ = fs::remove_file(path);
            }
            generated
        }
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
            read_valid_telemetry_id(path).unwrap_or(generated)
        }
        Err(_) => generated,
    }
}

fn read_valid_telemetry_id(path: &Path) -> Option<String> {
    let metadata = fs::symlink_metadata(path).ok()?;
    if !metadata.is_file() {
        return None;
    }

    let existing = fs::read_to_string(path).ok()?;
    valid_telemetry_id(&existing).map(str::to_owned)
}

fn telemetry_id_path() -> Option<PathBuf> {
    #[cfg(windows)]
    let cache_dir = nonempty_env_path("LOCALAPPDATA")?;

    #[cfg(not(windows))]
    let cache_dir = nonempty_env_path("XDG_CACHE_HOME")
        .or_else(|| nonempty_env_path("HOME").map(|home| home.join(".cache")))?;

    Some(cache_dir.join("rustwright").join("telemetry_id"))
}

fn nonempty_env_path(name: &str) -> Option<PathBuf> {
    env::var_os(name)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn valid_telemetry_id(value: &str) -> Option<&str> {
    let value = value.trim();
    (value.len() == 32 && value.bytes().all(|byte| byte.is_ascii_hexdigit())).then_some(value)
}

fn generate_telemetry_id() -> String {
    #[cfg(unix)]
    {
        let mut bytes = [0_u8; 16];
        if fs::File::open("/dev/urandom")
            .and_then(|mut file| file.read_exact(&mut bytes))
            .is_ok()
        {
            return hex_encode_telemetry_id(&bytes);
        }
    }

    generate_fallback_telemetry_id()
}

#[cfg(unix)]
fn hex_encode_telemetry_id(bytes: &[u8; 16]) -> String {
    const HEX_DIGITS: &[u8; 16] = b"0123456789abcdef";

    let mut encoded = String::with_capacity(32);
    for byte in bytes {
        encoded.push(HEX_DIGITS[(byte >> 4) as usize] as char);
        encoded.push(HEX_DIGITS[(byte & 0x0f) as usize] as char);
    }
    encoded
}

fn generate_fallback_telemetry_id() -> String {
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let process_id = std::process::id();
    let counter = ID_COUNTER.fetch_add(1, Ordering::Relaxed);

    let mut first = DefaultHasher::new();
    timestamp.hash(&mut first);
    process_id.hash(&mut first);
    counter.hash(&mut first);
    let first = first.finish();

    let mut second = DefaultHasher::new();
    first.hash(&mut second);
    timestamp.rotate_left(37).hash(&mut second);
    counter.wrapping_add(1).hash(&mut second);
    let second = second.finish();

    format!("{first:016x}{second:016x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    use std::ffi::OsString;
    use std::sync::Mutex;

    #[cfg(unix)]
    use std::os::unix::ffi::OsStringExt;
    #[cfg(unix)]
    use std::os::unix::fs::symlink;
    #[cfg(windows)]
    use std::os::windows::ffi::OsStringExt;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    struct EnvSnapshot(Vec<(&'static str, Option<OsString>)>);

    impl EnvSnapshot {
        fn clear() -> Self {
            let saved = DISABLE_ENV_VARS
                .iter()
                .map(|name| (*name, env::var_os(name)))
                .collect();
            for name in DISABLE_ENV_VARS {
                env::remove_var(name);
            }
            Self(saved)
        }
    }

    impl Drop for EnvSnapshot {
        fn drop(&mut self) {
            for (name, value) in self.0.drain(..) {
                if let Some(value) = value {
                    env::set_var(name, value);
                } else {
                    env::remove_var(name);
                }
            }
        }
    }

    #[test]
    fn telemetry_disabled_honors_truth_table_and_all_variable_names() {
        let _lock = ENV_LOCK.lock().unwrap();
        let _snapshot = EnvSnapshot::clear();

        assert!(!telemetry_disabled());

        for name in DISABLE_ENV_VARS {
            for value in ["0", "false", "FALSE"] {
                env::set_var(name, value);
                assert!(
                    !telemetry_disabled(),
                    "{name}={value:?} should enable telemetry"
                );
                env::remove_var(name);
            }

            for value in ["", "1", "true", "TRUE", "yes", "garbage"] {
                env::set_var(name, value);
                assert!(
                    telemetry_disabled(),
                    "{name}={value:?} should disable telemetry"
                );
                env::remove_var(name);
            }

            #[cfg(any(unix, windows))]
            {
                env::set_var(name, non_utf8_env_value());
                assert!(
                    telemetry_disabled(),
                    "{name}=<non-UTF8> should disable telemetry"
                );
                env::remove_var(name);
            }
        }
    }

    #[cfg(unix)]
    #[test]
    fn telemetry_id_does_not_read_or_overwrite_a_symlink() {
        let temp_dir = env::temp_dir().join(format!(
            "rustwright-telemetry-symlink-test-{}",
            generate_telemetry_id()
        ));
        fs::create_dir_all(&temp_dir).unwrap();
        let target = temp_dir.join("target");
        let path = temp_dir.join("telemetry_id");
        let target_id = "0123456789abcdef0123456789abcdef";
        fs::write(&target, target_id).unwrap();
        symlink(&target, &path).unwrap();

        let actual = load_or_create_telemetry_id_at(&path);

        assert_ne!(actual, target_id);
        assert_eq!(fs::read_to_string(&target).unwrap(), target_id);
        assert!(fs::symlink_metadata(&path)
            .unwrap()
            .file_type()
            .is_symlink());

        fs::remove_file(&path).unwrap();
        fs::remove_file(&target).unwrap();
        fs::remove_dir(&temp_dir).unwrap();
    }

    #[test]
    fn telemetry_id_create_new_rereads_concurrent_winner() {
        let temp_dir = env::temp_dir().join(format!(
            "rustwright-telemetry-race-test-{}",
            generate_telemetry_id()
        ));
        fs::create_dir_all(&temp_dir).unwrap();
        let path = temp_dir.join("telemetry_id");
        let winner_id = "0123456789abcdef0123456789abcdef";
        let generated_id = "fedcba9876543210fedcba9876543210";

        // The file appearing before this create_new call models another process
        // winning after the caller's earlier metadata check.
        fs::write(&path, winner_id).unwrap();

        let actual = create_telemetry_id_file(&path, generated_id.to_string());

        assert_eq!(actual, winner_id);
        assert_eq!(fs::read_to_string(&path).unwrap(), winner_id);

        fs::remove_file(&path).unwrap();
        fs::remove_dir(&temp_dir).unwrap();
    }

    #[test]
    fn telemetry_id_write_failure_removes_partial_file_for_repair() {
        let temp_dir = env::temp_dir().join(format!(
            "rustwright-telemetry-write-failure-test-{}",
            generate_telemetry_id()
        ));
        fs::create_dir_all(&temp_dir).unwrap();
        let path = temp_dir.join("telemetry_id");
        let generated_id = "0123456789abcdef0123456789abcdef";

        let actual =
            create_telemetry_id_file_with(&path, generated_id.to_string(), |file, contents| {
                file.write_all(&contents[..8])?;
                Err(std::io::Error::new(
                    std::io::ErrorKind::Other,
                    "injected write failure",
                ))
            });

        assert_eq!(actual, generated_id);
        assert!(!path.exists(), "partial telemetry id should be removed");

        let repaired = load_or_create_telemetry_id_at(&path);
        assert_eq!(valid_telemetry_id(&repaired), Some(repaired.as_str()));
        assert_eq!(fs::read_to_string(&path).unwrap(), repaired);

        fs::remove_file(&path).unwrap();
        fs::remove_dir(&temp_dir).unwrap();
    }

    #[cfg(unix)]
    fn non_utf8_env_value() -> OsString {
        OsString::from_vec(vec![0xff])
    }

    #[cfg(windows)]
    fn non_utf8_env_value() -> OsString {
        OsString::from_wide(&[0xd800])
    }

    #[test]
    fn engine_launched_payload_has_only_the_expected_fields() {
        let payload = serde_json::to_value(engine_launched_payload("anonymous-id".to_string()))
            .expect("telemetry payload should serialize");

        assert_eq!(
            payload,
            serde_json::json!({
                "api_key": POSTHOG_API_KEY,
                "event": "engine_launched",
                "distinct_id": "anonymous-id",
                "properties": {
                    "rustwright_version": env!("CARGO_PKG_VERSION"),
                    "os": env::consts::OS,
                    "arch": env::consts::ARCH,
                    "$process_person_profile": false,
                    "$geoip_disable": true,
                },
            })
        );
    }
}
