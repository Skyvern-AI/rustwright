use std::env;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct FeatureConfig {
    pub(crate) budget: bool,
    pub(crate) distill: bool,
    pub(crate) header: bool,
    pub(crate) console_dedup: bool,
    pub(crate) net_note: bool,
    lean_descriptions: ProfileFlag,
    byte_override: Override,
    line_override: Override,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
enum Override {
    #[default]
    Absent,
    Valid(usize),
    Invalid,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
enum ProfileFlag {
    #[default]
    ProfileDefault,
    On,
    Off,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct ResponseBudget {
    pub(crate) max_bytes: Option<usize>,
    pub(crate) max_lines: Option<usize>,
}

impl FeatureConfig {
    pub(crate) fn from_env() -> Self {
        Self {
            budget: flag("RUSTWRIGHT_MCP_BUDGET"),
            distill: flag("RUSTWRIGHT_MCP_DISTILL"),
            header: flag("RUSTWRIGHT_MCP_HEADER"),
            console_dedup: flag("RUSTWRIGHT_MCP_CONSOLE_DEDUP"),
            net_note: flag("RUSTWRIGHT_MCP_NET_NOTE"),
            lean_descriptions: profile_flag("RUSTWRIGHT_MCP_LEAN_DESCRIPTIONS"),
            byte_override: dimension("RUSTWRIGHT_MCP_MAX_RESPONSE_BYTES", 4096),
            line_override: dimension("RUSTWRIGHT_MCP_MAX_RESPONSE_LINES", 16),
        }
    }

    pub(crate) fn response_budget(&self, client_name: Option<&str>) -> ResponseBudget {
        if !self.budget {
            return ResponseBudget::default();
        }
        let codex = client_name.is_some_and(is_codex_client);
        let profile = codex.then_some(ResponseBudget {
            max_bytes: Some(9 * 1024),
            max_lines: Some(200),
        });
        ResponseBudget {
            max_bytes: resolve(&self.byte_override, profile.and_then(|p| p.max_bytes)),
            max_lines: resolve(&self.line_override, profile.and_then(|p| p.max_lines)),
        }
    }

    pub(crate) fn lean_descriptions(&self, client_name: Option<&str>) -> bool {
        match self.lean_descriptions {
            ProfileFlag::On => true,
            ProfileFlag::Off => false,
            ProfileFlag::ProfileDefault => client_name.is_some_and(is_codex_client),
        }
    }
}

fn flag(name: &str) -> bool {
    match env::var(name) {
        Err(env::VarError::NotPresent) => false,
        Ok(value) if value.eq_ignore_ascii_case("on") => true,
        Ok(value) if value.eq_ignore_ascii_case("off") => false,
        Ok(_) | Err(env::VarError::NotUnicode(_)) => {
            eprintln!(
                "rustwright_mcp_config_warning variable={name} expected=on_or_off fallback=off"
            );
            false
        }
    }
}

fn profile_flag(name: &str) -> ProfileFlag {
    match env::var(name) {
        Err(env::VarError::NotPresent) => ProfileFlag::ProfileDefault,
        Ok(value) if value.eq_ignore_ascii_case("on") => ProfileFlag::On,
        Ok(value) if value.eq_ignore_ascii_case("off") => ProfileFlag::Off,
        Ok(_) | Err(env::VarError::NotUnicode(_)) => {
            eprintln!(
                "rustwright_mcp_config_warning variable={name} expected=on_or_off fallback=profile_default"
            );
            ProfileFlag::ProfileDefault
        }
    }
}

fn dimension(name: &str, minimum: usize) -> Override {
    match env::var(name) {
        Err(env::VarError::NotPresent) => Override::Absent,
        Ok(value) => match value.parse::<usize>() {
            Ok(0) => Override::Valid(0),
            Ok(parsed) if parsed >= minimum => Override::Valid(parsed),
            _ => {
                eprintln!(
                    "rustwright_mcp_config_warning variable={name} invalid_or_too_small=true fallback=profile_default"
                );
                Override::Invalid
            }
        },
        Err(env::VarError::NotUnicode(_)) => {
            eprintln!(
                "rustwright_mcp_config_warning variable={name} invalid_unicode=true fallback=profile_default"
            );
            Override::Invalid
        }
    }
}

fn resolve(value: &Override, profile: Option<usize>) -> Option<usize> {
    match value {
        Override::Valid(0) => None,
        Override::Valid(value) => Some(*value),
        Override::Absent | Override::Invalid => profile,
    }
}

pub(crate) fn is_codex_client(name: &str) -> bool {
    let name = name.to_ascii_lowercase();
    name == "codex" || name == "codex-mcp-client" || name.starts_with("codex-")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    const CODEX_INITIALIZE_FIXTURE: &str =
        include_str!("../tests/fixtures/codex-mcp-initialize-0.146.0.json");

    #[test]
    fn client_matching_is_exact_except_documented_prefix() {
        for name in ["codex", "CODEX-MCP-CLIENT", "CoDeX-cli"] {
            assert!(is_codex_client(name), "{name}");
        }
        for name in ["codexish", "my-codex", "codex_mcp_client", ""] {
            assert!(!is_codex_client(name), "{name}");
        }
    }

    #[test]
    fn missing_unknown_and_supported_profiles_resolve_as_documented() {
        let config = FeatureConfig {
            budget: true,
            ..FeatureConfig::default()
        };
        assert_eq!(config.response_budget(None), ResponseBudget::default());
        assert_eq!(
            config.response_budget(Some("other")),
            ResponseBudget::default()
        );
        assert_eq!(
            config.response_budget(Some("codex")),
            ResponseBudget {
                max_bytes: Some(9216),
                max_lines: Some(200)
            }
        );
    }

    #[test]
    fn overrides_are_independent_and_zero_disables_a_dimension() {
        let config = FeatureConfig {
            budget: true,
            byte_override: Override::Valid(4096),
            line_override: Override::Valid(0),
            ..FeatureConfig::default()
        };
        assert_eq!(
            config.response_budget(Some("unknown")),
            ResponseBudget {
                max_bytes: Some(4096),
                max_lines: None
            }
        );
    }

    #[test]
    fn invalid_dimensions_fall_back_to_profile_defaults() {
        let config = FeatureConfig {
            budget: true,
            byte_override: Override::Invalid,
            line_override: Override::Invalid,
            ..FeatureConfig::default()
        };
        assert_eq!(
            config.response_budget(Some("codex-mcp-client")),
            ResponseBudget {
                max_bytes: Some(9216),
                max_lines: Some(200)
            }
        );
        assert_eq!(
            config.response_budget(Some("unknown")),
            ResponseBudget::default()
        );
    }

    #[test]
    fn captured_codex_initialize_client_selects_budget_profile() {
        let fixture: Value =
            serde_json::from_str(CODEX_INITIALIZE_FIXTURE).expect("fixture must be valid JSON");
        assert_eq!(fixture["schema_version"], 1);
        assert_eq!(fixture["captured_at_utc"], "2026-08-06");
        assert_eq!(fixture["codex_cli_version"], "0.146.0");
        assert_eq!(
            fixture["codex_js_launcher_sha256"],
            "134063e133f0b4244fa3b251acf973d4fe4b4aeeacbdc135211bf480f59f1477"
        );
        assert_eq!(
            fixture["codex_native_binary_sha256"],
            "ae1d3ffe6d48aec6a4dc3f50e7eb8e0d11962485a6a9406c5a7012139383da02"
        );
        assert_eq!(
            fixture["initialize_frame"],
            json!({
                "id": 0,
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "capabilities": {
                        "elicitation": {
                            "form": {},
                            "url": {}
                        }
                    },
                    "clientInfo": {
                        "name": "codex-mcp-client",
                        "title": "Codex",
                        "version": "0.146.0"
                    },
                    "protocolVersion": "2025-06-18"
                }
            })
        );

        let config = FeatureConfig {
            budget: true,
            ..FeatureConfig::default()
        };
        let client_name = fixture["initialize_frame"]["params"]["clientInfo"]["name"]
            .as_str()
            .expect("fixture client name must be a string");
        assert_eq!(
            config.response_budget(Some(client_name)),
            ResponseBudget {
                max_bytes: Some(9 * 1024),
                max_lines: Some(200)
            }
        );

        let mut near_match = fixture.clone();
        near_match["initialize_frame"]["params"]["clientInfo"]["name"] =
            Value::String("codex_mcp_client".to_owned());
        let near_match_name = near_match["initialize_frame"]["params"]["clientInfo"]["name"]
            .as_str()
            .expect("mutated client name must be a string");
        assert_eq!(
            config.response_budget(Some(near_match_name)),
            ResponseBudget::default()
        );
    }

    #[test]
    fn lean_description_override_precedes_client_profile() {
        let profile_default = FeatureConfig::default();
        assert!(profile_default.lean_descriptions(Some("codex-mcp-client")));
        assert!(!profile_default.lean_descriptions(Some("other")));
        assert!(!profile_default.lean_descriptions(None));

        let on = FeatureConfig {
            lean_descriptions: ProfileFlag::On,
            ..FeatureConfig::default()
        };
        assert!(on.lean_descriptions(Some("other")));

        let off = FeatureConfig {
            lean_descriptions: ProfileFlag::Off,
            ..FeatureConfig::default()
        };
        assert!(!off.lean_descriptions(Some("codex")));
    }
}
