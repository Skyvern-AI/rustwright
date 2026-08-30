use napi::bindgen_prelude::{
    Array, Buffer, Env, FnArgs, Function, JsObjectValue, Null, Object, ToNapiValue, Unknown,
};
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
fn to_unknown<'env, T: ToNapiValue>(env: &'env Env, value: T) -> Result<Unknown<'env>> {
    value.into_unknown(env)
}

struct WireMaterializer<'env, 'graph> {
    env: &'env Env,
    graph: &'graph rw::WireGraph,
    values: Vec<Option<Unknown<'env>>>,
}

impl<'env, 'graph> WireMaterializer<'env, 'graph> {
    fn new(env: &'env Env, graph: &'graph rw::WireGraph) -> Self {
        Self {
            env,
            graph,
            values: vec![None; graph.nodes().len()],
        }
    }

    fn first_pass(&mut self) -> Result<()> {
        for (index, kind) in self.graph.nodes().iter().enumerate() {
            let value = match kind {
                rw::WireNodeKind::Null => to_unknown(self.env, Null)?,
                rw::WireNodeKind::Bool(value) => to_unknown(self.env, *value)?,
                rw::WireNodeKind::Number(rw::WireNumber::Signed(value)) => {
                    to_unknown(self.env, *value as f64)?
                }
                rw::WireNodeKind::Number(rw::WireNumber::Unsigned(value)) => {
                    to_unknown(self.env, *value as f64)?
                }
                rw::WireNodeKind::Number(rw::WireNumber::Float(value)) => {
                    to_unknown(self.env, *value)?
                }
                rw::WireNodeKind::String(value) => {
                    to_unknown(self.env, self.env.create_string(value.clone())?)?
                }
                rw::WireNodeKind::Array(items) => self
                    .env
                    .create_array(items.len() as u32)?
                    .into_unknown(self.env)?,
                rw::WireNodeKind::Object(_) => Object::new(self.env)?.into_unknown(self.env)?,
                rw::WireNodeKind::Leaf(leaf) => leaf_to_unknown(self.env, leaf)?,
            };
            self.values[index] = Some(value);
        }
        Ok(())
    }

    fn value(&self, id: rw::WireNodeId) -> Result<Unknown<'env>> {
        self.values
            .get(id.index())
            .and_then(|value| value.as_ref())
            .copied()
            .ok_or_else(|| napi_error(format!("wire graph node {} has no value", id.index())))
    }

    fn second_pass(&mut self) -> Result<()> {
        for (index, kind) in self.graph.nodes().iter().enumerate() {
            let value = self
                .values
                .get(index)
                .and_then(|value| value.as_ref())
                .copied()
                .ok_or_else(|| napi_error(format!("wire graph node {index} has no value")))?;
            match kind {
                rw::WireNodeKind::Array(items) => {
                    // SAFETY: first_pass creates an Array at this dense graph index.
                    let mut array = unsafe { value.cast::<Array>()? };
                    for (item_index, child) in items.iter().enumerate() {
                        array.set(item_index as u32, self.value(*child)?)?;
                    }
                }
                rw::WireNodeKind::Object(entries) => {
                    // SAFETY: first_pass creates an Object at this dense graph index.
                    let mut object = unsafe { value.cast::<Object>()? };
                    let mut properties = Vec::with_capacity(entries.len());
                    for (key, child) in entries {
                        let child = self.value(*child)?;
                        properties.push(
                            napi::Property::new()
                                .with_name(self.env, key)?
                                .with_value(&child),
                        );
                    }
                    object.define_properties(&properties)?;
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn materialize(&mut self) -> Result<Unknown<'env>> {
        self.first_pass()?;
        self.second_pass()?;
        self.value(self.graph.root())
    }
}
fn unserializable_wrapper<'env>(env: &'env Env, value: &str) -> Result<Unknown<'env>> {
    let mut object = Object::new(env)?;
    let value = to_unknown(env, env.create_string(value)?)?;
    let property = napi::Property::new()
        .with_name(env, "__rustwright_cdp_unserializable_value__")?
        .with_value(&value);
    object.define_properties(&[property])?;
    object.into_unknown(env)
}

fn bigint_to_unknown<'env>(env: &'env Env, value: &str) -> Result<Unknown<'env>> {
    let payload = value.strip_suffix('n').unwrap_or(value).to_string();
    let constructor: Function<String, Unknown> =
        env.get_global()?.get_named_property_unchecked("BigInt")?;
    constructor.call(payload)
}

fn leaf_to_unknown<'env>(env: &'env Env, leaf: &rw::WireLeaf) -> Result<Unknown<'env>> {
    match leaf {
        rw::WireLeaf::Unserializable(value) => match value.as_str() {
            "NaN" => to_unknown(env, f64::NAN),
            "Infinity" => to_unknown(env, f64::INFINITY),
            "-Infinity" => to_unknown(env, f64::NEG_INFINITY),
            "-0" => to_unknown(env, -0.0_f64),
            value if value.ends_with('n') => bigint_to_unknown(env, value),
            value => unserializable_wrapper(env, value),
        },
        rw::WireLeaf::BigInt(value) => bigint_to_unknown(env, value),
        rw::WireLeaf::Date(value) => {
            let constructor: Function<String, Unknown> =
                env.get_global()?.get_named_property_unchecked("Date")?;
            constructor.new_instance(value.clone())
        }
        rw::WireLeaf::RegExp { pattern, flags } => {
            let constructor: Function<FnArgs<(String, String)>, Unknown> =
                env.get_global()?.get_named_property_unchecked("RegExp")?;
            constructor.new_instance(FnArgs::from((pattern.clone(), flags.clone())))
        }
        rw::WireLeaf::Url(value) => {
            let constructor: Function<String, Unknown> =
                env.get_global()?.get_named_property_unchecked("URL")?;
            constructor.new_instance(value.clone())
        }
        rw::WireLeaf::Error {
            name,
            message,
            stack,
        } => {
            let constructor: Function<String, Unknown> =
                env.get_global()?.get_named_property_unchecked("Error")?;
            let value = constructor.new_instance(message.clone())?;
            // SAFETY: the Error constructor returns an object for every message.
            let mut object = unsafe { value.cast::<Object>()? };
            object.set("name", if name.is_empty() { "Error" } else { name })?;
            object.set("stack", stack.clone())?;
            Ok(value)
        }
        rw::WireLeaf::Undefined | rw::WireLeaf::Symbol | rw::WireLeaf::Function => {
            to_unknown(env, ())
        }
    }
}

#[napi(js_name = "decodeWire")]
pub fn decode_wire<'env>(env: &'env Env, wire_json: String) -> Result<Unknown<'env>> {
    let graph = rw::parse_wire_graph(&wire_json).map_err(napi_error)?;
    WireMaterializer::new(env, &graph).materialize()
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
