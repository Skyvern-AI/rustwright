use rustwright::{chromium, GotoOptions, LaunchOptions};

fn main() -> rustwright::Result<()> {
    let browser = chromium().launch(LaunchOptions::default())?;
    let result = (|| {
        let page = browser.new_page()?;
        page.goto(
            "data:text/html,%3Ctitle%3ERustwright%20works%3C%2Ftitle%3E",
            GotoOptions::default(),
        )?;
        println!("{}", page.title(Default::default())?);
        page.close(Default::default())
    })();
    let close_result = browser.close();
    result.and(close_result)
}
