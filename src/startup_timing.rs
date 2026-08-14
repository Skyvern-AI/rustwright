use std::collections::hash_map::RandomState;
use std::env;
use std::fs::OpenOptions;
use std::future::Future;
use std::hash::BuildHasher;
use std::io::{self, Write};
#[cfg(unix)]
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::process;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;

const STARTUP_TIMING_ENV: &str = "RUSTWRIGHT_STARTUP_TIMING_FILE";

static PROCESS_IDENTITY: LazyLock<ProcessIdentity> = LazyLock::new(|| ProcessIdentity {
    epoch: Instant::now(),
    startup_id: random_startup_id(),
});
static PROCESS_WRITE_LOCK: Mutex<()> = Mutex::new(());

struct ProcessIdentity {
    epoch: Instant,
    startup_id: String,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum EntryPoint {
    PythonSync,
    PythonAsync,
    RustNative,
    Unknown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Phase {
    RuntimeCreate,
    LaunchPrepare,
    ProcessToEndpoint,
    TransportConnect,
    ServiceWorkerAutoAttach,
    BrowserReturn,
    ContextCreate,
    TargetCreate,
    TargetAttach,
    PageDomains,
    PageOptionalAttach,
    PageStealth,
    PageIframeAttach,
    PageStateAndFrameTree,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum ParentPhase {
    BrowserLaunch,
    PageCreate,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Status {
    Ok,
    Error,
    Skipped,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Transport {
    Websocket,
    Pipe,
    None,
}

impl Transport {
    const fn as_u8(self) -> u8 {
        match self {
            Self::None => 0,
            Self::Websocket => 1,
            Self::Pipe => 2,
        }
    }

    const fn from_u8(value: u8) -> Self {
        match value {
            1 => Self::Websocket,
            2 => Self::Pipe,
            _ => Self::None,
        }
    }
}

#[derive(Serialize)]
struct PhaseRecord<'a> {
    schema_version: u8,
    startup_id: &'a str,
    pid: u32,
    entrypoint: EntryPoint,
    phase: Phase,
    parent_phase: Option<ParentPhase>,
    start_offset_ns: u128,
    duration_ns: u128,
    status: Status,
    transport: Transport,
}

impl<'a> PhaseRecord<'a> {
    #[allow(clippy::too_many_arguments)]
    fn new(
        startup_id: &'a str,
        pid: u32,
        entrypoint: EntryPoint,
        phase: Phase,
        parent_phase: Option<ParentPhase>,
        start_offset_ns: u128,
        duration_ns: u128,
        status: Status,
        transport: Transport,
    ) -> Self {
        Self {
            schema_version: 1,
            startup_id,
            pid,
            entrypoint,
            phase,
            parent_phase,
            start_offset_ns,
            duration_ns,
            status,
            transport,
        }
    }
}

enum Destination {
    Stderr,
    File(PathBuf),
}

fn write_record(destination: &Destination, line: &[u8]) {
    match destination {
        Destination::Stderr => {
            let stderr = io::stderr();
            let mut stderr = stderr.lock();
            let _ = stderr.write_all(line);
        }
        Destination::File(path) => {
            let mut options = OpenOptions::new();
            options.create(true).append(true);
            #[cfg(unix)]
            options.custom_flags(libc::O_NONBLOCK);
            let Ok(mut file) = options.open(path) else {
                return;
            };
            let Ok(metadata) = file.metadata() else {
                return;
            };
            if !metadata.file_type().is_file() {
                return;
            }
            let _ = file.write_all(line);
        }
    }
}

pub(crate) struct StartupProbe {
    identity: &'static ProcessIdentity,
    destination: Destination,
    entrypoint: EntryPoint,
    transport: AtomicU8,
}

impl StartupProbe {
    fn new(destination: Destination, entrypoint: EntryPoint) -> Self {
        Self {
            identity: &PROCESS_IDENTITY,
            destination,
            entrypoint,
            transport: AtomicU8::new(Transport::None.as_u8()),
        }
    }

    /// Read the opt-in destination once for this launch call.
    pub(crate) fn from_env(entrypoint: EntryPoint) -> Option<Arc<Self>> {
        let destination = env::var_os(STARTUP_TIMING_ENV)?;
        let destination = if destination == "-" {
            Destination::Stderr
        } else {
            Destination::File(PathBuf::from(destination))
        };
        Some(Arc::new(Self::new(destination, entrypoint)))
    }

    pub(crate) fn set_transport(&self, transport: Transport) {
        self.transport.store(transport.as_u8(), Ordering::Relaxed);
    }

    pub(crate) fn record(
        &self,
        phase: Phase,
        parent_phase: Option<ParentPhase>,
        started: Instant,
        duration: Duration,
        status: Status,
    ) {
        let record = PhaseRecord::new(
            &self.identity.startup_id,
            process::id(),
            self.entrypoint,
            phase,
            parent_phase,
            started
                .saturating_duration_since(self.identity.epoch)
                .as_nanos(),
            duration.as_nanos(),
            status,
            Transport::from_u8(self.transport.load(Ordering::Relaxed)),
        );
        let Some(line) = format_json_line(&record) else {
            return;
        };
        let Ok(_guard) = PROCESS_WRITE_LOCK.lock() else {
            return;
        };
        write_record(&self.destination, &line);
    }
}

fn random_startup_id() -> String {
    let pid = process::id();
    let high = RandomState::new().hash_one((pid, 0_u8));
    let low = RandomState::new().hash_one((pid, 1_u8));
    format!("{high:016x}{low:016x}")
}

fn format_json_line(record: &PhaseRecord<'_>) -> Option<Vec<u8>> {
    let mut line = serde_json::to_vec(record).ok()?;
    line.push(b'\n');
    Some(line)
}

pub(crate) fn measure_phase<T, E>(
    probe: Option<&StartupProbe>,
    phase: Phase,
    parent_phase: Option<ParentPhase>,
    operation: impl FnOnce() -> Result<T, E>,
) -> Result<T, E> {
    match probe {
        None => operation(),
        Some(probe) => {
            let started = Instant::now();
            let result = operation();
            let status = if result.is_ok() {
                Status::Ok
            } else {
                Status::Error
            };
            probe.record(phase, parent_phase, started, started.elapsed(), status);
            result
        }
    }
}

pub(crate) async fn measure_phase_async<T, E, F>(
    probe: Option<&StartupProbe>,
    phase: Phase,
    parent_phase: Option<ParentPhase>,
    operation: F,
) -> Result<T, E>
where
    F: Future<Output = Result<T, E>>,
{
    match probe {
        None => operation.await,
        Some(probe) => {
            let started = Instant::now();
            let result = operation.await;
            let status = if result.is_ok() {
                Status::Ok
            } else {
                Status::Error
            };
            probe.record(phase, parent_phase, started, started.elapsed(), status);
            result
        }
    }
}

pub(crate) fn record_skipped(
    probe: Option<&StartupProbe>,
    phase: Phase,
    parent_phase: Option<ParentPhase>,
) {
    if let Some(probe) = probe {
        probe.record(
            phase,
            parent_phase,
            Instant::now(),
            Duration::ZERO,
            Status::Skipped,
        );
    }
}

struct ActivePhaseSpan<'a> {
    probe: &'a StartupProbe,
    phase: Phase,
    parent_phase: Option<ParentPhase>,
    started: Instant,
}

pub(crate) struct PhaseSpan<'a> {
    active: Option<ActivePhaseSpan<'a>>,
}

impl<'a> PhaseSpan<'a> {
    pub(crate) fn new(
        probe: Option<&'a StartupProbe>,
        phase: Phase,
        parent_phase: Option<ParentPhase>,
    ) -> Self {
        Self {
            active: probe.map(|probe| ActivePhaseSpan {
                probe,
                phase,
                parent_phase,
                started: Instant::now(),
            }),
        }
    }

    pub(crate) fn transition(&mut self, phase: Phase) {
        let Some(active) = self.active.as_mut() else {
            return;
        };
        if active.phase == phase {
            return;
        }
        active.probe.record(
            active.phase,
            active.parent_phase,
            active.started,
            active.started.elapsed(),
            Status::Ok,
        );
        active.phase = phase;
        active.started = Instant::now();
    }

    pub(crate) fn finish(mut self, status: Status) {
        if let Some(active) = self.active.take() {
            active.probe.record(
                active.phase,
                active.parent_phase,
                active.started,
                active.started.elapsed(),
                status,
            );
        }
    }
}

impl Drop for PhaseSpan<'_> {
    fn drop(&mut self) {
        if let Some(active) = self.active.take() {
            active.probe.record(
                active.phase,
                active.parent_phase,
                active.started,
                active.started.elapsed(),
                Status::Error,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    #[test]
    fn startup_phase_json_line_escapes_string_fields() {
        let record = PhaseRecord::new(
            "id\"with\ncontrol",
            7,
            EntryPoint::Unknown,
            Phase::RuntimeCreate,
            None,
            11,
            13,
            Status::Ok,
            Transport::None,
        );
        let line = format_json_line(&record).expect("record serialization must succeed");
        assert_eq!(line.last(), Some(&b'\n'));
        let value: Value = serde_json::from_slice(&line).expect("JSON line must parse");
        assert_eq!(value["startup_id"], "id\"with\ncontrol");
    }

    #[test]
    fn startup_phase_record_contains_pinned_fields() {
        let record = PhaseRecord::new(
            "0123456789abcdef",
            42,
            EntryPoint::PythonSync,
            Phase::TransportConnect,
            Some(ParentPhase::BrowserLaunch),
            100,
            25,
            Status::Ok,
            Transport::Websocket,
        );
        let value = serde_json::to_value(record).expect("record serialization must succeed");
        assert_eq!(value["schema_version"], 1);
        assert_eq!(value["entrypoint"], "python-sync");
        assert_eq!(value["phase"], "transport_connect");
        assert_eq!(value["parent_phase"], "browser_launch");
        assert_eq!(value["start_offset_ns"], 100);
        assert_eq!(value["duration_ns"], 25);
        assert_eq!(value["status"], "ok");
        assert_eq!(value["transport"], "websocket");
    }

    #[test]
    fn disabled_probe_short_circuits_to_operation() {
        let mut calls = 0;
        let result: Result<u8, ()> = measure_phase(
            None,
            Phase::RuntimeCreate,
            Some(ParentPhase::BrowserLaunch),
            || {
                calls += 1;
                Ok(9)
            },
        );
        assert_eq!(result, Ok(9));
        assert_eq!(calls, 1);
    }

    #[tokio::test]
    async fn disabled_probe_short_circuits_to_async_operation() {
        let mut calls = 0;
        let result: Result<u8, ()> = measure_phase_async(
            None,
            Phase::RuntimeCreate,
            Some(ParentPhase::BrowserLaunch),
            async {
                calls += 1;
                Ok(9)
            },
        )
        .await;
        assert_eq!(result, Ok(9));
        assert_eq!(calls, 1);
    }

    #[test]
    fn disabled_span_stays_inactive_across_boundaries() {
        let mut span = PhaseSpan::new(None, Phase::LaunchPrepare, Some(ParentPhase::BrowserLaunch));
        assert!(span.active.is_none());
        span.transition(Phase::ProcessToEndpoint);
        assert!(span.active.is_none());
        span.finish(Status::Ok);
    }

    #[test]
    fn skipped_context_record_has_zero_duration() {
        let directory = tempfile::tempdir().expect("temporary directory must be created");
        let path = directory.path().join("startup-timing.jsonl");
        let probe = StartupProbe::new(Destination::File(path.clone()), EntryPoint::RustNative);
        probe.set_transport(Transport::Pipe);

        record_skipped(
            Some(&probe),
            Phase::ContextCreate,
            Some(ParentPhase::PageCreate),
        );

        let line = std::fs::read(&path).expect("timing line must be readable");
        assert_eq!(line.iter().filter(|byte| **byte == b'\n').count(), 1);
        let value: Value = serde_json::from_slice(&line).expect("timing line must parse");
        assert_eq!(value["phase"], "context_create");
        assert_eq!(value["duration_ns"], 0);
        assert_eq!(value["status"], "skipped");
        assert_eq!(value["parent_phase"], "page_create");
        assert_eq!(value["transport"], "pipe");
    }
}
