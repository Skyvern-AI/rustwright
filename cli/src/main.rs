use std::path::PathBuf;

use std::io::{BufRead, Read};

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};
use rustwright_cli::daemon::{self, ensure_daemon, request_existing, CommandResponse};
use rustwright_cli::session::{BrowserAction, LaunchConfig};
use serde_json::{json, Value};

#[derive(Debug, Parser)]
#[command(
    name = "rustwright-cli",
    version,
    about = "Persistent browser automation CLI powered by Rustwright"
)]
struct Cli {
    /// Browser session name used by persistent CLI commands.
    #[arg(long, global = true)]
    session: Option<String>,

    /// Emit a machine-readable command response.
    #[arg(long, global = true)]
    json: bool,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Launch Chromium and optionally navigate to a URL.
    Open {
        url: Option<String>,
        #[arg(long)]
        headed: bool,
        #[arg(long)]
        executable_path: Option<String>,
    },
    /// Print a compact page snapshot with reusable @eN references.
    Snapshot {
        #[arg(long, default_value_t = 200)]
        max_items: usize,
    },
    /// Click a snapshot reference or selector.
    Click { target: String },
    /// Replace the value of an input selected by reference or selector.
    Fill { target: String, text: String },
    /// Read text from a reference, selector, or the page body.
    Text { target: Option<String> },
    /// Read the current page title.
    Title,
    /// Read the current page URL.
    Url,
    /// Evaluate JavaScript in the current page.
    Eval {
        #[arg(allow_hyphen_values = true)]
        expression: String,
    },
    /// Capture a PNG screenshot.
    Screenshot {
        #[arg(default_value = "screenshot.png")]
        path: PathBuf,
        #[arg(long)]
        full_page: bool,
    },
    /// Wait before the next command.
    Wait {
        #[arg(default_value_t = 1_000)]
        milliseconds: u64,
    },
    /// Report whether this CLI session has a running daemon.
    Status,
    /// Close Chromium and stop this CLI session daemon.
    Close,
    #[command(name = "__daemon", hide = true)]
    Daemon {
        #[arg(long)]
        session: String,
        #[arg(long)]
        headed: bool,
        #[arg(long)]
        executable_path: Option<String>,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let machine_readable = cli.json;
    match run(cli) {
        Ok(()) => Ok(()),
        Err(error) if machine_readable => {
            println!(
                "{}",
                serde_json::to_string(&CommandResponse {
                    success: false,
                    data: None,
                    error: Some(format!("{error:#}")),
                })?
            );
            std::process::exit(1);
        }
        Err(error) => Err(error),
    }
}

fn run(cli: Cli) -> Result<()> {
    let session = cli
        .session
        .or_else(|| std::env::var("RUSTWRIGHT_SESSION").ok())
        .unwrap_or_else(|| "default".to_string());

    match cli.command {
        Commands::Daemon {
            session,
            headed,
            executable_path,
        } => {
            let mut token = String::new();
            std::io::stdin()
                .lock()
                .take(65)
                .read_line(&mut token)
                .context("failed to read browser daemon token")?;
            let token = token.trim_end();
            if token.len() != 32 || !token.chars().all(|character| character.is_ascii_hexdigit()) {
                bail!("browser daemon token was invalid");
            }
            daemon::run_daemon(
                &session,
                token.to_string(),
                LaunchConfig {
                    headed,
                    executable_path,
                },
            )?;
        }
        Commands::Status => {
            let response =
                request_existing(&session, BrowserAction::Status)?.unwrap_or_else(|| {
                    CommandResponse {
                        success: true,
                        data: Some(
                            json!({ "running": false, "launch_failed": false, "url": null }),
                        ),
                        error: None,
                    }
                });
            print_response(response, cli.json)?;
        }
        Commands::Close => {
            let response = request_existing(&session, BrowserAction::Close)?.unwrap_or_else(|| {
                CommandResponse {
                    success: true,
                    data: Some(json!({ "closed": true, "already_stopped": true })),
                    error: None,
                }
            });
            print_response(response, cli.json)?;
        }
        command => {
            let (action, launch) = command_action(command)?;
            let connection = ensure_daemon(&session, launch)?;
            print_response(connection.request(action)?, cli.json)?;
        }
    }
    Ok(())
}

fn command_action(command: Commands) -> Result<(BrowserAction, LaunchConfig)> {
    let mut launch = LaunchConfig::default();
    let action = match command {
        Commands::Open {
            url,
            headed,
            executable_path,
        } => {
            launch = LaunchConfig {
                headed,
                executable_path,
            };
            BrowserAction::Open { url }
        }
        Commands::Snapshot { max_items } => BrowserAction::Snapshot {
            max_items: Some(max_items),
        },
        Commands::Click { target } => BrowserAction::Click { target },
        Commands::Fill { target, text } => BrowserAction::Fill { target, text },
        Commands::Text { target } => BrowserAction::Text { target },
        Commands::Title => BrowserAction::Title,
        Commands::Url => BrowserAction::Url,
        Commands::Eval { expression } => BrowserAction::Evaluate { expression },
        Commands::Screenshot { path, full_page } => BrowserAction::Screenshot {
            path: if path.is_absolute() {
                path
            } else {
                std::env::current_dir()
                    .context("failed to resolve screenshot path")?
                    .join(path)
            }
            .to_string_lossy()
            .to_string(),
            full_page,
        },
        Commands::Wait { milliseconds } => {
            if milliseconds > 120_000 {
                bail!("milliseconds must not exceed 120000");
            }
            BrowserAction::Wait { milliseconds }
        }
        Commands::Status | Commands::Close | Commands::Daemon { .. } => {
            bail!("command cannot be sent to the browser daemon")
        }
    };
    Ok((action, launch))
}

fn print_response(response: CommandResponse, machine_readable: bool) -> Result<()> {
    if machine_readable {
        println!("{}", serde_json::to_string(&response)?);
        if !response.success {
            std::process::exit(1);
        }
        return Ok(());
    }

    if !response.success {
        bail!(
            "{}",
            response
                .error
                .unwrap_or_else(|| "browser command failed".to_string())
        );
    }
    let Some(data) = response.data else {
        return Ok(());
    };
    if let Some(snapshot) = data.get("snapshot").and_then(Value::as_str) {
        println!("{snapshot}");
    } else if let Some(text) = data.get("text").and_then(Value::as_str) {
        println!("{text}");
    } else if let Some(title) = data.get("title").and_then(Value::as_str) {
        println!("{title}");
    } else if let Some(url) = data.get("url").and_then(Value::as_str) {
        println!("{url}");
    } else if let Some(value) = data.get("value") {
        println!("{}", serde_json::to_string_pretty(value)?);
    } else {
        println!("{}", serde_json::to_string_pretty(&data)?);
    }
    Ok(())
}
