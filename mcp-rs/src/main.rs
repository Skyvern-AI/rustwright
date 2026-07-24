mod actor;
mod server;
mod tools;
mod transport;

use rmcp::ServiceExt;
use server::BrowserServer;
use transport::LifecycleStdio;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    BrowserServer::new()?
        .serve(LifecycleStdio::new())
        .await?
        .waiting()
        .await?;
    Ok(())
}
