use std::borrow::Cow;

use rmcp::{
    ErrorData,
    model::{CallToolResult, ContentBlock, RequestId, ServerJsonRpcMessage, ServerResult},
};

use crate::config::ResponseBudget;

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct NetworkSection {
    pub(crate) name: &'static str,
    pub(crate) payload: String,
    pub(crate) body_marker: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct SnapshotStructure {
    pub(crate) legacy: String,
    pub(crate) units: Vec<String>,
    pub(crate) head: Option<String>,
    pub(crate) renderer_incomplete: Option<String>,
    pub(crate) renderer_incomplete_index: Option<usize>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct TabEntry {
    pub(crate) index: usize,
    pub(crate) title: String,
    pub(crate) url: String,
    pub(crate) active: bool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct TabsStructure {
    pub(crate) entries: Vec<TabEntry>,
    pub(crate) active_index: Option<usize>,
    /// Populated only by an explicit `browser_tabs select` operation.
    pub(crate) selected_exact_url: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ModalRecovery {
    pub(crate) owner: &'static str,
    pub(crate) kind: String,
    pub(crate) message: String,
    pub(crate) instruction: &'static str,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct FindStructure {
    pub(crate) blocks: Vec<String>,
    pub(crate) actor_omitted: usize,
    pub(crate) incomplete: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) enum NetworkStructure {
    Detail {
        sections: Vec<NetworkSection>,
    },
    List {
        entries: Vec<String>,
        tail_notices: Vec<String>,
    },
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub(crate) struct ResponseShape {
    pub(crate) page: Option<String>,
    pub(crate) modal_recovery: Vec<ModalRecovery>,
    pub(crate) snapshot: Option<SnapshotStructure>,
    pub(crate) tabs: Option<TabsStructure>,
    pub(crate) find: Option<FindStructure>,
    pub(crate) network: Option<NetworkStructure>,
    pub(crate) result_prefix: Option<String>,
}

#[derive(Clone, Copy)]
enum ToolClass {
    Success,
    BrowserError,
}

impl ToolClass {
    fn is_error(self) -> bool {
        matches!(self, Self::BrowserError)
    }

    fn label(self) -> &'static str {
        match self {
            Self::Success => "tool_success",
            Self::BrowserError => "browser_error",
        }
    }
}

pub(crate) fn shape_tool_text(
    tool: &str,
    text: String,
    is_error: bool,
    id: &RequestId,
    budget: ResponseBudget,
) -> String {
    shape_tool_text_with_shape(tool, text, is_error, id, budget, None)
}

pub(crate) fn shape_tool_text_with_shape(
    tool: &str,
    text: String,
    is_error: bool,
    id: &RequestId,
    budget: ResponseBudget,
    shape: Option<&ResponseShape>,
) -> String {
    if budget.max_bytes.is_none() && budget.max_lines.is_none() {
        return text;
    }
    let class = if is_error {
        ToolClass::BrowserError
    } else {
        ToolClass::Success
    };
    // Modal recovery has a semantic position, not merely a budget priority. Normalize it
    // before the fit check so fitting, equal-boundary, and oversized replies agree.
    let text = if let Some(shape) = shape.filter(|shape| !shape.modal_recovery.is_empty()) {
        normalize_modal(&text, &shape.modal_recovery)
    } else {
        text
    };
    let has_selected_exact_url = shape
        .and_then(|shape| shape.tabs.as_ref())
        .is_some_and(|tabs| tabs.selected_exact_url.is_some());
    if !has_selected_exact_url && tool_fits(&text, id, class, budget) {
        return text;
    }

    let original_budget = budget;
    let page = shape.and_then(|shape| shape.page.as_deref());
    let (text, budget) = if let Some(page) = page {
        (
            remove_page_section(text, page),
            reserve_page_budget(budget, page, id, class),
        )
    } else {
        (text, budget)
    };

    let candidate = if let Some(shape) = shape.filter(|shape| !shape.modal_recovery.is_empty()) {
        shape_modal_composed(tool, &text, shape, id, class, budget)
    } else if let Some(NetworkStructure::Detail { sections }) =
        shape.and_then(|s| s.network.as_ref())
    {
        shape_network_detail(sections, id, class, budget)
    } else if let Some(NetworkStructure::List {
        entries,
        tail_notices,
    }) = shape.and_then(|s| s.network.as_ref())
    {
        shape_network_list(entries, tail_notices, id, class, budget)
    } else if let Some(tabs) = shape.and_then(|s| s.tabs.as_ref()) {
        shape_tabs_structured(
            &text,
            tabs,
            shape.and_then(|s| s.snapshot.as_ref()),
            id,
            class,
            budget,
        )
    } else if let Some(find) = shape.and_then(|s| s.find.as_ref()) {
        shape_find_structured(find, id, class, budget)
    } else if let Some(snapshot) = shape.and_then(|s| s.snapshot.as_ref()) {
        if tool == "browser_snapshot" {
            shape_snapshot_structured(snapshot, id, class, budget)
        } else if tool == "browser_evaluate" {
            shape_evaluate_structured(
                shape
                    .and_then(|s| s.result_prefix.as_deref())
                    .unwrap_or(&text),
                snapshot,
                id,
                class,
                budget,
            )
        } else {
            shape_post_action_structured(
                shape
                    .and_then(|s| s.result_prefix.as_deref())
                    .unwrap_or(&text),
                snapshot,
                id,
                class,
                budget,
            )
        }
    } else {
        match tool {
            "browser_snapshot" | "browser_find" | "browser_tabs" => {
                shape_generic(&text, id, class, budget)
            }
            // These actor surfaces are shaped only from unforgeable structured metadata.
            // A missing shape must never cause rendered page/body text to be reparsed.
            "browser_network_request" | "browser_network_requests" => {
                shape_generic(&text, id, class, budget)
            }
            _ => shape_generic(&text, id, class, budget),
        }
    };
    let candidate = candidate.map(|candidate| compose_page(candidate, page, shape));
    finalize_tool(candidate, id, class, original_budget)
}

fn remove_page_section(text: String, page: &str) -> String {
    let prefix = format!("{page}\n\n");
    if let Some(body) = text.strip_prefix(&prefix) {
        return body.to_owned();
    }
    let embedded = format!("\n{page}\n\n");
    if let Some(index) = text.find(&embedded) {
        let mut body = text;
        body.replace_range(index..index + embedded.len(), "\n");
        return body;
    }
    text
}

fn reserve_page_budget(
    budget: ResponseBudget,
    page: &str,
    id: &RequestId,
    class: ToolClass,
) -> ResponseBudget {
    let with_page = tool_wire(&format!("{page}\n\nx"), id, class);
    let without_page = tool_wire("x", id, class);
    let byte_cost = with_page.saturating_sub(without_page);
    let line_cost = page.lines().count().saturating_add(1);
    ResponseBudget {
        max_bytes: budget
            .max_bytes
            .map(|limit| limit.saturating_sub(byte_cost)),
        max_lines: budget
            .max_lines
            .map(|limit| limit.saturating_sub(line_cost)),
    }
}

fn compose_page(candidate: String, page: Option<&str>, shape: Option<&ResponseShape>) -> String {
    let Some(page) = page else {
        return candidate;
    };
    if let Some(recovery) = shape.filter(|shape| !shape.modal_recovery.is_empty()) {
        let recovery = modal_legacy(&recovery.modal_recovery);
        if let Some(body) = candidate.strip_prefix(&format!("{recovery}\n")) {
            return format!("{recovery}\n{page}\n\n{body}");
        }
    }
    format!("{page}\n\n{candidate}")
}

pub(crate) fn shape_error(
    mut error: ErrorData,
    id: &RequestId,
    budget: ResponseBudget,
) -> ErrorData {
    if budget.max_bytes.is_none() && budget.max_lines.is_none() || error_fits(&error, id, budget) {
        return error;
    }
    let original = error.message.clone().into_owned();
    // ErrorData.data is optional context. Preserve the error code/class, but remove data before
    // shortening the required message so a large validation payload cannot defeat the envelope.
    error.data = None;
    let mut low = 0;
    let mut high = original.len();
    let mut best = None;
    while low <= high {
        let middle = low + (high - low) / 2;
        let cut = utf8_floor(&original, middle);
        let omitted = original.len() - cut;
        let message = if omitted == 0 {
            original.clone()
        } else {
            format!("{}… ({omitted} bytes omitted)", &original[..cut])
        };
        error.message = Cow::Owned(message);
        if error_fits(&error, id, budget) {
            best = Some(error.clone());
            low = middle.saturating_add(1);
        } else if middle == 0 {
            break;
        } else {
            high = middle - 1;
        }
    }
    if let Some(best) = best {
        return best;
    }
    error.message = Cow::Borrowed("");
    unavoidable("json_rpc_error", error_wire(&error, id), id, budget);
    error
}

fn finalize_tool(
    candidate: Option<String>,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> String {
    if let Some(candidate) = candidate.filter(|value| tool_fits(value, id, class, budget)) {
        return candidate;
    }
    unavoidable(class.label(), tool_wire("", id, class), id, budget);
    String::new()
}

fn unavoidable(class: &str, actual: usize, id: &RequestId, budget: ResponseBudget) {
    eprintln!(
        "wire_ceiling_unavoidable configured_bytes={:?} configured_lines={:?} actual_wire_bytes={} response_class={} serialized_request_id_length={}",
        budget.max_bytes,
        budget.max_lines,
        actual,
        class,
        serde_json::to_vec(id).map_or(0, |value| value.len())
    );
}

fn tool_wire(text: &str, id: &RequestId, class: ToolClass) -> usize {
    let result = if class.is_error() {
        CallToolResult::error(vec![ContentBlock::text(text.to_owned())])
    } else {
        CallToolResult::success(vec![ContentBlock::text(text.to_owned())])
    };
    let frame = ServerJsonRpcMessage::response(ServerResult::CallToolResult(result), id.clone());
    serde_json::to_vec(&frame).map_or(usize::MAX, |bytes| bytes.len() + 1)
}

fn error_wire(error: &ErrorData, id: &RequestId) -> usize {
    let frame = ServerJsonRpcMessage::error(error.clone(), Some(id.clone()));
    serde_json::to_vec(&frame).map_or(usize::MAX, |bytes| bytes.len() + 1)
}

fn tool_fits(text: &str, id: &RequestId, class: ToolClass, budget: ResponseBudget) -> bool {
    fits(text, tool_wire(text, id, class), budget)
}

fn error_fits(error: &ErrorData, id: &RequestId, budget: ResponseBudget) -> bool {
    fits(error.message.as_ref(), error_wire(error, id), budget)
}

fn fits(text: &str, wire: usize, budget: ResponseBudget) -> bool {
    budget.max_bytes.is_none_or(|limit| wire <= limit)
        && budget
            .max_lines
            .is_none_or(|limit| decoded_lines(text) <= limit)
}

fn decoded_lines(text: &str) -> usize {
    text.lines().count().max(1)
}

const MANDATORY_FIELD_BYTES: usize = 192;
const MAX_MODAL_RECORDS: usize = 4;

fn bounded_field(value: &str) -> String {
    let cut = utf8_floor(value, value.len().min(MANDATORY_FIELD_BYTES));
    let mut rendered = value[..cut]
        .chars()
        // Keep the mandatory renderer's serialized expansion bounded too: JSON would
        // otherwise double every quote and backslash after the local byte cap.
        .map(|ch| {
            if ch.is_control() || matches!(ch, '\\' | '"') {
                ' '
            } else {
                ch
            }
        })
        .collect::<String>();
    let omitted = value.len().saturating_sub(cut);
    if omitted > 0 {
        rendered.push_str(&format!("… [{omitted} bytes omitted]"));
    }
    rendered
}

fn modal_legacy(recovery: &[ModalRecovery]) -> String {
    recovery
        .iter()
        .map(|record| {
            if record.kind == "file chooser" {
                format!(
                    "- {}: File chooser pending: {}. {}",
                    record.owner, record.message, record.instruction
                )
            } else {
                format!(
                    "- {}: Dialog pending: type={}; message={:?}. {}",
                    record.owner, record.kind, record.message, record.instruction
                )
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn render_modal(recovery: &[ModalRecovery]) -> Vec<String> {
    let mut lines = recovery
        .iter()
        .take(MAX_MODAL_RECORDS)
        .map(|record| {
            format!(
                "- owner={}; kind={}; message={:?}; {}",
                record.owner,
                record.kind,
                bounded_field(&record.message),
                record.instruction
            )
        })
        .collect::<Vec<_>>();
    let omitted = recovery.len().saturating_sub(lines.len());
    if omitted > 0 {
        lines.push(format!(
            "[{omitted} additional modal recovery records omitted.]"
        ));
    }
    lines
}

fn render_tab(entry: &TabEntry) -> String {
    format!(
        "- index={}; title={:?}; url={:?}{}",
        entry.index,
        bounded_field(&entry.title),
        bounded_field(&entry.url),
        if entry.active { "; active=true" } else { "" }
    )
}

fn stable_url_hash(value: &str) -> u64 {
    value
        .as_bytes()
        .iter()
        .fold(0xcbf29ce484222325, |hash, byte| {
            (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3)
        })
}

fn selected_url_record(tabs: &TabsStructure, exact: bool) -> Option<String> {
    tabs.selected_exact_url.as_deref().map(|url| {
        if exact {
            format!(
                "Exact active URL JSON: {}",
                serde_json::to_string(url).expect("serializing a string cannot fail")
            )
        } else {
            format!(
                "[Exact active URL exceeds the client response ceiling: bytes={}; fnv1a64={:016x}; preview={:?}]",
                url.len(),
                stable_url_hash(url),
                bounded_field(url)
            )
        }
    })
}

fn shape_modal_composed(
    tool: &str,
    text: &str,
    shape: &ResponseShape,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    let recovery = modal_legacy(&shape.modal_recovery);
    let body = text.strip_prefix(&format!("{recovery}\n")).unwrap_or(text);
    let build = |prefix: &str, exact_url: bool| {
        let mut lines = render_modal(&shape.modal_recovery);
        if let Some(tabs) = &shape.tabs {
            lines.push("### Tabs".to_owned());
            if let Some(active) = tabs.active_index.and_then(|index| tabs.entries.get(index)) {
                lines.push(render_tab(active));
            }
            if let Some(record) = selected_url_record(tabs, exact_url) {
                lines.push(record);
            }
        }
        if let Some(snapshot) = &shape.snapshot {
            if tool != "browser_snapshot" {
                lines.push("### Result".to_owned());
                let result = shape.result_prefix.as_deref().unwrap_or(body);
                if tool == "browser_evaluate" {
                    let preview = bounded_field(result);
                    lines.push(
                        serde_json::json!({"truncated": true, "bytes": result.len(), "preview": preview})
                            .to_string(),
                    );
                } else {
                    lines.push(bounded_field(result));
                }
            }
            lines.push("### Snapshot".to_owned());
            if let Some(head) = &snapshot.head {
                lines.push(bounded_field(head));
            }
            if let Some(marker) = &snapshot.renderer_incomplete {
                if snapshot.head.as_ref() != Some(marker) {
                    lines.push(bounded_field(marker));
                }
            }
            let mandatory_indices = usize::from(snapshot.head.is_some())
                + usize::from(snapshot.renderer_incomplete.is_some());
            let omitted = snapshot.units.len().saturating_sub(mandatory_indices);
            if omitted > 0 {
                lines.push(format!("[{omitted} snapshot lines omitted.]"));
            }
        } else {
            let keep = complete_lines(prefix);
            if !keep.is_empty() {
                lines.push(keep.to_owned());
            }
            let omitted = body.len().saturating_sub(keep.len());
            lines.push(format!("[Response shortened; {omitted} bytes omitted.]"));
        }
        lines.join("\n")
    };
    maximize_prefix(body, id, class, budget, |prefix| build(prefix, true))
        .filter(|candidate| tool_fits(candidate, id, class, budget))
        .or_else(|| {
            shape.tabs.as_ref().and_then(|tabs| {
                tabs.selected_exact_url.as_ref().and_then(|_| {
                    maximize_prefix(body, id, class, budget, |prefix| build(prefix, false))
                })
            })
        })
}

fn normalize_modal(text: &str, recovery: &[ModalRecovery]) -> String {
    let recovery = modal_legacy(recovery);
    if recovery.is_empty() || text.starts_with(&format!("{recovery}\n")) {
        return text.to_owned();
    }
    let suffix = format!("\n\n### Modal\n{recovery}");
    let body = text.strip_suffix(&suffix).unwrap_or(text);
    if body.is_empty() {
        recovery
    } else {
        format!("{recovery}\n{body}")
    }
}

fn snapshot_parts(snapshot: &SnapshotStructure) -> (Vec<&str>, Vec<&str>) {
    let mut mandatory = Vec::new();
    if let Some(head) = snapshot.head.as_deref() {
        mandatory.push(head);
    }
    if let Some(incomplete) = snapshot.renderer_incomplete.as_deref()
        && !mandatory.contains(&incomplete)
    {
        mandatory.push(incomplete);
    }
    let optional = snapshot
        .units
        .iter()
        .map(String::as_str)
        .enumerate()
        .filter(|(index, unit)| {
            !(snapshot.head.as_deref() == Some(*unit) && *index == 0)
                && snapshot.renderer_incomplete_index != Some(*index)
        })
        .map(|(_, unit)| unit)
        .collect();
    (mandatory, optional)
}

fn build_snapshot_structured(snapshot: &SnapshotStructure, keep: usize) -> String {
    let (mandatory, optional) = snapshot_parts(snapshot);
    let kept = keep.min(optional.len());
    let omitted = optional.len().saturating_sub(kept);
    let mut output = mandatory.into_iter().map(bounded_field).collect::<Vec<_>>();
    output.extend(optional.into_iter().take(kept).map(str::to_owned));
    if omitted > 0 {
        output.push(format!("[{omitted} snapshot lines omitted. Possible narrower observations: browser_find, browser_get_text with a unique CSS selector, or a targeted browser_snapshot.]"));
    }
    output.join("\n")
}

fn shape_snapshot_structured(
    snapshot: &SnapshotStructure,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    let optional = snapshot_parts(snapshot).1.len();
    maximize_count(
        optional,
        |keep| build_snapshot_structured(snapshot, keep),
        id,
        class,
        budget,
    )
}

fn shape_post_action_structured(
    result: &str,
    snapshot: &SnapshotStructure,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    let result_units = result.lines().collect::<Vec<_>>();
    let snapshot_count = snapshot_parts(snapshot).1.len();
    maximize_count(
        result_units.len().saturating_add(snapshot_count),
        |keep| {
            let result_keep = keep.min(result_units.len());
            let snapshot_keep = keep.saturating_sub(result_units.len());
            let mut out = result_units
                .iter()
                .take(result_keep)
                .copied()
                .collect::<Vec<_>>()
                .join("\n");
            let omitted = result_units.len().saturating_sub(result_keep);
            if omitted > 0 {
                if !out.is_empty() {
                    out.push('\n');
                }
                out.push_str(&format!("[{omitted} result lines omitted.]"));
            }
            format!(
                "{out}\n\n### Snapshot\n{}",
                build_snapshot_structured(snapshot, snapshot_keep)
            )
        },
        id,
        class,
        budget,
    )
}

fn shape_evaluate_structured(
    value: &str,
    snapshot: &SnapshotStructure,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    let snapshot_count = snapshot_parts(snapshot).1.len();
    maximize_count(
        value.len().saturating_add(snapshot_count),
        |keep| {
            let preview_bytes = utf8_floor(value, keep.min(value.len()));
            let rendered = serde_json::json!({"truncated": true, "bytes": value.len(), "preview": &value[..preview_bytes]}).to_string();
            format!(
                "{rendered}\n\n### Snapshot\n{}",
                build_snapshot_structured(snapshot, keep.saturating_sub(value.len()))
            )
        },
        id,
        class,
        budget,
    )
}

fn shape_tabs_structured(
    text: &str,
    tabs: &TabsStructure,
    snapshot: Option<&SnapshotStructure>,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    let active = tabs.active_index.and_then(|index| tabs.entries.get(index));
    let optional = tabs
        .entries
        .iter()
        .enumerate()
        .filter(|(index, _)| Some(*index) != tabs.active_index)
        .map(|(_, entry)| entry)
        .collect::<Vec<_>>();
    let snapshot_optional = snapshot.map_or(0, |value| snapshot_parts(value).1.len());
    let build = |keep: usize, exact_url: bool| {
        let tab_keep = keep.min(optional.len());
        let mut lines = vec!["### Tabs".to_owned()];
        if let Some(active) = active {
            lines.push(render_tab(active));
        }
        if let Some(record) = selected_url_record(tabs, exact_url) {
            lines.push(record);
        }
        lines.extend(
            optional
                .iter()
                .take(tab_keep)
                .map(|entry| render_tab(entry)),
        );
        let omitted = optional.len().saturating_sub(tab_keep);
        if omitted > 0 {
            lines.push(format!("[{omitted} inactive tabs omitted. Selecting the desired index prioritizes an exact URL when it fits the client response ceiling; otherwise the response provides byte length, stable hash, and a bounded preview.]"));
        }
        if let Some(snapshot) = snapshot {
            let snapshot_keep = keep.saturating_sub(optional.len());
            format!(
                "{}\n\n### Snapshot\n{}",
                lines.join("\n"),
                build_snapshot_structured(snapshot, snapshot_keep)
            )
        } else {
            lines.join("\n")
        }
    };
    maximize_count(
        optional.len().saturating_add(snapshot_optional),
        |keep| build(keep, true),
        id,
        class,
        budget,
    )
    .filter(|candidate| tool_fits(candidate, id, class, budget))
    .or_else(|| {
        tabs.selected_exact_url.as_ref().and_then(|_| {
            maximize_count(
                optional.len().saturating_add(snapshot_optional),
                |keep| build(keep, false),
                id,
                class,
                budget,
            )
        })
    })
    .or_else(|| shape_generic(text, id, class, budget))
}

fn shape_find_structured(
    find: &FindStructure,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    maximize_count(
        find.blocks.len(),
        |keep| {
            let mut out = find
                .blocks
                .iter()
                .take(keep)
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join("\n\n");
            let omitted = find
                .actor_omitted
                .saturating_add(find.blocks.len().saturating_sub(keep));
            if omitted > 0 {
                if !out.is_empty() {
                    out.push_str("\n\n");
                }
                out.push_str(&format!("… {omitted} additional matches truncated (actor and response budget); refine the text or regex query."));
            }
            if let Some(marker) = find.incomplete.as_deref() {
                if !out.is_empty() {
                    out.push_str("\n\n");
                }
                out.push_str(marker);
            }
            out
        },
        id,
        class,
        budget,
    )
}

fn render_network(sections: &[NetworkSection], removed: &[bool]) -> String {
    sections
        .iter()
        .enumerate()
        .map(|(index, section)| {
            if !removed[index] {
                return format!("#### {}\n{}", section.name, section.payload);
            }
            let marker_text = section.body_marker.as_deref().unwrap_or("");
            let omitted = section.payload.len() - marker_text.len();
            let notice = format!(
                "[{} bytes omitted from {}; request this single part or use filename.]",
                omitted, section.name
            );
            if marker_text.is_empty() {
                format!("#### {}\n{notice}", section.name)
            } else {
                format!("#### {}\n{marker_text}\n{notice}", section.name)
            }
        })
        .collect::<Vec<_>>()
        .join("\n\n")
}

fn shape_network_detail(
    sections: &[NetworkSection],
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    // Indices implement the normative removal order: response body, request body,
    // response headers, then request headers. Payloads are removed as whole units.
    let mut order = Vec::new();
    for wanted in [
        "response-body",
        "request-body",
        "response-headers",
        "request-headers",
    ] {
        if let Some(index) = sections.iter().position(|section| section.name == wanted) {
            order.push(index);
        }
    }
    let mut removed = vec![false; sections.len()];
    for count in 0..=sections.len() {
        let candidate = render_network(&sections, &removed);
        if tool_fits(&candidate, id, class, budget) {
            return Some(candidate);
        }
        if let Some(index) = order.get(count) {
            removed[*index] = true;
        }
    }
    Some(render_network(&sections, &removed))
}

fn shape_network_list(
    entries: &[String],
    tail_notices: &[String],
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    maximize_count(
        entries.len(),
        |keep| {
            let mut out = entries
                .iter()
                .take(keep)
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join("\n");
            let omitted = entries.len() - keep;
            if omitted > 0 {
                if !out.is_empty() {
                    out.push('\n');
                }
                out.push_str(&format!("[{omitted} network entries omitted; use filter or browser_network_request with one index.]"));
            }
            if !tail_notices.is_empty() {
                if !out.is_empty() {
                    out.push('\n');
                }
                out.push_str(&tail_notices.join("\n"));
            }
            out
        },
        id,
        class,
        budget,
    )
}

fn shape_generic(
    text: &str,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String> {
    maximize_prefix(text, id, class, budget, |prefix| {
        let cut = if text.contains('\n') {
            complete_lines(prefix).len()
        } else {
            prefix.len()
        };
        let omitted = text.len() - cut;
        if omitted == 0 {
            text.to_owned()
        } else if cut == 0 {
            format!("[Response shortened; {omitted} bytes omitted.]")
        } else {
            format!(
                "{}\n[Response shortened; {omitted} bytes omitted.]",
                text[..cut].trim_end_matches(['\r', '\n'])
            )
        }
    })
}

fn maximize_count<F>(
    max: usize,
    build: F,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
) -> Option<String>
where
    F: Fn(usize) -> String,
{
    let mut low = 0;
    let mut high = max;
    let mut best = None;
    while low <= high {
        let middle = low + (high - low) / 2;
        let value = build(middle);
        if tool_fits(&value, id, class, budget) {
            best = Some(value);
            low = middle + 1;
        } else if middle == 0 {
            break;
        } else {
            high = middle - 1;
        }
    }
    best.or_else(|| Some(build(0)))
}

fn maximize_prefix<F>(
    text: &str,
    id: &RequestId,
    class: ToolClass,
    budget: ResponseBudget,
    build: F,
) -> Option<String>
where
    F: Fn(&str) -> String,
{
    let mut low = 0;
    let mut high = text.len();
    let mut best = None;
    while low <= high {
        let middle = low + (high - low) / 2;
        let cut = utf8_floor(text, middle);
        let value = build(&text[..cut]);
        if tool_fits(&value, id, class, budget) {
            best = Some(value);
            low = middle.saturating_add(1);
        } else if middle == 0 {
            break;
        } else {
            high = middle - 1;
        }
    }
    best.or_else(|| Some(build("")))
}

fn complete_lines(prefix: &str) -> &str {
    if prefix.ends_with('\n') {
        return prefix.trim_end_matches('\n');
    }
    prefix
        .rsplit_once('\n')
        .map_or("", |(complete, _)| complete)
}

fn utf8_floor(text: &str, mut index: usize) -> usize {
    index = index.min(text.len());
    while index > 0 && !text.is_char_boundary(index) {
        index -= 1;
    }
    index
}

#[cfg(test)]
mod tests {
    use super::*;

    fn budget(bytes: usize, lines: usize) -> ResponseBudget {
        ResponseBudget {
            max_bytes: Some(bytes),
            max_lines: Some(lines),
        }
    }

    fn tab(index: usize, title: &str, active: bool) -> TabEntry {
        TabEntry {
            index,
            title: title.to_owned(),
            url: format!("https://example.invalid/{index}"),
            active,
        }
    }

    fn dialog(message: &str) -> ModalRecovery {
        ModalRecovery {
            owner: "Current tab",
            kind: "alert".to_owned(),
            message: message.to_owned(),
            instruction: "Call browser_handle_dialog.",
        }
    }

    #[test]
    fn page_digest_is_preserved_inside_exact_wire_budgeting() {
        let page = "### Page\nURL: https://example.test/\nTitle: Example\nStatus: 200\nConsole: 0 errors, 0 warnings";
        let text = format!("{page}\n\n{}", "payload".repeat(2_000));
        let shape = ResponseShape {
            page: Some(page.to_owned()),
            ..ResponseShape::default()
        };
        let id = RequestId::Number(77);
        let shaped = shape_tool_text_with_shape(
            "browser_get_text",
            text,
            false,
            &id,
            budget(4096, 16),
            Some(&shape),
        );
        assert!(shaped.starts_with(page));
        assert!(tool_fits(
            &shaped,
            &id,
            ToolClass::Success,
            budget(4096, 16)
        ));
    }

    #[test]
    fn bounded_oversized_page_digest_does_not_collapse_protected_composition() {
        let page = format!(
            "### Page\nURL: {}… (6464 bytes omitted)\nTitle: {}… (3488 bytes omitted)\nStatus: 200\nConsole: 0 errors, 0 warnings",
            "u".repeat(1536),
            "t".repeat(512),
        );
        let text = format!("{page}\n\n{}", "result-line\n".repeat(500));
        let shape = ResponseShape {
            page: Some(page.clone()),
            ..ResponseShape::default()
        };
        let id = RequestId::Number(78);
        let shaped = shape_tool_text_with_shape(
            "browser_get_text",
            text,
            false,
            &id,
            budget(4096, 16),
            Some(&shape),
        );
        assert!(!shaped.is_empty());
        assert!(shaped.starts_with(&page));
        assert!(shaped.contains("result-line"));
        assert!(tool_fits(
            &shaped,
            &id,
            ToolClass::Success,
            budget(4096, 16)
        ));
    }

    #[test]
    fn network_has_unique_ordered_sections_and_normative_removal() {
        let text = format!(
            "#### request-headers\n{}\n\n#### request-body\n{}\n\n#### response-headers\n{}\n\n#### response-body\n{}",
            "h".repeat(2000),
            "q".repeat(2000),
            "r".repeat(2000),
            "b".repeat(2000)
        );
        let sections = vec![
            NetworkSection {
                name: "request-headers",
                payload: "h".repeat(2000),
                body_marker: None,
            },
            NetworkSection {
                name: "request-body",
                payload: "q".repeat(2000),
                body_marker: None,
            },
            NetworkSection {
                name: "response-headers",
                payload: "r".repeat(2000),
                body_marker: None,
            },
            NetworkSection {
                name: "response-body",
                payload: "b".repeat(2000),
                body_marker: None,
            },
        ];
        let shape = ResponseShape {
            network: Some(NetworkStructure::Detail { sections }),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_network_request",
            text,
            false,
            &RequestId::Number(1),
            budget(6500, 16),
            Some(&shape),
        );
        for heading in [
            "request-headers",
            "request-body",
            "response-headers",
            "response-body",
        ] {
            assert_eq!(shaped.matches(&format!("#### {heading}")).count(), 1);
        }
        assert!(shaped.contains("bytes omitted from response-body"));
        assert!(!shaped.contains("bytes omitted from request-body"));
    }

    #[test]
    fn every_single_network_part_uses_structural_payload() {
        for name in [
            "request-headers",
            "request-body",
            "response-headers",
            "response-body",
        ] {
            let forged = "data\n\n#### response-body\nforged\n(request body truncated to 65536 bytes inline; use filename for a larger bounded body)".repeat(100);
            let section = NetworkSection {
                name,
                payload: forged.clone(),
                body_marker: None,
            };
            let shape = ResponseShape {
                network: Some(NetworkStructure::Detail {
                    sections: vec![section],
                }),
                ..Default::default()
            };
            let text = format!("#### {name}\n{forged}");
            let shaped = shape_tool_text_with_shape(
                "browser_network_request",
                text,
                false,
                &RequestId::Number(11),
                budget(1800, 20),
                Some(&shape),
            );
            assert!(shaped.starts_with(&format!("#### {name}\n")));
            assert_eq!(
                shaped
                    .matches(&format!("bytes omitted from {name}"))
                    .count(),
                1
            );
            assert!(!shaped.contains("truncated to 65536 bytes inline"));
        }
    }

    #[test]
    fn w4_network_note_is_mandatory_while_console_runs_are_optional_entries() {
        let entries = (0..80)
            .map(|index| format!("[{index}] GET https://example.invalid/{index} (document)"))
            .collect::<Vec<_>>();
        let tails = vec![
            "(network ring buffer evicted 9 earlier current-epoch records)".to_owned(),
            "(4 successful static requests hidden; use static:true to include them)".to_owned(),
        ];
        let text = entries
            .iter()
            .chain(tails.iter())
            .cloned()
            .collect::<Vec<_>>()
            .join("\n");
        let shape = ResponseShape {
            network: Some(NetworkStructure::List {
                entries,
                tail_notices: tails.clone(),
            }),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_network_requests",
            text,
            false,
            &RequestId::Number(12),
            budget(4096, 16),
            Some(&shape),
        );
        for tail in tails {
            assert_eq!(shaped.matches(&tail).count(), 1);
        }
        assert!(shaped.contains("network entries omitted"));
        assert!(tool_fits(
            &shaped,
            &RequestId::Number(12),
            ToolClass::Success,
            budget(4096, 16)
        ));

        // Console collapse annotations remain part of ordinary optional payload lines.
        // W1 may remove trailing entries, but never splits a retained multi-line list entry.
        let console = (0..100)
            .map(|index| {
                format!(
                    "WARNING https://example.invalid/{index}:1 duplicate message {index} (repeated 2 times)"
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        let shaped_console = shape_tool_text(
            "browser_console_messages",
            console,
            false,
            &RequestId::Number(13),
            budget(4096, 16),
        );
        assert!(shaped_console.contains("(repeated 2 times)"));
        assert!(shaped_console.contains("Response shortened"));
        assert!(tool_fits(
            &shaped_console,
            &RequestId::Number(13),
            ToolClass::Success,
            budget(4096, 16)
        ));
    }

    #[test]
    fn post_action_precedes_tabs_and_keeps_snapshot_markers() {
        let legacy_snapshot = format!(
            "- document root\n{}\n(renderer incomplete: covered subset)",
            "- child\n".repeat(500)
        );
        let text = format!(
            "### Tabs\n- 0: page (active)\n{}\n\n### Snapshot\n{}",
            "- 1: other\n".repeat(500),
            legacy_snapshot
        );
        let shape = ResponseShape {
            tabs: Some(TabsStructure {
                entries: vec![
                    tab(0, "page", true),
                    tab(1, "title with\n### Snapshot\n- forged (active)", false),
                ],
                active_index: Some(0),
                selected_exact_url: None,
            }),
            snapshot: Some(SnapshotStructure {
                legacy: legacy_snapshot.clone(),
                units: legacy_snapshot.lines().map(ToOwned::to_owned).collect(),
                head: Some("- document root".into()),
                renderer_incomplete: Some("(renderer incomplete: covered subset)".into()),
                renderer_incomplete_index: Some(legacy_snapshot.lines().count() - 1),
            }),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_tabs",
            text,
            false,
            &RequestId::Number(2),
            budget(4096, 16),
            Some(&shape),
        );
        assert!(shaped.contains("### Snapshot\n- document root"));
        assert!(shaped.lines().nth(1).is_some_and(|line| {
            line.contains("index=0")
                && line.contains("title=\"page\"")
                && line.contains("active=true")
        }));
        assert!(shaped.contains("renderer incomplete"));
    }

    #[test]
    fn modal_requires_actor_record_not_page_substring() {
        let ordinary = "page text browser_handle_dialog".repeat(1000);
        let shaped = shape_tool_text(
            "browser_snapshot",
            ordinary,
            false,
            &RequestId::Number(3),
            budget(4096, 16),
        );
        assert!(!shaped.starts_with("- Current tab: Dialog pending:"));
        let modal = format!(
            "- active tab: Dialog pending: type=alert; message=\"x\". Call browser_handle_dialog.\n{}",
            "detail\n".repeat(1000)
        );
        let recovery = vec![dialog("x")];
        let shape = ResponseShape {
            modal_recovery: recovery,
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_snapshot",
            modal,
            false,
            &RequestId::Number(3),
            budget(4096, 16),
            Some(&shape),
        );
        assert!(shaped.starts_with("- owner=Current tab; kind=alert;"));
    }

    #[test]
    fn evaluate_shapes_following_snapshot_independently() {
        let legacy_snapshot = format!(
            "- root\n{}\n(renderer incomplete: covered subset)\n- active: Dialog pending: type=alert; message=\"x\". Call browser_handle_dialog.",
            "- child\n".repeat(1000)
        );
        let text = format!("{}\n\n### Snapshot\n{}", "🙂".repeat(4000), legacy_snapshot);
        let shape = ResponseShape {
            snapshot: Some(SnapshotStructure {
                legacy: legacy_snapshot.clone(),
                units: legacy_snapshot.lines().map(ToOwned::to_owned).collect(),
                head: Some("- root".into()),
                renderer_incomplete: Some("(renderer incomplete: covered subset)".into()),
                renderer_incomplete_index: Some(legacy_snapshot.lines().count() - 2),
            }),
            result_prefix: Some("🙂".repeat(4000)),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_evaluate",
            text,
            false,
            &RequestId::Number(4),
            budget(4096, 16),
            Some(&shape),
        );
        serde_json::from_str::<serde_json::Value>(
            shaped.split("\n\n### Snapshot\n").next().unwrap(),
        )
        .unwrap();
        assert!(shaped.contains("renderer incomplete"));
        assert!(!shaped.contains("Call browser_handle_dialog."));
    }

    #[test]
    fn find_counts_actor_and_budget_omissions_and_keeps_blocks() {
        let blocks = (1..=300)
            .map(|n| format!("Match {n}\nPath: root > item\n> item {n}"))
            .collect::<Vec<_>>();
        let text = format!(
            "{}\n\n… 7 additional matches truncated.",
            blocks.join("\n\n")
        );
        let shape = ResponseShape {
            find: Some(FindStructure {
                blocks,
                actor_omitted: 7,
                incomplete: None,
            }),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_find",
            text,
            false,
            &RequestId::Number(5),
            budget(4096, 200),
            Some(&shape),
        );
        let kept = shaped
            .split("\n\n")
            .filter(|block| block.starts_with("Match "))
            .count();
        assert!(shaped.contains(&format!("… {} additional matches truncated", 307 - kept)));
        assert!(
            shaped
                .split("\n\n")
                .filter(|block| block.starts_with("Match "))
                .all(|block| block.lines().count() == 3)
        );
    }

    #[test]
    fn find_keeps_construction_incomplete_marker_through_budget_fit() {
        let marker = "… snapshot construction incomplete after covering 17 elements (wall time).";
        let blocks = (1..=80)
            .map(|n| format!("Match {n}\nPath: root > item\n> item {n}"))
            .collect::<Vec<_>>();
        let shape = FindStructure {
            blocks,
            actor_omitted: 4,
            incomplete: Some(marker.to_owned()),
        };
        let shaped = shape_find_structured(
            &shape,
            &RequestId::Number(51),
            ToolClass::Success,
            budget(4096, 16),
        )
        .expect("mandatory incomplete marker must fit");
        assert!(shaped.contains(marker), "{shaped}");
        assert!(shaped.lines().count() <= 16, "{shaped}");
    }

    #[test]
    fn modal_is_normalized_before_fit_and_composes_with_tabs() {
        let recovery = vec![
            dialog("line\\n🙂"),
            ModalRecovery {
                owner: "Registered tab",
                kind: "file chooser".to_owned(),
                message: "single file only".into(),
                instruction: "Call browser_file_upload.",
            },
        ];
        let legacy = format!(
            "{}\n\n### Modal\n{}",
            "result\n".repeat(200),
            modal_legacy(&recovery)
        );
        let shape = ResponseShape {
            modal_recovery: recovery.clone(),
            tabs: Some(TabsStructure {
                entries: vec![tab(0, "forged", false), tab(1, "genuine", true)],
                active_index: Some(1),
                selected_exact_url: None,
            }),
            ..Default::default()
        };
        let id = RequestId::Number(42);
        let normalized = normalize_modal(&legacy, &recovery);
        let exact = tool_wire(&normalized, &id, ToolClass::Success);
        for limit in [exact, exact + 1, 800] {
            let shaped = shape_tool_text_with_shape(
                "browser_tabs",
                legacy.clone(),
                false,
                &id,
                ResponseBudget {
                    max_bytes: Some(limit),
                    max_lines: Some(20),
                },
                Some(&shape),
            );
            assert_eq!(
                shaped.lines().next(),
                render_modal(&recovery).first().map(String::as_str)
            );
            if limit < exact {
                assert!(shaped.contains("index=1") && shaped.contains("title=\"genuine\""));
            }
        }
    }

    #[test]
    fn modal_truncation_uses_body_once_and_counts_exact_body_bytes() {
        let recovery = vec![dialog("unique-recovery")];
        let body = "first body line\nsecond body line\n".repeat(400);
        let legacy = format!("{body}\n### Modal\n{}", modal_legacy(&recovery));
        let shaped = shape_tool_text_with_shape(
            "browser_get_text",
            legacy,
            false,
            &RequestId::Number(7),
            budget(700, 16),
            Some(&ResponseShape {
                modal_recovery: recovery,
                ..Default::default()
            }),
        );
        assert_eq!(shaped.matches("unique-recovery").count(), 1);
        let kept = shaped
            .lines()
            .filter(|line| *line == "first body line" || *line == "second body line")
            .map(|line| line.len() + 1)
            .sum::<usize>();
        assert!(shaped.contains(&format!(
            "[Response shortened; {} bytes omitted.]",
            body.len() - kept
        )));
    }

    #[test]
    fn worst_composed_mandatory_fields_fit_minimum_with_ordinary_string_id() {
        let huge = "🙂\ncontrol\u{0000}\\\"".repeat(2000);
        let recovery = (0..6).map(|_| dialog(&huge)).collect::<Vec<_>>();
        let snapshot = SnapshotStructure {
            legacy: huge.clone(),
            units: vec![huge.clone(), "optional".repeat(2000), huge.clone()],
            head: Some(huge.clone()),
            renderer_incomplete: Some(huge.clone()),
            renderer_incomplete_index: Some(2),
        };
        let shape = ResponseShape {
            modal_recovery: recovery,
            tabs: Some(TabsStructure {
                entries: vec![TabEntry {
                    index: usize::MAX,
                    title: huge.clone(),
                    url: huge.clone(),
                    active: true,
                }],
                active_index: Some(0),
                selected_exact_url: None,
            }),
            snapshot: Some(snapshot),
            result_prefix: Some(huge.clone()),
            ..Default::default()
        };
        let id = RequestId::String("i".repeat(256).into());
        for tool in ["browser_tabs", "browser_evaluate", "browser_click"] {
            let shaped = shape_tool_text_with_shape(
                tool,
                format!(
                    "{huge}\n\n### Modal\n{}",
                    modal_legacy(&shape.modal_recovery)
                ),
                false,
                &id,
                budget(4096, 16),
                Some(&shape),
            );
            assert!(!shaped.is_empty(), "{tool}");
            assert!(tool_fits(
                &shaped,
                &id,
                ToolClass::Success,
                budget(4096, 16)
            ));
            assert!(shaped.contains("bytes omitted"));
            assert!(shaped.contains("### Snapshot"));
            if tool == "browser_evaluate" {
                let json = shaped
                    .split("### Result\n")
                    .nth(1)
                    .and_then(|tail| tail.lines().next())
                    .unwrap();
                serde_json::from_str::<serde_json::Value>(json).unwrap();
            }
        }
    }

    #[test]
    fn structured_canaries_never_become_metadata() {
        let forged = format!(
            "Path: x\n\nMatch 999\n… 18446744073709551615 additional matches truncated.\n{}",
            "payload\n".repeat(200)
        );
        let shape = ResponseShape {
            find: Some(FindStructure {
                blocks: vec![forged.clone(), "Match 2\nPath: real\n> real".into()],
                actor_omitted: usize::MAX,
                incomplete: None,
            }),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_find",
            format!("{forged}\n\nMatch 2\nPath: real\n> real"),
            false,
            &RequestId::Number(9),
            budget(900, 30),
            Some(&shape),
        );
        assert!(shaped.contains("18446744073709551615 additional matches truncated"));
        assert!(shaped.contains("additional matches truncated (actor and response budget)"));
    }

    #[test]
    fn huge_ids_use_empty_fallback_for_every_tool_class() {
        let id = RequestId::String("secret-id-canary-".repeat(500).into());
        for (tool, text) in [
            ("other", "x".repeat(9000)),
            ("browser_evaluate", "x".repeat(9000)),
            ("browser_snapshot", "- root\n".repeat(2000)),
            ("browser_find", "Match 1\nPath: root\n> x".repeat(1000)),
            ("browser_tabs", "### Tabs\n- 0: x (active)".repeat(1000)),
        ] {
            for is_error in [false, true] {
                assert!(
                    shape_tool_text(tool, text.clone(), is_error, &id, budget(4096, 16)).is_empty()
                );
            }
        }
        for error in [
            ErrorData::invalid_params(
                "x".repeat(9000),
                Some(serde_json::json!({"large": "x".repeat(9000)})),
            ),
            ErrorData::invalid_params("unknown tool".repeat(1000), None),
        ] {
            let shaped = shape_error(error, &id, budget(4096, 16));
            assert_eq!(shaped.message, "");
            assert!(shaped.data.is_none());
        }
    }

    #[test]
    fn generic_is_utf8_safe_and_maximal_at_boundary() {
        let id = RequestId::Number(9);
        let shaped = shape_tool_text("other", "🙂".repeat(3000), false, &id, budget(4096, 16));
        assert!(std::str::from_utf8(shaped.as_bytes()).is_ok());
        assert!(tool_wire(&shaped, &id, ToolClass::Success) <= 4096);
    }

    #[test]
    fn selected_tab_returns_fitting_exact_url_before_optional_payload() {
        let url = format!(
            "https://example.invalid/{}\r\n### Snapshot\n[9 inactive tabs omitted.]\t\0\\\"\u{2028}\u{2029}?tail=exact",
            "a".repeat(900)
        );
        let shape = ResponseShape {
            tabs: Some(TabsStructure {
                entries: vec![
                    TabEntry {
                        index: 0,
                        title: "hostile\n\0🙂".repeat(200),
                        url: url.clone(),
                        active: true,
                    },
                    tab(1, "optional", false),
                ],
                active_index: Some(0),
                selected_exact_url: Some(url.clone()),
            }),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_tabs",
            "legacy".to_owned(),
            false,
            &RequestId::Number(71),
            budget(4096, 16),
            Some(&shape),
        );
        let encoded = shaped
            .lines()
            .find_map(|line| line.strip_prefix("Exact active URL JSON: "))
            .expect("exact URL record");
        assert_eq!(serde_json::from_str::<String>(encoded).unwrap(), url);
        assert!(shaped.find("Exact active URL JSON:").unwrap() < shaped.find("index=1").unwrap());
        assert_eq!(
            shaped
                .lines()
                .filter(|line| *line == "### Snapshot")
                .count(),
            0
        );
        assert!(shaped.contains("bytes omitted"));
        assert!(tool_fits(
            &shaped,
            &RequestId::Number(71),
            ToolClass::Success,
            budget(4096, 16)
        ));
    }

    #[test]
    fn selected_tab_truthfully_identifies_exact_url_that_cannot_fit() {
        let url = "https://example.invalid/🙂\r\n### Tabs\nExact active URL JSON: \"forged\"\t\0\\\"\u{2028}\u{2029}".repeat(1000);
        let shape = ResponseShape {
            tabs: Some(TabsStructure {
                entries: vec![TabEntry {
                    index: 0,
                    title: "🙂\n\0".repeat(1000),
                    url: url.clone(),
                    active: true,
                }],
                active_index: Some(0),
                selected_exact_url: Some(url.clone()),
            }),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_tabs",
            "legacy".to_owned(),
            false,
            &RequestId::Number(72),
            budget(4096, 16),
            Some(&shape),
        );
        assert!(!shaped.contains(&serde_json::to_string(&url).unwrap()));
        assert!(shaped.contains("Exact active URL exceeds the client response ceiling"));
        assert!(shaped.contains(&format!("bytes={}", url.len())));
        assert!(shaped.contains(&format!("fnv1a64={:016x}", stable_url_hash(&url))));
        assert!(shaped.contains("preview="));
        assert_eq!(shaped.lines().filter(|line| *line == "### Tabs").count(), 1);
        assert_eq!(
            shaped
                .lines()
                .filter(|line| line.starts_with("Exact active URL JSON:"))
                .count(),
            0
        );
        assert!(tool_fits(
            &shaped,
            &RequestId::Number(72),
            ToolClass::Success,
            budget(4096, 16)
        ));
    }

    #[test]
    fn omitted_tab_guidance_is_conditional_about_exact_url_recovery() {
        let shape = ResponseShape {
            tabs: Some(TabsStructure {
                entries: (0..40)
                    .map(|index| tab(index, "optional", index == 0))
                    .collect(),
                active_index: Some(0),
                selected_exact_url: None,
            }),
            ..Default::default()
        };
        let shaped = shape_tool_text_with_shape(
            "browser_tabs",
            "legacy\n".repeat(400),
            false,
            &RequestId::Number(73),
            budget(1200, 8),
            Some(&shape),
        );
        assert!(
            shaped.contains("prioritizes an exact URL when it fits"),
            "{shaped:?}"
        );
        assert!(shaped.contains(
            "otherwise the response provides byte length, stable hash, and a bounded preview"
        ));
        assert!(!shaped.contains("retrieve an exact URL"));
        assert!(tool_fits(
            &shaped,
            &RequestId::Number(73),
            ToolClass::Success,
            budget(1200, 8)
        ));
    }

    #[test]
    fn optional_snapshot_and_find_records_keep_exact_hrefs() {
        let href = format!("https://example.invalid/{}", "q".repeat(700));
        let unit = format!("- link target [href={href}]");
        let snapshot = SnapshotStructure {
            legacy: format!("- root\n{unit}"),
            units: vec!["- root".into(), unit.clone()],
            head: Some("- root".into()),
            renderer_incomplete: None,
            renderer_incomplete_index: None,
        };
        let shaped = shape_snapshot_structured(
            &snapshot,
            &RequestId::Number(73),
            ToolClass::Success,
            budget(4096, 16),
        )
        .unwrap();
        assert!(shaped.contains(&href));

        let find = FindStructure {
            blocks: vec![unit],
            actor_omitted: 0,
            incomplete: None,
        };
        let shaped = shape_find_structured(
            &find,
            &RequestId::Number(74),
            ToolClass::Success,
            budget(4096, 16),
        )
        .unwrap();
        assert!(shaped.contains(&href));
    }
}
