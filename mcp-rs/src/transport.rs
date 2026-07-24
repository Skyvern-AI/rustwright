use std::sync::Arc;

use rmcp::{
    RoleServer,
    model::{
        ClientJsonRpcMessage, ClientRequest, ErrorCode, ErrorData, RequestId, ServerJsonRpcMessage,
    },
    transport::Transport,
};
use serde::Serialize;
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, AsyncWrite, AsyncWriteExt, BufReader, Stdin, Stdout},
    sync::Mutex,
};

const SERVER_NOT_INITIALIZED: i32 = -32002;

/// Newline-delimited stdio with strict pre-initialize and parse-error responses.
///
/// The SDK still owns initialization and all post-initialize protocol routing. This
/// adapter only converts input that the stock transport drops or terminates on into
/// JSON-RPC errors before passing the next valid message to the SDK.
pub(crate) struct LifecycleStdio<R = Stdin, W = Stdout> {
    input: BufReader<R>,
    output: Arc<Mutex<W>>,
    line: Vec<u8>,
    initialize_seen: bool,
}

impl LifecycleStdio<Stdin, Stdout> {
    pub(crate) fn new() -> Self {
        Self::from_io(tokio::io::stdin(), tokio::io::stdout())
    }
}

impl<R, W> LifecycleStdio<R, W>
where
    R: Send + AsyncRead + Unpin,
    W: Send + AsyncWrite + Unpin + 'static,
{
    fn from_io(input: R, output: W) -> Self {
        Self {
            input: BufReader::new(input),
            output: Arc::new(Mutex::new(output)),
            line: Vec::new(),
            initialize_seen: false,
        }
    }

    fn send_error(
        &self,
        error: ErrorData,
        id: Option<RequestId>,
    ) -> impl Future<Output = std::io::Result<()>> + Send + 'static {
        send_frame(
            Arc::clone(&self.output),
            ServerJsonRpcMessage::error(error, id),
        )
    }

    fn reject_before_initialize(
        &self,
        id: RequestId,
    ) -> impl Future<Output = std::io::Result<()>> + Send + 'static {
        self.send_error(
            ErrorData::new(
                ErrorCode(SERVER_NOT_INITIALIZED),
                "Server not initialized",
                None,
            ),
            Some(id),
        )
    }
}

impl<R, W> Transport<RoleServer> for LifecycleStdio<R, W>
where
    R: Send + AsyncRead + Unpin,
    W: Send + AsyncWrite + Unpin + 'static,
{
    type Error = std::io::Error;

    fn send(
        &mut self,
        item: ServerJsonRpcMessage,
    ) -> impl Future<Output = Result<(), Self::Error>> + Send + 'static {
        send_frame(Arc::clone(&self.output), item)
    }

    async fn receive(&mut self) -> Option<ClientJsonRpcMessage> {
        loop {
            match self.input.read_until(b'\n', &mut self.line).await {
                Ok(0) => return None,
                Ok(_) => {}
                Err(error) => {
                    eprintln!("stdio read failed: {error}");
                    return None;
                }
            }

            let parsed = {
                let mut line = self.line.as_slice();
                while matches!(line.last(), Some(b'\n' | b'\r')) {
                    line = &line[..line.len() - 1];
                }
                if line.is_empty() {
                    self.line.clear();
                    continue;
                }
                serde_json::from_slice::<ClientJsonRpcMessage>(line)
            };
            // `read_until` may be cancelled after appending a partial frame. Keep
            // that buffer across calls, and clear it synchronously only once a
            // complete newline-delimited frame has been parsed.
            self.line.clear();

            let message = match parsed {
                Ok(message) => message,
                Err(error) => {
                    let error_data = match error.classify() {
                        serde_json::error::Category::Syntax | serde_json::error::Category::Eof => {
                            ErrorData::parse_error("Parse error", None)
                        }
                        serde_json::error::Category::Data | serde_json::error::Category::Io => {
                            ErrorData::invalid_request("Invalid request", None)
                        }
                    };
                    if let Err(write_error) = self.send_error(error_data, None).await {
                        eprintln!("stdio error response failed: {write_error}");
                        return None;
                    }
                    continue;
                }
            };

            if !self.initialize_seen {
                match &message {
                    ClientJsonRpcMessage::Request(request)
                        if matches!(request.request, ClientRequest::InitializeRequest(_)) =>
                    {
                        // The SDK sends InitializeResult before asking this transport for
                        // another message, so setting this here still gates the full handshake.
                        self.initialize_seen = true;
                    }
                    ClientJsonRpcMessage::Request(request)
                        if matches!(request.request, ClientRequest::PingRequest(_)) => {}
                    ClientJsonRpcMessage::Request(request) => {
                        if let Err(error) = self.reject_before_initialize(request.id.clone()).await
                        {
                            eprintln!("stdio lifecycle response failed: {error}");
                            return None;
                        }
                        continue;
                    }
                    _ => continue,
                }
            }

            return Some(message);
        }
    }

    async fn close(&mut self) -> Result<(), Self::Error> {
        self.output.lock().await.flush().await
    }
}

async fn send_frame<W, T>(output: Arc<Mutex<W>>, item: T) -> std::io::Result<()>
where
    W: AsyncWrite + Unpin,
    T: Serialize + Send + 'static,
{
    let mut frame = serde_json::to_vec(&item).map_err(std::io::Error::other)?;
    frame.push(b'\n');
    let mut output = output.lock().await;
    output.write_all(&frame).await?;
    output.flush().await
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use tokio::io::AsyncWriteExt;

    use super::*;

    #[tokio::test]
    async fn cancelled_receive_resumes_partial_frame() {
        let (mut client, input) = tokio::io::duplex(256);
        let mut transport = LifecycleStdio::from_io(input, tokio::io::sink());
        let frame = br#"{"jsonrpc":"2.0","id":7,"method":"ping","params":{}}
"#;
        let split = 24;

        client
            .write_all(&frame[..split])
            .await
            .expect("write partial frame");
        assert!(
            tokio::time::timeout(Duration::from_millis(20), transport.receive())
                .await
                .is_err(),
            "partial frame must keep receive pending"
        );
        assert_eq!(transport.line, frame[..split]);

        client
            .write_all(&frame[split..])
            .await
            .expect("finish frame");
        let message = transport.receive().await.expect("receive resumed frame");
        let ClientJsonRpcMessage::Request(request) = message else {
            panic!("expected request");
        };
        assert_eq!(request.id, RequestId::Number(7));
        assert!(matches!(request.request, ClientRequest::PingRequest(_)));
        assert!(transport.line.is_empty());
    }
}
