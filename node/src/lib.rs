use napi::bindgen_prelude::Buffer;
use napi::{Error, Result, Status};
use napi_derive::napi;
use rustwright_core as rw;
use serde::Deserialize;
use serde_json::Value;

fn napi_error(error: impl ToString) -> Error {
    Error::new(Status::GenericFailure, error.to_string())
}

async fn blocking<T, F>(operation: F) -> Result<T>
where
    T: Send + 'static,
    F: FnOnce() -> rw::RwResult<T> + Send + 'static,
{
    tokio::task::spawn_blocking(operation)
        .await
        .map_err(|error| {
            Error::new(
                Status::GenericFailure,
                format!("Rustwright worker task failed: {error}"),
            )
        })?
        .map_err(napi_error)
}

#[napi(js_name = "chromiumExecutablePath")]
pub async fn chromium_executable_path() -> Result<Option<String>> {
    Ok(rw::rustwright_chromium_executable_path())
}

#[napi(js_name = "launchChromium")]
pub async fn launch_chromium(options_json: String) -> Result<Browser> {
    let inner = blocking(move || rw::rustwright_launch_chromium(&options_json)).await?;
    Ok(Browser { inner })
}

#[napi]
pub struct Browser {
    inner: rw::RustwrightBrowser,
}

#[napi]
impl Browser {
    #[napi(js_name = "newPage")]
    pub async fn new_page(&self) -> Result<Page> {
        let browser = self.inner.clone();
        let inner = blocking(move || browser.new_page()).await?;
        Ok(Page { inner })
    }

    #[napi]
    pub async fn close(&self) -> Result<()> {
        let browser = self.inner.clone();
        blocking(move || browser.close()).await
    }

    #[napi(js_name = "wsEndpoint")]
    pub fn ws_endpoint(&self) -> String {
        self.inner.ws_endpoint()
    }
}

#[napi]
pub struct Page {
    inner: rw::RustwrightPage,
}

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ScreenshotOptions {
    path: Option<String>,
    full_page: Option<bool>,
    clip: Option<Value>,
    timeout: Option<f64>,
    #[serde(rename = "type")]
    image_type: Option<String>,
    quality: Option<u32>,
    omit_background: Option<bool>,
}

#[napi]
impl Page {
    #[napi(js_name = "targetId")]
    pub fn target_id(&self) -> String {
        self.inner.target_id()
    }

    #[napi(js_name = "setDefaultTimeout")]
    pub fn set_default_timeout(&self, timeout: f64) {
        self.inner
            .set_default_timeout((!timeout.is_nan()).then_some(timeout));
    }

    #[napi(js_name = "setDefaultNavigationTimeout")]
    pub fn set_default_navigation_timeout(&self, timeout: f64) {
        self.inner
            .set_default_navigation_timeout((!timeout.is_nan()).then_some(timeout));
    }

    #[napi(js_name = "setContextDefaultTimeout")]
    pub fn set_context_default_timeout(&self, timeout: f64) {
        self.inner
            .set_context_default_timeout((!timeout.is_nan()).then_some(timeout));
    }

    #[napi(js_name = "setContextDefaultNavigationTimeout")]
    pub fn set_context_default_navigation_timeout(&self, timeout: f64) {
        self.inner
            .set_context_default_navigation_timeout((!timeout.is_nan()).then_some(timeout));
    }

    #[napi]
    pub async fn goto(
        &self,
        url: String,
        wait_until: Option<String>,
        timeout: Option<f64>,
        referer: Option<String>,
    ) -> Result<String> {
        let page = self.inner.clone();
        blocking(move || page.goto(&url, wait_until.as_deref(), timeout, referer.as_deref())).await
    }

    #[napi]
    pub async fn click(&self, selector: String, timeout: Option<f64>) -> Result<()> {
        let page = self.inner.clone();
        blocking(move || page.click(&selector, timeout)).await
    }

    #[napi]
    pub async fn fill(&self, selector: String, value: String, timeout: Option<f64>) -> Result<()> {
        let page = self.inner.clone();
        blocking(move || page.fill(&selector, &value, timeout)).await
    }

    #[napi]
    pub async fn title(&self, timeout: Option<f64>) -> Result<String> {
        let page = self.inner.clone();
        blocking(move || page.title(timeout)).await
    }

    #[napi(js_name = "textContent")]
    pub async fn text_content(
        &self,
        selector: String,
        timeout: Option<f64>,
    ) -> Result<Option<String>> {
        let page = self.inner.clone();
        blocking(move || page.text_content(&selector, timeout)).await
    }

    #[napi]
    pub async fn evaluate(
        &self,
        expression: String,
        arg_json: Option<String>,
        timeout: Option<f64>,
    ) -> Result<String> {
        let page = self.inner.clone();
        blocking(move || page.evaluate(&expression, arg_json.as_deref(), timeout)).await
    }

    #[napi]
    pub async fn screenshot(&self, options_json: Option<String>) -> Result<Buffer> {
        let options = match options_json {
            Some(value) if !value.trim().is_empty() => {
                serde_json::from_str::<ScreenshotOptions>(&value).map_err(napi_error)?
            }
            _ => ScreenshotOptions::default(),
        };
        let page = self.inner.clone();
        let clip_json = options.clip.map(|clip| clip.to_string());
        let bytes = blocking(move || {
            page.screenshot(
                options.path.as_deref(),
                options.full_page,
                clip_json.as_deref(),
                options.timeout,
                options.image_type.as_deref(),
                options.quality,
                options.omit_background,
            )
        })
        .await?;
        Ok(bytes.into())
    }

    #[napi]
    pub async fn close(&self, timeout: Option<f64>, run_before_unload: Option<bool>) -> Result<()> {
        let page = self.inner.clone();
        blocking(move || page.close(timeout, run_before_unload.unwrap_or(false))).await
    }
}
