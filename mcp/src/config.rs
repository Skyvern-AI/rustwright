use std::env;

const DEFAULT_MAX_RESPONSE_BYTES: usize = 9 * 1024;
const DEFAULT_MAX_RESPONSE_LINES: usize = 200;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct FeatureConfig {
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

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) struct ResponseBudget {
    pub(crate) max_bytes: Option<usize>,
    pub(crate) max_lines: Option<usize>,
}

impl FeatureConfig {
    pub(crate) fn from_env() -> Self {
        Self {
            byte_override: dimension("RUSTWRIGHT_MCP_MAX_RESPONSE_BYTES", 4096),
            line_override: dimension("RUSTWRIGHT_MCP_MAX_RESPONSE_LINES", 16),
        }
    }

    pub(crate) fn response_budget(&self) -> ResponseBudget {
        ResponseBudget {
            max_bytes: resolve(&self.byte_override, DEFAULT_MAX_RESPONSE_BYTES),
            max_lines: resolve(&self.line_override, DEFAULT_MAX_RESPONSE_LINES),
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
                    "rustwright_mcp_config_warning variable={name} invalid_or_too_small=true fallback=default"
                );
                Override::Invalid
            }
        },
        Err(env::VarError::NotUnicode(_)) => {
            eprintln!(
                "rustwright_mcp_config_warning variable={name} invalid_unicode=true fallback=default"
            );
            Override::Invalid
        }
    }
}

fn resolve(value: &Override, default: usize) -> Option<usize> {
    match value {
        Override::Valid(0) => None,
        Override::Valid(value) => Some(*value),
        Override::Absent | Override::Invalid => Some(default),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_profile_applies_to_every_client() {
        assert_eq!(
            FeatureConfig::default().response_budget(),
            ResponseBudget {
                max_bytes: Some(9 * 1024),
                max_lines: Some(200),
            }
        );
    }

    #[test]
    fn overrides_are_independent_and_zero_disables_a_dimension() {
        let config = FeatureConfig {
            byte_override: Override::Valid(4096),
            line_override: Override::Valid(0),
        };
        assert_eq!(
            config.response_budget(),
            ResponseBudget {
                max_bytes: Some(4096),
                max_lines: None,
            }
        );
    }

    #[test]
    fn invalid_dimensions_fall_back_to_defaults() {
        let config = FeatureConfig {
            byte_override: Override::Invalid,
            line_override: Override::Invalid,
        };
        assert_eq!(
            config.response_budget(),
            ResponseBudget {
                max_bytes: Some(9 * 1024),
                max_lines: Some(200),
            }
        );
    }
}
