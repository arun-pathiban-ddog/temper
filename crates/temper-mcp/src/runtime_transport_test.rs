use super::*;
use std::pin::Pin;
use std::task::{Context, Poll};
use tokio::io::{AsyncRead, ReadBuf};

struct FaultReader(bool);
impl AsyncRead for FaultReader {
    fn poll_read(
        self: Pin<&mut Self>,
        _: &mut Context<'_>,
        _: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        assert!(!self.0, "injected reader task panic");
        Poll::Ready(Err(io::Error::other("injected read failure")))
    }
}

struct FaultWriter(bool);
impl AsyncWrite for FaultWriter {
    fn poll_write(self: Pin<&mut Self>, _: &mut Context<'_>, _: &[u8]) -> Poll<io::Result<usize>> {
        assert!(!self.0, "injected writer task panic");
        Poll::Ready(Err(io::Error::other("injected write failure")))
    }
    fn poll_flush(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
    fn poll_shutdown(self: Pin<&mut Self>, _: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

#[tokio::test]
async fn transport_reader_errors_and_panics_are_not_clean_eof() {
    for panic in [false, true] {
        let result = tokio::time::timeout(
            Duration::from_secs(2),
            run_loop(
                trajectory_test_ctx("default"),
                BufReader::new(FaultReader(panic)),
                tokio::io::sink(),
            ),
        )
        .await
        .expect("reader failure must end the loop promptly");
        assert!(result.is_err(), "reader failure must reach the caller");
    }
}

#[tokio::test]
async fn transport_writer_errors_and_panics_end_even_with_input_open() {
    for panic in [false, true] {
        let (mut client, input) = tokio::io::duplex(1024);
        client
            .write_all(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"ping\"}\n")
            .await
            .expect("send ping");
        let result = tokio::time::timeout(
            Duration::from_secs(2),
            run_loop(
                trajectory_test_ctx("default"),
                BufReader::new(input),
                FaultWriter(panic),
            ),
        )
        .await
        .expect("writer failure must not wait for input EOF");
        assert!(result.is_err(), "writer failure must reach the caller");
        drop(client);
    }
}

#[tokio::test]
async fn transport_normal_eof_remains_successful() {
    let result = run_loop(
        trajectory_test_ctx("default"),
        BufReader::new(tokio::io::empty()),
        tokio::io::sink(),
    )
    .await;
    assert!(result.is_ok());
}
