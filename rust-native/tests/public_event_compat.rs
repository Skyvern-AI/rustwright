use rustwright::PageEvent;
use serde_json::Value;

fn exhaustive(event: PageEvent) -> &'static str {
    match event {
        PageEvent::Dialog { .. } => "dialog",
        PageEvent::FileChooser { .. } => "filechooser",
        PageEvent::Download { .. } => "download",
        PageEvent::PageCrashed => "crashed",
        PageEvent::Closed => "closed",
        PageEvent::Navigated { url: _ } => "navigated",
    }
}

#[test]
fn legacy_navigation_variant_is_exact_and_exhaustive_downstream() {
    let event = PageEvent::Navigated {
        url: "https://example.test/".to_owned(),
    };
    let PageEvent::Navigated { url } = &event else {
        panic!("constructed legacy navigation event changed shape");
    };
    assert_eq!(url, "https://example.test/");
    assert_eq!(exhaustive(event), "navigated");
}

#[test]
fn legacy_navigation_method_signatures_and_results_remain_exact() {
    use rustwright::{CancelToken, GotoOptions, Page};

    let _: fn(&Page, &str, GotoOptions) -> rustwright::Result<Value> = Page::goto;
    let _: fn(&Page, &str, GotoOptions, Option<&CancelToken>) -> rustwright::Result<Value> =
        Page::goto_with_cancel;
    let _: fn(&Page, GotoOptions) -> rustwright::Result<Value> = Page::go_back;
    let _: fn(&Page, GotoOptions, Option<&CancelToken>) -> rustwright::Result<Value> =
        Page::go_back_with_cancel;
    let _: fn(&Page, GotoOptions) -> rustwright::Result<Value> = Page::go_forward;
    let _: fn(&Page, GotoOptions, Option<&CancelToken>) -> rustwright::Result<Value> =
        Page::go_forward_with_cancel;
    let _: fn(&Page, GotoOptions) -> rustwright::Result<Value> = Page::reload;
    let _: fn(&Page, GotoOptions, Option<&CancelToken>) -> rustwright::Result<Value> =
        Page::reload_with_cancel;
    let _: fn(&Page, GotoOptions, Option<&CancelToken>) -> rustwright::Result<(bool, Value)> =
        Page::go_back_with_cancel_status;
    let _: fn(&Page, GotoOptions, Option<&CancelToken>) -> rustwright::Result<(bool, Value)> =
        Page::go_forward_with_cancel_status;
}
