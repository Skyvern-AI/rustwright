use _rustwright::RustwrightPageEvent;
use std::time::Duration;

fn exhaustive(event: RustwrightPageEvent) -> &'static str {
    match event {
        RustwrightPageEvent::Dialog { .. } => "dialog",
        RustwrightPageEvent::FileChooser { .. } => "filechooser",
        RustwrightPageEvent::Download { .. } => "download",
        RustwrightPageEvent::PageCrashed => "crashed",
        RustwrightPageEvent::Closed => "closed",
        RustwrightPageEvent::Navigated { url: _ } => "navigated",
    }
}

#[test]
fn legacy_navigation_variant_is_exact_and_exhaustive_downstream() {
    let event = RustwrightPageEvent::Navigated {
        url: "https://example.test/".to_owned(),
    };
    let RustwrightPageEvent::Navigated { url } = &event else {
        panic!("constructed legacy navigation event changed shape");
    };
    assert_eq!(url, "https://example.test/");
    assert_eq!(exhaustive(event), "navigated");
}

#[test]
fn legacy_navigation_method_signatures_and_results_remain_exact() {
    use _rustwright::{CancelToken, RustwrightPage};

    let _: fn(
        &RustwrightPage,
        &str,
        Option<&str>,
        Option<f64>,
        Option<&str>,
    ) -> _rustwright::RwResult<String> = RustwrightPage::goto;
    let _: fn(
        &RustwrightPage,
        &str,
        Option<&str>,
        Option<f64>,
        Option<&str>,
        Option<&CancelToken>,
    ) -> _rustwright::RwResult<String> = RustwrightPage::goto_with_cancel;
    let _: fn(&RustwrightPage, Option<&str>, Duration) -> _rustwright::RwResult<String> =
        RustwrightPage::go_back;
    let _: fn(
        &RustwrightPage,
        Option<&str>,
        Duration,
        Option<&CancelToken>,
    ) -> _rustwright::RwResult<String> = RustwrightPage::go_back_with_cancel;
    let _: fn(&RustwrightPage, Option<&str>, Duration) -> _rustwright::RwResult<String> =
        RustwrightPage::go_forward;
    let _: fn(
        &RustwrightPage,
        Option<&str>,
        Duration,
        Option<&CancelToken>,
    ) -> _rustwright::RwResult<String> = RustwrightPage::go_forward_with_cancel;
    let _: fn(&RustwrightPage, Option<&str>, Duration) -> _rustwright::RwResult<String> =
        RustwrightPage::reload;
    let _: fn(
        &RustwrightPage,
        Option<&str>,
        Duration,
        Option<&CancelToken>,
    ) -> _rustwright::RwResult<String> = RustwrightPage::reload_with_cancel;
    let _: fn(
        &RustwrightPage,
        Option<&str>,
        Duration,
        Option<&CancelToken>,
    ) -> _rustwright::RwResult<(bool, String)> = RustwrightPage::go_back_with_cancel_status;
    let _: fn(
        &RustwrightPage,
        Option<&str>,
        Duration,
        Option<&CancelToken>,
    ) -> _rustwright::RwResult<(bool, String)> = RustwrightPage::go_forward_with_cancel_status;
}
